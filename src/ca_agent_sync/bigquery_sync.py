"""BigQuery Autonomous Embedding provisioning, atomic MERGE upserts, status, and vector search."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional, Sequence

# Enforce mTLS / client certificate hygiene before importing google.auth or google.cloud.bigquery
os.environ["CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE"] = "false"
os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"

import google.auth
import google.auth.transport.requests
from google.cloud import bigquery

from ca_agent_sync.config import SyncConfig
from ca_agent_sync.models import (
    AgentMetadataRecord,
    GoldenQuerySearchHit,
    SearchHit,
    StatusReport,
    SyncResult,
    TwoStepSearchResult,
    collect_golden_queries,
)

logger = logging.getLogger(__name__)

GCP_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

INACCESSIBLE_SOURCE_WARNING = (
    "User lacks access to one or more LookML sources on this agent; some queries may fail."
)


def _clean_id_list(ids: Sequence[str]) -> list[str]:
    """Deduplicate and strip agent IDs while preserving first-seen order."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in ids:
        if raw is None:
            continue
        val = str(raw).strip()
        if val and val not in seen:
            seen.add(val)
            cleaned.append(val)
    return cleaned


def _resolve_scope_ids(
    candidate_agent_ids: Optional[Sequence[str]],
    allowed_agent_ids: Optional[Sequence[str]],
) -> Optional[list[str]]:
    """Resolve effective `agent_id` scope from Stage 1 candidates and user ACLs."""
    if candidate_agent_ids is None and allowed_agent_ids is None:
        return None
    cleaned_candidates = (
        _clean_id_list(candidate_agent_ids) if candidate_agent_ids is not None else None
    )
    cleaned_allowed = (
        _clean_id_list(allowed_agent_ids) if allowed_agent_ids is not None else None
    )
    if cleaned_candidates is not None and cleaned_allowed is not None:
        allowed_set = set(cleaned_allowed)
        return [aid for aid in cleaned_candidates if aid in allowed_set]
    if cleaned_candidates is not None:
        return cleaned_candidates
    return cleaned_allowed


def build_table_ddl(
    project_id: str,
    dataset: str,
    table: str,
    full_connection_id: str,
    endpoint: str = "text-embedding-005",
) -> str:
    """Return the BigQuery `CREATE TABLE IF NOT EXISTS` DDL with Autonomous Embedding Generation."""
    return f"""CREATE TABLE IF NOT EXISTS `{project_id}.{dataset}.{table}` (
  agent_id STRING NOT NULL,
  instance_url STRING,
  agent_url STRING,
  name STRING,
  description STRING,
  category STRING,
  system_instructions STRING,
  sources JSON,
  golden_queries JSON,
  code_interpreter BOOL,
  created_by_name STRING,
  created_at TIMESTAMP,
  updated_at TIMESTAMP,
  synced_at TIMESTAMP,
  metadata_concat STRING,
  embedding STRUCT<result ARRAY<FLOAT64>, status STRING>
    GENERATED ALWAYS AS (
      AI.EMBED(
        metadata_concat,
        connection_id => '{full_connection_id}',
        endpoint => '{endpoint}'
      )
    )
    STORED OPTIONS (asynchronous = TRUE)
)"""


def build_golden_queries_table_ddl(
    project_id: str,
    dataset: str,
    table: str,
    full_connection_id: str,
    endpoint: str = "text-embedding-005",
) -> str:
    """Return the BigQuery `CREATE TABLE IF NOT EXISTS` DDL for `ca_golden_queries` with Autonomous Embedding Generation."""
    return f"""CREATE TABLE IF NOT EXISTS `{project_id}.{dataset}.{table}` (
  record_id STRING NOT NULL,
  golden_query_id STRING NOT NULL,
  agent_id STRING NOT NULL,
  agent_name STRING,
  agent_url STRING,
  instance_url STRING,
  questions ARRAY<STRING>,
  model STRING,
  explore STRING,
  fields ARRAY<STRING>,
  filters JSON,
  sorts ARRAY<STRING>,
  `limit` STRING,
  answer STRING,
  synced_at TIMESTAMP,
  metadata_concat STRING,
  embedding STRUCT<result ARRAY<FLOAT64>, status STRING>
    GENERATED ALWAYS AS (
      AI.EMBED(
        metadata_concat,
        connection_id => '{full_connection_id}',
        endpoint => '{endpoint}'
      )
    )
    STORED OPTIONS (asynchronous = TRUE)
)"""


def build_merge_sql(project_id: str, dataset: str, table: str) -> str:
    """Return the parameterized atomic `MERGE` SQL keyed on `agent_id`.

    Critically omits `embedding` from `INSERT` and `UPDATE SET` clauses so BigQuery
    autonomous embedding generation manages the `GENERATED ALWAYS` column, and avoids
    touching `metadata_concat` when its value is unchanged so existing generated
    embeddings are preserved across idempotent sync runs.
    """
    return f"""MERGE `{project_id}.{dataset}.{table}` AS T
USING (
  SELECT
    r.agent_id,
    r.instance_url,
    r.agent_url,
    r.name,
    r.description,
    r.category,
    r.system_instructions,
    SAFE.PARSE_JSON(r.sources_json) AS sources,
    SAFE.PARSE_JSON(r.golden_queries_json) AS golden_queries,
    r.code_interpreter,
    r.created_by_name,
    r.created_at,
    r.updated_at,
    r.synced_at,
    r.metadata_concat
  FROM UNNEST(@records) AS r
) AS S
ON T.agent_id = S.agent_id
WHEN MATCHED AND IFNULL(T.metadata_concat, '') != IFNULL(S.metadata_concat, '') THEN
  UPDATE SET
    instance_url = S.instance_url,
    agent_url = S.agent_url,
    name = S.name,
    description = S.description,
    category = S.category,
    system_instructions = S.system_instructions,
    sources = S.sources,
    golden_queries = S.golden_queries,
    code_interpreter = S.code_interpreter,
    created_by_name = S.created_by_name,
    created_at = S.created_at,
    updated_at = S.updated_at,
    synced_at = S.synced_at,
    metadata_concat = S.metadata_concat
WHEN MATCHED THEN
  UPDATE SET
    instance_url = S.instance_url,
    agent_url = S.agent_url,
    name = S.name,
    description = S.description,
    category = S.category,
    system_instructions = S.system_instructions,
    sources = S.sources,
    golden_queries = S.golden_queries,
    code_interpreter = S.code_interpreter,
    created_by_name = S.created_by_name,
    created_at = S.created_at,
    updated_at = S.updated_at,
    synced_at = S.synced_at
WHEN NOT MATCHED THEN
  INSERT (
    agent_id,
    instance_url,
    agent_url,
    name,
    description,
    category,
    system_instructions,
    sources,
    golden_queries,
    code_interpreter,
    created_by_name,
    created_at,
    updated_at,
    synced_at,
    metadata_concat
  )
  VALUES (
    S.agent_id,
    S.instance_url,
    S.agent_url,
    S.name,
    S.description,
    S.category,
    S.system_instructions,
    S.sources,
    S.golden_queries,
    S.code_interpreter,
    S.created_by_name,
    S.created_at,
    S.updated_at,
    S.synced_at,
    S.metadata_concat
  )"""


def build_golden_queries_merge_sql(project_id: str, dataset: str, table: str) -> str:
    """Return the parameterized atomic `MERGE` SQL for `ca_golden_queries` keyed on `record_id`.

    Omits `embedding` from `INSERT` and `UPDATE SET` clauses so BigQuery manages the
    autonomous embedding column, and preserves `metadata_concat` when unchanged so
    already-populated embeddings are not unnecessarily reset.
    """
    return f"""MERGE `{project_id}.{dataset}.{table}` AS T
USING (
  SELECT
    r.record_id,
    r.golden_query_id,
    r.agent_id,
    r.agent_name,
    r.agent_url,
    r.instance_url,
    ARRAY(SELECT JSON_VALUE(x) FROM UNNEST(JSON_QUERY_ARRAY(r.questions_json)) AS x) AS questions,
    r.model,
    r.explore,
    ARRAY(SELECT JSON_VALUE(x) FROM UNNEST(JSON_QUERY_ARRAY(r.fields_json)) AS x) AS fields,
    SAFE.PARSE_JSON(r.filters_json) AS filters,
    ARRAY(SELECT JSON_VALUE(x) FROM UNNEST(JSON_QUERY_ARRAY(r.sorts_json)) AS x) AS sorts,
    r.`limit` AS `limit`,
    r.answer,
    r.synced_at,
    r.metadata_concat
  FROM UNNEST(@records) AS r
) AS S
ON T.record_id = S.record_id
WHEN MATCHED AND IFNULL(T.metadata_concat, '') != IFNULL(S.metadata_concat, '') THEN
  UPDATE SET
    golden_query_id = S.golden_query_id,
    agent_id = S.agent_id,
    agent_name = S.agent_name,
    agent_url = S.agent_url,
    instance_url = S.instance_url,
    questions = S.questions,
    model = S.model,
    explore = S.explore,
    fields = S.fields,
    filters = S.filters,
    sorts = S.sorts,
    `limit` = S.`limit`,
    answer = S.answer,
    synced_at = S.synced_at,
    metadata_concat = S.metadata_concat
WHEN MATCHED THEN
  UPDATE SET
    golden_query_id = S.golden_query_id,
    agent_id = S.agent_id,
    agent_name = S.agent_name,
    agent_url = S.agent_url,
    instance_url = S.instance_url,
    questions = S.questions,
    model = S.model,
    explore = S.explore,
    fields = S.fields,
    filters = S.filters,
    sorts = S.sorts,
    `limit` = S.`limit`,
    answer = S.answer,
    synced_at = S.synced_at
WHEN NOT MATCHED THEN
  INSERT (
    record_id,
    golden_query_id,
    agent_id,
    agent_name,
    agent_url,
    instance_url,
    questions,
    model,
    explore,
    fields,
    filters,
    sorts,
    `limit`,
    answer,
    synced_at,
    metadata_concat
  )
  VALUES (
    S.record_id,
    S.golden_query_id,
    S.agent_id,
    S.agent_name,
    S.agent_url,
    S.instance_url,
    S.questions,
    S.model,
    S.explore,
    S.fields,
    S.filters,
    S.sorts,
    S.`limit`,
    S.answer,
    S.synced_at,
    S.metadata_concat
  )"""


class BigQueryAgentSync:
    """Manages BigQuery Autonomous Embedding provisioning, upserts, status, and search."""

    def __init__(
        self,
        config: SyncConfig,
        client: Optional[bigquery.Client] = None,
        credentials: Optional[Any] = None,
        session: Optional[Any] = None,
    ) -> None:
        os.environ["CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE"] = "false"
        os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"
        self.config = config
        self._client = client
        self._credentials = credentials
        self._session = session
        self._last_pruned_golden_queries_count: int = 0

    @property
    def credentials(self) -> Any:
        """Lazily resolve GCP Application Default Credentials."""
        if self._credentials is None:
            os.environ["CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE"] = "false"
            os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"
            self._credentials, _ = google.auth.default(scopes=GCP_SCOPES)
        return self._credentials

    @property
    def client(self) -> bigquery.Client:
        """Lazily initialize BigQuery client."""
        if self._client is None:
            self._client = bigquery.Client(
                project=self.config.project_id,
                credentials=self.credentials,
                location=self.config.location,
            )
        return self._client

    @property
    def session(self) -> Any:
        """Lazily initialize an `AuthorizedSession` for REST API calls (Connection/IAM)."""
        if self._session is None:
            self._session = google.auth.transport.requests.AuthorizedSession(self.credentials)
        return self._session

    def ensure_dataset(self) -> str:
        """Idempotently create the target BigQuery dataset (`looker_agent_registry`)."""
        dataset_id = self.config.full_dataset_id
        dataset_ref = bigquery.Dataset(dataset_id)
        dataset_ref.location = self.config.location
        self.client.create_dataset(dataset_ref, exists_ok=True)
        return dataset_id

    def ensure_connection_and_iam(self) -> dict[str, Any]:
        """Idempotently create the BigQuery Cloud Resource Connection and grant `roles/aiplatform.user`."""
        full_conn = self.config.full_connection_id
        conn_short = self.config.connection_id.split(".")[-1]
        loc_lower = self.config.connection_location

        # 1. Ensure connection exists via BigQuery SQL DCL/DDL
        create_conn_sql = f"""CREATE CONNECTION IF NOT EXISTS `{full_conn}`
OPTIONS (
  connection_type = 'CLOUD_RESOURCE',
  friendly_name = 'CA Agent Embedding Connection',
  description = 'Cloud Resource connection for Looker CA Agent autonomous embeddings'
)"""
        self.client.query(create_conn_sql).result()

        # 2. Retrieve connection metadata (specifically serviceAccountId) via BigQuery Connection REST API
        service_account_id: Optional[str] = None
        if self._session is not None or self._client is None or hasattr(self, "session"):
            try:
                conn_url = (
                    f"https://bigqueryconnection.googleapis.com/v1/projects/{self.config.project_id}"
                    f"/locations/{loc_lower}/connections/{conn_short}"
                )
                resp = self.session.get(conn_url, timeout=30)
                if resp.status_code == 200:
                    payload = resp.json()
                    service_account_id = payload.get("cloudResource", {}).get("serviceAccountId")
            except Exception as exc:
                logger.debug("REST connection lookup returned exception: %s", exc)

        # 3. Grant `roles/aiplatform.user` to the connection on the project via BigQuery SQL DCL
        grant_Error: Optional[str] = None
        grant_sql = f"""GRANT `roles/aiplatform.user`
ON PROJECT `{self.config.project_id}`
TO "connection:{full_conn}" """
        try:
            self.client.query(grant_sql).result()
        except Exception as exc:
            grant_Error = str(exc)
            logger.debug("SQL GRANT roles/aiplatform.user note: %s", exc)

        # 4. Also verify/bind via Cloud Resource Manager IAM API if service_account_id was retrieved
        iam_bound = grant_Error is None
        if service_account_id:
            try:
                crm_base = f"https://cloudresourcemanager.googleapis.com/v1/projects/{self.config.project_id}"
                get_resp = self.session.post(f"{crm_base}:getIamPolicy", json={}, timeout=30)
                if get_resp.status_code == 200:
                    policy = get_resp.json()
                    bindings = policy.setdefault("bindings", [])
                    member = f"serviceAccount:{service_account_id}"
                    role_name = "roles/aiplatform.user"
                    target_binding = next(
                        (
                            b
                            for b in bindings
                            if b.get("role") == role_name and "condition" not in b
                        ),
                        None,
                    )
                    needs_update = False
                    if target_binding is None:
                        bindings.append({"role": role_name, "members": [member]})
                        needs_update = True
                    elif member not in target_binding.get("members", []):
                        target_binding.setdefault("members", []).append(member)
                        needs_update = True
                    else:
                        iam_bound = True

                    if needs_update:
                        set_resp = self.session.post(
                            f"{crm_base}:setIamPolicy",
                            json={"policy": policy},
                            timeout=30,
                        )
                        if set_resp.status_code == 200:
                            iam_bound = True
            except Exception as exc:
                logger.debug("CRM IAM check note: %s", exc)

        return {
            "connection_id": full_conn,
            "service_account_id": service_account_id,
            "iam_role": "roles/aiplatform.user",
            "iam_bound": iam_bound,
        }

    def verify_embedding_warmup(self, max_attempts: int = 12, retry_delay_sec: float = 5.0) -> bool:
        """Verify that `AI.EMBED` succeeds with the connection (retrying during IAM propagation)."""
        warmup_sql = f"""SELECT ARRAY_LENGTH(
  AI.EMBED(
    'warmup',
    connection_id => '{self.config.full_connection_id}',
    endpoint => '{self.config.endpoint}'
  ).result
) AS dims"""
        for attempt in range(1, max_attempts + 1):
            try:
                rows = list(self.client.query(warmup_sql).result())
                if rows and getattr(rows[0], "dims", 0) and rows[0].dims > 0:
                    return True
                return True
            except Exception as exc:
                msg = str(exc).lower()
                if attempt < max_attempts and (
                    "permission" in msg
                    or "403" in msg
                    or "400" in msg
                    or "iam" in msg
                    or "access denied" in msg
                ):
                    logger.info(
                        "Waiting for IAM propagation on %s (attempt %d/%d)...",
                        self.config.full_connection_id,
                        attempt,
                        max_attempts,
                    )
                    if retry_delay_sec > 0:
                        time.sleep(retry_delay_sec)
                    continue
                logger.warning("AI.EMBED warmup check note: %s", exc)
                return False
        return False

    def _verify_table_embedding_column(self, table_name: str, full_table_id: str) -> dict[str, Any]:
        """Verify `embedding` column in `INFORMATION_SCHEMA.COLUMNS` for a given table."""
        info_sql = f"""SELECT
  column_name,
  data_type,
  is_generated,
  generation_expression,
  is_stored,
  async_generation_status
FROM `{self.config.project_id}.{self.config.dataset}.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = @table_name AND column_name = 'embedding'"""
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("table_name", "STRING", table_name),
            ]
        )
        rows = list(self.client.query(info_sql, job_config=job_config).result())
        if not rows:
            raise RuntimeError(
                f"Table `{full_table_id}` is missing the `embedding` column in INFORMATION_SCHEMA.COLUMNS."
            )
        row = rows[0]
        is_generated = getattr(row, "is_generated", None)
        generation_expression = getattr(row, "generation_expression", None)
        data_type = getattr(row, "data_type", None)
        is_stored = getattr(row, "is_stored", None)
        async_status = getattr(row, "async_generation_status", None)

        if is_generated != "ALWAYS":
            raise RuntimeError(
                f"Column `embedding` on `{full_table_id}` has is_generated={is_generated!r} (expected 'ALWAYS')."
            )

        return {
            "table_id": full_table_id,
            "column_name": getattr(row, "column_name", "embedding"),
            "data_type": data_type,
            "is_generated": is_generated,
            "generation_expression": generation_expression,
            "is_stored": is_stored,
            "async_generation_status": async_status,
        }

    def ensure_table(self) -> dict[str, Any]:
        """Create the `ca_agents` table with Autonomous Embedding DDL and verify `INFORMATION_SCHEMA`."""
        ddl = build_table_ddl(
            project_id=self.config.project_id,
            dataset=self.config.dataset,
            table=self.config.table,
            full_connection_id=self.config.full_connection_id,
            endpoint=self.config.endpoint,
        )
        self.client.query(ddl).result()
        return self._verify_table_embedding_column(self.config.table, self.config.full_table_id)

    def ensure_golden_queries_table(self) -> dict[str, Any]:
        """Create the `ca_golden_queries` table with Autonomous Embedding DDL and verify `INFORMATION_SCHEMA`."""
        ddl = build_golden_queries_table_ddl(
            project_id=self.config.project_id,
            dataset=self.config.dataset,
            table=self.config.golden_queries_table,
            full_connection_id=self.config.full_connection_id,
            endpoint=self.config.endpoint,
        )
        self.client.query(ddl).result()
        return self._verify_table_embedding_column(
            self.config.golden_queries_table, self.config.full_golden_queries_table_id
        )

    def init_infrastructure(self, run_warmup: bool = True) -> dict[str, Any]:
        """Provision dataset, Cloud Resource Connection, IAM binding, and both `ca_agents` & `ca_golden_queries` tables."""
        dataset_id = self.ensure_dataset()
        conn_info = self.ensure_connection_and_iam()
        warmup_ok = self.verify_embedding_warmup() if run_warmup else True
        table_info = self.ensure_table()
        gq_table_info = self.ensure_golden_queries_table()
        return {
            "project_id": self.config.project_id,
            "location": self.config.location,
            "dataset_id": dataset_id,
            "connection": conn_info,
            "warmup_verified": warmup_ok,
            "table": table_info,
            "golden_queries_table": gq_table_info,
        }

    def delete_agent(self, agent_id: str) -> int:
        """Delete a single `agent_id` row from BigQuery `ca_agents` and its Golden Queries from `ca_golden_queries`."""
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("agent_id", "STRING", agent_id),
            ]
        )
        delete_sql = f"DELETE FROM `{self.config.full_table_id}` WHERE agent_id = @agent_id"
        query_job = self.client.query(delete_sql, job_config=job_config)
        query_job.result()
        affected_agents = int(getattr(query_job, "num_dml_affected_rows", 0) or 0)

        delete_gq_sql = (
            f"DELETE FROM `{self.config.full_golden_queries_table_id}` WHERE agent_id = @agent_id"
        )
        gq_job = self.client.query(delete_gq_sql, job_config=job_config)
        gq_job.result()
        return affected_agents

    def prune_stale_agents(self, active_agent_ids: Sequence[str]) -> tuple[int, list[str]]:
        """Delete rows in `ca_agents` and `ca_golden_queries` whose `agent_id` is not in `active_agent_ids`."""
        self._last_pruned_golden_queries_count = 0
        active_list = [str(aid).strip() for aid in active_agent_ids if str(aid).strip()]
        if not active_list:
            return 0, []

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("active_ids", "STRING", active_list),
            ]
        )
        # Identify stale agent IDs first for accurate reporting
        find_stale_sql = f"""SELECT agent_id
FROM `{self.config.full_table_id}`
WHERE agent_id NOT IN UNNEST(@active_ids)
ORDER BY agent_id"""
        stale_rows = list(self.client.query(find_stale_sql, job_config=job_config).result())
        stale_ids = [
            str(getattr(r, "agent_id", "")) for r in stale_rows if getattr(r, "agent_id", None)
        ]

        # Clean up any orphaned Golden Queries in ca_golden_queries whose agent_id is not in active_ids
        delete_gq_sql = f"""DELETE FROM `{self.config.full_golden_queries_table_id}`
WHERE agent_id NOT IN UNNEST(@active_ids)"""
        gq_del_job = self.client.query(delete_gq_sql, job_config=job_config)
        gq_del_job.result()
        gq_pruned = int(getattr(gq_del_job, "num_dml_affected_rows", 0) or 0)
        self._last_pruned_golden_queries_count = gq_pruned

        if not stale_ids:
            return 0, []

        delete_sql = f"""DELETE FROM `{self.config.full_table_id}`
WHERE agent_id NOT IN UNNEST(@active_ids)"""
        del_job = self.client.query(delete_sql, job_config=job_config)
        del_job.result()
        affected = int(getattr(del_job, "num_dml_affected_rows", len(stale_ids)) or len(stale_ids))
        return affected, stale_ids

    def upsert_golden_queries(
        self,
        records: Sequence[AgentMetadataRecord],
    ) -> tuple[int, int]:
        """Atomic-MERGE all active Golden Queries for `records` into `ca_golden_queries` and delete unlinked rows.

        Returns:
            `(synced_golden_queries_count, deleted_unlinked_golden_queries_count)`
        """
        if not records:
            return 0, 0

        synced_agent_ids = [r.agent_id for r in records if r.agent_id]
        gq_records = collect_golden_queries(records)

        if gq_records:
            gq_struct_params = [
                bigquery.StructQueryParameter(
                    None,
                    bigquery.ScalarQueryParameter("record_id", "STRING", gq.record_id),
                    bigquery.ScalarQueryParameter("golden_query_id", "STRING", gq.golden_query_id),
                    bigquery.ScalarQueryParameter("agent_id", "STRING", gq.agent_id),
                    bigquery.ScalarQueryParameter("agent_name", "STRING", gq.agent_name),
                    bigquery.ScalarQueryParameter("agent_url", "STRING", gq.agent_url),
                    bigquery.ScalarQueryParameter("instance_url", "STRING", gq.instance_url),
                    bigquery.ScalarQueryParameter("questions_json", "STRING", gq.questions_json),
                    bigquery.ScalarQueryParameter("model", "STRING", gq.model),
                    bigquery.ScalarQueryParameter("explore", "STRING", gq.explore),
                    bigquery.ScalarQueryParameter("fields_json", "STRING", gq.fields_json),
                    bigquery.ScalarQueryParameter("filters_json", "STRING", gq.filters_json),
                    bigquery.ScalarQueryParameter("sorts_json", "STRING", gq.sorts_json),
                    bigquery.ScalarQueryParameter("limit", "STRING", gq.limit),
                    bigquery.ScalarQueryParameter("answer", "STRING", gq.answer),
                    bigquery.ScalarQueryParameter("synced_at", "TIMESTAMP", gq.synced_at),
                    bigquery.ScalarQueryParameter("metadata_concat", "STRING", gq.metadata_concat),
                )
                for gq in gq_records
            ]
            gq_merge_sql = build_golden_queries_merge_sql(
                project_id=self.config.project_id,
                dataset=self.config.dataset,
                table=self.config.golden_queries_table,
            )
            gq_merge_cfg = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("records", "STRUCT", gq_struct_params),
                ]
            )
            self.client.query(gq_merge_sql, job_config=gq_merge_cfg).result()

            active_record_ids = [gq.record_id for gq in gq_records]
            cleanup_sql = f"""DELETE FROM `{self.config.full_golden_queries_table_id}`
WHERE agent_id IN UNNEST(@synced_agent_ids)
  AND record_id NOT IN UNNEST(@active_record_ids)"""
            cleanup_cfg = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("synced_agent_ids", "STRING", synced_agent_ids),
                    bigquery.ArrayQueryParameter("active_record_ids", "STRING", active_record_ids),
                ]
            )
            cleanup_job = self.client.query(cleanup_sql, job_config=cleanup_cfg)
            cleanup_job.result()
            unlinked_deleted = int(getattr(cleanup_job, "num_dml_affected_rows", 0) or 0)
        else:
            cleanup_sql = f"""DELETE FROM `{self.config.full_golden_queries_table_id}`
WHERE agent_id IN UNNEST(@synced_agent_ids)"""
            cleanup_cfg = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("synced_agent_ids", "STRING", synced_agent_ids),
                ]
            )
            cleanup_job = self.client.query(cleanup_sql, job_config=cleanup_cfg)
            cleanup_job.result()
            unlinked_deleted = int(getattr(cleanup_job, "num_dml_affected_rows", 0) or 0)

        return len(gq_records), unlinked_deleted

    def upsert_agents(
        self,
        records: Sequence[AgentMetadataRecord],
        prune: bool = False,
        mode: str = "batch",
        explore_headers_cached: int = 0,
    ) -> SyncResult:
        """Execute an atomic parameterized `MERGE` keyed on `agent_id` and `record_id` (and optional `--prune`)."""
        self._last_pruned_golden_queries_count = 0
        if not records:
            return SyncResult(
                mode=mode,
                table_id=self.config.full_table_id,
                synced_count=0,
                explore_headers_cached=explore_headers_cached,
                golden_queries_table_id=self.config.full_golden_queries_table_id,
                golden_queries_synced_count=0,
            )

        struct_params = [
            bigquery.StructQueryParameter(
                None,
                bigquery.ScalarQueryParameter("agent_id", "STRING", rec.agent_id),
                bigquery.ScalarQueryParameter("instance_url", "STRING", rec.instance_url),
                bigquery.ScalarQueryParameter("agent_url", "STRING", rec.agent_url),
                bigquery.ScalarQueryParameter("name", "STRING", rec.name),
                bigquery.ScalarQueryParameter("description", "STRING", rec.description),
                bigquery.ScalarQueryParameter("category", "STRING", rec.category),
                bigquery.ScalarQueryParameter(
                    "system_instructions", "STRING", rec.system_instructions
                ),
                bigquery.ScalarQueryParameter("sources_json", "STRING", rec.sources_json),
                bigquery.ScalarQueryParameter(
                    "golden_queries_json", "STRING", rec.golden_queries_json
                ),
                bigquery.ScalarQueryParameter("code_interpreter", "BOOL", rec.code_interpreter),
                bigquery.ScalarQueryParameter("created_by_name", "STRING", rec.created_by_name),
                bigquery.ScalarQueryParameter("created_at", "TIMESTAMP", rec.created_at),
                bigquery.ScalarQueryParameter("updated_at", "TIMESTAMP", rec.updated_at),
                bigquery.ScalarQueryParameter("synced_at", "TIMESTAMP", rec.synced_at),
                bigquery.ScalarQueryParameter("metadata_concat", "STRING", rec.metadata_concat),
            )
            for rec in records
        ]

        merge_sql = build_merge_sql(
            project_id=self.config.project_id,
            dataset=self.config.dataset,
            table=self.config.table,
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("records", "STRUCT", struct_params),
            ]
        )
        self.client.query(merge_sql, job_config=job_config).result()

        gq_synced_count, gq_deleted_count = self.upsert_golden_queries(records)

        pruned_count = 0
        pruned_ids: list[str] = []
        if prune:
            pruned_count, pruned_ids = self.prune_stale_agents([r.agent_id for r in records])

        return SyncResult(
            mode=mode,
            table_id=self.config.full_table_id,
            synced_count=len(records),
            pruned_count=pruned_count,
            explore_headers_cached=explore_headers_cached,
            agent_ids=[r.agent_id for r in records],
            pruned_agent_ids=pruned_ids,
            golden_queries_table_id=self.config.full_golden_queries_table_id,
            golden_queries_synced_count=gq_synced_count,
            golden_queries_deleted_count=gq_deleted_count,
            golden_queries_pruned_count=getattr(self, "_last_pruned_golden_queries_count", 0),
        )

    def _get_single_table_status(
        self,
        table_name: str,
        full_table_id: str,
        id_col: str = "agent_id",
        name_col: str = "name",
    ) -> dict[str, Any]:
        """Inspect BigQuery autonomous embedding generation status for a single table."""
        counts_sql = f"""SELECT
  COUNT(*) AS total_rows,
  COUNTIF(embedding IS NOT NULL AND ARRAY_LENGTH(embedding.result) > 0) AS populated_count,
  COUNTIF(embedding IS NULL OR (ARRAY_LENGTH(embedding.result) = 0 AND IFNULL(embedding.status, '') = '')) AS pending_count,
  COUNTIF(embedding IS NOT NULL AND IFNULL(embedding.status, '') != '') AS error_count,
  MAX(synced_at) AS last_synced_at
FROM `{full_table_id}`"""
        count_rows = list(self.client.query(counts_sql).result())
        total_rows = 0
        populated_count = 0
        pending_count = 0
        error_count = 0
        last_synced_str: Optional[str] = None
        if count_rows:
            r0 = count_rows[0]
            total_rows = int(getattr(r0, "total_rows", 0) or 0)
            populated_count = int(getattr(r0, "populated_count", 0) or 0)
            pending_count = int(getattr(r0, "pending_count", 0) or 0)
            error_count = int(getattr(r0, "error_count", 0) or 0)
            ls = getattr(r0, "last_synced_at", None)
            if ls is not None:
                last_synced_str = ls.isoformat() if hasattr(ls, "isoformat") else str(ls)

        info_sql = f"""SELECT
  is_generated,
  generation_expression,
  async_generation_status
FROM `{self.config.project_id}.{self.config.dataset}.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = @table_name AND column_name = 'embedding'"""
        info_cfg = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("table_name", "STRING", table_name),
            ]
        )
        info_rows = list(self.client.query(info_sql, job_config=info_cfg).result())
        is_generated: Optional[str] = None
        generation_expression: Optional[str] = None
        async_status: Any = None
        if info_rows:
            ir = info_rows[0]
            is_generated = getattr(ir, "is_generated", None)
            generation_expression = getattr(ir, "generation_expression", None)
            raw_async = getattr(ir, "async_generation_status", None)
            if isinstance(raw_async, dict):
                async_status = {
                    k: (v.isoformat() if hasattr(v, "isoformat") else v)
                    for k, v in raw_async.items()
                }
            elif raw_async is not None:
                async_status = str(raw_async)

        errors: list[dict[str, Any]] = []
        if error_count > 0:
            err_sql = f"""SELECT
  {id_col} AS row_id,
  {name_col} AS row_name,
  embedding.status AS error_status
FROM `{full_table_id}`
WHERE embedding IS NOT NULL AND IFNULL(embedding.status, '') != ''
ORDER BY {id_col}"""
            err_rows = list(self.client.query(err_sql).result())
            for er in err_rows:
                errors.append(
                    {
                        id_col: getattr(er, "row_id", getattr(er, id_col, "")),
                        name_col: getattr(er, "row_name", getattr(er, name_col, "")),
                        "status": getattr(er, "error_status", ""),
                    }
                )

        return {
            "table_id": full_table_id,
            "total_rows": total_rows,
            "populated_embeddings": populated_count,
            "pending_embeddings": pending_count,
            "error_embeddings": error_count,
            "last_synced_at": last_synced_str,
            "is_generated": is_generated,
            "generation_expression": generation_expression,
            "async_generation_status": async_status,
            "errors": errors,
        }

    def get_status(self) -> StatusReport:
        """Inspect BigQuery `ca_agents` and `ca_golden_queries` autonomous embedding generation status."""
        agents_status = self._get_single_table_status(
            table_name=self.config.table,
            full_table_id=self.config.full_table_id,
            id_col="agent_id",
            name_col="name",
        )
        gq_status = self._get_single_table_status(
            table_name=self.config.golden_queries_table,
            full_table_id=self.config.full_golden_queries_table_id,
            id_col="record_id",
            name_col="agent_name",
        )
        return StatusReport(
            table_id=agents_status["table_id"],
            total_rows=agents_status["total_rows"],
            populated_embeddings=agents_status["populated_embeddings"],
            pending_embeddings=agents_status["pending_embeddings"],
            error_embeddings=agents_status["error_embeddings"],
            last_synced_at=agents_status["last_synced_at"],
            is_generated=agents_status["is_generated"],
            generation_expression=agents_status["generation_expression"],
            async_generation_status=agents_status["async_generation_status"],
            errors=agents_status["errors"],
            golden_queries_table_id=gq_status["table_id"],
            golden_queries_total_rows=gq_status["total_rows"],
            golden_queries_populated_embeddings=gq_status["populated_embeddings"],
            golden_queries_pending_embeddings=gq_status["pending_embeddings"],
            golden_queries_error_embeddings=gq_status["error_embeddings"],
            golden_queries_last_synced_at=gq_status["last_synced_at"],
            golden_queries_is_generated=gq_status["is_generated"],
            golden_queries_generation_expression=gq_status["generation_expression"],
            golden_queries_async_generation_status=gq_status["async_generation_status"],
            golden_queries_errors=gq_status["errors"],
        )

    def search_golden_queries(
        self,
        question: str,
        top_k: int = 5,
        allowed_agent_ids: Optional[Sequence[str]] = None,
        inaccessible_source_agent_ids: Optional[Sequence[str]] = None,
    ) -> list[GoldenQuerySearchHit]:
        """Stage 1: Search `ca_golden_queries` using `AI.SEARCH` with automatic `AI.EMBED` + `ML.DISTANCE` fallback."""
        cleaned_q = question.strip()
        if not cleaned_q:
            raise ValueError("Search question must not be empty.")
        k = max(1, int(top_k))

        scope_ids = _resolve_scope_ids(None, allowed_agent_ids)
        if scope_ids is not None and not scope_ids:
            return []
        inaccessible_set = set(_clean_id_list(inaccessible_source_agent_ids or ()))

        check_sql = f"""SELECT
  COUNT(*) AS total_rows,
  COUNTIF(embedding IS NOT NULL AND ARRAY_LENGTH(embedding.result) > 0) AS populated_count
FROM `{self.config.full_golden_queries_table_id}`"""
        check_rows = list(self.client.query(check_sql).result())
        total_rows = int(getattr(check_rows[0], "total_rows", 0) or 0) if check_rows else 0
        populated_count = (
            int(getattr(check_rows[0], "populated_count", 0) or 0) if check_rows else 0
        )

        if total_rows == 0:
            return []

        conn_id = self.config.full_connection_id
        endpoint = self.config.endpoint

        query_parameters: list[Any] = [
            bigquery.ScalarQueryParameter("query_text", "STRING", cleaned_q),
            bigquery.ScalarQueryParameter("top_k", "INT64", k),
        ]

        if populated_count >= total_rows and total_rows > 0:
            if scope_ids is not None:
                query_parameters.append(
                    bigquery.ArrayQueryParameter("allowed_agent_ids", "STRING", scope_ids)
                )
                gq_source_expr = (
                    f"(SELECT * FROM `{self.config.full_golden_queries_table_id}` "
                    "WHERE agent_id IN UNNEST(@allowed_agent_ids))"
                )
            else:
                gq_source_expr = f"TABLE `{self.config.full_golden_queries_table_id}`"

            search_sql = f"""SELECT
  base.record_id,
  base.golden_query_id,
  base.agent_id,
  base.agent_name,
  base.agent_url,
  base.instance_url,
  base.questions,
  base.model,
  base.explore,
  base.fields,
  TO_JSON_STRING(base.filters) AS filters_json,
  base.sorts,
  base.`limit` AS `limit`,
  base.answer,
  'STORED' AS embedding_source,
  (POW(distance, 2) / 2.0) AS distance,
  (1.0 - POW(distance, 2) / 2.0) AS cosine_similarity
FROM AI.SEARCH(
  {gq_source_expr},
  'metadata_concat',
  @query_text,
  top_k => @top_k
)
ORDER BY distance ASC
LIMIT @top_k"""
        else:
            where_filter = ""
            if scope_ids is not None:
                where_filter = "\n  WHERE t.agent_id IN UNNEST(@allowed_agent_ids)"
                query_parameters.append(
                    bigquery.ArrayQueryParameter("allowed_agent_ids", "STRING", scope_ids)
                )
            search_sql = f"""WITH query_emb AS (
  SELECT AI.EMBED(
    @query_text,
    connection_id => '{conn_id}',
    endpoint => '{endpoint}'
  ).result AS vec
),
candidate_gqs AS (
  SELECT
    t.record_id,
    t.golden_query_id,
    t.agent_id,
    t.agent_name,
    t.agent_url,
    t.instance_url,
    t.questions,
    t.model,
    t.explore,
    t.fields,
    TO_JSON_STRING(t.filters) AS filters_json,
    t.sorts,
    t.`limit` AS `limit`,
    t.answer,
    CASE
      WHEN t.embedding IS NOT NULL AND ARRAY_LENGTH(t.embedding.result) > 0 THEN 'STORED'
      ELSE 'ON_THE_FLY_FALLBACK'
    END AS embedding_source,
    CASE
      WHEN t.embedding IS NOT NULL AND ARRAY_LENGTH(t.embedding.result) > 0 THEN t.embedding.result
      ELSE AI.EMBED(
        t.metadata_concat,
        connection_id => '{conn_id}',
        endpoint => '{endpoint}'
      ).result
    END AS gq_vec
  FROM `{self.config.full_golden_queries_table_id}` AS t{where_filter}
)
SELECT
  c.record_id,
  c.golden_query_id,
  c.agent_id,
  c.agent_name,
  c.agent_url,
  c.instance_url,
  c.questions,
  c.model,
  c.explore,
  c.fields,
  c.filters_json,
  c.sorts,
  c.`limit`,
  c.answer,
  c.embedding_source,
  ML.DISTANCE(c.gq_vec, q.vec, 'COSINE') AS distance,
  (1.0 - ML.DISTANCE(c.gq_vec, q.vec, 'COSINE')) AS cosine_similarity
FROM candidate_gqs AS c
CROSS JOIN query_emb AS q
WHERE ARRAY_LENGTH(c.gq_vec) > 0
ORDER BY distance ASC
LIMIT @top_k"""

        job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
        rows = list(self.client.query(search_sql, job_config=job_config).result())
        hits: list[GoldenQuerySearchHit] = []
        for idx, row in enumerate(rows, start=1):
            raw_filters_str = getattr(row, "filters_json", None)
            parsed_filters: dict[str, Any] = {}
            if isinstance(raw_filters_str, str) and raw_filters_str.strip():
                try:
                    loaded = json.loads(raw_filters_str)
                    if isinstance(loaded, dict):
                        parsed_filters = loaded
                except json.JSONDecodeError:
                    parsed_filters = {}
            elif isinstance(getattr(row, "filters", None), dict):
                parsed_filters = dict(getattr(row, "filters"))

            dist = float(getattr(row, "distance", 0.0) or 0.0)
            sim = float(getattr(row, "cosine_similarity", 1.0 - dist) or (1.0 - dist))
            questions_val = getattr(row, "questions", None) or []
            fields_val = getattr(row, "fields", None) or []
            sorts_val = getattr(row, "sorts", None) or []
            agent_id_str = str(getattr(row, "agent_id", ""))
            has_inaccessible = agent_id_str in inaccessible_set
            hits.append(
                GoldenQuerySearchHit(
                    rank=idx,
                    record_id=str(getattr(row, "record_id", "")),
                    golden_query_id=str(getattr(row, "golden_query_id", "")),
                    agent_id=agent_id_str,
                    agent_name=str(getattr(row, "agent_name", "")),
                    agent_url=str(getattr(row, "agent_url", "")),
                    instance_url=getattr(row, "instance_url", None),
                    questions=[str(q) for q in questions_val if q is not None],
                    model=getattr(row, "model", None),
                    explore=getattr(row, "explore", None),
                    fields=[str(f) for f in fields_val if f is not None],
                    filters=parsed_filters,
                    sorts=[str(s) for s in sorts_val if s is not None],
                    limit=getattr(row, "limit", None),
                    answer=getattr(row, "answer", None),
                    similarity_score=round(sim, 6),
                    distance=round(dist, 6),
                    embedding_source=str(getattr(row, "embedding_source", "STORED")),
                    has_inaccessible_source=has_inaccessible,
                    warning=INACCESSIBLE_SOURCE_WARNING if has_inaccessible else None,
                )
            )
        return hits

    def search_agents(
        self,
        question: str,
        top_k: int = 5,
        candidate_agent_ids: Optional[Sequence[str]] = None,
        allowed_agent_ids: Optional[Sequence[str]] = None,
        inaccessible_source_agent_ids: Optional[Sequence[str]] = None,
    ) -> list[SearchHit]:
        """Route a natural language question to top-K CA agents using semantic vector search.

        When `candidate_agent_ids` and/or `allowed_agent_ids` are provided,
        filters `ca_agents` strictly to `WHERE agent_id IN UNNEST(@matched_agent_ids)`.
        Uses `AI.SEARCH` when all rows have populated autonomous embeddings, and
        transparently falls back to `AI.EMBED` + `ML.DISTANCE(..., 'COSINE')` when
        background autonomous embedding generation is still pending for any row.
        """
        cleaned_q = question.strip()
        if not cleaned_q:
            raise ValueError("Search question must not be empty.")
        k = max(1, int(top_k))

        matched_ids = _resolve_scope_ids(candidate_agent_ids, allowed_agent_ids)
        if matched_ids is not None and not matched_ids:
            return []
        inaccessible_set = set(_clean_id_list(inaccessible_source_agent_ids or ()))

        # Check whether all rows have populated stored embeddings
        check_sql = f"""SELECT
  COUNT(*) AS total_rows,
  COUNTIF(embedding IS NOT NULL AND ARRAY_LENGTH(embedding.result) > 0) AS populated_count
FROM `{self.config.full_table_id}`"""
        check_rows = list(self.client.query(check_sql).result())
        total_rows = int(getattr(check_rows[0], "total_rows", 0) or 0) if check_rows else 0
        populated_count = (
            int(getattr(check_rows[0], "populated_count", 0) or 0) if check_rows else 0
        )

        if total_rows == 0:
            return []

        conn_id = self.config.full_connection_id
        endpoint = self.config.endpoint

        query_params: list[Any] = [
            bigquery.ScalarQueryParameter("query_text", "STRING", cleaned_q),
            bigquery.ScalarQueryParameter("top_k", "INT64", k),
        ]

        if populated_count >= total_rows and total_rows > 0:
            if matched_ids is not None:
                query_params.append(
                    bigquery.ArrayQueryParameter("matched_agent_ids", "STRING", matched_ids)
                )
                agent_source_expr = (
                    f"(SELECT * FROM `{self.config.full_table_id}` "
                    "WHERE agent_id IN UNNEST(@matched_agent_ids))"
                )
            else:
                agent_source_expr = f"TABLE `{self.config.full_table_id}`"

            search_sql = f"""SELECT
  base.agent_id,
  base.name,
  base.description,
  base.category,
  base.agent_url,
  base.instance_url,
  TO_JSON_STRING(base.sources) AS sources_json,
  'STORED' AS embedding_source,
  (POW(distance, 2) / 2.0) AS distance,
  (1.0 - POW(distance, 2) / 2.0) AS cosine_similarity
FROM AI.SEARCH(
  {agent_source_expr},
  'metadata_concat',
  @query_text,
  top_k => @top_k
)
ORDER BY distance ASC
LIMIT @top_k"""
        else:
            where_filter = ""
            if matched_ids is not None:
                where_filter = "\n  WHERE agent_id IN UNNEST(@matched_agent_ids)"
                query_params.append(
                    bigquery.ArrayQueryParameter("matched_agent_ids", "STRING", matched_ids)
                )
            search_sql = f"""WITH query_emb AS (
  SELECT AI.EMBED(
    @query_text,
    connection_id => '{conn_id}',
    endpoint => '{endpoint}'
  ).result AS vec
),
candidate_agents AS (
  SELECT
    t.agent_id,
    t.name,
    t.description,
    t.category,
    t.agent_url,
    t.instance_url,
    TO_JSON_STRING(t.sources) AS sources_json,
    CASE
      WHEN t.embedding IS NOT NULL AND ARRAY_LENGTH(t.embedding.result) > 0 THEN 'STORED'
      ELSE 'ON_THE_FLY_FALLBACK'
    END AS embedding_source,
    CASE
      WHEN t.embedding IS NOT NULL AND ARRAY_LENGTH(t.embedding.result) > 0 THEN t.embedding.result
      ELSE AI.EMBED(
        t.metadata_concat,
        connection_id => '{conn_id}',
        endpoint => '{endpoint}'
      ).result
    END AS agent_vec
  FROM `{self.config.full_table_id}` AS t{where_filter}
)
SELECT
  c.agent_id,
  c.name,
  c.description,
  c.category,
  c.agent_url,
  c.instance_url,
  c.sources_json,
  c.embedding_source,
  ML.DISTANCE(c.agent_vec, q.vec, 'COSINE') AS distance,
  (1.0 - ML.DISTANCE(c.agent_vec, q.vec, 'COSINE')) AS cosine_similarity
FROM candidate_agents AS c
CROSS JOIN query_emb AS q
WHERE ARRAY_LENGTH(c.agent_vec) > 0
ORDER BY distance ASC
LIMIT @top_k"""

        job_config = bigquery.QueryJobConfig(query_parameters=query_params)
        rows = list(self.client.query(search_sql, job_config=job_config).result())
        hits: list[SearchHit] = []
        for idx, row in enumerate(rows, start=1):
            raw_sources_str = getattr(row, "sources_json", None)
            parsed_sources: list[dict[str, Any]] = []
            if isinstance(raw_sources_str, str) and raw_sources_str.strip():
                try:
                    loaded = json.loads(raw_sources_str)
                    if isinstance(loaded, list):
                        parsed_sources = [s for s in loaded if isinstance(s, dict)]
                except json.JSONDecodeError:
                    parsed_sources = []
            elif isinstance(getattr(row, "sources", None), list):
                parsed_sources = [s for s in getattr(row, "sources") if isinstance(s, dict)]

            explores_list: list[str] = []
            for s in parsed_sources:
                m = str(s.get("model") or "").strip()
                e = str(s.get("explore") or s.get("name") or "").strip()
                if m and e:
                    explores_list.append(f"{m}.{e}")
                elif e or m:
                    explores_list.append(e or m)

            dist = float(getattr(row, "distance", 0.0) or 0.0)
            sim = float(getattr(row, "cosine_similarity", 1.0 - dist) or (1.0 - dist))
            agent_id_str = str(getattr(row, "agent_id", ""))
            has_inaccessible = agent_id_str in inaccessible_set
            hits.append(
                SearchHit(
                    rank=idx,
                    agent_id=agent_id_str,
                    name=str(getattr(row, "name", "")),
                    description=getattr(row, "description", None),
                    category=getattr(row, "category", None),
                    similarity_score=round(sim, 6),
                    distance=round(dist, 6),
                    agent_url=str(getattr(row, "agent_url", "")),
                    instance_url=getattr(row, "instance_url", None),
                    explores=explores_list,
                    sources=parsed_sources,
                    embedding_source=str(getattr(row, "embedding_source", "STORED")),
                    has_inaccessible_source=has_inaccessible,
                    warning=INACCESSIBLE_SOURCE_WARNING if has_inaccessible else None,
                )
            )
        return hits

    def search_two_step(
        self,
        question: str,
        top_k: int = 5,
        golden_queries_top_k: int = 5,
        confidence_score: float = 0.60,
        stop_on_golden_match: bool = False,
        allowed_agent_ids: Optional[Sequence[str]] = None,
        inaccessible_source_agent_ids: Optional[Sequence[str]] = None,
        authz_mode: str = "admin",
        user_id: Optional[str] = None,
    ) -> TwoStepSearchResult:
        """Execute Two-Step Semantic Routing Search across `ca_golden_queries` and `ca_agents`.

        Stage 1: Search `ca_golden_queries` for up to `golden_queries_top_k` matches.
        Filter Stage 1 matches by `similarity_score >= confidence_score`.
        - If >= 1 Golden Queries meet `confidence_score`:
          - Return those matched Golden Queries.
          - If `stop_on_golden_match` is True, stop after Stage 1 (`results=[]`).
          - Otherwise run Stage 2 Agent search on `ca_agents` filtered strictly to
            the distinct `agent_id`s from those matched Golden Queries (`WHERE agent_id IN UNNEST(@matched_agent_ids)`).
        - If 0 Golden Queries meet `confidence_score`:
          - Return no Golden Queries (`golden_query_results=[]`) and proceed to an
            unfiltered Stage 2 Agent search across all rows in `ca_agents` (or within `allowed_agent_ids` when user-scoped).
        """
        if not (0.0 <= confidence_score <= 1.0):
            raise ValueError("confidence_score must be between 0.0 and 1.0 inclusive.")
        gq_k = max(1, int(golden_queries_top_k))

        cleaned_allowed = _resolve_scope_ids(None, allowed_agent_ids)
        allowed_count = len(cleaned_allowed) if cleaned_allowed is not None else None
        base_kwargs: dict[str, Any] = {
            "question": question,
            "top_k": top_k,
            "golden_queries_requested": gq_k,
            "confidence_score": confidence_score,
            "stop_on_golden_match": stop_on_golden_match,
            "authz_mode": authz_mode,
            "user_id": user_id,
            "allowed_agent_count": allowed_count,
        }
        if cleaned_allowed is not None and not cleaned_allowed:
            return TwoStepSearchResult(**base_kwargs)

        stage1_candidates = self.search_golden_queries(
            question=question,
            top_k=gq_k,
            allowed_agent_ids=cleaned_allowed,
            inaccessible_source_agent_ids=inaccessible_source_agent_ids,
        )
        matched_gqs: list[GoldenQuerySearchHit] = []
        matched_agent_ids: list[str] = []
        seen_matched_agents: set[str] = set()
        for hit in stage1_candidates:
            if hit.similarity_score >= confidence_score:
                matched_gqs.append(hit.model_copy(update={"rank": len(matched_gqs) + 1}))
                if hit.agent_id and hit.agent_id not in seen_matched_agents:
                    seen_matched_agents.add(hit.agent_id)
                    matched_agent_ids.append(hit.agent_id)

        if matched_gqs:
            agent_hits = (
                []
                if stop_on_golden_match
                else self.search_agents(
                    question=question,
                    top_k=top_k,
                    candidate_agent_ids=matched_agent_ids,
                    allowed_agent_ids=cleaned_allowed,
                    inaccessible_source_agent_ids=inaccessible_source_agent_ids,
                )
            )
            return TwoStepSearchResult(
                **base_kwargs,
                golden_query_matched=True,
                stage2_filtered=True,
                matched_agent_ids=matched_agent_ids,
                golden_query_results=matched_gqs,
                results=agent_hits,
            )

        agent_hits = self.search_agents(
            question=question,
            top_k=top_k,
            candidate_agent_ids=None,
            allowed_agent_ids=cleaned_allowed,
            inaccessible_source_agent_ids=inaccessible_source_agent_ids,
        )
        return TwoStepSearchResult(
            **base_kwargs,
            results=agent_hits,
        )

