"""Configuration and environment loading for `ca-agent-sync`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values, load_dotenv

# Ensure GCP client certificate variables are disabled prior to any GCP client creation.
os.environ["CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE"] = "false"
os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"

DEFAULT_PROJECT_ID = ""
DEFAULT_LOCATION = "US"
DEFAULT_DATASET = "looker_agent_registry"
DEFAULT_TABLE = "ca_agents"
DEFAULT_GOLDEN_QUERIES_TABLE = "ca_golden_queries"
DEFAULT_CONNECTION_ID = "ca_agent_embedding_conn"
DEFAULT_ENDPOINT = "text-embedding-005"

LOOKER_ENV_KEYS = (
    "LOOKERSDK_BASE_URL",
    "LOOKERSDK_CLIENT_ID",
    "LOOKERSDK_CLIENT_SECRET",
    "LOOKERSDK_VERIFY_SSL",
    "LOOKERSDK_TIMEOUT",
)


def default_env_path() -> Path:
    """Return the default `.env` path in the project root or current working directory."""
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return cwd_env
    pkg_root_env = Path(__file__).resolve().parents[2] / ".env"
    return pkg_root_env


def load_looker_env(env_file: Optional[Path | str] = None, override: bool = False) -> Path:
    """Load `.env` and export `LOOKERSDK_*` variables into `os.environ`.

    Args:
        env_file: Optional explicit path to `.env`. Defaults to project root `.env`.
        override: Whether values in `.env` should overwrite existing `os.environ` keys.

    Returns:
        Resolved `Path` to the `.env` file checked.
    """
    os.environ["CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE"] = "false"
    os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"

    resolved_path = Path(env_file) if env_file is not None else default_env_path()
    if resolved_path.exists():
        load_dotenv(dotenv_path=resolved_path, override=override)
        parsed = dotenv_values(resolved_path)
        for key in LOOKER_ENV_KEYS:
            val = parsed.get(key)
            if val is not None and (override or key not in os.environ):
                os.environ[key] = str(val).strip()

    # Set sensible defaults for optional Looker SDK flags if not already present
    if "LOOKERSDK_VERIFY_SSL" not in os.environ:
        os.environ["LOOKERSDK_VERIFY_SSL"] = "true"
    if "LOOKERSDK_TIMEOUT" not in os.environ:
        os.environ["LOOKERSDK_TIMEOUT"] = "120"

    return resolved_path


@dataclass(frozen=True)
class SyncConfig:
    """Runtime configuration for Looker extraction and BigQuery autonomous embedding sync."""

    looker_base_url: str = ""
    looker_client_id: str = ""
    looker_client_secret: str = ""
    looker_verify_ssl: str = "true"
    looker_timeout: str = "120"
    project_id: str = DEFAULT_PROJECT_ID
    location: str = DEFAULT_LOCATION
    dataset: str = DEFAULT_DATASET
    table: str = DEFAULT_TABLE
    golden_queries_table: str = DEFAULT_GOLDEN_QUERIES_TABLE
    connection_id: str = DEFAULT_CONNECTION_ID
    endpoint: str = DEFAULT_ENDPOINT

    @property
    def normalized_instance_url(self) -> str:
        """Return the Looker instance base URL without trailing slash."""
        return self.looker_base_url.rstrip("/")

    @property
    def connection_location(self) -> str:
        """Return the lowercase location identifier used in BigQuery connection paths."""
        return self.location.lower()

    @property
    def full_connection_id(self) -> str:
        """Return the fully qualified BigQuery connection ID (`PROJECT.LOCATION.CONNECTION_ID`)."""
        if "." in self.connection_id:
            return self.connection_id
        return f"{self.project_id}.{self.connection_location}.{self.connection_id}"

    @property
    def full_dataset_id(self) -> str:
        """Return `project_id.dataset`."""
        return f"{self.project_id}.{self.dataset}"

    @property
    def full_table_id(self) -> str:
        """Return `project_id.dataset.table`."""
        return f"{self.project_id}.{self.dataset}.{self.table}"

    @property
    def full_golden_queries_table_id(self) -> str:
        """Return `project_id.dataset.golden_queries_table`."""
        return f"{self.project_id}.{self.dataset}.{self.golden_queries_table}"

    def export_looker_env(self) -> None:
        """Export the configured Looker SDK settings into `os.environ` prior to `init40()`."""
        if self.looker_base_url:
            os.environ["LOOKERSDK_BASE_URL"] = self.looker_base_url
        if self.looker_client_id:
            os.environ["LOOKERSDK_CLIENT_ID"] = self.looker_client_id
        if self.looker_client_secret:
            os.environ["LOOKERSDK_CLIENT_SECRET"] = self.looker_client_secret
        if self.looker_verify_ssl:
            os.environ["LOOKERSDK_VERIFY_SSL"] = str(self.looker_verify_ssl)
        if self.looker_timeout:
            os.environ["LOOKERSDK_TIMEOUT"] = str(self.looker_timeout)

    def validate_looker_credentials(self) -> None:
        """Ensure required `LOOKERSDK_*` credentials are set before calling Looker API."""
        missing = []
        if not self.looker_base_url:
            missing.append("LOOKERSDK_BASE_URL")
        if not self.looker_client_id:
            missing.append("LOOKERSDK_CLIENT_ID")
        if not self.looker_client_secret:
            missing.append("LOOKERSDK_CLIENT_SECRET")
        if missing:
            raise ValueError(
                f"Missing required Looker SDK credentials: {', '.join(missing)}. "
                "Please configure them in .env or environment variables."
            )

    @classmethod
    def from_env(
        cls,
        env_file: Optional[Path | str] = None,
        project_id: Optional[str] = None,
        location: Optional[str] = None,
        dataset: Optional[str] = None,
        table: Optional[str] = None,
        golden_queries_table: Optional[str] = None,
        connection_id: Optional[str] = None,
        endpoint: Optional[str] = None,
    ) -> SyncConfig:
        """Load `.env` and construct a `SyncConfig` with optional CLI overrides."""
        load_looker_env(env_file=env_file)

        resolved_project = (
            project_id
            or os.environ.get("CA_AGENT_SYNC_PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or DEFAULT_PROJECT_ID
        )
        resolved_location = location or os.environ.get("CA_AGENT_SYNC_LOCATION") or DEFAULT_LOCATION
        resolved_dataset = dataset or os.environ.get("CA_AGENT_SYNC_DATASET") or DEFAULT_DATASET
        resolved_table = table or os.environ.get("CA_AGENT_SYNC_TABLE") or DEFAULT_TABLE
        resolved_gq_table = (
            golden_queries_table
            or os.environ.get("CA_AGENT_SYNC_GOLDEN_QUERIES_TABLE")
            or DEFAULT_GOLDEN_QUERIES_TABLE
        )
        resolved_connection = (
            connection_id or os.environ.get("CA_AGENT_SYNC_CONNECTION_ID") or DEFAULT_CONNECTION_ID
        )
        resolved_endpoint = endpoint or os.environ.get("CA_AGENT_SYNC_ENDPOINT") or DEFAULT_ENDPOINT

        cfg = cls(
            looker_base_url=os.environ.get("LOOKERSDK_BASE_URL", "").strip(),
            looker_client_id=os.environ.get("LOOKERSDK_CLIENT_ID", "").strip(),
            looker_client_secret=os.environ.get("LOOKERSDK_CLIENT_SECRET", "").strip(),
            looker_verify_ssl=os.environ.get("LOOKERSDK_VERIFY_SSL", "true").strip(),
            looker_timeout=os.environ.get("LOOKERSDK_TIMEOUT", "120").strip(),
            project_id=resolved_project.strip(),
            location=resolved_location.strip(),
            dataset=resolved_dataset.strip(),
            table=resolved_table.strip(),
            golden_queries_table=resolved_gq_table.strip(),
            connection_id=resolved_connection.strip(),
            endpoint=resolved_endpoint.strip(),
        )
        cfg.export_looker_env()
        return cfg

