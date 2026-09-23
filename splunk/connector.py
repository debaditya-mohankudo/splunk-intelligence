"""
Facade wrapping the Splunk investigation engine — loading data, running
detectors, driving the standalone agent loop, and persisting run state.

All callers (MCP tools in mcp_server.py, the TUI in tui.py, and this
module's own CLI) go through here instead of talking to each other
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

CLI (`python -m splunk` is an alias for `python -m splunk.connector`):
    uv run python -m splunk findings --source results/x.json [--json]
    uv run python -m splunk findings --spl "index=pki" --earliest -6h
    uv run python -m splunk agent --source results/x.json
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
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from splunk import dispatcher
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
    _execute_queries,
    _parse_confidence,
    _prepare_df,
)
from splunk.logger import RunLogger
from splunk.parsers import parse_splunk_csv, parse_splunk_json

logger = logging.getLogger(__name__)

_sessions: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Loaders — shared by the MCP path, the TUI and the CLI.
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
        "repo_path": row.get("repo_path") or "",
    }
    _sessions[run_id] = session
    return session


def get_session(run_id: str) -> dict[str, Any] | None:
    """Public accessor for callers that need session fields like repo_path."""
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
            # Full, untruncated live-query params — source_label above is
            # truncated to spl[:60] for display; these are kept as a
            # durable reference for store_report (see db.py:store_report).
            "spl": spl,
            "earliest": earliest if spl else "",
            "latest": latest if spl else "",
        }
        store_events(df, run_id)
        upsert_active_run(
            run_id,
            source=source_label,
            iteration=0,
            confidence="—",
            events=df.height,
            findings_json=json.dumps(findings, default=str),
            repo_path=repo_path,
        )

        result = {
            "run_id": run_id,
            "source": source_label,
            "event_count": findings["event_count"],
            "findings": json.loads(json.dumps(findings, default=str)),
            "ui_url": f"Run: uv run python -m splunk.tui  (select run {run_id[:8]})",
            "next": "Reason over these findings and call splunk__submit_report with your report and follow-up SPL queries.",
        }
        if repo_path:
            result["repo_path"] = repo_path
            result["code_context"] = "splunk__find_symbol_refs is available — use it to find where an error's log site is defined and referenced before writing follow-up queries."
        elif nudge := dispatcher.repo_path_nudge(repo_path):
            result["repo_path_nudge"] = nudge

        with RunLogger(run_id) as log:
            log.info("investigate.start", source=source_label, event_count=findings["event_count"], repo_path=repo_path or None)
        return result

    except Exception as exc:
        with RunLogger(run_id) as log:
            log.error("investigate.start_failed", error=str(exc))
        return {"error": str(exc)}


@dataclass
class _StepOutcome:
    status: str  # "done" | "continue"
    confidence: str
    reason: str = ""
    new_df: pl.DataFrame | None = None  # prepared events, set only on "continue"


def _pause_requested(run_id: str) -> bool:
    row = get_active_run_row(run_id)
    return bool(row and row.get("pause_requested"))


def _step(run_id: str, iteration: int, report: str, queries: list[str], event_count: int) -> _StepOutcome:
    """One loop step shared by submit_report (MCP) and run_standalone_agent:
    applies the done-rules, executes follow-ups, and records every submitted
    query — with per-query result_rows whenever the queries were executed."""
    confidence = _parse_confidence(report, event_count)
    high = confidence == "High"

    reason = ""
    if high:
        reason = "high confidence"
    elif not queries:
        reason = "no follow-up queries"
    elif iteration >= INVESTIGATOR_MAX_ITER:
        reason = "max iterations reached"
    if reason:
        if queries:
            store_queries(run_id, iteration, queries)
        return _StepOutcome("done", confidence, reason)

    new_df, counts = _execute_queries(queries)
    store_queries(run_id, iteration, queries, counts)
    if new_df is None or new_df.height == 0:
        return _StepOutcome("done", confidence, "no new events from follow-up queries")
    return _StepOutcome("continue", confidence, new_df=_prepare_df(new_df))


def submit_report(run_id: str, report: str, queries: list[str] | None = None) -> dict[str, Any]:
    queries = queries or []
    session = _get_or_rehydrate_session(run_id)
    if session is None:
        return {"error": f"run_id {run_id!r} not found in active session"}

    # The MCP analogue of the standalone loop's pause poll: the agent can't be
    # blocked, so the step is refused (nothing stored, iteration unchanged)
    # until the analyst resumes.
    if _pause_requested(run_id):
        return {
            "status": "paused",
            "run_id": run_id,
            "next": "The analyst paused this run. Wait, then call splunk__submit_report again with the same report and queries.",
        }

    iteration = session.get("iteration", 0) + 1
    session["iteration"] = iteration
    store_report(
        report, run_id, session.get("source", ""),
        spl=session.get("spl", ""), earliest=session.get("earliest", ""), latest=session.get("latest", ""),
    )

    events = session["df"].height if session.get("df") is not None else 0
    step = _step(run_id, iteration, report, queries, events)
    confidence = step.confidence
    session["confidence"] = confidence

    upsert_active_run(run_id, iteration=iteration, confidence=confidence, events=events)

    with RunLogger(run_id) as log:
        log.info("submit_report.iteration", iteration=iteration, confidence=confidence, queries=len(queries))

    ui_url = f"Run: uv run python -m splunk.tui  (select run {run_id[:8]})"

    if step.status == "done":
        clear_active_run_row(run_id)
        _sessions.pop(run_id, None)
        with RunLogger(run_id) as log:
            log.info("investigate.done", confidence=confidence, iterations=iteration, reason=step.reason)
        result = {
            "status": "done",
            "run_id": run_id,
            "confidence": confidence,
            "iterations": iteration,
            "reason": step.reason,
            "ui_url": ui_url,
        }
        if nudge := dispatcher.confidence_nudge("done", confidence):
            result["confidence_nudge"] = nudge
        if nudge := dispatcher.no_followup_nudge("done", queries):
            result["followup_nudge"] = nudge
        return result

    df = pl.concat([session["df"], step.new_df], how="diagonal")
    session["df"] = df
    findings = _build_findings(df)
    hint = pop_hint(run_id)
    if hint:
        findings["analyst_hint"] = hint
        with RunLogger(run_id) as log:
            log.info("hint.injected", hint=hint)
    session["findings"] = findings
    store_events(step.new_df, run_id)
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


def _grep_matches(pattern: str, repo: Path) -> list[dict[str, Any]]:
    """Whole-word extended-regex grep over repo. grep rather than ripgrep:
    it is always installed, and rg often exists only as a shell function."""
    out = subprocess.run(
        ["grep", "-rnwIE", "--exclude-dir=.git", pattern, str(repo)],
        capture_output=True, text=True, timeout=10,
    ).stdout
    matches = []
    for line in out.splitlines():
        file, lineno, text = line.split(":", 2)
        matches.append({"file": file, "line": int(lineno), "text": text.strip()})
    return matches


def find_symbol_refs(run_id: str, symbol: str, max_refs: int = 15) -> dict[str, Any]:
    """Whole-word grep over the run's repo_path: where `symbol` is defined
    (`def`/`class`) and every other non-comment line that mentions it."""
    session = _get_or_rehydrate_session(run_id)
    if session is None:
        return {"error": f"run_id {run_id!r} not active"}

    repo_path = session.get("repo_path", "")
    if not repo_path:
        return {
            "error": "No repo_path in session. Re-start the investigation with repo_path set to the microservice repo.",
            "hint": "Call splunk__investigate_start again with repo_path='<path to repo>'.",
        }
    repo = Path(repo_path)
    if not repo.exists():
        return {"error": f"repo_path does not exist: {repo_path}"}

    try:
        definitions = _grep_matches(f"(def|class) {symbol}", repo)
        if not definitions:
            return {"error": f"Symbol '{symbol}' not found in {repo_path}"}
        refs = [
            m for m in _grep_matches(symbol, repo)
            if not any((m["file"], m["line"]) == (d["file"], d["line"]) for d in definitions)
            and not m["text"].startswith("#")
        ]
    except subprocess.TimeoutExpired:
        return {"error": "Symbol search timed out"}
    except Exception as exc:
        return {"error": str(exc)}

    return {
        "symbol": symbol,
        "repo": repo_path,
        "results": [{"definition": d, "references": refs[:max_refs]} for d in definitions[:3]],
    }


# ---------------------------------------------------------------------------
# Standalone agent loop — replaces investigator.investigate()/investigate_task()
# ---------------------------------------------------------------------------

def run_standalone_agent(df: pl.DataFrame, run_id: str, source: str = "") -> tuple[str, list[str]]:
    """LangGraph/Ollama ReAct loop over detector findings. Used by
    this module's CLI `agent` subcommand and the TUI's launch flow when no
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

    from standalone.agent import analyse

    df = _prepare_df(df)
    upsert_active_run(run_id, source=source, iteration=0, confidence="—", events=df.height)
    all_queries: list[str] = []
    report = ""

    with RunLogger(run_id) as log:
        log.info("investigate.start", source="standalone-agent", event_count=df.height)

        for iteration in range(1, INVESTIGATOR_MAX_ITER + 1):
            while _pause_requested(run_id):
                log.debug("agent.paused")
                time.sleep(1)

            logger.info("run_standalone_agent: iteration %d/%d — %d events", iteration, INVESTIGATOR_MAX_ITER, df.height)
            findings = _build_findings(df)

            hint = pop_hint(run_id)
            if hint:
                findings["analyst_hint"] = hint
                log.info("hint.injected", hint=hint)

            report, queries = analyse(findings)
            all_queries.extend(queries)

            step = _step(run_id, iteration, report, queries, df.height)
            upsert_active_run(run_id, iteration=iteration, confidence=step.confidence, events=df.height)
            log.info("agent.iteration", iteration=iteration, confidence=step.confidence, queries=len(queries), events=df.height)

            if step.status == "done":
                log.info("investigate.done", confidence=step.confidence, iterations=iteration, reason=step.reason)
                break

            df = pl.concat([df, step.new_df], how="diagonal")
            log.debug("df.grown", events=df.height)

    session = _sessions.get(run_id) or {}
    store_report(
        report, run_id, source,
        spl=session.get("spl", ""), earliest=session.get("earliest", ""), latest=session.get("latest", ""),
    )
    clear_active_run_row(run_id)
    return report, all_queries


# ---------------------------------------------------------------------------
# One-shot findings report (no agent) — the `findings` CLI subcommand
# ---------------------------------------------------------------------------

def _findings_to_markdown(findings: dict[str, Any]) -> str:
    """Generate a markdown findings report."""
    lines = [
        "# Splunk Findings",
        "",
        f"**Events analysed:** {findings['event_count']}",
        f"**Severity breakdown:** {findings['severity']}",
        "",
        f"## Spikes ({len(findings['spikes'])})",
    ]
    for s in findings["spikes"]:
        lines.append(f"- {s['window_start']} — {s['event_count']} events on {', '.join(s['hosts'])}")

    lines += [f"\n## Cert Anomalies ({len(findings['cert_anomalies'])})"]
    for c in findings["cert_anomalies"]:
        lines.append(f"- [{c['host']}] {', '.join(c['matched_keywords'])} at {c['time']}")

    lines += ["\n## Top Error Hosts"]
    for h in findings["host_ranking"][:5]:
        lines.append(f"- {h['host']}: {h['error_count']} errors")

    lines += [f"\n## Slow Queries ({len(findings['slow_queries'])})"]
    for q in findings["slow_queries"][:10]:
        host = f" [{q['host']}]" if "host" in q else ""
        lines.append(f"- {q['duration_ms']:.0f}ms{host} — {q.get('query', '')}")

    lines += [f"\n## Event Pairs ({len(findings['event_pairs'])})"]
    for p in findings["event_pairs"]:
        lines.append(
            f"- [{p['entity']}] '{p['first_pattern']}' at {p['first_time']} -> "
            f"'{p['second_pattern']}' at {p['second_time']} (+{p['span_seconds']:.0f}s)"
        )

    lines += [f"\n## Numeric Anomalies ({len(findings['numeric_anomalies'])})"]
    for a in findings["numeric_anomalies"][:10]:
        host = f" [{a['host']}]" if "host" in a else ""
        tainted = " (likely window-contamination artifact)" if a.get("window_contaminated") else ""
        lines.append(
            f"- {a['field']}={a['value']:.2f}{host} — z={a['z_score']:.2f} "
            f"(mean={a['rolling_mean']:.2f}, std={a['rolling_std']:.2f}) at {a['time']}{tainted}"
        )

    return "\n".join(lines)


def _write_report(report: str, output_dir: str, input_name: str, log: RunLogger) -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    stem = Path(input_name).stem if input_name != "-" else "stdin"
    report_path = out / f"{stem}_{ts}.md"
    report_path.write_text(report)
    log.report_written(path=str(report_path), size_bytes=len(report.encode()))
    return report_path


def _stdout_summary(findings: dict[str, Any], report_path: Path) -> None:
    severity = findings["severity"]
    critical = severity.get("CRITICAL", 0)
    errors = severity.get("ERROR", 0)
    n_findings = (
        len(findings["spikes"])
        + len(findings["cert_anomalies"])
        + len(findings["patterns"])
        + len(findings["slow_queries"])
        + len(findings["numeric_anomalies"])
        + len(findings["event_pairs"])
    )
    top_host = findings["host_ranking"][0]["host"] if findings["host_ranking"] else "n/a"
    print(
        f"{n_findings} findings | {critical} CRITICAL {errors} ERROR | "
        f"top error host: {top_host} | report: {report_path}"
    )


def _load_cli_source(args: argparse.Namespace) -> tuple[pl.DataFrame, str]:
    if args.spl:
        return _load_from_live(args.spl, args.earliest, args.latest), "live"
    return _load_from_file(args.source), args.source


def _run_findings_cli(args: argparse.Namespace) -> None:
    init_db()
    run_id = str(uuid.uuid4())
    with RunLogger(run_id) as log:
        df, input_name = _load_cli_source(args)
        df = _prepare_df(df)
        fmt = "json" if input_name.endswith(".json") or input_name == "live" else "csv"
        log.parse_done(event_count=df.height, source=input_name, fmt=fmt)
        findings = _build_findings(df)
        log.detect_done(findings)
        if args.json:
            print(json.dumps(findings, default=str, indent=2))
            return
        report_path = _write_report(_findings_to_markdown(findings), args.output, input_name, log)
    _stdout_summary(findings, report_path)
    print(f"[log] logs/{run_id}.jsonl")


def _run_agent_cli(args: argparse.Namespace) -> None:
    init_db()
    run_id = str(uuid.uuid4())
    with RunLogger(run_id) as log:
        df, input_name = _load_cli_source(args)
        print(f"Watch live progress: uv run python -m splunk.tui  (select run {run_id[:8]})")
        report, queries = run_standalone_agent(df, run_id, source=input_name)
        if queries:
            print(f"\n--- Follow-up queries ({len(queries)}) ---")
            for q in queries:
                print(q)
                print()
        report_path = _write_report(report, args.output, input_name, log)
    print(f"report: {report_path}")
    print(f"[log] logs/{run_id}.jsonl")


# ---------------------------------------------------------------------------
# CLI — replaces the old curl-based REST fallback
# ---------------------------------------------------------------------------

def _build_cli_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m splunk",
        description="Splunk investigation engine CLI: one-shot findings, the standalone "
                     "agent, or the step-wise loop (no MCP client, no server process required).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="Start an investigation")
    p_start.add_argument("--source", default="", help="Splunk export file path")
    p_start.add_argument("--spl", default="", help="Live SPL query")
    p_start.add_argument("--earliest", default="-24h")
    p_start.add_argument("--latest", default="now")
    p_start.add_argument("--repo-path", default="")

    def add_source_args(sp: argparse.ArgumentParser) -> None:
        src = sp.add_mutually_exclusive_group(required=True)
        src.add_argument("--source", default="", help="Splunk export file (JSON/CSV) or - for stdin")
        src.add_argument("--spl", default="", help="Live SPL query")
        sp.add_argument("--earliest", default="-24h")
        sp.add_argument("--latest", default="now")
        sp.add_argument("--output", "-o", default="reports/", help="Report directory (default: reports/)")

    p_oneshot = sub.add_parser("findings", help="Run detectors once and write a markdown findings report")
    add_source_args(p_oneshot)
    p_oneshot.add_argument("--json", action="store_true", help="Print the findings JSON to stdout instead")

    p_agent = sub.add_parser("agent", help="Run the standalone LangGraph/Ollama investigation loop")
    add_source_args(p_agent)

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

    if args.cmd == "findings":
        return _run_findings_cli(args)
    if args.cmd == "agent":
        return _run_agent_cli(args)

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
