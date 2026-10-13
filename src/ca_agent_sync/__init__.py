"""Looker CA Agent Metadata & BigQuery Autonomous Embedding Sync (`ca-agent-sync`)."""

from __future__ import annotations

import os

# Disable GCP context-aware / mTLS client certificates before any GCP SDK imports.
os.environ["CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE"] = "false"
os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"

__version__ = "0.1.0"

__all__ = ["__version__"]
