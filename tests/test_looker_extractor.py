"""Unit tests for `LookerAgentExtractor` (R1 & R2 single/batch extraction, caching, 404/deleted)."""

from __future__ import annotations

from conftest import (
    FakeAgent,
    FakeContext,
    FakeExplore,
    FakeGoldenQuery,
    FakeLookerSDK,
    FakeSource,
)

from ca_agent_sync.config import SyncConfig
from ca_agent_sync.looker_extractor import LookerAgentExtractor


def test_extract_batch_caches_lightweight_explore_headers(
    sample_sync_config: SyncConfig,
) -> None:
    """Verify `lookml_model_explore` is called with `fields='name,label,description'` and cached across agents."""
    agents = [
        FakeAgent(
            id="agent_1",
            name="Agent One",
            description="First agent",
            category="Operations",
            sources=[
                FakeSource(model="ops_model", explore="tickets"),
                FakeSource(model="ops_model", explore="tickets"),  # duplicate within same agent
            ],
            golden_queries=[
                FakeGoldenQuery(
                    id=1,
                    questions=["How many open tickets?"],
                    model="ops_model",
                    explore="tickets",
                    fields=["tickets.count"],
                    filters={"tickets.status": "open"},
                )
            ],
            context=FakeContext(instructions="Prioritize P0 tickets."),
        ),
        FakeAgent(
            id="agent_2",
            name="Agent Two",
            description="Second agent sharing the same explore",
            category="Operations",
            sources=[FakeSource(model="ops_model", explore="tickets")],
        ),
        FakeAgent(
            id="agent_deleted",
            name="Deleted Agent",
            deleted=True,
        ),
    ]
    explores = {
        ("ops_model", "tickets"): FakeExplore(
            name="tickets",
            label="Support Tickets",
            description="Customer support tickets and SLA metrics",
        )
    }
    fake_sdk = FakeLookerSDK(agents=agents, explores=explores)
    extractor = LookerAgentExtractor(config=sample_sync_config, sdk=fake_sdk)

    records = extractor.extract_batch(include_explore_headers=True)

    assert len(records) == 2
    assert [r.agent_id for r in records] == ["agent_1", "agent_2"]
    assert fake_sdk.search_calls == [{"deleted": False}]
    # Even though 2 agents (and 3 source entries) referenced ops_model.tickets,
    # lookml_model_explore must be called exactly ONCE with fields="name,label,description".
    assert fake_sdk.explore_calls == [("ops_model", "tickets", "name,label,description")]
    assert len(extractor.explore_cache) == 1
    assert records[0].sources[0].label == "Support Tickets"
    assert (
        records[0].agent_url == "https://looker.example.com/conversational-analytics/agents/agent_1"
    )
    assert "Label: Support Tickets" in records[0].metadata_concat
    assert "Prioritize P0 tickets." in records[0].metadata_concat


def test_extract_batch_no_explore_headers_skips_lookml_calls(
    sample_sync_config: SyncConfig,
) -> None:
    """Verify `--no-explore-headers` (`include_explore_headers=False`) makes 0 `lookml_model_explore` calls."""
    agents = [
        FakeAgent(
            id="agent_1",
            name="Agent One",
            sources=[FakeSource(model="ops_model", explore="tickets")],
        )
    ]
    fake_sdk = FakeLookerSDK(agents=agents, explores={})
    extractor = LookerAgentExtractor(config=sample_sync_config, sdk=fake_sdk)

    records = extractor.extract_batch(include_explore_headers=False)

    assert len(records) == 1
    assert fake_sdk.explore_calls == []
    assert records[0].sources[0].model == "ops_model"
    assert records[0].sources[0].explore == "tickets"
    assert records[0].sources[0].label is None


def test_extract_gracefully_handles_inaccessible_404_explore(
    sample_sync_config: SyncConfig,
) -> None:
    """Verify a 404/inaccessible LookML explore falls back gracefully without failing the sync."""
    agents = [
        FakeAgent(
            id="agent_broken_source",
            name="Agent With Missing Model",
            sources=[FakeSource(model="missing_model", explore="missing_explore")],
        )
    ]
    fake_sdk = FakeLookerSDK(agents=agents, explores={})
    extractor = LookerAgentExtractor(config=sample_sync_config, sdk=fake_sdk)

    records = extractor.extract_batch(include_explore_headers=True)

    assert len(records) == 1
    assert records[0].sources[0].model == "missing_model"
    assert records[0].sources[0].explore == "missing_explore"
    assert records[0].sources[0].label is None
    assert "missing_model.missing_explore" in records[0].metadata_concat


def test_extract_single_returns_should_delete_on_404_or_deleted_flag(
    sample_sync_config: SyncConfig,
) -> None:
    """Verify `extract_single` signals `should_delete=True` when agent is `deleted=True` or HTTP 404."""
    deleted_agent = FakeAgent(id="soft_deleted_id", name="Old Agent", deleted=True)
    active_agent = FakeAgent(id="active_id", name="Active Agent", deleted=False)
    fake_sdk = FakeLookerSDK(agents=[deleted_agent, active_agent])
    extractor = LookerAgentExtractor(config=sample_sync_config, sdk=fake_sdk)

    # 1. Active agent -> returns record, should_delete=False
    rec, should_del = extractor.extract_single("active_id")
    assert should_del is False
    assert rec is not None
    assert rec.agent_id == "active_id"

    # 2. Soft-deleted agent -> returns None, should_delete=True
    rec_del, should_del_soft = extractor.extract_single("soft_deleted_id")
    assert rec_del is None
    assert should_del_soft is True

    # 3. 404 non-existent agent -> returns None, should_delete=True
    rec_404, should_del_404 = extractor.extract_single("non_existent_404_id")
    assert rec_404 is None
    assert should_del_404 is True


def test_extract_populates_golden_query_record_fields_and_deduplicates(
    sample_sync_config: SyncConfig,
) -> None:
    """Verify `_normalize_agent` populates `record_id = '{agent_id}:{golden_query_id}'`, `metadata_concat`, and fallback IDs."""
    agent = FakeAgent(
        id="agent-eng-001",
        name="Linear Engineering Intelligence Assistant",
        description="AI Conversational Analytics Assistant",
        category="Engineering",
        sources=[FakeSource(model="engineering_analytics", explore="project")],
        golden_queries=[
            FakeGoldenQuery(
                id=101,
                questions=["What is the status and completion progress of all strategic projects?"],
                answer="Strategic projects progress summary.",
                is_active=True,
                model="engineering_analytics",
                explore="project",
                fields=["project.name", "project.progress"],
                filters={"project.state": "started"},
                sorts=["project.progress desc"],
                limit="100",
            ),
            # Duplicate id=101 should be deduplicated
            FakeGoldenQuery(
                id=101,
                questions=["Duplicate ID"],
                model="engineering_analytics",
                explore="project",
            ),
            # Missing id=None should deterministically fallback to query_id or content hash
            FakeGoldenQuery(
                id=None,
                query_id=None,
                questions=["Show engineer workload and completed story points per developer"],
                model="engineering_analytics",
                explore="issue",
                fields=["assignee.name", "issue.completed_story_points"],
                sorts=["issue.completed_story_points desc"],
            ),
        ],
    )
    fake_sdk = FakeLookerSDK(agents=[agent], explores={})
    extractor = LookerAgentExtractor(config=sample_sync_config, sdk=fake_sdk)

    rec, should_del = extractor.extract_single("agent-eng-001")
    assert should_del is False
    assert rec is not None
    assert len(rec.golden_queries) == 2

    gq0 = rec.golden_queries[0]
    assert gq0.golden_query_id == "101"
    assert gq0.record_id == "agent-eng-001:101"
    assert gq0.agent_id == "agent-eng-001"
    assert gq0.agent_name == "Linear Engineering Intelligence Assistant"
    assert (
        gq0.agent_url
        == "https://looker.example.com/conversational-analytics/agents/agent-eng-001"
    )
    assert gq0.instance_url == "https://looker.example.com"
    assert gq0.limit == "100"
    assert gq0.answer == "Strategic projects progress summary."
    assert (
        "Golden Query Questions: What is the status and completion progress of all strategic projects?"
        in gq0.metadata_concat
    )
    assert "Agent Name: Linear Engineering Intelligence Assistant" in gq0.metadata_concat
    assert "LookML Explore: engineering_analytics.project" in gq0.metadata_concat
    assert "Fields: project.name, project.progress" in gq0.metadata_concat
    assert "Filters: project.state=started" in gq0.metadata_concat
    assert "Sorts: project.progress desc" in gq0.metadata_concat

    gq1 = rec.golden_queries[1]
    assert gq1.golden_query_id.startswith("h_")
    assert gq1.record_id == f"agent-eng-001:{gq1.golden_query_id}"
    assert "LookML Explore: engineering_analytics.issue" in gq1.metadata_concat

