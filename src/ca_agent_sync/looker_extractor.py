"""Read-only Looker 4.0 SDK extractor for CA agent metadata and lightweight Explore headers."""

from __future__ import annotations

import datetime
import hashlib
import logging
from typing import Any, Optional, Protocol

import looker_sdk
import requests
from looker_sdk import error as looker_error

from ca_agent_sync.config import SyncConfig
from ca_agent_sync.models import (
    AgentMetadataRecord,
    ExploreHeader,
    GoldenQueryRecord,
    UserAgentAccess,
    build_golden_query_metadata_concat,
    build_metadata_concat,
)

logger = logging.getLogger(__name__)


class LookerSDKProtocol(Protocol):
    """Subset of Looker 4.0 SDK methods allowed under R4 Read-Only constraint."""

    def me(self, fields: Optional[str] = None) -> Any: ...
    def search_agents(self, deleted: Optional[bool] = None, **kwargs: Any) -> Any: ...
    def get_agent(self, agent_id: str, fields: Optional[str] = None) -> Any: ...
    def lookml_model_explore(
        self,
        lookml_model_name: str,
        explore_name: str,
        fields: Optional[str] = None,
    ) -> Any: ...


def _clean_optional_str(value: Any) -> Optional[str]:
    """Return stripped non-empty string or None."""
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned if cleaned else None


def _derive_golden_query_id(
    gq: Any,
    agent_id: str,
    questions: list[str],
    model: Optional[str],
    explore: Optional[str],
    fields: list[str],
    sorts: list[str],
    answer: Optional[str],
) -> str:
    """Resolve a deterministic non-empty `golden_query_id` from a Looker `GoldenQuery`."""
    raw_id = getattr(gq, "id", None)
    if raw_id is not None and str(raw_id).strip():
        return str(raw_id).strip()
    raw_qid = getattr(gq, "query_id", None)
    if raw_qid is not None and str(raw_qid).strip():
        return f"q_{str(raw_qid).strip()}"
    raw_cid = getattr(gq, "client_id", None)
    if raw_cid is not None and str(raw_cid).strip():
        return f"c_{str(raw_cid).strip()}"
    fingerprint = "|".join(
        [
            agent_id,
            ";".join(questions),
            model or "",
            explore or "",
            ",".join(fields),
            ",".join(sorts),
            answer or "",
        ]
    )
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:12]
    return f"h_{digest}"


def _parse_timestamp(value: Any) -> Optional[datetime.datetime]:
    """Normalize Looker timestamp (`datetime.datetime` or ISO-8601 `str`) to UTC `datetime`."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc)
        except ValueError:
            return None
    return None


def _is_not_found_error(exc: Exception) -> bool:
    """Return True if an SDK exception represents an HTTP 404 / Not Found response."""
    status_code = getattr(exc, "status", None) or getattr(exc, "code", None)
    if status_code == 404 or str(status_code) == "404":
        return True
    msg = str(exc).lower()
    return "404" in msg or "not found" in msg


class LookerAgentExtractor:
    """Extracts CA Agent metadata and cached lightweight Explore headers via Looker 4.0 API.

    Strictly enforces R4 Read-Only operations (`me`, `search_agents`, `get_agent`,
    `lookml_model_explore`). Never creates, updates, or deletes Looker resources.
    """

    def __init__(
        self,
        config: SyncConfig,
        sdk: Optional[LookerSDKProtocol] = None,
    ) -> None:
        self.config = config
        self._sdk = sdk
        self._explore_cache: dict[tuple[str, str], ExploreHeader] = {}

    @property
    def sdk(self) -> LookerSDKProtocol:
        """Lazily initialize Looker 4.0 SDK after exporting `.env` credentials into `os.environ`."""
        if self._sdk is None:
            self.config.export_looker_env()
            self.config.validate_looker_credentials()
            self._sdk = looker_sdk.init40()
        return self._sdk

    @property
    def explore_cache(self) -> dict[tuple[str, str], ExploreHeader]:
        """Return the in-memory `(model, explore)` header cache."""
        return self._explore_cache

    def verify_auth(self) -> Any:
        """Verify Looker API authentication via read-only `sdk.me()`."""
        return self.sdk.me()

    def get_explore_header(
        self,
        model: str,
        explore: str,
        include_explore_headers: bool = True,
    ) -> ExploreHeader:
        """Fetch lightweight Explore header (`name,label,description`), cached per `(model, explore)`.

        Never fetches raw dimensions or measures. If `include_explore_headers` is False
        or if Looker returns 404 / error for an inaccessible explore, returns a graceful
        fallback `ExploreHeader` with `name=explore`.
        """
        model_clean = (model or "").strip()
        explore_clean = (explore or "").strip()
        if not include_explore_headers:
            return ExploreHeader(
                model=model_clean,
                explore=explore_clean,
                name=explore_clean or None,
                label=None,
                description=None,
            )

        cache_key = (model_clean, explore_clean)
        if cache_key in self._explore_cache:
            return self._explore_cache[cache_key]

        try:
            exp_obj = self.sdk.lookml_model_explore(
                model_clean,
                explore_clean,
                fields="name,label,description",
            )
            header = ExploreHeader(
                model=model_clean,
                explore=explore_clean,
                name=getattr(exp_obj, "name", None) or explore_clean,
                label=getattr(exp_obj, "label", None),
                description=getattr(exp_obj, "description", None),
            )
        except (looker_error.SDKError, Exception) as exc:
            logger.debug(
                "Could not fetch Explore header for %s.%s (falling back to raw identifier): %s",
                model_clean,
                explore_clean,
                exc,
            )
            header = ExploreHeader(
                model=model_clean,
                explore=explore_clean,
                name=explore_clean or None,
                label=None,
                description=None,
            )

        self._explore_cache[cache_key] = header
        return header

    def _normalize_agent(
        self,
        agent: Any,
        include_explore_headers: bool = True,
    ) -> AgentMetadataRecord:
        """Convert a Looker `models40.Agent` into a normalized `AgentMetadataRecord`."""
        agent_id = str(getattr(agent, "id", "") or "").strip()
        if not agent_id:
            raise ValueError("Encountered Looker Agent without a valid `id`.")

        instance_url = self.config.normalized_instance_url or "https://looker.example.com"
        agent_url = f"{instance_url}/conversational-analytics/agents/{agent_id}"

        name = str(getattr(agent, "name", "") or "").strip()
        description = _clean_optional_str(getattr(agent, "description", None))
        category = _clean_optional_str(getattr(agent, "category", None))
        synced_at = datetime.datetime.now(datetime.timezone.utc)

        # Deduplicate (model, explore) pairs per agent while preserving deterministic order
        raw_sources = getattr(agent, "sources", None) or []
        seen_sources: set[tuple[str, str]] = set()
        explore_headers: list[ExploreHeader] = []
        for src in raw_sources:
            model_name = str(getattr(src, "model", "") or "").strip()
            explore_name = str(getattr(src, "explore", "") or "").strip()
            if not model_name and not explore_name:
                continue
            pair = (model_name, explore_name)
            if pair in seen_sources:
                continue
            seen_sources.add(pair)
            explore_headers.append(
                self.get_explore_header(
                    model=model_name,
                    explore=explore_name,
                    include_explore_headers=include_explore_headers,
                )
            )

        raw_gqs = getattr(agent, "golden_queries", None) or []
        golden_queries: list[GoldenQueryRecord] = []
        seen_gq_record_ids: set[str] = set()
        for gq in raw_gqs:
            questions_raw = getattr(gq, "questions", None) or []
            questions = [str(q).strip() for q in questions_raw if q is not None and str(q).strip()]
            fields_raw = getattr(gq, "fields", None) or []
            fields = [str(f).strip() for f in fields_raw if f is not None and str(f).strip()]
            filters_raw = getattr(gq, "filters", None) or {}
            filters = (
                {
                    str(k).strip(): v
                    for k, v in filters_raw.items()
                    if k is not None and str(k).strip() and v is not None
                }
                if isinstance(filters_raw, dict)
                else {}
            )
            sorts_raw = getattr(gq, "sorts", None) or []
            sorts = [str(s).strip() for s in sorts_raw if s is not None and str(s).strip()]
            is_active_val = getattr(gq, "is_active", None)
            is_active = True if is_active_val is None else bool(is_active_val)
            gq_model = _clean_optional_str(getattr(gq, "model", None))
            gq_explore = _clean_optional_str(getattr(gq, "explore", None))
            gq_answer = _clean_optional_str(getattr(gq, "answer", None))
            gq_limit = _clean_optional_str(getattr(gq, "limit", None))

            gq_id_str = _derive_golden_query_id(
                gq=gq,
                agent_id=agent_id,
                questions=questions,
                model=gq_model,
                explore=gq_explore,
                fields=fields,
                sorts=sorts,
                answer=gq_answer,
            )
            record_id = f"{agent_id}:{gq_id_str}"
            if record_id in seen_gq_record_ids:
                continue
            seen_gq_record_ids.add(record_id)

            gq_metadata_concat = build_golden_query_metadata_concat(
                questions=questions,
                agent_name=name,
                model=gq_model,
                explore=gq_explore,
                fields=fields,
                filters=filters,
                sorts=sorts,
            )

            golden_queries.append(
                GoldenQueryRecord(
                    record_id=record_id,
                    golden_query_id=gq_id_str,
                    id=getattr(gq, "id", None),
                    query_id=getattr(gq, "query_id", None),
                    agent_id=agent_id,
                    agent_name=name,
                    agent_url=agent_url,
                    instance_url=instance_url,
                    questions=questions,
                    answer=gq_answer,
                    is_active=is_active,
                    model=gq_model,
                    explore=gq_explore,
                    fields=fields,
                    filters=filters,
                    sorts=sorts,
                    limit=gq_limit,
                    synced_at=synced_at,
                    metadata_concat=gq_metadata_concat,
                )
            )

        context_obj = getattr(agent, "context", None)
        system_instructions = (
            getattr(context_obj, "instructions", None) if context_obj is not None else None
        )

        metadata_concat = build_metadata_concat(
            name=name,
            description=description,
            category=category,
            sources=explore_headers,
            golden_queries=golden_queries,
            system_instructions=system_instructions,
        )

        return AgentMetadataRecord(
            agent_id=agent_id,
            instance_url=instance_url,
            agent_url=agent_url,
            name=name,
            description=description,
            category=category,
            system_instructions=system_instructions,
            sources=explore_headers,
            golden_queries=golden_queries,
            code_interpreter=bool(getattr(agent, "code_interpreter", False)),
            created_by_name=getattr(agent, "created_by_name", None),
            created_at=_parse_timestamp(getattr(agent, "created_at", None)),
            updated_at=_parse_timestamp(getattr(agent, "updated_at", None)),
            synced_at=synced_at,
            metadata_concat=metadata_concat,
        )

    def extract_batch(
        self,
        include_explore_headers: bool = True,
    ) -> list[AgentMetadataRecord]:
        """Fetch all active CA agents (`sdk.search_agents(deleted=False)`) and normalize them."""
        agents = self.sdk.search_agents(deleted=False) or []
        records: list[AgentMetadataRecord] = []
        seen_ids: set[str] = set()

        for agent in agents:
            if getattr(agent, "deleted", False) is True:
                continue
            agent_id = str(getattr(agent, "id", "") or "").strip()
            if not agent_id or agent_id in seen_ids:
                continue
            seen_ids.add(agent_id)
            records.append(
                self._normalize_agent(
                    agent,
                    include_explore_headers=include_explore_headers,
                )
            )

        return records

    def extract_single(
        self,
        agent_id: str,
        include_explore_headers: bool = True,
    ) -> tuple[Optional[AgentMetadataRecord], bool]:
        """Fetch a single CA agent by ID via `sdk.get_agent(agent_id)`.

        Returns:
            `(record, should_delete)`:
            - `(AgentMetadataRecord, False)` when the agent exists and is active.
            - `(None, True)` when the agent has `deleted=True` or `sdk.get_agent` raises 404.
        """
        cleaned_id = agent_id.strip()
        try:
            agent = self.sdk.get_agent(cleaned_id)
        except Exception as exc:
            if _is_not_found_error(exc):
                logger.info(
                    "Agent %s returned 404 Not Found in Looker; marking for BigQuery deletion.",
                    cleaned_id,
                )
                return None, True
            raise

        if agent is None or getattr(agent, "deleted", False) is True:
            logger.info(
                "Agent %s is marked deleted=True in Looker; marking for BigQuery deletion.",
                cleaned_id,
            )
            return None, True

        record = self._normalize_agent(
            agent,
            include_explore_headers=include_explore_headers,
        )
        return record, False

    def fetch_user_accessible_agents(
        self,
        *,
        user_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> UserAgentAccess:
        """Resolve the CA agents accessible to a specific Looker user via sudo (`user_id`) or OAuth (`access_token`)."""
        cleaned_uid = _clean_optional_str(user_id)
        cleaned_token = _clean_optional_str(access_token)

        if (cleaned_uid is None) == (cleaned_token is None):
            raise ValueError(
                "Exactly one of `user_id` or `access_token` must be provided to fetch_user_accessible_agents."
            )

        fields_spec = "id,deleted,has_inaccessible_source"
        if cleaned_token is not None:
            if not self.config.normalized_instance_url:
                raise ValueError(
                    "Missing required Looker base URL: LOOKERSDK_BASE_URL must be configured when using `access_token`."
                )
            auth_header = {"Authorization": f"Bearer {cleaned_token}"}
            if self._sdk is not None:
                raw_agents = (
                    self._sdk.search_agents(
                        deleted=False,
                        fields=fields_spec,
                        transport_options={"headers": auth_header},
                    )
                    or []
                )
            else:
                verify_ssl = str(self.config.looker_verify_ssl or "true").strip().lower() not in (
                    "false",
                    "0",
                    "no",
                )
                try:
                    timeout = int(str(self.config.looker_timeout or "120").strip())
                except ValueError:
                    timeout = 120

                url = f"{self.config.normalized_instance_url}/api/4.0/agents/search"
                response = requests.get(
                    url,
                    params={"deleted": "false", "fields": fields_spec},
                    headers=auth_header,
                    verify=verify_ssl,
                    timeout=timeout,
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Looker /api/4.0/agents/search failed with HTTP {response.status_code}: {response.text}"
                    )
                parsed_json = response.json()
                raw_agents = parsed_json if isinstance(parsed_json, list) else []
        else:
            assert cleaned_uid is not None
            sdk = self.sdk
            auth_obj = getattr(sdk, "auth", None)
            sudo_target: Any = int(cleaned_uid) if cleaned_uid.isdigit() else cleaned_uid
            logout_fn = None
            try:
                if auth_obj is not None and hasattr(auth_obj, "login_user"):
                    auth_obj.login_user(sudo_target)
                    logout_fn = getattr(auth_obj, "logout", None)
                elif hasattr(sdk, "login_user_sudo"):
                    sdk.login_user_sudo(sudo_target)
                    logout_fn = getattr(sdk, "logout", None)
                raw_agents = sdk.search_agents(deleted=False, fields=fields_spec) or []
            finally:
                if callable(logout_fn):
                    logout_fn()

        allowed_agent_ids: list[str] = []
        inaccessible_source_agent_ids: list[str] = []
        seen_ids: set[str] = set()
        seen_inaccessible_ids: set[str] = set()

        for item in raw_agents or []:
            getter = item.get if isinstance(item, dict) else lambda k, d=False: getattr(item, k, d)
            if getter("deleted", False) is True:
                continue
            agent_id = str(getter("id", "") or "").strip()
            if not agent_id:
                continue
            if agent_id not in seen_ids:
                seen_ids.add(agent_id)
                allowed_agent_ids.append(agent_id)
            if getter("has_inaccessible_source", False) is True and agent_id not in seen_inaccessible_ids:
                seen_inaccessible_ids.add(agent_id)
                inaccessible_source_agent_ids.append(agent_id)

        return UserAgentAccess(
            mode="access_token" if cleaned_token is not None else "user_id",
            user_id=cleaned_uid,
            allowed_agent_ids=allowed_agent_ids,
            inaccessible_source_agent_ids=inaccessible_source_agent_ids,
        )

