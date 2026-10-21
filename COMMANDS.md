# `ca-agent-sync` CLI Command Reference (`COMMANDS.md`)

Complete reference for the `ca-agent-sync` Python CLI service, which synchronizes Looker Conversational Analytics (CA) agents and their Golden Queries into two Google Cloud BigQuery Autonomous Embedding tables (`ca_agents` and `ca_golden_queries`) and performs single-step or two-step candidate-filtered semantic vector routing—with optional multi-tenant / user-scoped authorization (`--user-id` or `--access-token`).

For a high-level overview, quickstart, and multi-tenant usage guide, see [**`README.md`**](README.md). For internal package architecture and module contracts, see [**`PROJECT.md`**](PROJECT.md).

---

## Table of Contents

1. [Global CLI Overview](#1-global-cli-overview)
2. [Environment Variables (`.env`)](#2-environment-variables-env)
3. [Common Infrastructure & Target Flags](#3-common-infrastructure--target-flags)
4. [Command 1: `ca-agent-sync init`](#4-command-1-ca-agent-sync-init)
5. [Command 2: `ca-agent-sync sync`](#5-command-2-ca-agent-sync-sync)
6. [Command 3: `ca-agent-sync status`](#6-command-3-ca-agent-sync-status)
7. [Command 4: `ca-agent-sync search`](#7-command-4-ca-agent-sync-search)
   - [7.1 Single-Step Agent Semantic Search](#71-single-step-agent-semantic-search)
   - [7.2 Two-Step Golden Query + Agent Candidate-Filtered Search](#72-two-step-golden-query--agent-candidate-filtered-search)
   - [7.3 Two-Step Search with Stage 1 Early Exit (`--stop-on-golden-match`)](#73-two-step-search-with-stage-1-early-exit---stop-on-golden-match)
   - [7.4 Two-Step Search with Stage 1 Miss (Unfiltered Stage 2 Fallback)](#74-two-step-search-with-stage-1-miss-unfiltered-stage-2-fallback)
   - [7.5 Multi-Tenant / User-Scoped Search (`--user-id` & `--access-token`) & Partial Source Access Warnings](#75-multi-tenant--user-scoped-search---user-id----access-token--partial-source-access-warnings)
8. [BigQuery Dual-Table Schemas & Direct SQL Recipes](#8-bigquery-dual-table-schemas--direct-sql-recipes)
   - [8.1 Table Schemas (`ca_agents` & `ca_golden_queries`)](#81-table-schemas-ca_agents--ca_golden_queries)
   - [8.2 Stage 1 SQL: Search Curated Golden Queries (`ca_golden_queries`)](#82-stage-1-sql-search-curated-golden-queries-ca_golden_queries)
   - [8.3 Stage 2 SQL: Candidate-Filtered & User-Scoped Agent Search (`ca_agents`)](#83-stage-2-sql-candidate-filtered--user-scoped-agent-search-ca_agents)
   - [8.4 Single-Step / Unfiltered Fallback SQL (`ca_agents`)](#84-single-step--unfiltered-fallback-sql-ca_agents)
9. [Google Cloud APIs, IAM Roles & Troubleshooting](#9-google-cloud-apis-iam-roles--troubleshooting)
10. [Exit Codes & Error Handling](#10-exit-codes--error-handling)

---

## 1. Global CLI Overview

Invoke the CLI via `uv run ca-agent-sync` from the repository root (or `ca-agent-sync` directly when the virtual environment is activated):

```bash
uv run ca-agent-sync --help
```

```text
Usage: ca-agent-sync [OPTIONS] COMMAND [ARGS]...

  Synchronize Looker Conversational Analytics (CA) agent metadata to BigQuery
  with Autonomous Embedding Generation (`AI.EMBED`) and semantic vector search
  routing.

Options:
  --help  Show this message and exit.

Commands:
  init    Provision and verify BigQuery dataset, Cloud Resource Connection + IAM, and `ca_agents` + `ca_golden_queries` table DDLs.
  sync    Extract CA agent and Golden Query metadata from Looker and atomic-MERGE into BigQuery `ca_agents` and `ca_golden_queries`.
  status  Report BigQuery row counts and autonomous embedding generation status for both `ca_agents` and `ca_golden_queries`.
  search  Route a natural language question to top-K matching CA agents using single-step or two-step semantic vector search.
```

---

## 2. Environment Variables (`.env`)

At startup, `ca-agent-sync` loads `.env` (or the file specified via `--env-file`) and exports `LOOKERSDK_*` variables into `os.environ` before initializing the Looker 4.0 SDK (`looker_sdk.init40()`). It also sets `CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE="false"` and `GOOGLE_API_USE_CLIENT_CERTIFICATE="false"` prior to Google Cloud client creation.

| Environment Variable | Required | Default | Description |
| :--- | :---: | :--- | :--- |
| `LOOKERSDK_BASE_URL` | Yes (for `sync` & user-scoped `search`) | `https://your-instance.cloud.looker.com` | Base URL of the Looker instance. |
| `LOOKERSDK_CLIENT_ID` | Yes (for `sync` & `search --user-id`) | — | Looker API3 Client ID (read-only access to CA agents & LookML Explores; `login_user` sudo permission required for `search --user-id`). Not required when using `search --access-token`. |
| `LOOKERSDK_CLIENT_SECRET` | Yes (for `sync` & `search --user-id`) | — | Looker API3 Client Secret. Not required when using `search --access-token`. |
| `LOOKERSDK_VERIFY_SSL` | No | `true` | Whether to verify TLS/SSL certificates when connecting to Looker. |
| `LOOKERSDK_TIMEOUT` | No | `120` | Looker HTTP request timeout in seconds. |
| `LOOKER_ACCESS_TOKEN` | No | — | Optional Looker user OAuth or session Bearer token for user-scoped search (equivalent to passing `--access-token` to `ca-agent-sync search`). |
| `CA_AGENT_SYNC_PROJECT_ID` | No | `<YOUR_GCP_PROJECT_ID>` | Target Google Cloud project ID hosting BigQuery and Vertex AI. |
| `CA_AGENT_SYNC_LOCATION` | No | `US` | BigQuery dataset and Cloud Resource Connection location. |
| `CA_AGENT_SYNC_DATASET` | No | `looker_agent_registry` | BigQuery dataset name. |
| `CA_AGENT_SYNC_TABLE` | No | `ca_agents` | BigQuery Agents autonomous embedding table name. |
| `CA_AGENT_SYNC_GOLDEN_QUERIES_TABLE` | No | `ca_golden_queries` | BigQuery Golden Queries autonomous embedding table name. |
| `CA_AGENT_SYNC_CONNECTION_ID` | No | `ca_agent_embedding_conn` | BigQuery Cloud Resource Connection ID used by `AI.EMBED`. |
| `CA_AGENT_SYNC_ENDPOINT` | No | `text-embedding-005` | Vertex AI text embedding model endpoint used by `AI.EMBED`. |

### Example `.env` File

```dotenv
# Looker Python SDK Credentials (Required for `sync` and `search --user-id`)
LOOKERSDK_BASE_URL=https://your-instance.cloud.looker.com
LOOKERSDK_CLIENT_ID=your_looker_client_id
LOOKERSDK_CLIENT_SECRET=your_looker_client_secret
LOOKERSDK_VERIFY_SSL=true
LOOKERSDK_TIMEOUT=120

# Optional: Looker User OAuth / Session Token for User-Scoped Search (`search --access-token`)
# LOOKER_ACCESS_TOKEN=eyJhbGciOi...

# Google Cloud / BigQuery Autonomous Embedding Target Defaults (Optional Overrides)
CA_AGENT_SYNC_PROJECT_ID=<YOUR_GCP_PROJECT_ID>
CA_AGENT_SYNC_LOCATION=US
CA_AGENT_SYNC_DATASET=looker_agent_registry
CA_AGENT_SYNC_TABLE=ca_agents
CA_AGENT_SYNC_GOLDEN_QUERIES_TABLE=ca_golden_queries
CA_AGENT_SYNC_CONNECTION_ID=ca_agent_embedding_conn
CA_AGENT_SYNC_ENDPOINT=text-embedding-005
```

---

## 3. Common Infrastructure & Target Flags

Every subcommand (`init`, `sync`, `status`, `search`) accepts the following shared target and output flags, which override `.env` and built-in defaults when provided:

| Flag / Alias | Type | Default | Supported Commands | Description |
| :--- | :--- | :--- | :--- | :--- |
| `--project-id`, `--project` | `TEXT` | `<YOUR_GCP_PROJECT_ID>` | `init`, `sync`, `status`, `search` | Google Cloud project ID. |
| `--location` | `TEXT` | `US` | `init`, `sync`, `status`, `search` | BigQuery dataset and connection location. |
| `--dataset` | `TEXT` | `looker_agent_registry` | `init`, `sync`, `status`, `search` | BigQuery dataset name. |
| `--table` | `TEXT` | `ca_agents` | `init`, `sync`, `status`, `search` | BigQuery Agents table name (`ca_agents`). |
| `--golden-queries-table` | `TEXT` | `ca_golden_queries` | `init`, `sync`, `status`, `search` | BigQuery Golden Queries table name (`ca_golden_queries`). |
| `--connection-id`, `--connection` | `TEXT` | `ca_agent_embedding_conn` | `init`, `sync`, `search` | BigQuery Cloud Resource Connection ID. |
| `--endpoint` | `TEXT` | `text-embedding-005` | `init`, `sync`, `search` | Vertex AI embedding model endpoint. |
| `--env-file` | `PATH` | `.env` | `init`, `sync`, `status`, `search` | Optional path to a custom `.env` file. |
| `--json` | `FLAG` | `False` | `init`, `sync`, `status`, `search` | Emit structured, machine-readable JSON to `stdout`. |

---

## 4. Command 1: `ca-agent-sync init`

Provisions and verifies the complete BigQuery Autonomous Embedding infrastructure:
1. Ensures dataset `<YOUR_GCP_PROJECT_ID>.looker_agent_registry` exists in location `US`.
2. Ensures Cloud Resource Connection `us.ca_agent_embedding_conn` exists and binds `roles/aiplatform.user` to its dedicated service account (`bqcx-...`).
3. Runs an `AI.EMBED` warmup probe (unless `--skip-warmup` is passed) to verify IAM propagation.
4. Creates both autonomous embedding tables (`ca_agents` and `ca_golden_queries`) with `embedding STRUCT<result ARRAY<FLOAT64>, status STRING> GENERATED ALWAYS AS (AI.EMBED(...)) STORED OPTIONS (asynchronous = TRUE)` and verifies `is_generated = 'ALWAYS'` and `is_stored = 'YES'` in `INFORMATION_SCHEMA.COLUMNS`.

### Flags for `init`

| Flag | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--skip-warmup` | `FLAG` | `False` | Skip running the `AI.EMBED` warmup verification query during initialization. |
| `--project-id`, `--project` | `TEXT` | `<YOUR_GCP_PROJECT_ID>` | Google Cloud project ID. |
| `--location` | `TEXT` | `US` | BigQuery dataset/connection location. |
| `--dataset` | `TEXT` | `looker_agent_registry` | BigQuery dataset name. |
| `--table` | `TEXT` | `ca_agents` | BigQuery Agents table name. |
| `--golden-queries-table` | `TEXT` | `ca_golden_queries` | BigQuery Golden Queries table name. |
| `--connection-id`, `--connection` | `TEXT` | `ca_agent_embedding_conn` | BigQuery Cloud Resource Connection ID. |
| `--endpoint` | `TEXT` | `text-embedding-005` | Vertex AI embedding endpoint. |
| `--env-file` | `PATH` | `.env` | Optional path to `.env` file. |
| `--json` | `FLAG` | `False` | Output structured JSON to `stdout`. |

### Copy-Pasteable Examples

```bash
# Provision and verify both ca_agents and ca_golden_queries (human output)
uv run ca-agent-sync init

# Provision and verify with structured JSON output
uv run ca-agent-sync init --json

# Provision while skipping the Vertex AI warmup probe
uv run ca-agent-sync init --skip-warmup
```

### Sample Human Output (`init`)

```text
BigQuery Autonomous Embedding Infrastructure Ready
  • Dataset: <YOUR_GCP_PROJECT_ID>.looker_agent_registry (US)
  • Connection: <YOUR_GCP_PROJECT_ID>.us.ca_agent_embedding_conn
  • Connection Service Account: bqcx-123456789012-abcd@gcp-sa-bigquery-condel.iam.gserviceaccount.com
  • IAM Role (roles/aiplatform.user): Granted
  • Agents Table: <YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_agents
  • Autonomous Column `embedding` (ca_agents): is_generated=ALWAYS, is_stored=YES
  • Expression (ca_agents): AI.EMBED(metadata_concat, connection_id => '<YOUR_GCP_PROJECT_ID>.us.ca_agent_embedding_conn', endpoint => 'text-embedding-005')
  • Golden Queries Table: <YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_golden_queries
  • Autonomous Column `embedding` (ca_golden_queries): is_generated=ALWAYS, is_stored=YES
  • Expression (ca_golden_queries): AI.EMBED(metadata_concat, connection_id => '<YOUR_GCP_PROJECT_ID>.us.ca_agent_embedding_conn', endpoint => 'text-embedding-005')
```

### Sample `--json` Output (`init --json`)

```json
{
  "status": "ok",
  "project_id": "<YOUR_GCP_PROJECT_ID>",
  "location": "US",
  "dataset_id": "<YOUR_GCP_PROJECT_ID>.looker_agent_registry",
  "connection": {
    "connection_id": "<YOUR_GCP_PROJECT_ID>.us.ca_agent_embedding_conn",
    "name": "projects/123456789012/locations/us/connections/ca_agent_embedding_conn",
    "service_account_id": "bqcx-123456789012-abcd@gcp-sa-bigquery-condel.iam.gserviceaccount.com",
    "iam_role": "roles/aiplatform.user",
    "iam_bound": true
  },
  "warmup_verified": true,
  "table": {
    "table_id": "<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_agents",
    "column_name": "embedding",
    "data_type": "STRUCT<result ARRAY<FLOAT64>, status STRING>",
    "is_generated": "ALWAYS",
    "generation_expression": "AI.EMBED(metadata_concat, connection_id => '<YOUR_GCP_PROJECT_ID>.us.ca_agent_embedding_conn', endpoint => 'text-embedding-005')",
    "is_stored": "YES",
    "async_generation_status": "OK"
  },
  "golden_queries_table": {
    "table_id": "<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_golden_queries",
    "column_name": "embedding",
    "data_type": "STRUCT<result ARRAY<FLOAT64>, status STRING>",
    "is_generated": "ALWAYS",
    "generation_expression": "AI.EMBED(metadata_concat, connection_id => '<YOUR_GCP_PROJECT_ID>.us.ca_agent_embedding_conn', endpoint => 'text-embedding-005')",
    "is_stored": "YES",
    "async_generation_status": "OK"
  }
}
```

---

## 5. Command 2: `ca-agent-sync sync`

Extracts Conversational Analytics agent metadata and Golden Queries from Looker in read-only mode, enriches LookML Explores with cached lightweight headers (`fields="name,label,description"`), builds deterministic `metadata_concat` text strings for both Agents and Golden Queries, and executes atomic BigQuery `MERGE` operations into both `ca_agents` (keyed on `agent_id`) and `ca_golden_queries` (keyed on `record_id = "{agent_id}:{golden_query_id}"`).

- **Preserves Populated Embeddings**: On re-sync, both `MERGE` statements only update `metadata_concat` when `IFNULL(T.metadata_concat, '') != IFNULL(S.metadata_concat, '')`, preventing BigQuery from resetting already-populated embeddings for unchanged rows.
- **Automatic Unlinked Golden Query Cleanup**: Any Golden Query rows in `ca_golden_queries` belonging to the synced `agent_id`(s) that were unlinked or removed in Looker are automatically deleted (`WHERE agent_id IN UNNEST(@synced_agent_ids) AND record_id NOT IN UNNEST(@active_record_ids)`).
- **Deleted & Pruned Agent Cleanup**: When a single `--agent-id` is soft-deleted (`deleted=True`) or `404` in Looker, or when `--prune` is passed with `--batch`, stale rows are removed from **both** `ca_agents` and `ca_golden_queries`.

### Flags for `sync`

*(Note: Exactly one of `--batch` or `--agent-id <ID>` is required.)*

| Flag | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--batch` | `FLAG` | `False` | Sync all active CA agents (`sdk.search_agents(deleted=False)`) and all their Golden Queries. |
| `--agent-id` | `TEXT` | `None` | Sync and overwrite a single CA agent by ID (`sdk.get_agent(agent_id)`) and its Golden Queries. |
| `--prune` | `FLAG` | `False` | During `--batch` sync, delete rows in `ca_agents` and `ca_golden_queries` whose `agent_id` no longer exists in Looker. |
| `--no-explore-headers` | `FLAG` | `False` | Skip calling `sdk.lookml_model_explore(model, explore, fields="name,label,description")`. |
| `--skip-init` | `FLAG` | `False` | Skip ensuring BigQuery dataset, connection, and tables before syncing. |
| `--project-id`, `--project` | `TEXT` | `<YOUR_GCP_PROJECT_ID>` | Google Cloud project ID. |
| `--location` | `TEXT` | `US` | BigQuery dataset/connection location. |
| `--dataset` | `TEXT` | `looker_agent_registry` | BigQuery dataset name. |
| `--table` | `TEXT` | `ca_agents` | BigQuery Agents table name. |
| `--golden-queries-table` | `TEXT` | `ca_golden_queries` | BigQuery Golden Queries table name. |
| `--connection-id`, `--connection` | `TEXT` | `ca_agent_embedding_conn` | BigQuery Cloud Resource Connection ID. |
| `--endpoint` | `TEXT` | `text-embedding-005` | Vertex AI embedding endpoint. |
| `--env-file` | `PATH` | `.env` | Optional path to `.env` file. |
| `--json` | `FLAG` | `False` | Output structured JSON to `stdout`. |

### Copy-Pasteable Examples

```bash
# Single-agent sync (syncs 1 agent into ca_agents and its 12 Golden Queries into ca_golden_queries)
uv run ca-agent-sync sync --agent-id agent-eng-001

# Single-agent sync with JSON output
uv run ca-agent-sync sync --agent-id agent-eng-001 --json

# Batch sync all active agents and their Golden Queries
uv run ca-agent-sync sync --batch

# Batch sync and prune any stale agents + their Golden Queries from BigQuery
uv run ca-agent-sync sync --batch --prune

# Batch sync without fetching LookML Explore headers
uv run ca-agent-sync sync --batch --no-explore-headers
```

### Sample Human Output (`sync --agent-id` and `sync --batch`)

```text
Sync Complete (single mode) → <YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_agents
  • Agents upserted: 1
  • Golden Queries upserted (<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_golden_queries): 12
  • Lightweight Explore headers cached: 5
```

```text
Sync Complete (batch mode) → <YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_agents
  • Agents upserted: 22
  • Golden Queries upserted (<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_golden_queries): 134
  • Lightweight Explore headers cached: 45
```

### Sample `--json` Output (`sync --agent-id agent-eng-001 --json`)

```json
{
  "status": "ok",
  "mode": "single",
  "table_id": "<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_agents",
  "synced_count": 1,
  "deleted_count": 0,
  "pruned_count": 0,
  "explore_headers_cached": 5,
  "agent_ids": [
    "agent-eng-001"
  ],
  "deleted_agent_ids": [],
  "pruned_agent_ids": [],
  "golden_queries_table_id": "<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_golden_queries",
  "golden_queries_synced_count": 12,
  "golden_queries_deleted_count": 0,
  "golden_queries_pruned_count": 0
}
```

---

## 6. Command 3: `ca-agent-sync status`

Inspects both `ca_agents` and `ca_golden_queries` in BigQuery and reports:
- Total rows in each table (`COUNT(*)`)
- Rows with populated autonomous embeddings (`ARRAY_LENGTH(embedding.result) > 0`)
- Rows pending background embedding generation (`embedding IS NULL OR ARRAY_LENGTH(embedding.result) = 0`)
- Rows with non-empty `embedding.status` error strings
- Latest `synced_at` timestamp and `INFORMATION_SCHEMA.COLUMNS` generation metadata (`is_generated`, `async_generation_status`) for both tables.

### Flags for `status`

| Flag | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--project-id`, `--project` | `TEXT` | `<YOUR_GCP_PROJECT_ID>` | Google Cloud project ID. |
| `--location` | `TEXT` | `US` | BigQuery dataset/connection location. |
| `--dataset` | `TEXT` | `looker_agent_registry` | BigQuery dataset name. |
| `--table` | `TEXT` | `ca_agents` | BigQuery Agents table name. |
| `--golden-queries-table` | `TEXT` | `ca_golden_queries` | BigQuery Golden Queries table name. |
| `--env-file` | `PATH` | `.env` | Optional path to `.env` file. |
| `--json` | `FLAG` | `False` | Output structured JSON to `stdout`. |

### Copy-Pasteable Examples

```bash
# Dual-table status report (human Rich tables)
uv run ca-agent-sync status

# Dual-table status report (machine-readable JSON)
uv run ca-agent-sync status --json
```

### Sample Human Output (`status`)

```text
       Autonomous Embedding Status: <YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_agents        
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Metric                        ┃ Value                            ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Total Agent Rows              │ 22                               │
│ Populated Embeddings (READY)  │ 22                               │
│ Pending Background Generation │ 0                                │
│ Embedding Errors              │ 0                                │
│ Last Synced At                │ 2026-10-13 16:40:12.418290+00:00 │
│ Column is_generated           │ ALWAYS                           │
│ Async Generation Status       │ OK                               │
└───────────────────────────────┴──────────────────────────────────┘
   Autonomous Embedding Status (Golden Queries): <YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_golden_queries   
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Metric                        ┃ Value                            ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Total Golden Query Rows       │ 134                              │
│ Populated Embeddings (READY)  │ 134                              │
│ Pending Background Generation │ 0                                │
│ Embedding Errors              │ 0                                │
│ Last Synced At                │ 2026-10-13 16:40:12.418290+00:00 │
│ Column is_generated           │ ALWAYS                           │
│ Async Generation Status       │ OK                               │
└───────────────────────────────┴──────────────────────────────────┘
```

### Sample `--json` Output (`status --json`)

```json
{
  "status": "ok",
  "table_id": "<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_agents",
  "total_rows": 22,
  "populated_embeddings": 22,
  "pending_embeddings": 0,
  "error_embeddings": 0,
  "last_synced_at": "2026-10-13 16:40:12.418290+00:00",
  "is_generated": "ALWAYS",
  "generation_expression": "AI.EMBED(metadata_concat, connection_id => '<YOUR_GCP_PROJECT_ID>.us.ca_agent_embedding_conn', endpoint => 'text-embedding-005')",
  "async_generation_status": "OK",
  "errors": [],
  "golden_queries_table_id": "<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_golden_queries",
  "golden_queries_total_rows": 134,
  "golden_queries_populated_embeddings": 134,
  "golden_queries_pending_embeddings": 0,
  "golden_queries_error_embeddings": 0,
  "golden_queries_last_synced_at": "2026-10-13 16:40:12.418290+00:00",
  "golden_queries_is_generated": "ALWAYS",
  "golden_queries_generation_expression": "AI.EMBED(metadata_concat, connection_id => '<YOUR_GCP_PROJECT_ID>.us.ca_agent_embedding_conn', endpoint => 'text-embedding-005')",
  "golden_queries_async_generation_status": "OK",
  "golden_queries_errors": []
}
```

---

## 7. Command 4: `ca-agent-sync search`

Routes a natural language `<question>` to the best Looker CA agent(s) using BigQuery semantic vector search (`AI.SEARCH` when all autonomous embeddings are populated, with automatic fallback to `AI.EMBED` + `ML.DISTANCE(..., 'COSINE')` when background generation is still pending for newly synced rows).

Supports two routing modes:
1. **Single-Step Agent Search** (default when `--golden-queries` is omitted): Searches `ca_agents` directly across all accessible agents.
2. **Two-Step Golden Query + Agent Candidate-Filtered Search** (enabled when `--golden-queries` / `-g` is passed):
   - **Stage 1 (`ca_golden_queries`)**: Pulls the top `--golden-queries=<N>` Golden Query matches (pre-filtered to `@allowed_agent_ids` when user-scoped) and filters them against `--confidence_score` (or `--confidence-score` / `-c`, defaulting to `0.60`, validated in `[0.0, 1.0]`).
   - **Stage 2 (`ca_agents`)**:
     - If $\ge 1$ Golden Queries have `similarity_score >= confidence_score`: Returns those matched Golden Queries and (unless `--stop-on-golden-match` is set) searches `ca_agents` **strictly filtered to the distinct `agent_id`s from those matched Golden Queries** (`WHERE agent_id IN UNNEST(@matched_agent_ids)`, intersected with `@allowed_agent_ids` when user-scoped).
     - If $0$ Golden Queries meet `confidence_score`: Returns no Golden Queries (`golden_query_results: []`) and falls back to a Stage 2 search across `ca_agents` (pre-filtered to `@allowed_agent_ids` when user-scoped, or across all rows in `ca_agents` in Admin mode).

Supports three authorization scopes (mutually exclusive):
- **Admin / Global Mode (Default)**: When neither `--user-id` nor `--access-token` is provided (`authz_mode: "admin"`), searches across all synced rows in `ca_agents` and `ca_golden_queries`.
- **User-Scoped via Admin Sudo (`--user-id`, `-u <LOOKER_USER_ID>`)**: Impersonates a target Looker user using the Admin credentials in `.env` (`POST /api/4.0/login/{user_id}`), fetches only the CA agents that user has access to (`GET /api/4.0/agents/search`), logs out the temporary sudo session, and pre-filters BigQuery vector search (`Stage 1` Golden Queries and `Stage 2` Agents) to that user's allowlist (`authz_mode: "user_id"`).
- **User-Scoped via Bearer Token (`--access-token <TOKEN>` or `LOOKER_ACCESS_TOKEN`)**: Passes a target user's Looker OAuth or session access token directly to `GET /api/4.0/agents/search` (`Authorization: Bearer <TOKEN>`) **without requiring Admin client credentials (`LOOKERSDK_CLIENT_ID` / `LOOKERSDK_CLIENT_SECRET`) on the search caller**, then pre-filters BigQuery vector search to those accessible agents (`authz_mode: "access_token"`).

### Flags & Arguments for `search`

| Argument / Flag | Aliases / Env Var | Type | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `QUESTION` *(Positional)* | — | `TEXT` | *Required* | Natural language question to route to the best Looker CA agent. |
| `--top-k` | `-k` | `INTEGER` | `5` | Number of top matching agents to return (`1..100`). |
| `--golden-queries` | `-g` | `INTEGER` | `None` | Enable Two-Step Search: number of Stage 1 Golden Query embeddings to pull from `ca_golden_queries` (`1..100`). |
| `--confidence_score` | `--confidence-score`, `-c` | `FLOAT` | `0.60` | Minimum cosine similarity score in `[0.0, 1.0]` for Stage 1 Golden Query matches (active when `--golden-queries` is specified). |
| `--stop-on-golden-match` | — | `FLAG` | `False` | Stop after Stage 1 and return matched Golden Queries without querying `ca_agents` if $\ge 1$ Golden Queries meet `confidence_score`. |
| `--user-id` | `-u` | `TEXT` | `None` | Looker user ID to sudo as (via Admin credentials in `.env`, `POST /api/4.0/login/{user_id}`) so only CA agents accessible to that user are searched. Mutually exclusive with `--access-token`. |
| `--access-token` | `LOOKER_ACCESS_TOKEN` | `TEXT` | `None` | Looker user access token (OAuth or session Bearer token) to fetch only CA agents accessible to that user (`GET /api/4.0/agents/search`) without requiring Admin client credentials. Mutually exclusive with `--user-id`. |
| `--project-id` | `--project` | `TEXT` | `<YOUR_GCP_PROJECT_ID>` | Google Cloud project ID. |
| `--location` | — | `TEXT` | `US` | BigQuery dataset/connection location. |
| `--dataset` | — | `TEXT` | `looker_agent_registry` | BigQuery dataset name. |
| `--table` | — | `TEXT` | `ca_agents` | BigQuery Agents table name (`ca_agents`). |
| `--golden-queries-table` | — | `TEXT` | `ca_golden_queries` | BigQuery Golden Queries table name (`ca_golden_queries`). |
| `--connection-id` | `--connection` | `TEXT` | `ca_agent_embedding_conn` | BigQuery Cloud Resource Connection ID. |
| `--endpoint` | — | `TEXT` | `text-embedding-005` | Vertex AI embedding endpoint. |
| `--env-file` | — | `PATH` | `.env` | Optional path to `.env` file. |
| `--json` | — | `FLAG` | `False` | Output structured JSON to `stdout`. |

---

### 7.1 Single-Step Agent Semantic Search

Queries `ca_agents` directly without consulting `ca_golden_queries`.

```bash
# Single-step semantic search (human output)
uv run ca-agent-sync search "What is our sprint velocity and SLA compliance rate for engineering issues?" --top-k 3

# Single-step semantic search (JSON output)
uv run ca-agent-sync search "What is our sprint velocity and SLA compliance rate for engineering issues?" --top-k 3 --json
```

#### Sample `--json` Output (Single-Step `search`)

```json
{
  "status": "ok",
  "question": "What is our sprint velocity and SLA compliance rate for engineering issues?",
  "top_k": 3,
  "authz_mode": "admin",
  "user_id": null,
  "allowed_agent_count": null,
  "results": [
    {
      "rank": 1,
      "agent_id": "agent-eng-001",
      "name": "Engineering Intelligence Assistant",
      "description": "AI Conversational Analytics Assistant for engineering_analytics",
      "category": null,
      "similarity_score": 0.728419,
      "distance": 0.271581,
      "agent_url": "https://your-instance.cloud.looker.com/extensions/conversational_analytics::conversational_analytics/agents/agent-eng-001",
      "instance_url": "https://your-instance.cloud.looker.com",
      "explores": [
        "engineering_analytics.comment",
        "engineering_analytics.issue",
        "engineering_analytics.issue_history",
        "engineering_analytics.issue_labels",
        "engineering_analytics.project"
      ],
      "sources": [
        {
          "description": "Issue tracking, story points, cycle velocity, and SLA compliance.",
          "explore": "issue",
          "label": "Issues & Sprint Delivery",
          "model": "engineering_analytics",
          "name": "issue"
        }
      ],
      "embedding_source": "STORED",
      "has_inaccessible_source": false,
      "warning": null
    }
  ]
}
```

---

### 7.2 Two-Step Golden Query + Agent Candidate-Filtered Search

When `--golden-queries=5` and `--confidence_score=0.65` are passed, Stage 1 searches `ca_golden_queries` for the top 5 Golden Queries. Because the Golden Query `"What is the status and completion progress of all strategic projects?"` on agent `agent-eng-001` exceeds `0.65` cosine similarity, Stage 1 returns the matched Golden Query and Stage 2 restricts its `ca_agents` search strictly to `WHERE agent_id IN UNNEST(@matched_agent_ids)`.

```bash
# Two-step search with Golden Query match >= 0.65 (human output)
uv run ca-agent-sync search "What is the status and completion progress of all strategic projects?" \
  --golden-queries=5 \
  --confidence_score=0.65 \
  --top-k 3

# Using hyphenated alias --confidence-score or short flags -g / -c / -k with --json
uv run ca-agent-sync search "What is the status and completion progress of all strategic projects?" \
  -g 5 -c 0.65 -k 3 --json
```

#### Sample `--json` Output (Two-Step Search — Stage 1 Match + Filtered Stage 2)

Matches the exact `TwoStepSearchResult` schema:

```json
{
  "status": "ok",
  "question": "What is the status and completion progress of all strategic projects?",
  "top_k": 3,
  "golden_queries_requested": 5,
  "confidence_score": 0.65,
  "stop_on_golden_match": false,
  "golden_query_matched": true,
  "stage2_filtered": true,
  "matched_agent_ids": [
    "agent-eng-001"
  ],
  "authz_mode": "admin",
  "user_id": null,
  "allowed_agent_count": null,
  "golden_query_results": [
    {
      "rank": 1,
      "record_id": "agent-eng-001:1",
      "golden_query_id": "1",
      "agent_id": "agent-eng-001",
      "agent_name": "Engineering Intelligence Assistant",
      "agent_url": "https://your-instance.cloud.looker.com/extensions/conversational_analytics::conversational_analytics/agents/agent-eng-001",
      "instance_url": "https://your-instance.cloud.looker.com",
      "questions": [
        "What is the status and completion progress of all strategic projects?"
      ],
      "model": "engineering_analytics",
      "explore": "project",
      "fields": [
        "project.name",
        "team.name",
        "lead.name",
        "project_status.name",
        "project.state",
        "project_metrics_rollup.latest_health_status",
        "project.progress"
      ],
      "filters": {},
      "sorts": [
        "project.progress desc"
      ],
      "limit": null,
      "answer": null,
      "similarity_score": 0.841205,
      "distance": 0.158795,
      "embedding_source": "STORED",
      "has_inaccessible_source": false,
      "warning": null
    }
  ],
  "results": [
    {
      "rank": 1,
      "agent_id": "agent-eng-001",
      "name": "Engineering Intelligence Assistant",
      "description": "AI Conversational Analytics Assistant for engineering_analytics",
      "category": null,
      "similarity_score": 0.752104,
      "distance": 0.247896,
      "agent_url": "https://your-instance.cloud.looker.com/extensions/conversational_analytics::conversational_analytics/agents/agent-eng-001",
      "instance_url": "https://your-instance.cloud.looker.com",
      "explores": [
        "engineering_analytics.comment",
        "engineering_analytics.issue",
        "engineering_analytics.issue_history",
        "engineering_analytics.issue_labels",
        "engineering_analytics.project"
      ],
      "sources": [
        {
          "description": "Project roadmap and health status.",
          "explore": "project",
          "label": "Strategic Projects",
          "model": "engineering_analytics",
          "name": "project"
        }
      ],
      "embedding_source": "STORED",
      "has_inaccessible_source": false,
      "warning": null
    }
  ]
}
```

---

### 7.3 Two-Step Search with Stage 1 Early Exit (`--stop-on-golden-match`)

When `--stop-on-golden-match` is passed and $\ge 1$ Golden Queries meet `confidence_score`, `ca-agent-sync` stops immediately after Stage 1 and returns the matched Golden Queries without querying `ca_agents` (`results: []`).

```bash
uv run ca-agent-sync search "What is the status and completion progress of all strategic projects?" \
  --golden-queries=5 \
  --confidence_score=0.65 \
  --stop-on-golden-match \
  --json
```

#### Sample `--json` Output (`--stop-on-golden-match`)

```json
{
  "status": "ok",
  "question": "What is the status and completion progress of all strategic projects?",
  "top_k": 5,
  "golden_queries_requested": 5,
  "confidence_score": 0.65,
  "stop_on_golden_match": true,
  "golden_query_matched": true,
  "stage2_filtered": true,
  "matched_agent_ids": [
    "agent-eng-001"
  ],
  "authz_mode": "admin",
  "user_id": null,
  "allowed_agent_count": null,
  "golden_query_results": [
    {
      "rank": 1,
      "record_id": "agent-eng-001:1",
      "golden_query_id": "1",
      "agent_id": "agent-eng-001",
      "agent_name": "Engineering Intelligence Assistant",
      "agent_url": "https://your-instance.cloud.looker.com/extensions/conversational_analytics::conversational_analytics/agents/agent-eng-001",
      "instance_url": "https://your-instance.cloud.looker.com",
      "questions": [
        "What is the status and completion progress of all strategic projects?"
      ],
      "model": "engineering_analytics",
      "explore": "project",
      "fields": [
        "project.name",
        "team.name",
        "lead.name",
        "project_status.name",
        "project.state",
        "project_metrics_rollup.latest_health_status",
        "project.progress"
      ],
      "filters": {},
      "sorts": [
        "project.progress desc"
      ],
      "limit": null,
      "answer": null,
      "similarity_score": 0.841205,
      "distance": 0.158795,
      "embedding_source": "STORED",
      "has_inaccessible_source": false,
      "warning": null
    }
  ],
  "results": []
}
```

---

### 7.4 Two-Step Search with Stage 1 Miss (Unfiltered Stage 2 Fallback)

When 0 Golden Queries meet `confidence_score` (for example, when `--confidence_score=0.99`), Stage 1 returns no Golden Queries (`golden_query_matched: false`, `golden_query_results: []`) and Stage 2 automatically runs an **unfiltered** semantic vector search across all agents in `ca_agents` (`stage2_filtered: false`—or pre-filtered to `@allowed_agent_ids` when `--user-id` / `--access-token` is active).

```bash
# Two-step search where 0 Golden Queries meet confidence_score=0.99 -> unfiltered Stage 2 fallback
uv run ca-agent-sync search "What is our sprint velocity and SLA compliance rate for engineering issues?" \
  --golden-queries=5 \
  --confidence_score=0.99 \
  --top-k 3

# With JSON output
uv run ca-agent-sync search "What is our sprint velocity and SLA compliance rate for engineering issues?" \
  --golden-queries=5 \
  --confidence_score=0.99 \
  --top-k 3 \
  --json
```

#### Sample `--json` Output (Stage 1 Miss -> Unfiltered Stage 2 Fallback)

```json
{
  "status": "ok",
  "question": "What is our sprint velocity and SLA compliance rate for engineering issues?",
  "top_k": 3,
  "golden_queries_requested": 5,
  "confidence_score": 0.99,
  "stop_on_golden_match": false,
  "golden_query_matched": false,
  "stage2_filtered": false,
  "matched_agent_ids": [],
  "authz_mode": "admin",
  "user_id": null,
  "allowed_agent_count": null,
  "golden_query_results": [],
  "results": [
    {
      "rank": 1,
      "agent_id": "agent-eng-001",
      "name": "Engineering Intelligence Assistant",
      "description": "AI Conversational Analytics Assistant for engineering_analytics",
      "category": null,
      "similarity_score": 0.728419,
      "distance": 0.271581,
      "agent_url": "https://your-instance.cloud.looker.com/extensions/conversational_analytics::conversational_analytics/agents/agent-eng-001",
      "instance_url": "https://your-instance.cloud.looker.com",
      "explores": [
        "engineering_analytics.comment",
        "engineering_analytics.issue",
        "engineering_analytics.issue_history",
        "engineering_analytics.issue_labels",
        "engineering_analytics.project"
      ],
      "sources": [
        {
          "description": "Issue tracking, story points, cycle velocity, and SLA compliance.",
          "explore": "issue",
          "label": "Issues & Sprint Delivery",
          "model": "engineering_analytics",
          "name": "issue"
        }
      ],
      "embedding_source": "STORED",
      "has_inaccessible_source": false,
      "warning": null
    }
  ]
}
```

---

### 7.5 Multi-Tenant / User-Scoped Search (`--user-id` & `--access-token`) & Partial Source Access Warnings

To ensure search only returns CA agents that a specific target Looker user is authorized to access—while maintaining a single global set of embeddings in BigQuery—pass either `--user-id` (`-u`) or `--access-token` (`LOOKER_ACCESS_TOKEN`):

1. **`--user-id`, `-u <LOOKER_USER_ID>` (Admin Sudo Impersonation)**:
   - Uses the Admin credentials in `.env` (`LOOKERSDK_CLIENT_ID` / `LOOKERSDK_CLIENT_SECRET`) to impersonate the target Looker user (`POST /api/4.0/login/{user_id}` via `sdk.auth.login_user(user_id)`).
   - Fetches only the CA agents that user has access to (`GET /api/4.0/agents/search?deleted=false&fields=id,deleted,has_inaccessible_source`), guarantees `sdk.auth.logout()` of the sudo session, and pre-filters BigQuery vector search (`Stage 1` Golden Queries and `Stage 2` Agents) to that user's allowlist (`WHERE agent_id IN UNNEST(@allowed_agent_ids)`).
   - **Looker Core Nuance**: On **Looker (Google Cloud core)**, `login_user` (`--user-id` sudo) is only permitted when the Admin credentials belong to an **API-only Service Account** and the target user is an **Embed User**. For regular Looker Core users (authenticated via Google OAuth), pass their OAuth token via `--access-token` / `LOOKER_ACCESS_TOKEN`.
2. **`--access-token <TOKEN>` / `LOOKER_ACCESS_TOKEN` (Direct User Bearer Token)**:
   - Passes a target user's Looker OAuth or session access token directly to `GET /api/4.0/agents/search` (`Authorization: Bearer <TOKEN>`) **without requiring Admin client credentials (`LOOKERSDK_CLIENT_ID` / `LOOKERSDK_CLIENT_SECRET`) on the search caller** (only `LOOKERSDK_BASE_URL` is needed), then pre-filters BigQuery vector search to those accessible agents.
3. **Partial Source Access Warning (`has_inaccessible_source`)**:
   - If a user has access to a CA agent in Looker, that agent is **included** in search results even if `has_inaccessible_source=true` (because the agent may have multiple LookML Explores/sources that the user *can* access).
   - Both the human CLI output and `--json` output surface an explicit warning on matching Agent and Golden Query hits:
     - `has_inaccessible_source`: `true`
     - `warning`: `"User lacks access to one or more LookML sources on this agent; some queries may fail."`
4. **Viewer Disclosure Policy**:
   - Viewer users who have access to an agent in Looker (`can.show` / `can.chat`) can see full Golden Query and agent metadata in search results.
5. **Single Instance & Direct BigQuery SQL Consumers**:
   - Each BigQuery dataset/table pair (`ca_agents` and `ca_golden_queries`) is scoped to a **single Looker instance** and stores the global Admin-synced catalog without BigQuery Row-Level Security (RLS). Any downstream applications or analysts querying BigQuery directly via SQL (outside `ca-agent-sync search`) will see all synced agents unless they apply their own Looker allowlist filter (`WHERE agent_id IN UNNEST(@allowed_agent_ids)`).

#### Copy-Pasteable User-Scoped Search Examples

```bash
# 1. User-scoped two-step search via Admin sudo (--user-id / -u)
uv run ca-agent-sync search "What is the status and completion progress of all strategic projects?" \
  --user-id 42 \
  --golden-queries=5 \
  --confidence_score=0.65 \
  --top-k 3

# 2. User-scoped single-step search via direct user OAuth / session Bearer token (--access-token)
uv run ca-agent-sync search "What is our sprint velocity and SLA compliance rate for engineering issues?" \
  --access-token "eyJhbGciOi..." \
  --top-k 3 \
  --json

# 3. User-scoped search using the LOOKER_ACCESS_TOKEN environment variable
LOOKER_ACCESS_TOKEN="eyJhbGciOi..." uv run ca-agent-sync search \
  "What is the status and completion progress of all strategic projects?" \
  -g 5 -c 0.65 -k 3 --json
```

#### Sample `--json` Output (User-Scoped Two-Step Search with `has_inaccessible_source` Warning)

```json
{
  "status": "ok",
  "question": "What is the status and completion progress of all strategic projects?",
  "top_k": 3,
  "golden_queries_requested": 5,
  "confidence_score": 0.65,
  "stop_on_golden_match": false,
  "golden_query_matched": true,
  "stage2_filtered": true,
  "matched_agent_ids": [
    "agent-eng-001"
  ],
  "authz_mode": "user_id",
  "user_id": "42",
  "allowed_agent_count": 2,
  "golden_query_results": [
    {
      "rank": 1,
      "record_id": "agent-eng-001:1",
      "golden_query_id": "1",
      "agent_id": "agent-eng-001",
      "agent_name": "Engineering Intelligence Assistant",
      "agent_url": "https://your-instance.cloud.looker.com/extensions/conversational_analytics::conversational_analytics/agents/agent-eng-001",
      "instance_url": "https://your-instance.cloud.looker.com",
      "questions": [
        "What is the status and completion progress of all strategic projects?"
      ],
      "model": "engineering_analytics",
      "explore": "project",
      "fields": [
        "project.name",
        "team.name",
        "lead.name",
        "project_status.name",
        "project.state",
        "project_metrics_rollup.latest_health_status",
        "project.progress"
      ],
      "filters": {},
      "sorts": [
        "project.progress desc"
      ],
      "limit": null,
      "answer": null,
      "similarity_score": 0.841205,
      "distance": 0.158795,
      "embedding_source": "STORED",
      "has_inaccessible_source": true,
      "warning": "User lacks access to one or more LookML sources on this agent; some queries may fail."
    }
  ],
  "results": [
    {
      "rank": 1,
      "agent_id": "agent-eng-001",
      "name": "Engineering Intelligence Assistant",
      "description": "AI Conversational Analytics Assistant for engineering_analytics",
      "category": null,
      "similarity_score": 0.752104,
      "distance": 0.247896,
      "agent_url": "https://your-instance.cloud.looker.com/extensions/conversational_analytics::conversational_analytics/agents/agent-eng-001",
      "instance_url": "https://your-instance.cloud.looker.com",
      "explores": [
        "engineering_analytics.comment",
        "engineering_analytics.issue",
        "engineering_analytics.issue_history",
        "engineering_analytics.issue_labels",
        "engineering_analytics.project"
      ],
      "sources": [
        {
          "description": "Project roadmap and health status.",
          "explore": "project",
          "label": "Strategic Projects",
          "model": "engineering_analytics",
          "name": "project"
        }
      ],
      "embedding_source": "STORED",
      "has_inaccessible_source": true,
      "warning": "User lacks access to one or more LookML sources on this agent; some queries may fail."
    }
  ]
}
```

---

## 8. BigQuery Dual-Table Schemas & Direct SQL Recipes

### 8.1 Table Schemas (`ca_agents` & `ca_golden_queries`)

#### Table 1: `ca_agents` (`<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_agents`)

Stores one row per active Looker CA agent, keyed on `agent_id`:

```sql
CREATE TABLE IF NOT EXISTS `<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_agents` (
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
        connection_id => '<YOUR_GCP_PROJECT_ID>.us.ca_agent_embedding_conn',
        endpoint => 'text-embedding-005'
      )
    )
    STORED OPTIONS (asynchronous = TRUE)
);
```

##### Example `ca_agents.metadata_concat` Format

```text
Agent Name: Engineering Intelligence Assistant
Description: AI Conversational Analytics Assistant for engineering_analytics

Explores:
- Explore: engineering_analytics.issue | Label: Issues & Sprint Delivery | Description: Issue tracking, story points, cycle velocity, and SLA compliance.
- Explore: engineering_analytics.project | Label: Strategic Projects | Description: Project roadmap and health status.

Golden Queries:
- Questions: Show engineer workload and completed story points per developer | Explore: engineering_analytics.issue | Fields: assignee.name, team.name, issue.count, issue.total_story_points, issue.completed_story_points
- Questions: What is the status and completion progress of all strategic projects? | Explore: engineering_analytics.project | Fields: project.name, team.name, lead.name, project_status.name, project.state, project_metrics_rollup.latest_health_status, project.progress

System Instructions:
You are an expert Senior Engineering Intelligence & Delivery Analyst specializing in engineering operations...
```

#### Table 2: `ca_golden_queries` (`<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_golden_queries`)

Stores one row per active Looker Golden Query, keyed on `record_id = "{agent_id}:{golden_query_id}"` and mapped to `agent_id`:

```sql
CREATE TABLE IF NOT EXISTS `<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_golden_queries` (
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
        connection_id => '<YOUR_GCP_PROJECT_ID>.us.ca_agent_embedding_conn',
        endpoint => 'text-embedding-005'
      )
    )
    STORED OPTIONS (asynchronous = TRUE)
);
```

##### Example `ca_golden_queries.metadata_concat` Format

```text
Golden Query Questions: What is the status and completion progress of all strategic projects?
Agent Name: Engineering Intelligence Assistant
LookML Explore: engineering_analytics.project
Fields: project.name, team.name, lead.name, project_status.name, project.state, project_metrics_rollup.latest_health_status, project.progress
Sorts: project.progress desc
```

---

### 8.2 Stage 1 SQL: Search Curated Golden Queries (`ca_golden_queries`)

Downstream applications, orchestrator agents, or analysts can query the BigQuery tables directly using either **`AI.SEARCH`** (native autonomous embedding search) or **`AI.EMBED` + `ML.DISTANCE(..., 'COSINE')`**.

> [!WARNING]
> **Multi-Tenant Pre-Filtering for Direct SQL Consumers:** Because `ca_agents` and `ca_golden_queries` store the global Admin-synced agent catalog without BigQuery Row-Level Security (RLS), any direct SQL queries running on behalf of a specific Looker user **must** pass that user's accessible `agent_id` allowlist (`@allowed_agent_ids`) into `WHERE agent_id IN UNNEST(@allowed_agent_ids)` as shown below.

*(Note: On unit-normalized `text-embedding-005` embeddings, `AI.SEARCH` returns Euclidean distance $L_2$, which converts directly to cosine distance via $d_{\text{cosine}} = L_2^2 / 2$ and cosine similarity via $1 - L_2^2 / 2$.)*

#### Unscoped (Admin Mode)
```sql
SELECT
  base.record_id,
  base.golden_query_id,
  base.agent_id,
  base.agent_name,
  base.agent_url,
  base.questions,
  base.model,
  base.explore,
  base.fields,
  base.filters,
  base.sorts,
  (1.0 - POW(distance, 2) / 2.0) AS similarity_score,
  (POW(distance, 2) / 2.0) AS distance
FROM AI.SEARCH(
  TABLE `<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_golden_queries`,
  'metadata_concat',
  'What is the status and completion progress of all strategic projects?',
  top_k => 5
)
WHERE (1.0 - POW(distance, 2) / 2.0) >= 0.65
ORDER BY distance ASC;
```

#### User-Scoped (Pre-Filtered inside `AI.SEARCH` with `@allowed_agent_ids`)
```sql
SELECT
  base.record_id,
  base.golden_query_id,
  base.agent_id,
  base.agent_name,
  base.agent_url,
  base.questions,
  base.model,
  base.explore,
  base.fields,
  base.filters,
  base.sorts,
  (1.0 - POW(distance, 2) / 2.0) AS similarity_score,
  (POW(distance, 2) / 2.0) AS distance
FROM AI.SEARCH(
  (SELECT * FROM `<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_golden_queries` WHERE agent_id IN UNNEST(@allowed_agent_ids)),
  'metadata_concat',
  @query_text,
  top_k => @top_k
)
WHERE (1.0 - POW(distance, 2) / 2.0) >= @confidence_score
ORDER BY distance ASC;
```

---

### 8.3 Stage 2 SQL: Candidate-Filtered & User-Scoped Agent Search (`ca_agents`)

When Stage 1 returns $\ge 1$ Golden Queries above your confidence threshold (or when pre-filtering by a user's `@allowed_agent_ids`), pass the target `agent_id`s as `@matched_agent_ids` (`ARRAY<STRING>`) to filter `ca_agents`:

#### Option 1: Using `AI.SEARCH` Pre-Filtered with `WHERE agent_id IN UNNEST(@matched_agent_ids)`

```sql
SELECT
  base.agent_id,
  base.name,
  base.description,
  base.agent_url,
  base.sources,
  (1.0 - POW(distance, 2) / 2.0) AS similarity_score,
  (POW(distance, 2) / 2.0) AS distance
FROM AI.SEARCH(
  (SELECT * FROM `<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_agents` WHERE agent_id IN UNNEST(@matched_agent_ids)),
  'metadata_concat',
  @query_text,
  top_k => @top_k
)
ORDER BY distance ASC
LIMIT @top_k;
```

#### Option 2: Using `AI.EMBED` + `ML.DISTANCE(..., 'COSINE')` with `WHERE agent_id IN UNNEST(@matched_agent_ids)`

Works even while background autonomous embedding generation is still warming up for newly synced rows:

```sql
WITH query_emb AS (
  SELECT AI.EMBED(
    @query_text,
    connection_id => '<YOUR_GCP_PROJECT_ID>.us.ca_agent_embedding_conn',
    endpoint => 'text-embedding-005'
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
    t.sources,
    CASE
      WHEN t.embedding IS NOT NULL AND ARRAY_LENGTH(t.embedding.result) > 0 THEN t.embedding.result
      ELSE AI.EMBED(
        t.metadata_concat,
        connection_id => '<YOUR_GCP_PROJECT_ID>.us.ca_agent_embedding_conn',
        endpoint => 'text-embedding-005'
      ).result
    END AS agent_vec
  FROM `<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_agents` AS t
  WHERE agent_id IN UNNEST(@matched_agent_ids)
)
SELECT
  c.agent_id,
  c.name,
  c.description,
  c.agent_url,
  c.sources,
  ML.DISTANCE(c.agent_vec, q.vec, 'COSINE') AS distance,
  (1.0 - ML.DISTANCE(c.agent_vec, q.vec, 'COSINE')) AS similarity_score
FROM candidate_agents AS c
CROSS JOIN query_emb AS q
WHERE ARRAY_LENGTH(c.agent_vec) > 0
ORDER BY distance ASC
LIMIT @top_k;
```

---

### 8.4 Single-Step / Unfiltered Fallback SQL (`ca_agents`)

When running unscoped Admin single-step search or when 0 Golden Queries meet `confidence_score` in Stage 1 (in Admin mode):

```sql
SELECT
  base.agent_id,
  base.name,
  base.description,
  base.agent_url,
  base.sources,
  (1.0 - POW(distance, 2) / 2.0) AS similarity_score,
  (POW(distance, 2) / 2.0) AS distance
FROM AI.SEARCH(
  TABLE `<YOUR_GCP_PROJECT_ID>.looker_agent_registry.ca_agents`,
  'metadata_concat',
  'What is our sprint velocity and SLA compliance rate for engineering issues?',
  top_k => 3
)
ORDER BY distance ASC;
```

---

## 9. Google Cloud APIs, IAM Roles & Troubleshooting

### Required Google Cloud APIs

```bash
gcloud services enable \
  bigquery.googleapis.com \
  bigqueryconnection.googleapis.com \
  aiplatform.googleapis.com \
  cloudresourcemanager.googleapis.com \
  --project=<YOUR_GCP_PROJECT_ID>
```

### Required IAM Roles

#### 1. The Sync Runner (User or Service Account running `ca-agent-sync`)

| IAM Role | Purpose |
| :--- | :--- |
| **`roles/bigquery.dataEditor`** | Create the `looker_agent_registry` dataset, create the `ca_agents` and `ca_golden_queries` tables, and run `MERGE` / `DELETE` statements. |
| **`roles/bigquery.jobUser`** | Submit BigQuery DDL, DML (`MERGE` / `DELETE`), and `AI.SEARCH` / `AI.EMBED` query jobs. |
| **`roles/bigquery.connectionAdmin`** | Create or inspect the BigQuery Cloud Resource Connection (`ca_agent_embedding_conn`), or `roles/bigquery.connectionUser` if using a pre-created connection. |
| **`roles/resourcemanager.projectIamAdmin`** *(Optional)* | Only required if you want `ca-agent-sync init` to automatically bind `roles/aiplatform.user` to the BigQuery connection's service account. |

#### 2. The BigQuery Cloud Resource Connection Service Account (`bqcx-...`)

When a BigQuery Cloud Resource Connection is created, Google Cloud provisions a dedicated service account (`bqcx-<PROJECT_NUMBER>-<HASH>@gcp-sa-bigquery-condel.iam.gserviceaccount.com`). BigQuery uses this service account in the background to call Vertex AI's `text-embedding-005` model.

| IAM Role | Target Principal | Purpose |
| :--- | :--- | :--- |
| **`roles/aiplatform.user`** *(Vertex AI User)* | `serviceAccount:bqcx-...` (Connection Service Account) | Allows BigQuery's background autonomous embedding job (`ca_agents` and `ca_golden_queries`) and `AI.EMBED` / `AI.SEARCH` calls to invoke Vertex AI embedding endpoints. |

`uv run ca-agent-sync init` binds this role automatically, or an admin can grant it manually:

```bash
gcloud projects add-iam-policy-binding <YOUR_GCP_PROJECT_ID> \
  --member="serviceAccount:<CONNECTION_SERVICE_ACCOUNT_EMAIL>" \
  --role="roles/aiplatform.user"
```

---

## 10. Exit Codes & Error Handling

| Exit Code | Meaning | Example Cause |
| :---: | :--- | :--- |
| `0` | Success | Command completed normally. |
| `1` | Runtime / Infrastructure Error | Looker API authentication/sudo error, BigQuery permission error, or missing `embedding` column in `INFORMATION_SCHEMA.COLUMNS`. |
| `2` | CLI Usage / Validation Error | Passing both or neither of `--batch` and `--agent-id` to `sync`, passing both `--user-id` and `--access-token` to `search`, or passing `--confidence_score` outside `[0.0, 1.0]`. |
