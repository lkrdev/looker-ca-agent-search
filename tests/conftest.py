"""Shared pytest fixtures and hermetic fakes for `ca-agent-sync` unit tests."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

from ca_agent_sync.config import SyncConfig


@dataclass
class FakeSource:
    model: Optional[str] = None
    explore: Optional[str] = None


@dataclass
class FakeContext:
    instructions: Optional[str] = None


@dataclass
class FakeGoldenQuery:
    id: Optional[int] = None
    query_id: Optional[int] = None
    questions: Optional[list[str]] = None
    answer: Optional[str] = None
    is_active: Optional[bool] = True
    model: Optional[str] = None
    explore: Optional[str] = None
    fields: Optional[list[str]] = None
    filters: Optional[dict[str, Any]] = None
    sorts: Optional[list[str]] = None
    limit: Optional[str] = None


@dataclass
class FakeAgent:
    id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    sources: Optional[list[FakeSource]] = field(default_factory=list)
    golden_queries: Optional[list[FakeGoldenQuery]] = field(default_factory=list)
    context: Optional[FakeContext] = None
    deleted: Optional[bool] = False
    has_inaccessible_source: Optional[bool] = False
    code_interpreter: Optional[bool] = False
    created_by_name: Optional[str] = "Alice Engineer"
    created_at: Optional[Any] = field(
        default_factory=lambda: datetime.datetime(
            2026, 1, 15, 10, 0, 0, tzinfo=datetime.timezone.utc
        )
    )
    updated_at: Optional[Any] = field(
        default_factory=lambda: datetime.datetime(
            2026, 2, 20, 12, 30, 0, tzinfo=datetime.timezone.utc
        )
    )


@dataclass
class FakeExplore:
    name: Optional[str] = None
    label: Optional[str] = None
    description: Optional[str] = None


class FakeSDKError(Exception):
    """Simulates `looker_sdk.error.SDKError` with an HTTP status code."""

    def __init__(self, message: str = "Not Found", status: int = 404) -> None:
        super().__init__(message)
        self.status = status


class FakeAuth:
    """Simulates `sdk.auth` for sudo (`login_user` / `logout`) tracking."""

    def __init__(self) -> None:
        self.login_user_calls: list[Any] = []
        self.logout_calls: int = 0
        self.active_sudo_user: Optional[Any] = None

    def login_user(self, user_id: Any) -> None:
        self.login_user_calls.append(user_id)
        self.active_sudo_user = user_id

    def logout(self) -> None:
        self.logout_calls += 1
        self.active_sudo_user = None


class FakeLookerSDK:
    """In-memory fake of Looker 4.0 SDK tracking read-only API calls and sudo auth."""

    def __init__(
        self,
        agents: Optional[list[FakeAgent]] = None,
        explores: Optional[dict[tuple[str, str], FakeExplore]] = None,
        search_error: Optional[Exception] = None,
    ) -> None:
        self._agents_list: list[FakeAgent] = list(agents or [])
        self.agents_by_id: dict[str, FakeAgent] = {a.id: a for a in self._agents_list if a.id}
        self.explores_by_pair: dict[tuple[str, str], FakeExplore] = dict(explores or {})
        self.search_calls: list[dict[str, Any]] = []
        self.get_agent_calls: list[str] = []
        self.explore_calls: list[tuple[str, str, Optional[str]]] = []
        self.auth = FakeAuth()
        self.search_error = search_error

    def me(self, fields: Optional[str] = None) -> dict[str, Any]:
        return {"id": "1", "display_name": "Test Looker User"}

    def search_agents(self, deleted: Optional[bool] = None, **kwargs: Any) -> list[FakeAgent]:
        self.search_calls.append({"deleted": deleted, **kwargs})
        if self.search_error is not None:
            raise self.search_error
        results = list(self._agents_list)
        if deleted is False:
            results = [a for a in results if not a.deleted]
        return results

    def get_agent(self, agent_id: str, fields: Optional[str] = None) -> FakeAgent:
        self.get_agent_calls.append(agent_id)
        if agent_id not in self.agents_by_id:
            raise FakeSDKError(f"Agent {agent_id} not found (404)", status=404)
        return self.agents_by_id[agent_id]

    def lookml_model_explore(
        self,
        lookml_model_name: str,
        explore_name: str,
        fields: Optional[str] = None,
    ) -> FakeExplore:
        self.explore_calls.append((lookml_model_name, explore_name, fields))
        key = (lookml_model_name, explore_name)
        if key not in self.explores_by_pair:
            raise FakeSDKError(
                f"Explore {lookml_model_name}.{explore_name} not found (404)", status=404
            )
        return self.explores_by_pair[key]


@pytest.fixture
def sample_env_file(tmp_path: Path) -> Path:
    """Create a temporary `.env` file with test Looker credentials."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "LOOKERSDK_BASE_URL=https://looker.example.com",
                "LOOKERSDK_CLIENT_ID=test_client_id",
                "LOOKERSDK_CLIENT_SECRET=test_client_secret",
                "LOOKERSDK_VERIFY_SSL=true",
                "LOOKERSDK_TIMEOUT=120",
                "CA_AGENT_SYNC_PROJECT_ID=test-gcp-project",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return env_path


@pytest.fixture
def sample_sync_config() -> SyncConfig:
    """Return a standard test `SyncConfig`."""
    return SyncConfig(
        looker_base_url="https://looker.example.com",
        looker_client_id="test_client_id",
        looker_client_secret="test_client_secret",
        looker_verify_ssl="true",
        looker_timeout="120",
        project_id="test-gcp-project",
        location="US",
        dataset="looker_agent_registry",
        table="ca_agents",
        connection_id="ca_agent_embedding_conn",
        endpoint="text-embedding-005",
    )
