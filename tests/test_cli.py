"""Unit tests for the `ca-agent-sync` Typer CLI commands (`init`, `sync`, `status`, `search`)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from ca_agent_sync import cli
from ca_agent_sync.models import (
    AgentMetadataRecord,
    ExploreHeader,
    GoldenQuerySearchHit,
    SearchHit,
    StatusReport,
    SyncResult,
    TwoStepSearchResult,
    UserAgentAccess,
)

runner = CliRunner()


def test_cli_init_json_and_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify `ca-agent-sync init` succeeds in both human, `--json`, and `--skip-warmup` modes."""
    warmup_calls: list[bool] = []

    def _fake_init_infra(self: object, run_warmup: bool = True) -> dict[str, object]:
        warmup_calls.append(run_warmup)
        return {
            "project_id": "test-gcp-project",
            "location": "US",
            "dataset_id": "test-gcp-project.looker_agent_registry",
            "connection": {
                "connection_id": "test-gcp-project.us.ca_agent_embedding_conn",
                "service_account_id": "bqcx-test@gcp-sa-bigquery-condel.iam.gserviceaccount.com",
                "iam_role": "roles/aiplatform.user",
                "iam_bound": True,
            },
            "warmup_verified": True,
            "table": {
                "table_id": "test-gcp-project.looker_agent_registry.ca_agents",
                "column_name": "embedding",
                "is_generated": "ALWAYS",
                "is_stored": "YES",
                "generation_expression": "AI.EMBED(...)",
            },
            "golden_queries_table": {
                "table_id": "test-gcp-project.looker_agent_registry.ca_golden_queries",
                "column_name": "embedding",
                "is_generated": "ALWAYS",
                "is_stored": "YES",
                "generation_expression": "AI.EMBED(...)",
            },
        }

    monkeypatch.setattr(
        cli.BigQueryAgentSync,
        "init_infrastructure",
        _fake_init_infra,
    )

    res_json = runner.invoke(cli.app, ["init", "--json"])
    assert res_json.exit_code == 0
    payload = json.loads(res_json.stdout)
    assert payload["status"] == "ok"
    assert payload["table"]["is_generated"] == "ALWAYS"
    assert payload["golden_queries_table"]["is_generated"] == "ALWAYS"
    assert warmup_calls[-1] is True

    res_skip = runner.invoke(cli.app, ["init", "--skip-warmup", "--json"])
    assert res_skip.exit_code == 0
    assert warmup_calls[-1] is False

    res_human = runner.invoke(cli.app, ["init"])
    assert res_human.exit_code == 0
    assert "BigQuery Autonomous Embedding Infrastructure Ready" in res_human.stdout
    assert "ca_golden_queries" in res_human.stdout
    assert warmup_calls[-1] is True


def test_cli_sync_batch_and_single_and_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify `sync --batch`, `sync --agent-id`, and deleted agent handling via CLI."""
    monkeypatch.setattr(
        cli.BigQueryAgentSync,
        "init_infrastructure",
        lambda self, run_warmup=False: {},
    )

    sample_rec = AgentMetadataRecord(
        agent_id="agent-eng-001",
        instance_url="https://looker.example.com",
        agent_url="https://looker.example.com/conversational-analytics/agents/agent-eng-001",
        name="Sprint Velocity Agent",
        sources=[ExploreHeader(model="eng", explore="sprints")],
        metadata_concat="Agent Name: Sprint Velocity Agent",
    )

    monkeypatch.setattr(
        cli.LookerAgentExtractor,
        "extract_batch",
        lambda self, include_explore_headers=True: [sample_rec],
    )
    monkeypatch.setattr(
        cli.BigQueryAgentSync,
        "upsert_agents",
        lambda self, records, prune=False, mode="batch", explore_headers_cached=0: SyncResult(
            mode=mode,
            table_id="test-gcp-project.looker_agent_registry.ca_agents",
            synced_count=len(records),
            pruned_count=2 if prune else 0,
            explore_headers_cached=explore_headers_cached,
            agent_ids=[r.agent_id for r in records],
            golden_queries_table_id="test-gcp-project.looker_agent_registry.ca_golden_queries",
            golden_queries_synced_count=12,
        ),
    )

    # 1. Batch sync with --prune and --json
    res_batch = runner.invoke(cli.app, ["sync", "--batch", "--prune", "--json"])
    assert res_batch.exit_code == 0
    batch_data = json.loads(res_batch.stdout)
    assert batch_data["mode"] == "batch"
    assert batch_data["synced_count"] == 1
    assert batch_data["golden_queries_synced_count"] == 12
    assert batch_data["pruned_count"] == 2

    # 2. Single agent sync (active)
    monkeypatch.setattr(
        cli.LookerAgentExtractor,
        "extract_single",
        lambda self, agent_id, include_explore_headers=True: (sample_rec, False),
    )
    res_single = runner.invoke(
        cli.app,
        ["sync", "--agent-id", "agent-eng-001", "--json"],
    )
    assert res_single.exit_code == 0
    single_data = json.loads(res_single.stdout)
    assert single_data["mode"] == "single"
    assert single_data["synced_count"] == 1
    assert single_data["golden_queries_synced_count"] == 12

    # 3. Single agent sync when agent is deleted / 404 in Looker
    monkeypatch.setattr(
        cli.LookerAgentExtractor,
        "extract_single",
        lambda self, agent_id, include_explore_headers=True: (None, True),
    )
    monkeypatch.setattr(
        cli.BigQueryAgentSync,
        "delete_agent",
        lambda self, agent_id: 1,
    )
    res_del = runner.invoke(
        cli.app,
        ["sync", "--agent-id", "deleted-agent-id", "--json"],
    )
    assert res_del.exit_code == 0
    del_data = json.loads(res_del.stdout)
    assert del_data["deleted_count"] == 1
    assert del_data["deleted_agent_ids"] == ["deleted-agent-id"]


def test_cli_sync_validates_mutually_exclusive_flags() -> None:
    """Verify `sync` rejects missing or simultaneously specified `--batch` and `--agent-id`."""
    res_neither = runner.invoke(cli.app, ["sync"])
    assert res_neither.exit_code != 0

    res_both = runner.invoke(cli.app, ["sync", "--batch", "--agent-id", "123"])
    assert res_both.exit_code != 0


def test_cli_status_and_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify `ca-agent-sync status` and single-step/two-step `ca-agent-sync search` commands."""
    monkeypatch.setattr(
        cli.BigQueryAgentSync,
        "get_status",
        lambda self: StatusReport(
            table_id="test-gcp-project.looker_agent_registry.ca_agents",
            total_rows=22,
            populated_embeddings=22,
            pending_embeddings=0,
            error_embeddings=0,
            is_generated="ALWAYS",
            golden_queries_table_id="test-gcp-project.looker_agent_registry.ca_golden_queries",
            golden_queries_total_rows=84,
            golden_queries_populated_embeddings=84,
            golden_queries_pending_embeddings=0,
            golden_queries_error_embeddings=0,
            golden_queries_is_generated="ALWAYS",
        ),
    )
    res_status = runner.invoke(cli.app, ["status", "--json"])
    assert res_status.exit_code == 0
    status_data = json.loads(res_status.stdout)
    assert status_data["total_rows"] == 22
    assert status_data["populated_embeddings"] == 22
    assert status_data["golden_queries_total_rows"] == 84

    res_status_human = runner.invoke(cli.app, ["status"])
    assert res_status_human.exit_code == 0
    assert "Total Golden Query Rows" in res_status_human.stdout

    sample_hit = SearchHit(
        rank=1,
        agent_id="agent-eng-001",
        name="Engineering Velocity Agent",
        description="Sprint velocity and SLA compliance",
        category="Engineering",
        similarity_score=0.9123,
        distance=0.0877,
        agent_url="https://looker.example.com/conversational-analytics/agents/agent-eng-001",
        explores=["jira.issues"],
    )
    monkeypatch.setattr(
        cli.BigQueryAgentSync,
        "search_agents",
        lambda self, question, top_k=5, **kwargs: [sample_hit],
    )
    res_search = runner.invoke(
        cli.app,
        ["search", "What is our sprint velocity?", "--top-k", "3", "--json"],
    )
    assert res_search.exit_code == 0
    search_data = json.loads(res_search.stdout)
    assert search_data["status"] == "ok"
    assert search_data["authz_mode"] == "admin"
    assert search_data["user_id"] is None
    assert search_data["allowed_agent_count"] is None
    assert len(search_data["results"]) == 1
    assert search_data["results"][0]["agent_id"] == "agent-eng-001"

    # Two-step search via CLI (--golden-queries and --confidence_score)
    sample_gq_hit = GoldenQuerySearchHit(
        rank=1,
        record_id="agent-eng-001:101",
        golden_query_id="101",
        agent_id="agent-eng-001",
        agent_name="Linear Engineering Intelligence Assistant",
        agent_url="https://looker.example.com/conversational-analytics/agents/agent-eng-001",
        questions=["What is the status and completion progress of all strategic projects?"],
        model="engineering_analytics",
        explore="project",
        fields=["project.name", "project.progress"],
        similarity_score=0.94,
        distance=0.06,
    )
    def _fake_two_step(
        self: object,
        question: str,
        top_k: int = 5,
        golden_queries_top_k: int = 5,
        confidence_score: float = 0.60,
        stop_on_golden_match: bool = False,
        allowed_agent_ids: object = None,
        inaccessible_source_agent_ids: object = None,
        authz_mode: str = "admin",
        user_id: object = None,
    ) -> TwoStepSearchResult:
        matched = confidence_score <= 0.94
        return TwoStepSearchResult(
            question=question,
            top_k=top_k,
            golden_queries_requested=golden_queries_top_k,
            confidence_score=confidence_score,
            stop_on_golden_match=stop_on_golden_match,
            golden_query_matched=matched,
            stage2_filtered=matched,
            matched_agent_ids=["agent-eng-001"] if matched else [],
            golden_query_results=[sample_gq_hit] if matched else [],
            results=[] if (stop_on_golden_match and matched) else [sample_hit],
            authz_mode=authz_mode,
            user_id=user_id if isinstance(user_id, str) else None,
            allowed_agent_count=len(allowed_agent_ids) if isinstance(allowed_agent_ids, list) else None,
        )

    monkeypatch.setattr(cli.BigQueryAgentSync, "search_two_step", _fake_two_step)

    res_two_step = runner.invoke(
        cli.app,
        [
            "search",
            "What is the status and completion progress of all strategic projects?",
            "--golden-queries=5",
            "--confidence_score=0.65",
            "--top-k",
            "3",
            "--json",
        ],
    )
    assert res_two_step.exit_code == 0
    ts_data = json.loads(res_two_step.stdout)
    assert ts_data["golden_query_matched"] is True
    assert ts_data["stage2_filtered"] is True
    assert ts_data["matched_agent_ids"] == ["agent-eng-001"]
    assert len(ts_data["golden_query_results"]) == 1
    assert len(ts_data["results"]) == 1

    # Verify --stop-on-golden-match
    res_stop = runner.invoke(
        cli.app,
        [
            "search",
            "What is the status and completion progress of all strategic projects?",
            "-g",
            "5",
            "--confidence-score",
            "0.65",
            "--stop-on-golden-match",
            "--json",
        ],
    )
    assert res_stop.exit_code == 0
    stop_data = json.loads(res_stop.stdout)
    assert stop_data["stop_on_golden_match"] is True
    assert stop_data["results"] == []

    # Verify boundary confidence_score values 0.0 and 1.0 succeed
    res_conf_zero = runner.invoke(
        cli.app,
        ["search", "Question?", "--golden-queries=5", "--confidence_score=0.0", "--json"],
    )
    assert res_conf_zero.exit_code == 0
    assert json.loads(res_conf_zero.stdout)["confidence_score"] == 0.0

    res_conf_one = runner.invoke(
        cli.app,
        ["search", "Question?", "--golden-queries=5", "--confidence_score=1.0", "--json"],
    )
    assert res_conf_one.exit_code == 0
    assert json.loads(res_conf_one.stdout)["confidence_score"] == 1.0

    # Verify invalid confidence_score outside [0.0, 1.0] exits with code 2
    res_bad_conf = runner.invoke(
        cli.app,
        ["search", "Question?", "--golden-queries=5", "--confidence_score=1.5"],
    )
    assert res_bad_conf.exit_code == 2

    res_neg_conf = runner.invoke(
        cli.app,
        ["search", "Question?", "--golden-queries=5", "--confidence-score=-0.1"],
    )
    assert res_neg_conf.exit_code == 2


def test_cli_search_with_user_id_and_access_token_and_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify `search` with `--user-id` and `--access-token`, mutual exclusion (`code=2`), and inaccessible source warnings."""
    # 1. Mutual exclusion error (code=2) when both --user-id and --access-token are passed
    res_both = runner.invoke(
        cli.app,
        [
            "search",
            "What is our sprint velocity?",
            "--user-id",
            "42",
            "--access-token",
            "token_abc",
        ],
    )
    assert res_both.exit_code == 2

    res_both_json = runner.invoke(
        cli.app,
        [
            "search",
            "What is our sprint velocity?",
            "-u",
            "42",
            "--access-token",
            "token_abc",
            "--json",
        ],
    )
    assert res_both_json.exit_code == 2
    assert json.loads(res_both_json.stdout)["status"] == "error"

    # 2. Mock fetch_user_accessible_agents and verify single-step & two-step with --user-id and --access-token
    access_calls: list[dict[str, object]] = []

    def _fake_fetch_user_access(
        self: object,
        *,
        user_id: object = None,
        access_token: object = None,
    ) -> UserAgentAccess:
        access_calls.append({"user_id": user_id, "access_token": access_token})
        if user_id == "empty_user":
            return UserAgentAccess(
                mode="user_id",
                user_id="empty_user",
                allowed_agent_ids=[],
                inaccessible_source_agent_ids=[],
            )
        return UserAgentAccess(
            mode="access_token" if access_token else "user_id",
            user_id=str(user_id) if user_id else None,
            allowed_agent_ids=["agent-eng-001", "agent-fin-002"],
            inaccessible_source_agent_ids=["agent-eng-001"],
        )

    monkeypatch.setattr(
        cli.LookerAgentExtractor,
        "fetch_user_accessible_agents",
        _fake_fetch_user_access,
    )

    warn_msg = "User lacks access to one or more LookML sources on this agent; some queries may fail."
    warned_hit = SearchHit(
        rank=1,
        agent_id="agent-eng-001",
        name="Engineering Velocity Agent",
        description="Sprint velocity and SLA compliance",
        category="Engineering",
        similarity_score=0.9123,
        distance=0.0877,
        agent_url="https://looker.example.com/conversational-analytics/agents/agent-eng-001",
        explores=["jira.issues"],
        has_inaccessible_source=True,
        warning=warn_msg,
    )
    warned_gq_hit = GoldenQuerySearchHit(
        rank=1,
        record_id="agent-eng-001:101",
        golden_query_id="101",
        agent_id="agent-eng-001",
        agent_name="Engineering Velocity Agent",
        agent_url="https://looker.example.com/conversational-analytics/agents/agent-eng-001",
        questions=["What is our sprint velocity?"],
        model="jira",
        explore="issues",
        fields=["issues.count"],
        similarity_score=0.95,
        distance=0.05,
        has_inaccessible_source=True,
        warning=warn_msg,
    )

    captured_single_step: list[dict[str, object]] = []

    def _fake_search_agents(
        self: object,
        question: str,
        top_k: int = 5,
        candidate_agent_ids: object = None,
        allowed_agent_ids: object = None,
        inaccessible_source_agent_ids: object = None,
    ) -> list[SearchHit]:
        captured_single_step.append(
            {
                "allowed_agent_ids": allowed_agent_ids,
                "inaccessible_source_agent_ids": inaccessible_source_agent_ids,
            }
        )
        if allowed_agent_ids == []:
            return []
        return [warned_hit]

    def _fake_search_two_step(
        self: object,
        question: str,
        top_k: int = 5,
        golden_queries_top_k: int = 5,
        confidence_score: float = 0.60,
        stop_on_golden_match: bool = False,
        allowed_agent_ids: object = None,
        inaccessible_source_agent_ids: object = None,
        authz_mode: str = "admin",
        user_id: object = None,
    ) -> TwoStepSearchResult:
        if allowed_agent_ids == []:
            return TwoStepSearchResult(
                question=question,
                top_k=top_k,
                golden_queries_requested=golden_queries_top_k,
                confidence_score=confidence_score,
                stop_on_golden_match=stop_on_golden_match,
                authz_mode=authz_mode,
                user_id=str(user_id) if user_id else None,
                allowed_agent_count=0,
            )
        return TwoStepSearchResult(
            question=question,
            top_k=top_k,
            golden_queries_requested=golden_queries_top_k,
            confidence_score=confidence_score,
            stop_on_golden_match=stop_on_golden_match,
            golden_query_matched=True,
            stage2_filtered=True,
            matched_agent_ids=["agent-eng-001"],
            golden_query_results=[warned_gq_hit],
            results=[warned_hit],
            authz_mode=authz_mode,
            user_id=str(user_id) if user_id else None,
            allowed_agent_count=len(allowed_agent_ids) if isinstance(allowed_agent_ids, list) else None,
        )

    monkeypatch.setattr(cli.BigQueryAgentSync, "search_agents", _fake_search_agents)
    monkeypatch.setattr(cli.BigQueryAgentSync, "search_two_step", _fake_search_two_step)

    # Single-step --user-id with --json
    res_uid_json = runner.invoke(
        cli.app,
        ["search", "What is our sprint velocity?", "--user-id", "42", "--json"],
    )
    assert res_uid_json.exit_code == 0
    uid_payload = json.loads(res_uid_json.stdout)
    assert uid_payload["authz_mode"] == "user_id"
    assert uid_payload["user_id"] == "42"
    assert uid_payload["allowed_agent_count"] == 2
    assert uid_payload["results"][0]["has_inaccessible_source"] is True
    assert uid_payload["results"][0]["warning"] == warn_msg
    assert captured_single_step[-1] == {
        "allowed_agent_ids": ["agent-eng-001", "agent-fin-002"],
        "inaccessible_source_agent_ids": ["agent-eng-001"],
    }

    # Single-step --access-token human output with warning
    res_tok_human = runner.invoke(
        cli.app,
        ["search", "What is our sprint velocity?", "--access-token", "oauth_tok_123"],
    )
    assert res_tok_human.exit_code == 0
    assert "Warning" in res_tok_human.stdout
    assert "LookML sources" in res_tok_human.stdout

    # Two-step --user-id with --json and human output
    res_ts_json = runner.invoke(
        cli.app,
        ["search", "What is our sprint velocity?", "-g", "5", "-u", "42", "--json"],
    )
    assert res_ts_json.exit_code == 0
    ts_payload = json.loads(res_ts_json.stdout)
    assert ts_payload["authz_mode"] == "user_id"
    assert ts_payload["user_id"] == "42"
    assert ts_payload["allowed_agent_count"] == 2
    assert ts_payload["golden_query_results"][0]["has_inaccessible_source"] is True
    assert ts_payload["results"][0]["has_inaccessible_source"] is True

    res_ts_human = runner.invoke(
        cli.app,
        ["search", "What is our sprint velocity?", "-g", "5", "--access-token", "oauth_tok_123"],
    )
    assert res_ts_human.exit_code == 0
    assert "Warning" in res_ts_human.stdout
    assert "LookML sources" in res_ts_human.stdout

    # User with 0 accessible agents in Looker prints clear message
    res_empty_human = runner.invoke(
        cli.app,
        ["search", "What is our sprint velocity?", "--user-id", "empty_user"],
    )
    assert res_empty_human.exit_code == 0
    assert "0 CA agents in Looker" in res_empty_human.stdout


