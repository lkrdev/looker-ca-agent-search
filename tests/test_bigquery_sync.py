"""Unit tests for BigQuery Autonomous Embedding DDL, parameterized MERGE, prune, status, and search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ca_agent_sync.bigquery_sync import (
    BigQueryAgentSync,
    build_golden_queries_merge_sql,
    build_golden_queries_table_ddl,
    build_merge_sql,
    build_table_ddl,
)
from ca_agent_sync.config import SyncConfig
from ca_agent_sync.models import AgentMetadataRecord, ExploreHeader, GoldenQueryRecord


@dataclass
class FakeRow:
    """Simple attribute-accessible row returned by FakeBigQueryClient."""

    data: dict[str, Any]

    def __getattr__(self, item: str) -> Any:
        if item in self.data:
            return self.data[item]
        raise AttributeError(item)


class FakeQueryJob:
    """Simulates a BigQuery QueryJob."""

    def __init__(self, rows: list[FakeRow], num_dml_affected_rows: int = 0) -> None:
        self._rows = rows
        self.num_dml_affected_rows = num_dml_affected_rows

    def result(self) -> list[FakeRow]:
        return self._rows


class FakeBigQueryClient:
    """Captures SQL queries and parameters executed by `BigQueryAgentSync`."""

    def __init__(self) -> None:
        self.created_datasets: list[tuple[str, Optional[str]]] = []
        self.queries: list[tuple[str, Any]] = []
        self.info_schema_rows: list[FakeRow] = [
            FakeRow(
                {
                    "column_name": "embedding",
                    "data_type": "STRUCT<result ARRAY<FLOAT64>, status STRING>",
                    "is_generated": "ALWAYS",
                    "generation_expression": (
                        "AI.EMBED(metadata_concat, "
                        "connection_id => 'test-gcp-project.us.ca_agent_embedding_conn', "
                        "endpoint => 'text-embedding-005')"
                    ),
                    "is_stored": "YES",
                    "async_generation_status": {"blocking_error": None, "last_updated_time": None},
                }
            )
        ]
        self.stale_agent_rows: list[FakeRow] = [FakeRow({"agent_id": "stale_agent_99"})]
        self.status_count_rows: list[FakeRow] = [
            FakeRow(
                {
                    "total_rows": 22,
                    "populated_count": 20,
                    "pending_count": 2,
                    "error_count": 0,
                    "last_synced_at": "2026-10-12T23:35:00+00:00",
                }
            )
        ]
        self.gq_status_count_rows: list[FakeRow] = [
            FakeRow(
                {
                    "total_rows": 84,
                    "populated_count": 84,
                    "pending_count": 0,
                    "error_count": 0,
                    "last_synced_at": "2026-10-13T16:40:00+00:00",
                }
            )
        ]
        self.search_rows: list[FakeRow] = [
            FakeRow(
                {
                    "agent_id": "agent-eng-001",
                    "name": "Engineering Velocity & SLA Agent",
                    "description": "Tracks sprint velocity and SLA compliance.",
                    "category": "Engineering",
                    "agent_url": "https://looker.example.com/conversational-analytics/agents/agent-eng-001",
                    "instance_url": "https://looker.example.com",
                    "sources_json": '[{"model": "jira", "explore": "sprint_issues"}]',
                    "embedding_source": "STORED",
                    "distance": 0.12,
                    "cosine_similarity": 0.88,
                }
            )
        ]
        self.gq_search_rows: list[FakeRow] = [
            FakeRow(
                {
                    "record_id": "agent-eng-001:101",
                    "golden_query_id": "101",
                    "agent_id": "agent-eng-001",
                    "agent_name": "Linear Engineering Intelligence Assistant",
                    "agent_url": "https://looker.example.com/conversational-analytics/agents/agent-eng-001",
                    "instance_url": "https://looker.example.com",
                    "questions": [
                        "What is the status and completion progress of all strategic projects?"
                    ],
                    "model": "engineering_analytics",
                    "explore": "project",
                    "fields": ["project.name", "project.progress"],
                    "filters_json": '{"project.state": "started"}',
                    "sorts": ["project.progress desc"],
                    "limit": "100",
                    "answer": None,
                    "embedding_source": "STORED",
                    "distance": 0.08,
                    "cosine_similarity": 0.92,
                }
            )
        ]

    def create_dataset(self, dataset_ref: Any, exists_ok: bool = True) -> Any:
        self.created_datasets.append((dataset_ref.dataset_id, dataset_ref.location))
        return dataset_ref

    def query(self, sql: str, job_config: Any = None) -> FakeQueryJob:
        self.queries.append((sql, job_config))
        upper = sql.upper()
        is_gq_table = "CA_GOLDEN_QUERIES" in upper
        if "INFORMATION_SCHEMA.COLUMNS" in upper:
            return FakeQueryJob(self.info_schema_rows)
        if "SELECT AGENT_ID" in upper and "NOT IN UNNEST" in upper:
            return FakeQueryJob(self.stale_agent_rows)
        if "DELETE FROM" in upper:
            return FakeQueryJob([], num_dml_affected_rows=1)
        if "COUNT(*) AS TOTAL_ROWS" in upper and "PENDING_COUNT" in upper:
            return FakeQueryJob(self.gq_status_count_rows if is_gq_table else self.status_count_rows)
        if "COUNT(*) AS TOTAL_ROWS" in upper and "POPULATED_COUNT" in upper:
            return FakeQueryJob(self.gq_status_count_rows if is_gq_table else self.status_count_rows)
        if "AI.SEARCH" in upper or "VECTOR_SEARCH" in upper or "ML.DISTANCE" in upper:
            return FakeQueryJob(self.gq_search_rows if is_gq_table else self.search_rows)
        if "AI.EMBED" in upper and "'WARMUP'" in upper:
            return FakeQueryJob([FakeRow({"dims": 768})])
        return FakeQueryJob([])


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeAuthorizedSession:
    def get(self, url: str, timeout: int = 30) -> FakeResponse:
        return FakeResponse(
            200,
            {
                "name": "projects/test-gcp-project/locations/us/connections/ca_agent_embedding_conn",
                "cloudResource": {
                    "serviceAccountId": "bqcx-123456789012-test@gcp-sa-bigquery-condel.iam.gserviceaccount.com"
                },
            },
        )

    def post(self, url: str, json: Any = None, timeout: int = 30) -> FakeResponse:
        return FakeResponse(
            200,
            {
                "bindings": [
                    {
                        "role": "roles/aiplatform.user",
                        "members": [
                            "serviceAccount:bqcx-123456789012-test@gcp-sa-bigquery-condel.iam.gserviceaccount.com"
                        ],
                    }
                ]
            },
        )


def test_build_table_ddl_has_exact_autonomous_embedding_syntax() -> None:
    """Verify the generated table DDL includes all required columns and `AI.EMBED` options."""
    ddl = build_table_ddl(
        project_id="test-gcp-project",
        dataset="looker_agent_registry",
        table="ca_agents",
        full_connection_id="test-gcp-project.us.ca_agent_embedding_conn",
        endpoint="text-embedding-005",
    )
    assert "CREATE TABLE IF NOT EXISTS `test-gcp-project.looker_agent_registry.ca_agents`" in ddl
    assert "embedding STRUCT<result ARRAY<FLOAT64>, status STRING>" in ddl
    assert "GENERATED ALWAYS AS (" in ddl
    assert "AI.EMBED(" in ddl
    assert "metadata_concat," in ddl
    assert "connection_id => 'test-gcp-project.us.ca_agent_embedding_conn'," in ddl
    assert "endpoint => 'text-embedding-005'" in ddl
    assert "STORED OPTIONS (asynchronous = TRUE)" in ddl


def test_build_golden_queries_table_ddl_and_merge_sql() -> None:
    """Verify `ca_golden_queries` DDL and atomic `MERGE` SQL."""
    ddl = build_golden_queries_table_ddl(
        project_id="test-gcp-project",
        dataset="looker_agent_registry",
        table="ca_golden_queries",
        full_connection_id="test-gcp-project.us.ca_agent_embedding_conn",
        endpoint="text-embedding-005",
    )
    assert (
        "CREATE TABLE IF NOT EXISTS `test-gcp-project.looker_agent_registry.ca_golden_queries`"
        in ddl
    )
    assert "record_id STRING NOT NULL" in ddl
    assert "golden_query_id STRING NOT NULL" in ddl
    assert "agent_id STRING NOT NULL" in ddl
    assert "questions ARRAY<STRING>" in ddl
    assert "fields ARRAY<STRING>" in ddl
    assert "filters JSON" in ddl
    assert "sorts ARRAY<STRING>" in ddl
    assert "`limit` STRING" in ddl
    assert "embedding STRUCT<result ARRAY<FLOAT64>, status STRING>" in ddl
    assert "STORED OPTIONS (asynchronous = TRUE)" in ddl

    merge_sql = build_golden_queries_merge_sql(
        "test-gcp-project", "looker_agent_registry", "ca_golden_queries"
    )
    assert "MERGE `test-gcp-project.looker_agent_registry.ca_golden_queries` AS T" in merge_sql
    assert "ON T.record_id = S.record_id" in merge_sql
    assert "IFNULL(T.metadata_concat, '') != IFNULL(S.metadata_concat, '')" in merge_sql
    assert "embedding" not in merge_sql


def test_build_merge_sql_omits_embedding_column_from_insert_and_update() -> None:
    """Verify `MERGE` SQL never writes to the `GENERATED ALWAYS` column `embedding`."""
    sql = build_merge_sql("test-gcp-project", "looker_agent_registry", "ca_agents")
    assert "MERGE `test-gcp-project.looker_agent_registry.ca_agents` AS T" in sql
    assert "ON T.agent_id = S.agent_id" in sql
    assert "embedding" not in sql


def test_init_infrastructure_provisions_dataset_connection_iam_and_both_tables(
    sample_sync_config: SyncConfig,
) -> None:
    """Verify `init_infrastructure` creates dataset, connection, IAM grant, warmup, and verifies both tables."""
    fake_bq = FakeBigQueryClient()
    fake_session = FakeAuthorizedSession()
    sync = BigQueryAgentSync(
        config=sample_sync_config,
        client=fake_bq,  # type: ignore[arg-type]
        credentials=object(),
        session=fake_session,
    )

    info = sync.init_infrastructure(run_warmup=True)

    assert info["dataset_id"] == "test-gcp-project.looker_agent_registry"
    assert info["connection"]["connection_id"] == "test-gcp-project.us.ca_agent_embedding_conn"
    assert (
        info["connection"]["service_account_id"]
        == "bqcx-123456789012-test@gcp-sa-bigquery-condel.iam.gserviceaccount.com"
    )
    assert info["table"]["is_generated"] == "ALWAYS"
    assert info["golden_queries_table"]["is_generated"] == "ALWAYS"
    assert (
        info["golden_queries_table"]["table_id"]
        == "test-gcp-project.looker_agent_registry.ca_golden_queries"
    )


def test_upsert_agents_with_golden_queries_cleanup_prune_and_delete(
    sample_sync_config: SyncConfig,
) -> None:
    """Verify `upsert_agents` MERGEs both tables, cleans Golden Queries, and prunes stale rows."""
    fake_bq = FakeBigQueryClient()
    sync = BigQueryAgentSync(
        config=sample_sync_config,
        client=fake_bq,  # type: ignore[arg-type]
        credentials=object(),
        session=FakeAuthorizedSession(),
    )
    rec = AgentMetadataRecord(
        agent_id="agent_active_1",
        instance_url="https://looker.example.com",
        agent_url="https://looker.example.com/conversational-analytics/agents/agent_active_1",
        name="Active Agent 1",
        sources=[ExploreHeader(model="m", explore="e")],
        golden_queries=[
            GoldenQueryRecord(
                id=10,
                agent_id="agent_active_1",
                agent_name="Active Agent 1",
                agent_url="https://looker.example.com/conversational-analytics/agents/agent_active_1",
                instance_url="https://looker.example.com",
                questions=["How many orders?"],
                model="m",
                explore="e",
                fields=["e.count"],
            )
        ],
        metadata_concat="Agent Name: Active Agent 1",
    )

    res = sync.upsert_agents([rec], prune=True, mode="batch", explore_headers_cached=1)
    assert res.synced_count == 1
    assert res.golden_queries_synced_count == 1
    assert res.golden_queries_deleted_count == 1
    assert res.pruned_count == 1
    assert res.golden_queries_pruned_count == 1
    assert res.pruned_agent_ids == ["stale_agent_99"]

    executed_sqls = [q[0] for q in fake_bq.queries]
    assert any("MERGE `test-gcp-project.looker_agent_registry.ca_golden_queries`" in s for s in executed_sqls)
    assert any(
        "DELETE FROM `test-gcp-project.looker_agent_registry.ca_golden_queries`" in s
        and "record_id NOT IN UNNEST(@active_record_ids)" in s
        for s in executed_sqls
    )

    # Also verify syncing an agent with 0 active golden queries deletes all its rows in ca_golden_queries
    fake_bq.queries.clear()
    rec_no_gq = rec.model_copy(update={"golden_queries": []})
    res_no_gq = sync.upsert_agents([rec_no_gq], prune=False, mode="single")
    assert res_no_gq.golden_queries_synced_count == 0
    assert res_no_gq.golden_queries_deleted_count == 1
    assert res_no_gq.golden_queries_pruned_count == 0
    assert any(
        "DELETE FROM `test-gcp-project.looker_agent_registry.ca_golden_queries`\nWHERE agent_id IN UNNEST(@synced_agent_ids)"
        in q[0]
        for q in fake_bq.queries
    )

    fake_bq.queries.clear()
    deleted = sync.delete_agent("deleted_agent_id")
    assert deleted == 1
    del_sqls = [q[0] for q in fake_bq.queries]
    assert any("DELETE FROM `test-gcp-project.looker_agent_registry.ca_agents`" in s for s in del_sqls)
    assert any(
        "DELETE FROM `test-gcp-project.looker_agent_registry.ca_golden_queries`" in s
        for s in del_sqls
    )


def test_get_status_and_search_agents(sample_sync_config: SyncConfig) -> None:
    """Verify `get_status` (for both tables) and `search_agents` parse BigQuery results into typed models."""
    fake_bq = FakeBigQueryClient()
    sync = BigQueryAgentSync(
        config=sample_sync_config,
        client=fake_bq,  # type: ignore[arg-type]
        credentials=object(),
        session=FakeAuthorizedSession(),
    )

    status = sync.get_status()
    assert status.total_rows == 22
    assert status.populated_embeddings == 20
    assert status.pending_embeddings == 2
    assert status.is_generated == "ALWAYS"
    assert status.golden_queries_total_rows == 84
    assert status.golden_queries_populated_embeddings == 84
    assert status.golden_queries_pending_embeddings == 0
    assert status.golden_queries_is_generated == "ALWAYS"

    hits = sync.search_agents("What is our sprint velocity?", top_k=3)
    assert len(hits) == 1
    assert hits[0].agent_id == "agent-eng-001"
    assert hits[0].explores == ["jira.sprint_issues"]
    assert hits[0].similarity_score == 0.88


def test_search_two_step_with_match_stop_on_match_and_unfiltered_fallback(
    sample_sync_config: SyncConfig,
) -> None:
    """Verify two-step search with match filtering, stop_on_golden_match, and unfiltered fallback."""
    fake_bq = FakeBigQueryClient()
    sync = BigQueryAgentSync(
        config=sample_sync_config,
        client=fake_bq,  # type: ignore[arg-type]
        credentials=object(),
        session=FakeAuthorizedSession(),
    )

    # Case 1a: Stage 1 Golden Query similarity_score (0.92) >= confidence_score (0.65)
    # (fallback path when populated_count < total_rows)
    fake_bq.queries.clear()
    res_matched = sync.search_two_step(
        question="What is the status and completion progress of all strategic projects?",
        top_k=3,
        golden_queries_top_k=5,
        confidence_score=0.65,
        stop_on_golden_match=False,
    )
    assert res_matched.golden_query_matched is True
    assert res_matched.stage2_filtered is True
    assert res_matched.matched_agent_ids == ["agent-eng-001"]
    assert len(res_matched.golden_query_results) == 1
    assert res_matched.golden_query_results[0].record_id == "agent-eng-001:101"
    assert len(res_matched.results) == 1
    # Verify Stage 2 query on ca_agents included WHERE agent_id IN UNNEST(@matched_agent_ids)
    agent_search_queries = [
        q[0] for q in fake_bq.queries if "ca_agents" in q[0] and "COUNT(*)" not in q[0]
    ]
    assert len(agent_search_queries) == 1
    assert "WHERE agent_id IN UNNEST(@matched_agent_ids)" in agent_search_queries[0]

    # Case 1b: Stage 2 AI.SEARCH filtered path when all ca_agents embeddings are populated
    fake_bq.status_count_rows = [
        FakeRow(
            {
                "total_rows": 22,
                "populated_count": 22,
                "pending_count": 0,
                "error_count": 0,
                "last_synced_at": "2026-10-12T23:35:00+00:00",
            }
        )
    ]
    fake_bq.queries.clear()
    res_ai_search_filtered = sync.search_two_step(
        question="What is the status and completion progress of all strategic projects?",
        top_k=3,
        golden_queries_top_k=5,
        confidence_score=0.65,
        stop_on_golden_match=False,
    )
    assert res_ai_search_filtered.golden_query_matched is True
    assert res_ai_search_filtered.stage2_filtered is True
    ai_search_agent_queries = [
        q[0] for q in fake_bq.queries if "ca_agents" in q[0] and "COUNT(*)" not in q[0]
    ]
    assert len(ai_search_agent_queries) == 1
    assert "FROM AI.SEARCH(" in ai_search_agent_queries[0]
    assert "WHERE agent_id IN UNNEST(@matched_agent_ids)" in ai_search_agent_queries[0]

    # Case 2: stop_on_golden_match=True -> stops after Stage 1 without querying ca_agents
    fake_bq.queries.clear()
    res_stopped = sync.search_two_step(
        question="What is the status and completion progress of all strategic projects?",
        top_k=3,
        golden_queries_top_k=5,
        confidence_score=0.65,
        stop_on_golden_match=True,
    )
    assert res_stopped.golden_query_matched is True
    assert res_stopped.stop_on_golden_match is True
    assert len(res_stopped.golden_query_results) == 1
    assert res_stopped.results == []
    assert not any("ca_agents" in q[0] for q in fake_bq.queries)

    # Case 3: confidence_score=0.99 -> 0 Golden Queries match -> proceeds to unfiltered Stage 2
    fake_bq.queries.clear()
    res_fallback = sync.search_two_step(
        question="What is our sprint velocity and SLA compliance rate for engineering issues?",
        top_k=3,
        golden_queries_top_k=5,
        confidence_score=0.99,
        stop_on_golden_match=False,
    )
    assert res_fallback.golden_query_matched is False
    assert res_fallback.stage2_filtered is False
    assert res_fallback.matched_agent_ids == []
    assert res_fallback.golden_query_results == []
    assert len(res_fallback.results) == 1
    unfiltered_agent_queries = [
        q[0] for q in fake_bq.queries if "ca_agents" in q[0] and "COUNT(*)" not in q[0]
    ]
    assert len(unfiltered_agent_queries) == 1
    assert "WHERE agent_id IN UNNEST(@matched_agent_ids)" not in unfiltered_agent_queries[0]


def test_user_scoped_search_pre_filtering_short_circuit_and_warnings(
    sample_sync_config: SyncConfig,
) -> None:
    """Verify `allowed_agent_ids` pre-filtering, empty short-circuit, and `inaccessible_source_agent_ids` warnings."""
    fake_bq = FakeBigQueryClient()
    sync = BigQueryAgentSync(
        config=sample_sync_config,
        client=fake_bq,  # type: ignore[arg-type]
        credentials=object(),
        session=FakeAuthorizedSession(),
    )

    # 1. Empty allowed_agent_ids=[] short-circuits with 0 BigQuery queries executed
    fake_bq.queries.clear()
    assert sync.search_golden_queries("Question?", top_k=5, allowed_agent_ids=[]) == []
    assert sync.search_agents("Question?", top_k=5, allowed_agent_ids=[]) == []
    empty_ts = sync.search_two_step(
        "Question?",
        top_k=5,
        golden_queries_top_k=5,
        allowed_agent_ids=[],
        authz_mode="user_id",
        user_id="42",
    )
    assert empty_ts.allowed_agent_count == 0
    assert empty_ts.authz_mode == "user_id"
    assert empty_ts.user_id == "42"
    assert empty_ts.golden_query_results == []
    assert empty_ts.results == []
    assert fake_bq.queries == []

    # 2. Pre-filtering search_golden_queries and search_agents on AI.SEARCH path + inaccessible_source_agent_ids warning
    fake_bq.gq_status_count_rows = [
        FakeRow({"total_rows": 84, "populated_count": 84, "pending_count": 0, "error_count": 0})
    ]
    fake_bq.status_count_rows = [
        FakeRow({"total_rows": 22, "populated_count": 22, "pending_count": 0, "error_count": 0})
    ]
    fake_bq.queries.clear()
    gq_hits_ai = sync.search_golden_queries(
        "Strategic projects?",
        top_k=3,
        allowed_agent_ids=["agent-eng-001", "agent-fin-002"],
        inaccessible_source_agent_ids=["agent-eng-001"],
    )
    assert len(gq_hits_ai) == 1
    assert gq_hits_ai[0].has_inaccessible_source is True
    assert gq_hits_ai[0].warning is not None
    assert "lacks access to one or more LookML sources" in gq_hits_ai[0].warning
    gq_ai_sql = [q[0] for q in fake_bq.queries if "AI.SEARCH" in q[0]][0]
    assert "WHERE agent_id IN UNNEST(@allowed_agent_ids)" in gq_ai_sql

    fake_bq.queries.clear()
    agent_hits_ai = sync.search_agents(
        "Sprint velocity?",
        top_k=3,
        allowed_agent_ids=["agent-eng-001"],
        inaccessible_source_agent_ids=["agent-eng-001"],
    )
    assert len(agent_hits_ai) == 1
    assert agent_hits_ai[0].has_inaccessible_source is True
    assert agent_hits_ai[0].warning is not None
    agent_ai_sql = [q[0] for q in fake_bq.queries if "AI.SEARCH" in q[0]][0]
    assert "WHERE agent_id IN UNNEST(@matched_agent_ids)" in agent_ai_sql

    # 3. Pre-filtering search_golden_queries and search_agents on ML.DISTANCE fallback path
    fake_bq.gq_status_count_rows = [
        FakeRow({"total_rows": 84, "populated_count": 80, "pending_count": 4, "error_count": 0})
    ]
    fake_bq.status_count_rows = [
        FakeRow({"total_rows": 22, "populated_count": 20, "pending_count": 2, "error_count": 0})
    ]
    fake_bq.queries.clear()
    gq_hits_fb = sync.search_golden_queries(
        "Strategic projects?",
        top_k=3,
        allowed_agent_ids=["agent-eng-001"],
        inaccessible_source_agent_ids=[],
    )
    assert len(gq_hits_fb) == 1
    assert gq_hits_fb[0].has_inaccessible_source is False
    assert gq_hits_fb[0].warning is None
    gq_fb_sql = [q[0] for q in fake_bq.queries if "ML.DISTANCE" in q[0]][0]
    assert "WHERE t.agent_id IN UNNEST(@allowed_agent_ids)" in gq_fb_sql

    fake_bq.queries.clear()
    agent_hits_fb = sync.search_agents(
        "Sprint velocity?",
        top_k=3,
        allowed_agent_ids=["agent-eng-001"],
        inaccessible_source_agent_ids=[],
    )
    assert len(agent_hits_fb) == 1
    assert agent_hits_fb[0].has_inaccessible_source is False
    assert agent_hits_fb[0].warning is None
    agent_fb_sql = [q[0] for q in fake_bq.queries if "ML.DISTANCE" in q[0]][0]
    assert "WHERE agent_id IN UNNEST(@matched_agent_ids)" in agent_fb_sql

    # 4. search_two_step intersects Stage 1 matched agents with allowed_agent_ids,
    #    and when Stage 1 has 0 matches, Stage 2 still filters by allowed_agent_ids (never unscoped!)
    fake_bq.queries.clear()
    ts_matched = sync.search_two_step(
        question="What is the status and completion progress of all strategic projects?",
        top_k=3,
        golden_queries_top_k=5,
        confidence_score=0.65,
        stop_on_golden_match=False,
        allowed_agent_ids=["agent-eng-001", "agent-ops-002"],
        inaccessible_source_agent_ids=["agent-eng-001"],
        authz_mode="user_id",
        user_id="42",
    )
    assert ts_matched.authz_mode == "user_id"
    assert ts_matched.user_id == "42"
    assert ts_matched.allowed_agent_count == 2
    assert ts_matched.golden_query_matched is True
    assert ts_matched.matched_agent_ids == ["agent-eng-001"]
    assert ts_matched.golden_query_results[0].has_inaccessible_source is True
    assert ts_matched.results[0].has_inaccessible_source is True

    # Stage 1 has 0 matches (confidence_score=0.99) -> Stage 2 MUST still filter by allowed_agent_ids!
    fake_bq.queries.clear()
    ts_stage1_miss = sync.search_two_step(
        question="Unmatched question?",
        top_k=3,
        golden_queries_top_k=5,
        confidence_score=0.99,
        stop_on_golden_match=False,
        allowed_agent_ids=["agent-eng-001"],
        inaccessible_source_agent_ids=["agent-eng-001"],
        authz_mode="access_token",
    )
    assert ts_stage1_miss.authz_mode == "access_token"
    assert ts_stage1_miss.allowed_agent_count == 1
    assert ts_stage1_miss.golden_query_matched is False
    stage2_queries = [
        (sql, cfg)
        for sql, cfg in fake_bq.queries
        if "ca_agents" in sql and "COUNT(*)" not in sql
    ]
    assert len(stage2_queries) == 1
    assert "WHERE agent_id IN UNNEST(@matched_agent_ids)" in stage2_queries[0][0]


