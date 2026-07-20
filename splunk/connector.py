"""
Facade wrapping the Splunk investigation engine — loading data, running
detectors, driving the standalone agent loop, and persisting run state.

All callers (MCP tools in mcp_server.py, the TUI in tui.py, runner.py's CLI,
and this module's own CLI) go through here instead of talking to each other
over HTTP. There is no server process anymore.

Heavy per-run objects (the events DataFrame, the findings dict) are cached
in-process in `_sessions`, keyed by run_id — fast for the common case where
the same process drives an entire investigation (e.g. one MCP server session).
Lightweight fields (iteration, confidence, event count, pause flag, hint) are
mirrored to splunk.db's `active_runs` table on every update, so any other
process (the TUI, or a later CLI invocation) can observe live progress.

For a *different* process to resume a run mid-loop (e.g. `submit-report` in a
fresh CLI invocation after `start` in another), findings are also persisted
as JSON on the active_runs row and events are persisted via the existing
events table (store_events/query_events) — `_get_or_rehydrate_session`
reconstructs an in-process session from those on first touch.

CLI (replaces the old curl-based REST fallback):
    uv run python -m splunk.connector start --source results/x.json
    uv run python -m splunk.connector submit-report --run-id <id> --report "..." --queries "..." "..."
    uv run python -m splunk.connector get-findings --run-id <id>
    uv run python -m splunk.connector pause --run-id <id>
    uv run python -m splunk.connector resume --run-id <id>
    uv run python -m splunk.connector hint --run-id <id> --text "..."
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

import polars as pl

from splunk.config import INVESTIGATOR_MAX_ITER
from splunk.db import (
    clear_active_run_row,
    get_active_run_row,
    init_db,
    pop_hint,
    query_events,
    store_events,
    store_queries,
    store_report,
    upsert_active_run,
)
from splunk.investigator import (
    _build_findings,
    _confidence_high,
    _execute_queries,
    _prepare_df,
)
from splunk.logger import RunLogger
from splunk.parsers import parse_splunk_csv, parse_splunk_json

logger = logging.getLogger(__name__)

_sessions: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Loaders — moved from runner.py so the MCP path, the CLI, and runner.py all
# share one copy instead of duplicating parse-source-into-DataFrame logic.
# ---------------------------------------------------------------------------

def _load_from_file(path: str) -> pl.DataFrame:
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text()
    stripped = raw.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return parse_splunk_json(raw)
    return parse_splunk_csv(raw)


def _load_from_live(spl: str, earliest: str = "-24h", latest: str = "now") -> pl.DataFrame:
    from splunk.client import run_query
    return pl.DataFrame(run_query(spl, earliest=earliest, latest=latest))


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _get_or_rehydrate_session(run_id: str) -> dict[str, Any] | None:
    """Return the in-process session for run_id, reconstructing it from
    splunk.db if this process didn't start the run (e.g. a fresh CLI call)."""
    session = _sessions.get(run_id)
    if session is not None:
        return session

    row = get_active_run_row(run_id)
    if row is None or not row.get("findings_json"):
        return None

    df = _prepare_df(query_events(run_id))
    session = {
        "df": df,
        "findings": json.loads(row["findings_json"]),
        "iteration": row.get("iteration", 0),
        "confidence": row.get("confidence", "—"),
        "source": row.get("source", ""),
        "repo_path": "",  # only ever available in the process that started the run
    }
    _sessions[run_id] = session
    return session


def get_session(run_id: str) -> dict[str, Any] | None:
    """Public accessor for callers (e.g. splunk__lsp_call_chain) that need
    same-process-only fields like repo_path."""
    return _get_or_rehydrate_session(run_id)


# ---------------------------------------------------------------------------
# Core operations — one MCP-driven investigation step each
# ---------------------------------------------------------------------------

def start_investigation(
    source: str = "",
    spl: str = "",
    earliest: str = "-24h",
    latest: str = "now",
    repo_path: str = "",
) -> dict[str, Any]:
    if not source and not spl:
        return {"error": "Provide 'source' (file path) or 'spl' (live SPL query)"}

    run_id = str(uuid.uuid4())
    try:
        if source:
            df = _load_from_file(source)
            source_label = source
        else:
            df = _load_from_live(spl, earliest, latest)
            source_label = f"live: {spl[:60]}"

        init_db()
        df = _prepare_df(df)
        findings = _build_findings(df)

        _sessions[run_id] = {
            "df": df,
            "findings": findings,
            "repo_path": repo_path,
            "iteration": 0,
            "confidence": "—",
            "source": source_label,
        }
        store_events(df, run_id)
        upsert_active_run(
            run_id,
            source=source_label,
            iteration=0,
            confidence="—",
            events=df.height,
            findings_json=json.dumps(findings, default=str),
        )

        result: dict[str, Any] = {
            "run_id": run_id,
            "source": source_label,
            "event_count": findings["event_count"],
            "findings": json.loads(json.dumps(findings, default=str)),
            "ui_url": f"Run: uv run python -m splunk.tui  (select run {run_id[:8]})",
            "next": "Reason over these findings and call splunk__submit_report with your report and follow-up SPL queries.",
        }
        if repo_path:
            result["repo_path"] = repo_path
            result["code_context"] = "splunk__lsp_call_chain is available — use it to trace error log sites back through the call graph before writing follow-up queries."

        with RunLogger(run_id) as log:
            log.info("investigate.start", source=source_label, event_count=findings["event_count"], repo_path=repo_path or None)
        return result

    except Exception as exc:
        with RunLogger(run_id) as log:
            log.error("investigate.start_failed", error=str(exc))
        return {"error": str(exc)}


def submit_report(run_id: str, report: str, queries: list[str] | None = None) -> dict[str, Any]:
    queries = queries or []
    session = _get_or_rehydrate_session(run_id)
    if session is None:
        return {"error": f"run_id {run_id!r} not found in active session"}

    iteration = session.get("iteration", 0) + 1
    session["iteration"] = iteration
    confidence = "High" if _confidence_high(report) else "Medium"
    session["confidence"] = confidence

    store_report(report, run_id, session.get("source", ""))
    if queries:
        store_queries(run_id, iteration, queries)

    events = session["df"].height if session.get("df") is not None else 0
    upsert_active_run(run_id, iteration=iteration, confidence=confidence, events=events)

    with RunLogger(run_id) as log:
        log.info("submit_report.iteration", iteration=iteration, confidence=confidence, queries=len(queries))

    ui_url = f"Run: uv run python -m splunk.tui  (select run {run_id[:8]})"

    if _confidence_high(report) or iteration >= INVESTIGATOR_MAX_ITER or not queries:
        clear_active_run_row(run_id)
        _sessions.pop(run_id, None)
        with RunLogger(run_id) as log:
            log.info("investigate.done", confidence=confidence, iterations=iteration)
        return {
            "status": "done",
            "run_id": run_id,
            "confidence": confidence,
            "iterations": iteration,
            "ui_url": ui_url,
        }

    new_df = _execute_queries(queries)
    if new_df is None or new_df.height == 0:
        clear_active_run_row(run_id)
        _sessions.pop(run_id, None)
        with RunLogger(run_id) as log:
            log.info("investigate.done", confidence=confidence, iterations=iteration, reason="no new events from follow-up queries")
        return {
            "status": "done",
            "run_id": run_id,
            "confidence": confidence,
            "iterations": iteration,
            "reason": "no new events from follow-up queries",
            "ui_url": ui_url,
        }

    new_df = _prepare_df(new_df)
    df = pl.concat([session["df"], new_df], how="diagonal")
    session["df"] = df
    findings = _build_findings(df)
    session["findings"] = findings
    store_events(new_df, run_id)
    upsert_active_run(run_id, events=df.height, findings_json=json.dumps(findings, default=str))

    return {
        "status": "continue",
        "run_id": run_id,
        "iteration": iteration,
        "confidence": confidence,
        "event_count": findings["event_count"],
        "findings": json.loads(json.dumps(findings, default=str)),
        "next": "Reason over these findings and call splunk__submit_report again with your updated report and next follow-up queries.",
    }


def get_findings(run_id: str) -> dict[str, Any]:
    session = _get_or_rehydrate_session(run_id)
    if session is None:
        return {"error": f"run_id {run_id!r} not active"}
    findings = session.get("findings")
    if findings is None:
        return {"error": "No findings yet for this run"}
    return {
        "run_id": run_id,
        "iteration": session.get("iteration", 0),
        "confidence": session.get("confidence", "—"),
        "findings": json.loads(json.dumps(findings, default=str)),
    }


def request_pause(run_id: str) -> dict[str, Any]:
    if _sessions.get(run_id) is None and get_active_run_row(run_id) is None:
        return {"error": f"run_id {run_id!r} not active"}
    upsert_active_run(run_id, pause_requested=1)
    with RunLogger(run_id) as log:
        log.info("pause.requested")
    return {"status": "paused", "run_id": run_id}


def resume(run_id: str) -> dict[str, Any]:
    if _sessions.get(run_id) is None and get_active_run_row(run_id) is None:
        return {"error": f"run_id {run_id!r} not active"}
    upsert_active_run(run_id, pause_requested=0)
    with RunLogger(run_id) as log:
        log.info("resume.requested")
    return {"status": "resumed", "run_id": run_id}


def set_hint(run_id: str, hint: str) -> dict[str, Any]:
    if _sessions.get(run_id) is None and get_active_run_row(run_id) is None:
        return {"error": f"run_id {run_id!r} not active"}
    upsert_active_run(run_id, hint=hint)
    with RunLogger(run_id) as log:
        log.info("hint.set", hint=hint)
    return {"status": "hint set", "run_id": run_id, "hint": hint}


# ---------------------------------------------------------------------------
# Standalone agent loop — replaces investigator.investigate()/investigate_task()
# ---------------------------------------------------------------------------

def run_standalone_agent(df: pl.DataFrame, run_id: str, source: str = "") -> tuple[str, list[str]]:
    """LangGraph/Ollama ReAct loop over detector findings. Used by
    `python -m splunk --investigate` and this module's CLI `start` when no
    MCP-driven agent is reasoning over the findings instead.

    Unifies the old sync investigate() (runner.py CLI path — no pause gate)
    and async investigate_task() (server background task — had a pause gate
    investigate() lacked). This version polls pause_requested via the DB
    before every iteration, so both call sites now get the same behavior.

    Note: unlike submit_report(), this never called store_report() until now
    — a pre-existing gap (true of the old investigate()/investigate_task()
    too, confirmed via git history) that meant standalone-agent-driven runs
    never showed up in run history / the reports table. Fixed here since the
    TUI's launch flow depends on being able to see the run afterward."""
    import time

    from splunk.agent import analyse

    df = _prepare_df(df)
    upsert_active_run(run_id, source=source, iteration=0, confidence="—", events=df.height)
    all_queries: list[str] = []
    report = ""

    with RunLogger(run_id) as log:
        log.info("investigate.start", source="standalone-agent", event_count=df.height)

        for iteration in range(1, INVESTIGATOR_MAX_ITER + 1):
            row = get_active_run_row(run_id)
            while row and row.get("pause_requested"):
                log.debug("agent.paused")
                time.sleep(1)
                row = get_active_run_row(run_id)

            logger.info("run_standalone_agent: iteration %d/%d — %d events", iteration, INVESTIGATOR_MAX_ITER, df.height)
            findings = _build_findings(df)

            hint = pop_hint(run_id)
            if hint:
                findings["analyst_hint"] = hint
                log.info("hint.injected", hint=hint)

            report, queries = analyse(findings)
            all_queries.extend(queries)

            confidence = "High" if _confidence_high(report) else "Medium"
            upsert_active_run(run_id, iteration=iteration, confidence=confidence, events=df.height)
            log.info("agent.iteration", iteration=iteration, confidence=confidence, queries=len(queries), events=df.height)

            if _confidence_high(report):
                store_queries(run_id, iteration, queries)
                log.info("investigate.done", confidence=confidence, iterations=iteration, reason="high confidence")
                break

            if not queries:
                log.info("investigate.done", confidence=confidence, iterations=iteration, reason="no follow-up queries")
                break

            if iteration == INVESTIGATOR_MAX_ITER:
                store_queries(run_id, iteration, queries)
                log.info("investigate.done", confidence=confidence, iterations=iteration, reason="max iterations reached")
                break

            new_df = _execute_queries(queries)
            result_rows = [new_df.height if new_df is not None else 0] * len(queries)
            store_queries(run_id, iteration, queries, result_rows)

            if new_df is None or new_df.height == 0:
                log.info("investigate.done", confidence=confidence, iterations=iteration, reason="no new events from follow-up queries")
                break

            df = pl.concat([df, _prepare_df(new_df)], how="diagonal")
            log.debug("df.grown", events=df.height)

    store_report(report, run_id, source)
    clear_active_run_row(run_id)
    return report, all_queries


# ---------------------------------------------------------------------------
# CLI — replaces the old curl-based REST fallback
# ---------------------------------------------------------------------------

def _build_cli_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="splunk.connector",
        description="Direct-call fallback for the Splunk investigation engine "
                     "(no MCP client, no server process required).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="Start an investigation")
    p_start.add_argument("--source", default="", help="Splunk export file path")
    p_start.add_argument("--spl", default="", help="Live SPL query")
    p_start.add_argument("--earliest", default="-24h")
    p_start.add_argument("--latest", default="now")
    p_start.add_argument("--repo-path", default="")

    p_report = sub.add_parser("submit-report", help="Submit a report + follow-up queries")
    p_report.add_argument("--run-id", required=True)
    p_report.add_argument("--report", required=True)
    p_report.add_argument("--queries", nargs="*", default=[])

    p_findings = sub.add_parser("get-findings", help="Get current findings")
    p_findings.add_argument("--run-id", required=True)

    p_pause = sub.add_parser("pause", help="Pause after the current iteration")
    p_pause.add_argument("--run-id", required=True)

    p_resume = sub.add_parser("resume", help="Resume a paused investigation")
    p_resume.add_argument("--run-id", required=True)

    p_hint = sub.add_parser("hint", help="Inject an analyst hint")
    p_hint.add_argument("--run-id", required=True)
    p_hint.add_argument("--text", required=True)

    return p


def _cli_main(argv: list[str] | None = None) -> None:
    args = _build_cli_parser().parse_args(argv)

    if args.cmd == "start":
        result = start_investigation(
            source=args.source, spl=args.spl, earliest=args.earliest,
            latest=args.latest, repo_path=args.repo_path,
        )
    elif args.cmd == "submit-report":
        result = submit_report(args.run_id, args.report, args.queries)
    elif args.cmd == "get-findings":
        result = get_findings(args.run_id)
    elif args.cmd == "pause":
        result = request_pause(args.run_id)
    elif args.cmd == "resume":
        result = resume(args.run_id)
    elif args.cmd == "hint":
        result = set_hint(args.run_id, args.text)
    else:  # pragma: no cover — argparse enforces valid subcommands
        result = {"error": f"unknown command {args.cmd!r}"}

    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    _cli_main()
