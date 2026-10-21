"""Data models and deterministic `metadata_concat` builder for `ca-agent-sync`."""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any, Mapping, Optional, Sequence

from pydantic import BaseModel, Field, model_validator


def _clean_text(value: Optional[str]) -> str:
    """Normalize optional string values, stripping leading/trailing whitespace."""
    if value is None:
        return ""
    return str(value).strip()


def build_golden_query_metadata_concat(
    *,
    questions: Sequence[str] = (),
    agent_name: Optional[str] = None,
    model: Optional[str] = None,
    explore: Optional[str] = None,
    fields: Sequence[str] = (),
    filters: Optional[Mapping[str, Any]] = None,
    sorts: Sequence[str] = (),
) -> str:
    """Construct a deterministic `metadata_concat` text string for a Golden Query in `ca_golden_queries`.

    Combines:
    1. Natural language `questions` (`list[str]`)
    2. `Agent Name: {agent_name}`
    3. `LookML Explore: {model}.{explore}`
    4. `Fields: {fields}`
    5. `Filters: {k=v, ...}` (sorted by key for determinism)
    6. `Sorts: {sorts}`

    Never includes volatile timestamps (like `synced_at`) or raw `"None"` literals,
    and guarantees a non-empty string so BigQuery `AI.EMBED` never receives NULL.
    """
    lines: list[str] = []

    cleaned_questions = [_clean_text(q) for q in (questions or ()) if _clean_text(q)]
    if cleaned_questions:
        lines.append(f"Golden Query Questions: {'; '.join(cleaned_questions)}")

    cleaned_agent_name = _clean_text(agent_name)
    if cleaned_agent_name:
        lines.append(f"Agent Name: {cleaned_agent_name}")

    model_clean = _clean_text(model)
    explore_clean = _clean_text(explore)
    if model_clean or explore_clean:
        target = (
            f"{model_clean}.{explore_clean}"
            if (model_clean and explore_clean)
            else (model_clean or explore_clean)
        )
        lines.append(f"LookML Explore: {target}")

    cleaned_fields = [_clean_text(f) for f in (fields or ()) if _clean_text(f)]
    if cleaned_fields:
        lines.append(f"Fields: {', '.join(cleaned_fields)}")

    if filters:
        cleaned_filters = {
            _clean_text(str(k)): _clean_text(str(v))
            for k, v in filters.items()
            if _clean_text(str(k)) and v is not None and _clean_text(str(v)) != ""
        }
        if cleaned_filters:
            filter_strs = [f"{k}={cleaned_filters[k]}" for k in sorted(cleaned_filters.keys())]
            lines.append(f"Filters: {', '.join(filter_strs)}")

    cleaned_sorts = [_clean_text(s) for s in (sorts or ()) if _clean_text(s)]
    if cleaned_sorts:
        lines.append(f"Sorts: {', '.join(cleaned_sorts)}")

    if not lines:
        return "Golden Query: Unspecified"

    return "\n".join(lines)


class ExploreHeader(BaseModel):
    """Lightweight LookML Explore header (`name`, `label`, `description`) without raw fields."""

    model: str
    explore: str
    name: Optional[str] = None
    label: Optional[str] = None
    description: Optional[str] = None

    def to_json_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-serializable dict for the `sources` column."""
        return {
            "model": self.model,
            "explore": self.explore,
            "name": self.name or self.explore,
            "label": self.label,
            "description": self.description,
        }


class GoldenQueryRecord(BaseModel):
    """Normalized Looker CA Agent Golden Query metadata for `ca_golden_queries` and `ca_agents`."""

    record_id: str = ""
    golden_query_id: str = ""
    id: Optional[int] = None
    query_id: Optional[int] = None
    agent_id: str = ""
    agent_name: str = ""
    agent_url: str = ""
    instance_url: str = ""
    questions: list[str] = Field(default_factory=list)
    answer: Optional[str] = None
    is_active: bool = True
    model: Optional[str] = None
    explore: Optional[str] = None
    fields: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    sorts: list[str] = Field(default_factory=list)
    limit: Optional[str] = None
    synced_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    metadata_concat: str = ""

    @model_validator(mode="after")
    def _populate_derived_identifiers_and_concat(self) -> GoldenQueryRecord:
        if not self.golden_query_id:
            if self.id is not None:
                self.golden_query_id = str(self.id).strip()
            elif self.query_id is not None:
                self.golden_query_id = f"q_{self.query_id}"
        if not self.record_id and self.agent_id and self.golden_query_id:
            self.record_id = f"{self.agent_id}:{self.golden_query_id}"
        if not self.metadata_concat:
            self.metadata_concat = build_golden_query_metadata_concat(
                questions=self.questions,
                agent_name=self.agent_name or None,
                model=self.model,
                explore=self.explore,
                fields=self.fields,
                filters=self.filters,
                sorts=self.sorts,
            )
        return self

    def with_agent_context(
        self,
        *,
        agent_id: str,
        agent_name: str,
        agent_url: str,
        instance_url: str,
        synced_at: Optional[datetime.datetime] = None,
        fallback_index: int = 1,
    ) -> GoldenQueryRecord:
        """Return a copy of this `GoldenQueryRecord` with full parent agent context populated."""
        resolved_agent_id = (self.agent_id or agent_id).strip()
        resolved_agent_name = (self.agent_name or agent_name).strip()
        resolved_agent_url = (self.agent_url or agent_url).strip()
        resolved_instance_url = (self.instance_url or instance_url).strip()
        resolved_synced_at = synced_at if synced_at is not None else self.synced_at

        gq_id = self.golden_query_id
        if not gq_id:
            if self.id is not None:
                gq_id = str(self.id).strip()
            elif self.query_id is not None:
                gq_id = f"q_{self.query_id}"
            else:
                fingerprint = "|".join(
                    [
                        resolved_agent_id,
                        ";".join(self.questions),
                        self.model or "",
                        self.explore or "",
                        ",".join(self.fields),
                        ",".join(self.sorts),
                        self.answer or "",
                        str(fallback_index),
                    ]
                )
                digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:12]
                gq_id = f"h_{digest}"

        resolved_record_id = self.record_id or f"{resolved_agent_id}:{gq_id}"
        resolved_concat = build_golden_query_metadata_concat(
            questions=self.questions,
            agent_name=resolved_agent_name or None,
            model=self.model,
            explore=self.explore,
            fields=self.fields,
            filters=self.filters,
            sorts=self.sorts,
        )
        return self.model_copy(
            update={
                "record_id": resolved_record_id,
                "golden_query_id": gq_id,
                "agent_id": resolved_agent_id,
                "agent_name": resolved_agent_name,
                "agent_url": resolved_agent_url,
                "instance_url": resolved_instance_url,
                "synced_at": resolved_synced_at,
                "metadata_concat": resolved_concat,
            }
        )

    @property
    def questions_json(self) -> str:
        """Return deterministic JSON array string for `questions`."""
        return json.dumps(list(self.questions), ensure_ascii=False)

    @property
    def fields_json(self) -> str:
        """Return deterministic JSON array string for `fields`."""
        return json.dumps(list(self.fields), ensure_ascii=False)

    @property
    def sorts_json(self) -> str:
        """Return deterministic JSON array string for `sorts`."""
        return json.dumps(list(self.sorts), ensure_ascii=False)

    @property
    def filters_json(self) -> str:
        """Return deterministic JSON object string for `filters`."""
        sorted_filters = {k: self.filters[k] for k in sorted(self.filters.keys())}
        return json.dumps(sorted_filters, sort_keys=True, ensure_ascii=False)

    def to_json_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-serializable dict for the `golden_queries` column."""
        sorted_filters = {k: self.filters[k] for k in sorted(self.filters.keys())}
        return {
            "id": self.id,
            "query_id": self.query_id,
            "questions": list(self.questions),
            "answer": self.answer,
            "is_active": self.is_active,
            "model": self.model,
            "explore": self.explore,
            "fields": list(self.fields),
            "filters": sorted_filters,
            "sorts": list(self.sorts),
            "limit": self.limit,
        }


def build_metadata_concat(
    *,
    name: Optional[str],
    description: Optional[str] = None,
    category: Optional[str] = None,
    sources: Sequence[ExploreHeader] = (),
    golden_queries: Sequence[GoldenQueryRecord] = (),
    system_instructions: Optional[str] = None,
) -> str:
    """Construct a deterministic `metadata_concat` text block for `AI.EMBED`.

    Combines:
    1. Agent `name`, `description`, and `category`
    2. Lightweight Explore `name`, `label`, and `description`
    3. Golden Query natural language `questions` and referenced `fields`/`filters`
    4. System instructions (`context.instructions`)

    Never includes volatile timestamps (like `synced_at`) or raw `"None"` literals,
    and guarantees a non-empty string so BigQuery `AI.EMBED` never receives NULL.
    """
    sections: list[str] = []

    agent_name = _clean_text(name) or "Unnamed Agent"
    header_lines = [f"Agent Name: {agent_name}"]

    desc_clean = _clean_text(description)
    if desc_clean:
        header_lines.append(f"Description: {desc_clean}")

    cat_clean = _clean_text(category)
    if cat_clean:
        header_lines.append(f"Category: {cat_clean}")

    sections.append("\n".join(header_lines))

    # Deterministic Explore Sources section
    if sources:
        # Sort sources deterministically by (model, explore) while preserving uniqueness
        seen_pairs: set[tuple[str, str]] = set()
        sorted_sources = sorted(
            sources,
            key=lambda s: (_clean_text(s.model).lower(), _clean_text(s.explore).lower()),
        )
        explore_lines: list[str] = ["Explores:"]
        for src in sorted_sources:
            model_name = _clean_text(src.model)
            explore_name = _clean_text(src.name) or _clean_text(src.explore)
            raw_explore = _clean_text(src.explore) or explore_name
            key = (model_name, raw_explore)
            if not model_name and not raw_explore:
                continue
            if key in seen_pairs:
                continue
            seen_pairs.add(key)

            qualified = f"{model_name}.{raw_explore}" if model_name else raw_explore
            parts = [f"- Explore: {qualified}"]
            if explore_name and explore_name != raw_explore:
                parts.append(f"Name: {explore_name}")
            label_clean = _clean_text(src.label)
            if label_clean:
                parts.append(f"Label: {label_clean}")
            src_desc_clean = _clean_text(src.description)
            if src_desc_clean:
                parts.append(f"Description: {src_desc_clean}")
            explore_lines.append(" | ".join(parts))

        if len(explore_lines) > 1:
            sections.append("\n".join(explore_lines))

    # Deterministic Golden Queries section
    active_gqs = [gq for gq in golden_queries if gq.is_active is not False]
    if active_gqs:

        def _gq_sort_key(gq: GoldenQueryRecord) -> tuple[str, str, int]:
            first_q = _clean_text(gq.questions[0]) if gq.questions else ""
            mod_exp = f"{_clean_text(gq.model)}.{_clean_text(gq.explore)}"
            return (first_q.lower(), mod_exp.lower(), gq.id or 0)

        gq_lines: list[str] = ["Golden Queries:"]
        for gq in sorted(active_gqs, key=_gq_sort_key):
            cleaned_questions = [_clean_text(q) for q in gq.questions if _clean_text(q)]
            cleaned_fields = [_clean_text(f) for f in gq.fields if _clean_text(f)]
            cleaned_filters = {
                _clean_text(k): _clean_text(str(v))
                for k, v in gq.filters.items()
                if _clean_text(k) and v is not None and _clean_text(str(v)) != ""
            }

            if not cleaned_questions and not cleaned_fields and not cleaned_filters:
                continue

            entry_parts: list[str] = []
            if cleaned_questions:
                entry_parts.append(f"Questions: {'; '.join(cleaned_questions)}")
            gq_model = _clean_text(gq.model)
            gq_explore = _clean_text(gq.explore)
            if gq_model or gq_explore:
                target = (
                    f"{gq_model}.{gq_explore}"
                    if (gq_model and gq_explore)
                    else (gq_model or gq_explore)
                )
                entry_parts.append(f"Explore: {target}")
            if cleaned_fields:
                entry_parts.append(f"Fields: {', '.join(cleaned_fields)}")
            if cleaned_filters:
                filter_strs = [f"{k}={cleaned_filters[k]}" for k in sorted(cleaned_filters.keys())]
                entry_parts.append(f"Filters: {', '.join(filter_strs)}")

            gq_lines.append("- " + " | ".join(entry_parts))

        if len(gq_lines) > 1:
            sections.append("\n".join(gq_lines))

    # System Instructions section
    instructions_clean = _clean_text(system_instructions)
    if instructions_clean:
        sections.append(f"System Instructions:\n{instructions_clean}")

    return "\n\n".join(sections)


class AgentMetadataRecord(BaseModel):
    """Normalized Looker CA Agent record ready for BigQuery `ca_agents` upsert."""

    agent_id: str
    instance_url: str
    agent_url: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    system_instructions: Optional[str] = None
    sources: list[ExploreHeader] = Field(default_factory=list)
    golden_queries: list[GoldenQueryRecord] = Field(default_factory=list)
    code_interpreter: bool = False
    created_by_name: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None
    synced_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    metadata_concat: str

    @property
    def sources_json(self) -> str:
        """Return deterministic JSON string for `sources`."""
        return json.dumps(
            [s.to_json_dict() for s in self.sources],
            sort_keys=True,
            ensure_ascii=False,
        )

    @property
    def golden_queries_json(self) -> str:
        """Return deterministic JSON string for `golden_queries`."""
        return json.dumps(
            [gq.to_json_dict() for gq in self.golden_queries],
            sort_keys=True,
            ensure_ascii=False,
        )


def collect_golden_queries(records: Sequence[AgentMetadataRecord]) -> list[GoldenQueryRecord]:
    """Collect all active `GoldenQueryRecord` rows across synced agents, deduplicated by `record_id`."""
    collected: list[GoldenQueryRecord] = []
    seen_record_ids: set[str] = set()
    for agent_rec in records:
        for idx, gq in enumerate(agent_rec.golden_queries, start=1):
            if gq.is_active is False:
                continue
            populated = gq.with_agent_context(
                agent_id=agent_rec.agent_id,
                agent_name=agent_rec.name,
                agent_url=agent_rec.agent_url,
                instance_url=agent_rec.instance_url,
                synced_at=agent_rec.synced_at,
                fallback_index=idx,
            )
            if not populated.record_id or populated.record_id in seen_record_ids:
                continue
            seen_record_ids.add(populated.record_id)
            collected.append(populated)
    return collected


class SyncResult(BaseModel):
    """Summary of a `ca-agent-sync sync` execution across `ca_agents` and `ca_golden_queries`."""

    mode: str
    table_id: str
    synced_count: int = 0
    deleted_count: int = 0
    pruned_count: int = 0
    explore_headers_cached: int = 0
    agent_ids: list[str] = Field(default_factory=list)
    deleted_agent_ids: list[str] = Field(default_factory=list)
    pruned_agent_ids: list[str] = Field(default_factory=list)
    golden_queries_table_id: Optional[str] = None
    golden_queries_synced_count: int = 0
    golden_queries_deleted_count: int = 0
    golden_queries_pruned_count: int = 0


class StatusReport(BaseModel):
    """Summary of BigQuery `ca_agents` and `ca_golden_queries` autonomous embedding status."""

    table_id: str
    total_rows: int = 0
    populated_embeddings: int = 0
    pending_embeddings: int = 0
    error_embeddings: int = 0
    last_synced_at: Optional[str] = None
    is_generated: Optional[str] = None
    generation_expression: Optional[str] = None
    async_generation_status: Optional[Any] = None
    errors: list[dict[str, Any]] = Field(default_factory=list)
    golden_queries_table_id: Optional[str] = None
    golden_queries_total_rows: int = 0
    golden_queries_populated_embeddings: int = 0
    golden_queries_pending_embeddings: int = 0
    golden_queries_error_embeddings: int = 0
    golden_queries_last_synced_at: Optional[str] = None
    golden_queries_is_generated: Optional[str] = None
    golden_queries_generation_expression: Optional[str] = None
    golden_queries_async_generation_status: Optional[Any] = None
    golden_queries_errors: list[dict[str, Any]] = Field(default_factory=list)


class UserAgentAccess(BaseModel):
    """User's accessible CA agents resolved from Looker via sudo or access token."""

    mode: str = "admin"
    user_id: Optional[str] = None
    allowed_agent_ids: list[str] = Field(default_factory=list)
    inaccessible_source_agent_ids: list[str] = Field(default_factory=list)


class SearchHit(BaseModel):
    """Single semantic vector search result from BigQuery `ca_agents`."""

    rank: int
    agent_id: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    similarity_score: float
    distance: float
    agent_url: str
    instance_url: Optional[str] = None
    explores: list[str] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    embedding_source: str = "STORED"
    has_inaccessible_source: bool = False
    warning: Optional[str] = None


class GoldenQuerySearchHit(BaseModel):
    """Single semantic vector search match from BigQuery `ca_golden_queries` (Stage 1)."""

    rank: int
    record_id: str
    golden_query_id: str
    agent_id: str
    agent_name: str
    agent_url: str
    instance_url: Optional[str] = None
    questions: list[str] = Field(default_factory=list)
    model: Optional[str] = None
    explore: Optional[str] = None
    fields: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    sorts: list[str] = Field(default_factory=list)
    limit: Optional[str] = None
    answer: Optional[str] = None
    similarity_score: float
    distance: float
    embedding_source: str = "STORED"
    has_inaccessible_source: bool = False
    warning: Optional[str] = None


class TwoStepSearchResult(BaseModel):
    """Structured result of Two-Step Semantic Routing Search (`ca_golden_queries` -> `ca_agents`)."""

    question: str
    top_k: int
    golden_queries_requested: int
    confidence_score: float
    stop_on_golden_match: bool = False
    golden_query_matched: bool = False
    stage2_filtered: bool = False
    matched_agent_ids: list[str] = Field(default_factory=list)
    golden_query_results: list[GoldenQuerySearchHit] = Field(default_factory=list)
    results: list[SearchHit] = Field(default_factory=list)
    authz_mode: str = "admin"
    user_id: Optional[str] = None
    allowed_agent_count: Optional[int] = None


