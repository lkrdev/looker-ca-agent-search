"""Unit tests for deterministic `metadata_concat` construction and `models.py`."""

from __future__ import annotations

from ca_agent_sync.models import (
    AgentMetadataRecord,
    ExploreHeader,
    GoldenQueryRecord,
    build_golden_query_metadata_concat,
    build_metadata_concat,
    collect_golden_queries,
)


def test_build_metadata_concat_includes_all_sections_deterministically() -> None:
    """Verify `build_metadata_concat` combines agent info, explores, golden queries, and instructions."""
    sources = [
        ExploreHeader(
            model="jira_engineering",
            explore="issues",
            name="issues",
            label="Engineering Issues & Sprints",
            description="Tracks sprint velocity, story points, and SLA compliance.",
        ),
        ExploreHeader(
            model="core_hr",
            explore="employees",
            name="employees",
            label="Employee Directory",
            description="Headcount and team hierarchy.",
        ),
    ]
    gqs = [
        GoldenQueryRecord(
            id=10,
            questions=[
                "What is our sprint velocity?",
                "What is our SLA compliance rate for engineering issues?",
            ],
            is_active=True,
            model="jira_engineering",
            explore="issues",
            fields=["issues.sprint_velocity", "issues.sla_compliance_rate"],
            filters={"issues.status": "Resolved", "issues.priority": "P1"},
        )
    ]
    text1 = build_metadata_concat(
        name="Engineering Velocity Agent",
        description="Answers engineering delivery, sprint velocity, and SLA questions.",
        category="Engineering",
        sources=sources,
        golden_queries=gqs,
        system_instructions="Always filter out cancelled sprints unless asked.",
    )
    # Reversing source order must still produce the exact same deterministic output
    text2 = build_metadata_concat(
        name="Engineering Velocity Agent",
        description="Answers engineering delivery, sprint velocity, and SLA questions.",
        category="Engineering",
        sources=list(reversed(sources)),
        golden_queries=gqs,
        system_instructions="Always filter out cancelled sprints unless asked.",
    )
    assert text1 == text2
    assert "Agent Name: Engineering Velocity Agent" in text1
    assert "Description: Answers engineering delivery, sprint velocity, and SLA questions." in text1
    assert "Category: Engineering" in text1
    assert "Explores:" in text1
    assert "core_hr.employees" in text1
    assert "jira_engineering.issues" in text1
    assert "Label: Engineering Issues & Sprints" in text1
    assert "Golden Queries:" in text1
    assert (
        "What is our sprint velocity?; What is our SLA compliance rate for engineering issues?"
        in text1
    )
    assert "Fields: issues.sprint_velocity, issues.sla_compliance_rate" in text1
    assert "Filters: issues.priority=P1, issues.status=Resolved" in text1
    assert "System Instructions:\nAlways filter out cancelled sprints unless asked." in text1
    assert "None" not in text1


def test_build_metadata_concat_handles_missing_and_empty_optional_fields() -> None:
    """Verify optional/None fields never emit 'None' literals or empty section headers."""
    text = build_metadata_concat(
        name="Minimal Agent",
        description=None,
        category="",
        sources=[],
        golden_queries=[
            GoldenQueryRecord(
                id=1,
                questions=["Inactive question should be excluded"],
                is_active=False,
            )
        ],
        system_instructions=None,
    )
    assert text == "Agent Name: Minimal Agent"
    assert "None" not in text
    assert "Explores:" not in text
    assert "Golden Queries:" not in text
    assert "System Instructions:" not in text


def test_agent_metadata_record_json_serialization_is_sorted_and_deterministic() -> None:
    """Verify `sources_json` and `golden_queries_json` serialize keys deterministically."""
    record = AgentMetadataRecord(
        agent_id="agent-123",
        instance_url="https://looker.example.com",
        agent_url="https://looker.example.com/conversational-analytics/agents/agent-123",
        name="Test Agent",
        sources=[
            ExploreHeader(
                model="sales",
                explore="orders",
                name="orders",
                label="Orders",
                description="Order items",
            )
        ],
        golden_queries=[
            GoldenQueryRecord(
                id=5,
                questions=["Total revenue?"],
                model="sales",
                explore="orders",
                fields=["orders.total_revenue"],
                filters={"orders.region": "NA", "orders.year": "2026"},
            )
        ],
        metadata_concat="Agent Name: Test Agent",
    )
    assert '"explore": "orders"' in record.sources_json
    assert '"model": "sales"' in record.sources_json
    assert '"questions": ["Total revenue?"]' in record.golden_queries_json


def test_build_golden_query_metadata_concat_determinism_and_fields() -> None:
    """Verify `build_golden_query_metadata_concat` combines questions, agent name, explore, fields, filters, and sorts."""
    concat1 = build_golden_query_metadata_concat(
        questions=[
            "What is the status and completion progress of all strategic projects?",
            "Show project health status",
        ],
        agent_name="Linear Engineering Intelligence Assistant",
        model="engineering_analytics",
        explore="project",
        fields=["project.name", "project.progress", "team.name"],
        filters={"project.state": "started", "team.name": "Core"},
        sorts=["project.progress desc"],
    )
    concat2 = build_golden_query_metadata_concat(
        questions=[
            "What is the status and completion progress of all strategic projects?",
            "Show project health status",
        ],
        agent_name="Linear Engineering Intelligence Assistant",
        model="engineering_analytics",
        explore="project",
        fields=["project.name", "project.progress", "team.name"],
        filters={"team.name": "Core", "project.state": "started"},
        sorts=["project.progress desc"],
    )
    assert concat1 == concat2
    assert (
        "Golden Query Questions: What is the status and completion progress of all strategic projects?; "
        "Show project health status"
    ) in concat1
    assert "Agent Name: Linear Engineering Intelligence Assistant" in concat1
    assert "LookML Explore: engineering_analytics.project" in concat1
    assert "Fields: project.name, project.progress, team.name" in concat1
    assert "Filters: project.state=started, team.name=Core" in concat1
    assert "Sorts: project.progress desc" in concat1
    assert "None" not in concat1


def test_build_golden_query_metadata_concat_empty_fallback() -> None:
    """Verify `build_golden_query_metadata_concat` guarantees a non-empty string when all inputs are empty."""
    fallback = build_golden_query_metadata_concat(
        questions=[],
        agent_name=None,
        model=None,
        explore=None,
        fields=[],
        filters=None,
        sorts=[],
    )
    assert fallback == "Golden Query: Unspecified"
    assert "None" not in fallback


def test_golden_query_record_derives_record_id_and_collect_golden_queries() -> None:
    """Verify `GoldenQueryRecord` derives `record_id` and `collect_golden_queries` deduplicates."""
    gq = GoldenQueryRecord(
        id=42,
        agent_id="agent_abc",
        agent_name="Sales Agent",
        agent_url="https://looker.example.com/conversational-analytics/agents/agent_abc",
        instance_url="https://looker.example.com",
        questions=["What is Q4 pipeline?"],
        model="crm",
        explore="opportunities",
        fields=["opportunities.total_amount"],
        filters={"opportunities.quarter": "Q4"},
        sorts=["opportunities.total_amount desc"],
        limit="50",
    )
    assert gq.golden_query_id == "42"
    assert gq.record_id == "agent_abc:42"
    assert "Golden Query Questions: What is Q4 pipeline?" in gq.metadata_concat
    assert "Agent Name: Sales Agent" in gq.metadata_concat
    assert "LookML Explore: crm.opportunities" in gq.metadata_concat
    assert gq.questions_json == '["What is Q4 pipeline?"]'
    assert gq.fields_json == '["opportunities.total_amount"]'
    assert gq.sorts_json == '["opportunities.total_amount desc"]'
    assert gq.filters_json == '{"opportunities.quarter": "Q4"}'

    rec = AgentMetadataRecord(
        agent_id="agent_abc",
        instance_url="https://looker.example.com",
        agent_url="https://looker.example.com/conversational-analytics/agents/agent_abc",
        name="Sales Agent",
        golden_queries=[
            gq,
            gq,  # duplicate record_id
            GoldenQueryRecord(id=99, questions=["Inactive"], is_active=False),
        ],
        metadata_concat="Agent Name: Sales Agent",
    )
    collected = collect_golden_queries([rec])
    assert len(collected) == 1
    assert collected[0].record_id == "agent_abc:42"

