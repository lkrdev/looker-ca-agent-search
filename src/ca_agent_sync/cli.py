"""Typer CLI entrypoint (`ca-agent-sync`) with `init`, `sync`, `status`, and `search` subcommands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Sequence

# Ensure mTLS client certificates are disabled prior to any GCP client creation
os.environ["CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE"] = "false"
os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"

import typer
from rich.console import Console
from rich.table import Table

from ca_agent_sync.bigquery_sync import INACCESSIBLE_SOURCE_WARNING, BigQueryAgentSync
from ca_agent_sync.config import SyncConfig
from ca_agent_sync.looker_extractor import LookerAgentExtractor
from ca_agent_sync.models import GoldenQuerySearchHit, SearchHit, SyncResult, UserAgentAccess

app = typer.Typer(
    name="ca-agent-sync",
    help=(
        "Synchronize Looker Conversational Analytics (CA) agent metadata and Golden Queries "
        "to BigQuery with Autonomous Embedding Generation (`AI.EMBED`) and Two-Step semantic vector search routing."
    ),
    no_args_is_help=True,
    add_completion=False,
)

err_console = Console(stderr=True)
out_console = Console(stderr=False)


def _emit_json(payload: object) -> None:
    """Write formatted JSON to stdout."""
    typer.echo(json.dumps(payload, indent=2, default=str))


def _render_agent_hits_table(
    question: str, hits: Sequence[SearchHit], title_prefix: str = ""
) -> None:
    """Render a Rich table of Stage 2 CA Agent search hits."""
    if not hits:
        out_console.print("[yellow]No matching CA agents found in BigQuery.[/yellow]")
        return

    tbl = Table(title=f'{title_prefix}Top {len(hits)} Matching Looker CA Agents for: "{question}"')
    tbl.add_column("Rank", justify="right", style="cyan")
    tbl.add_column("Similarity", justify="right", style="green")
    tbl.add_column("Agent Name & ID", style="bold")
    tbl.add_column("Category", style="magenta")
    tbl.add_column("Explores (`model.explore`)", style="yellow")
    tbl.add_column("Deep Link (`agent_url`)", style="blue")

    warnings_to_print: list[tuple[str, str, str]] = []
    for hit in hits:
        explores_str = ", ".join(hit.explores) if hit.explores else "N/A"
        if hit.has_inaccessible_source or hit.warning:
            warn_text = hit.warning or INACCESSIBLE_SOURCE_WARNING
            explores_str = f"{explores_str}\n[bold yellow]⚠ Warning:[/bold yellow] {warn_text}"
            warnings_to_print.append((hit.name, hit.agent_id, warn_text))
        tbl.add_row(
            str(hit.rank),
            f"{hit.similarity_score:.4f}",
            f"{hit.name}\n({hit.agent_id})",
            hit.category or "Uncategorized",
            explores_str,
            hit.agent_url,
        )

    out_console.print(tbl)
    for name, agent_id, warn_text in warnings_to_print:
        out_console.print(f"[bold yellow]⚠ Warning ({name} / {agent_id}):[/bold yellow] {warn_text}")


def _render_golden_query_hits_table(
    hits: Sequence[GoldenQuerySearchHit],
    confidence_score: float,
) -> None:
    """Render a Rich table of Stage 1 matched Golden Queries."""
    tbl = Table(
        title=(
            f"Stage 1: {len(hits)} Matched Golden Queries "
            f"(similarity_score >= {confidence_score:.2f})"
        )
    )
    tbl.add_column("Rank", justify="right", style="cyan")
    tbl.add_column("Similarity", justify="right", style="green")
    tbl.add_column("Golden Query (`record_id`)", style="bold")
    tbl.add_column("Questions", style="white")
    tbl.add_column("Explore (`model.explore`)", style="yellow")
    tbl.add_column("Agent (`agent_name` & `agent_id`)", style="magenta")

    warnings_to_print: list[tuple[str, str, str]] = []
    for gq in hits:
        explore_str = (
            f"{gq.model}.{gq.explore}"
            if (gq.model and gq.explore)
            else (gq.model or gq.explore or "N/A")
        )
        if gq.has_inaccessible_source or gq.warning:
            warn_text = gq.warning or INACCESSIBLE_SOURCE_WARNING
            explore_str = f"{explore_str}\n[bold yellow]⚠ Warning:[/bold yellow] {warn_text}"
            warnings_to_print.append((gq.agent_name, gq.agent_id, warn_text))
        questions_str = "; ".join(gq.questions) if gq.questions else "N/A"
        tbl.add_row(
            str(gq.rank),
            f"{gq.similarity_score:.4f}",
            gq.record_id,
            questions_str,
            explore_str,
            f"{gq.agent_name}\n({gq.agent_id})",
        )

    out_console.print(tbl)
    for agent_name, agent_id, warn_text in warnings_to_print:
        out_console.print(
            f"[bold yellow]⚠ Warning ({agent_name} / {agent_id}):[/bold yellow] {warn_text}"
        )


@app.command("init")
def init_cmd(
    project_id: Optional[str] = typer.Option(
        None, "--project-id", "--project", help="Google Cloud project ID."
    ),
    location: Optional[str] = typer.Option(
        None, "--location", help="BigQuery dataset/connection location (default: US)."
    ),
    dataset: Optional[str] = typer.Option(
        None, "--dataset", help="BigQuery dataset name (default: looker_agent_registry)."
    ),
    table: Optional[str] = typer.Option(
        None, "--table", help="BigQuery agents table name (default: ca_agents)."
    ),
    golden_queries_table: Optional[str] = typer.Option(
        None,
        "--golden-queries-table",
        help="BigQuery Golden Queries table name (default: ca_golden_queries).",
    ),
    connection_id: Optional[str] = typer.Option(
        None,
        "--connection-id",
        "--connection",
        help="BigQuery Cloud Resource connection ID (default: ca_agent_embedding_conn).",
    ),
    endpoint: Optional[str] = typer.Option(
        None, "--endpoint", help="Vertex AI embedding endpoint (default: text-embedding-005)."
    ),
    skip_warmup: bool = typer.Option(
        False,
        "--skip-warmup",
        help="Skip the synchronous AI.EMBED warmup check during initialization.",
    ),
    env_file: Optional[Path] = typer.Option(None, "--env-file", help="Optional path to .env file."),
    json_output: bool = typer.Option(False, "--json", help="Output structured JSON to stdout."),
) -> None:
    """Provision and verify BigQuery dataset, Cloud Resource Connection + IAM, and `ca_agents` & `ca_golden_queries` tables."""
    cfg = SyncConfig.from_env(
        env_file=env_file,
        project_id=project_id,
        location=location,
        dataset=dataset,
        table=table,
        golden_queries_table=golden_queries_table,
        connection_id=connection_id,
        endpoint=endpoint,
    )
    bq_sync = BigQueryAgentSync(config=cfg)
    try:
        result = bq_sync.init_infrastructure(run_warmup=not skip_warmup)
    except Exception as exc:
        if json_output:
            _emit_json({"status": "error", "error": str(exc)})
        else:
            err_console.print(f"[bold red]Initialization failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        _emit_json({"status": "ok", **result})
        return

    out_console.print("[bold green]BigQuery Autonomous Embedding Infrastructure Ready[/bold green]")
    out_console.print(f"  • Dataset: [cyan]{result['dataset_id']}[/cyan] ({result['location']})")
    conn = result["connection"]
    out_console.print(f"  • Connection: [cyan]{conn['connection_id']}[/cyan]")
    if conn.get("service_account_id"):
        out_console.print(
            f"  • Connection Service Account: [cyan]{conn['service_account_id']}[/cyan]"
        )
    out_console.print(
        f"  • IAM Role ({conn['iam_role']}): [green]{'Granted' if conn['iam_bound'] else 'Checked'}[/green]"
    )
    tbl = result["table"]
    out_console.print(f"  • Agents Table: [cyan]{tbl['table_id']}[/cyan]")
    out_console.print(
        f"  • Autonomous Column `{tbl['column_name']}`: is_generated=[bold]{tbl['is_generated']}[/bold], "
        f"is_stored=[bold]{tbl['is_stored']}[/bold]"
    )
    out_console.print(f"  • Expression: [dim]{tbl['generation_expression']}[/dim]")

    gq_tbl = result.get("golden_queries_table")
    if isinstance(gq_tbl, dict):
        out_console.print(f"  • Golden Queries Table: [cyan]{gq_tbl['table_id']}[/cyan]")
        out_console.print(
            f"  • Golden Queries Autonomous Column `{gq_tbl['column_name']}`: "
            f"is_generated=[bold]{gq_tbl['is_generated']}[/bold], "
            f"is_stored=[bold]{gq_tbl['is_stored']}[/bold]"
        )
        out_console.print(
            f"  • Golden Queries Expression: [dim]{gq_tbl['generation_expression']}[/dim]"
        )


@app.command("sync")
def sync_cmd(
    batch: bool = typer.Option(
        False,
        "--batch",
        help="Sync all active CA agents and their Golden Queries from Looker (`search_agents(deleted=False)`).",
    ),
    agent_id: Optional[str] = typer.Option(
        None,
        "--agent-id",
        help="Sync and overwrite a single CA agent and its Golden Queries by ID (`get_agent(agent_id)`).",
    ),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="Delete BigQuery rows that no longer exist in Looker during batch sync.",
    ),
    no_explore_headers: bool = typer.Option(
        False,
        "--no-explore-headers",
        help="Skip fetching lightweight LookML Explore headers (`name,label,description`).",
    ),
    skip_init: bool = typer.Option(
        False,
        "--skip-init",
        help="Skip ensuring BigQuery dataset/connection/tables before syncing.",
    ),
    project_id: Optional[str] = typer.Option(
        None, "--project-id", "--project", help="Google Cloud project ID."
    ),
    location: Optional[str] = typer.Option(
        None, "--location", help="BigQuery dataset/connection location (default: US)."
    ),
    dataset: Optional[str] = typer.Option(
        None, "--dataset", help="BigQuery dataset name (default: looker_agent_registry)."
    ),
    table: Optional[str] = typer.Option(
        None, "--table", help="BigQuery agents table name (default: ca_agents)."
    ),
    golden_queries_table: Optional[str] = typer.Option(
        None,
        "--golden-queries-table",
        help="BigQuery Golden Queries table name (default: ca_golden_queries).",
    ),
    connection_id: Optional[str] = typer.Option(
        None,
        "--connection-id",
        "--connection",
        help="BigQuery Cloud Resource connection ID (default: ca_agent_embedding_conn).",
    ),
    endpoint: Optional[str] = typer.Option(
        None, "--endpoint", help="Vertex AI embedding endpoint (default: text-embedding-005)."
    ),
    env_file: Optional[Path] = typer.Option(None, "--env-file", help="Optional path to .env file."),
    json_output: bool = typer.Option(False, "--json", help="Output structured JSON to stdout."),
) -> None:
    """Extract CA agent & Golden Query metadata from Looker and atomic-MERGE into `ca_agents` and `ca_golden_queries`."""
    if batch and agent_id:
        err_console.print(
            "[bold red]Error:[/bold red] Specify either `--batch` or `--agent-id <ID>`, not both."
        )
        raise typer.Exit(code=2)
    if not batch and not agent_id:
        err_console.print(
            "[bold red]Error:[/bold red] Must specify either `--batch` or `--agent-id <ID>`."
        )
        raise typer.Exit(code=2)

    cfg = SyncConfig.from_env(
        env_file=env_file,
        project_id=project_id,
        location=location,
        dataset=dataset,
        table=table,
        golden_queries_table=golden_queries_table,
        connection_id=connection_id,
        endpoint=endpoint,
    )
    extractor = LookerAgentExtractor(config=cfg)
    bq_sync = BigQueryAgentSync(config=cfg)
    include_headers = not no_explore_headers

    try:
        if not skip_init:
            bq_sync.init_infrastructure(run_warmup=False)

        if agent_id:
            record, should_delete = extractor.extract_single(
                agent_id=agent_id,
                include_explore_headers=include_headers,
            )
            if should_delete or record is None:
                deleted_rows = bq_sync.delete_agent(agent_id.strip())
                sync_res = SyncResult(
                    mode="single",
                    table_id=cfg.full_table_id,
                    synced_count=0,
                    deleted_count=deleted_rows,
                    explore_headers_cached=len(extractor.explore_cache),
                    deleted_agent_ids=[agent_id.strip()],
                    golden_queries_table_id=cfg.full_golden_queries_table_id,
                    golden_queries_synced_count=0,
                )
            else:
                sync_res = bq_sync.upsert_agents(
                    records=[record],
                    prune=False,
                    mode="single",
                    explore_headers_cached=len(extractor.explore_cache),
                )
        else:
            records = extractor.extract_batch(include_explore_headers=include_headers)
            sync_res = bq_sync.upsert_agents(
                records=records,
                prune=prune,
                mode="batch",
                explore_headers_cached=len(extractor.explore_cache),
            )
    except Exception as exc:
        if json_output:
            _emit_json({"status": "error", "error": str(exc)})
        else:
            err_console.print(f"[bold red]Sync failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        _emit_json({"status": "ok", **sync_res.model_dump()})
        return

    out_console.print(
        f"[bold green]Sync Complete ({sync_res.mode} mode)[/bold green] → "
        f"[cyan]{sync_res.table_id}[/cyan] & [cyan]{sync_res.golden_queries_table_id or cfg.full_golden_queries_table_id}[/cyan]"
    )
    out_console.print(f"  • Agents upserted: [bold]{sync_res.synced_count}[/bold]")
    out_console.print(
        f"  • Golden Queries upserted: [bold]{sync_res.golden_queries_synced_count}[/bold]"
    )
    if sync_res.golden_queries_deleted_count > 0:
        out_console.print(
            f"  • Unlinked Golden Queries deleted: [yellow]{sync_res.golden_queries_deleted_count}[/yellow]"
        )
    out_console.print(
        f"  • Lightweight Explore headers cached: [bold]{sync_res.explore_headers_cached}[/bold]"
    )
    if sync_res.deleted_count > 0 or sync_res.deleted_agent_ids:
        out_console.print(
            f"  • Deleted inactive/missing agent rows: [yellow]{sync_res.deleted_count}[/yellow] "
            f"({', '.join(sync_res.deleted_agent_ids)})"
        )
    if prune:
        out_console.print(f"  • Pruned stale agent rows: [yellow]{sync_res.pruned_count}[/yellow]")


@app.command("status")
def status_cmd(
    project_id: Optional[str] = typer.Option(
        None, "--project-id", "--project", help="Google Cloud project ID."
    ),
    location: Optional[str] = typer.Option(
        None, "--location", help="BigQuery dataset/connection location (default: US)."
    ),
    dataset: Optional[str] = typer.Option(
        None, "--dataset", help="BigQuery dataset name (default: looker_agent_registry)."
    ),
    table: Optional[str] = typer.Option(
        None, "--table", help="BigQuery agents table name (default: ca_agents)."
    ),
    golden_queries_table: Optional[str] = typer.Option(
        None,
        "--golden-queries-table",
        help="BigQuery Golden Queries table name (default: ca_golden_queries).",
    ),
    env_file: Optional[Path] = typer.Option(None, "--env-file", help="Optional path to .env file."),
    json_output: bool = typer.Option(False, "--json", help="Output structured JSON to stdout."),
) -> None:
    """Report BigQuery row counts and autonomous embedding generation status for `ca_agents` and `ca_golden_queries`."""
    cfg = SyncConfig.from_env(
        env_file=env_file,
        project_id=project_id,
        location=location,
        dataset=dataset,
        table=table,
        golden_queries_table=golden_queries_table,
    )
    bq_sync = BigQueryAgentSync(config=cfg)
    try:
        report = bq_sync.get_status()
    except Exception as exc:
        if json_output:
            _emit_json({"status": "error", "error": str(exc)})
        else:
            err_console.print(f"[bold red]Status check failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        _emit_json({"status": "ok", **report.model_dump()})
        return

    tbl = Table(title=f"Autonomous Embedding Status (Agents): {report.table_id}")
    tbl.add_column("Metric", style="cyan")
    tbl.add_column("Value", style="bold")
    tbl.add_row("Total Agent Rows", str(report.total_rows))
    tbl.add_row("Populated Embeddings (READY)", f"[green]{report.populated_embeddings}[/green]")
    tbl.add_row("Pending Background Generation", f"[yellow]{report.pending_embeddings}[/yellow]")
    tbl.add_row(
        "Embedding Errors",
        f"[red]{report.error_embeddings}[/red]" if report.error_embeddings else "0",
    )
    tbl.add_row("Last Synced At", report.last_synced_at or "N/A")
    tbl.add_row("Column is_generated", report.is_generated or "N/A")
    tbl.add_row("Async Generation Status", str(report.async_generation_status or "OK"))
    out_console.print(tbl)

    if report.errors:
        out_console.print("\n[bold red]Agent Row-Level Embedding Errors:[/bold red]")
        for err in report.errors:
            out_console.print(f"  • {err.get('agent_id')} ({err.get('name')}): {err.get('status')}")

    gq_table_id = report.golden_queries_table_id or cfg.full_golden_queries_table_id
    gq_tbl = Table(title=f"Autonomous Embedding Status (Golden Queries): {gq_table_id}")
    gq_tbl.add_column("Metric", style="cyan")
    gq_tbl.add_column("Value", style="bold")
    gq_tbl.add_row("Total Golden Query Rows", str(report.golden_queries_total_rows))
    gq_tbl.add_row(
        "Populated Embeddings (READY)",
        f"[green]{report.golden_queries_populated_embeddings}[/green]",
    )
    gq_tbl.add_row(
        "Pending Background Generation",
        f"[yellow]{report.golden_queries_pending_embeddings}[/yellow]",
    )
    gq_tbl.add_row(
        "Embedding Errors",
        f"[red]{report.golden_queries_error_embeddings}[/red]"
        if report.golden_queries_error_embeddings
        else "0",
    )
    gq_tbl.add_row("Last Synced At", report.golden_queries_last_synced_at or "N/A")
    gq_tbl.add_row("Column is_generated", report.golden_queries_is_generated or "N/A")
    gq_tbl.add_row(
        "Async Generation Status",
        str(report.golden_queries_async_generation_status or "OK"),
    )
    out_console.print()
    out_console.print(gq_tbl)

    if report.golden_queries_errors:
        out_console.print("\n[bold red]Golden Query Row-Level Embedding Errors:[/bold red]")
        for err in report.golden_queries_errors:
            out_console.print(
                f"  • {err.get('record_id')} ({err.get('agent_name')}): {err.get('status')}"
            )


@app.command("search")
def search_cmd(
    question: str = typer.Argument(
        ..., help="Natural language question to route to the best Looker CA agent."
    ),
    top_k: int = typer.Option(
        5, "--top-k", "-k", min=1, max=100, help="Number of top matching agents to return."
    ),
    golden_queries: Optional[int] = typer.Option(
        None,
        "--golden-queries",
        "-g",
        min=1,
        max=100,
        help=(
            "Stage 1: Number of Golden Query embeddings to search in `ca_golden_queries` "
            "before Stage 2 agent search."
        ),
    ),
    confidence_score: Optional[float] = typer.Option(
        None,
        "--confidence_score",
        "--confidence-score",
        "-c",
        help=(
            "Minimum cosine similarity score in [0.0, 1.0] for Stage 1 Golden Query matches "
            "(defaults to 0.60 when --golden-queries is specified)."
        ),
    ),
    stop_on_golden_match: bool = typer.Option(
        False,
        "--stop-on-golden-match",
        help=(
            "Stop after Stage 1 and return matched Golden Queries without querying `ca_agents` "
            "if >= 1 Golden Queries meet confidence_score."
        ),
    ),
    user_id: Optional[str] = typer.Option(
        None,
        "--user-id",
        "-u",
        help="Looker user ID to sudo as (via Admin credentials) so only agents accessible to that user are searched.",
    ),
    access_token: Optional[str] = typer.Option(
        None,
        "--access-token",
        envvar="LOOKER_ACCESS_TOKEN",
        help="Looker user access token (OAuth or session token) to fetch only agents accessible to that user.",
    ),
    project_id: Optional[str] = typer.Option(
        None, "--project-id", "--project", help="Google Cloud project ID."
    ),
    location: Optional[str] = typer.Option(
        None, "--location", help="BigQuery dataset/connection location (default: US)."
    ),
    dataset: Optional[str] = typer.Option(
        None, "--dataset", help="BigQuery dataset name (default: looker_agent_registry)."
    ),
    table: Optional[str] = typer.Option(
        None, "--table", help="BigQuery agents table name (default: ca_agents)."
    ),
    golden_queries_table: Optional[str] = typer.Option(
        None,
        "--golden-queries-table",
        help="BigQuery Golden Queries table name (default: ca_golden_queries).",
    ),
    connection_id: Optional[str] = typer.Option(
        None,
        "--connection-id",
        "--connection",
        help="BigQuery Cloud Resource connection ID (default: ca_agent_embedding_conn).",
    ),
    endpoint: Optional[str] = typer.Option(
        None, "--endpoint", help="Vertex AI embedding endpoint (default: text-embedding-005)."
    ),
    env_file: Optional[Path] = typer.Option(None, "--env-file", help="Optional path to .env file."),
    json_output: bool = typer.Option(False, "--json", help="Output structured JSON to stdout."),
) -> None:
    """Route a natural language question to top-K matching CA agents using single-step or two-step semantic vector search."""
    if confidence_score is not None and not (0.0 <= confidence_score <= 1.0):
        msg = "Error: --confidence_score / --confidence-score must be between 0.0 and 1.0."
        if json_output:
            _emit_json({"status": "error", "error": msg})
        else:
            err_console.print(f"[bold red]{msg}[/bold red]")
        raise typer.Exit(code=2)

    cleaned_user_id = user_id.strip() if user_id and user_id.strip() else None
    cleaned_access_token = (
        access_token.strip() if access_token and access_token.strip() else None
    )
    if cleaned_user_id is not None and cleaned_access_token is not None:
        msg = "Error: Specify either --user-id / -u or --access-token, not both."
        if json_output:
            _emit_json({"status": "error", "error": msg})
        else:
            err_console.print(f"[bold red]{msg}[/bold red]")
        raise typer.Exit(code=2)

    cfg = SyncConfig.from_env(
        env_file=env_file,
        project_id=project_id,
        location=location,
        dataset=dataset,
        table=table,
        golden_queries_table=golden_queries_table,
        connection_id=connection_id,
        endpoint=endpoint,
    )

    user_access: Optional[UserAgentAccess] = None
    if cleaned_user_id is not None or cleaned_access_token is not None:
        extractor = LookerAgentExtractor(config=cfg)
        try:
            user_access = extractor.fetch_user_accessible_agents(
                user_id=cleaned_user_id,
                access_token=cleaned_access_token,
            )
        except Exception as exc:
            if json_output:
                _emit_json({"status": "error", "error": str(exc)})
            else:
                err_console.print(f"[bold red]User access check failed:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc

    bq_sync = BigQueryAgentSync(config=cfg)

    if golden_queries is not None:
        effective_confidence = 0.60 if confidence_score is None else float(confidence_score)
        try:
            two_step_res = bq_sync.search_two_step(
                question=question,
                top_k=top_k,
                golden_queries_top_k=golden_queries,
                confidence_score=effective_confidence,
                stop_on_golden_match=stop_on_golden_match,
                allowed_agent_ids=user_access.allowed_agent_ids if user_access else None,
                inaccessible_source_agent_ids=(
                    user_access.inaccessible_source_agent_ids if user_access else None
                ),
                authz_mode=user_access.mode if user_access else "admin",
                user_id=user_access.user_id if user_access else None,
            )
        except Exception as exc:
            if json_output:
                _emit_json({"status": "error", "error": str(exc)})
            else:
                err_console.print(f"[bold red]Search failed:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc

        if json_output:
            _emit_json(
                {
                    "status": "ok",
                    **two_step_res.model_dump(),
                }
            )
            return

        if user_access is not None and len(user_access.allowed_agent_ids) == 0:
            out_console.print(
                "[yellow]User has access to 0 CA agents in Looker; skipping BigQuery search.[/yellow]"
            )
            return

        if two_step_res.golden_query_results:
            _render_golden_query_hits_table(
                two_step_res.golden_query_results,
                effective_confidence,
            )
            if two_step_res.stop_on_golden_match:
                out_console.print(
                    "[bold green]Stopped after Stage 1 (--stop-on-golden-match).[/bold green]"
                )
                return
            out_console.print(
                f"\n[cyan]Stage 2: Filtering `ca_agents` strictly to matched agent_id(s): "
                f"{', '.join(two_step_res.matched_agent_ids)}[/cyan]"
            )
            _render_agent_hits_table(
                question,
                two_step_res.results,
                title_prefix="Stage 2 (Candidate-Filtered): ",
            )
        else:
            out_console.print(
                f"[yellow]Stage 1: 0 Golden Queries met confidence_score >= {effective_confidence:.2f}; "
                "proceeding to unfiltered Stage 2 Agent search.[/yellow]"
            )
            _render_agent_hits_table(
                question,
                two_step_res.results,
                title_prefix="Stage 2 (Unfiltered Fallback): ",
            )
        return

    try:
        hits = bq_sync.search_agents(
            question=question,
            top_k=top_k,
            allowed_agent_ids=user_access.allowed_agent_ids if user_access else None,
            inaccessible_source_agent_ids=(
                user_access.inaccessible_source_agent_ids if user_access else None
            ),
        )
    except Exception as exc:
        if json_output:
            _emit_json({"status": "error", "error": str(exc)})
        else:
            err_console.print(f"[bold red]Search failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        _emit_json(
            {
                "status": "ok",
                "question": question,
                "top_k": top_k,
                "authz_mode": user_access.mode if user_access else "admin",
                "user_id": user_access.user_id if user_access else None,
                "allowed_agent_count": len(user_access.allowed_agent_ids) if user_access else None,
                "results": [h.model_dump() for h in hits],
            }
        )
        return

    if user_access is not None and len(user_access.allowed_agent_ids) == 0:
        out_console.print(
            "[yellow]User has access to 0 CA agents in Looker; skipping BigQuery search.[/yellow]"
        )
        return

    _render_agent_hits_table(question, hits)


if __name__ == "__main__":
    app()

