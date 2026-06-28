"""
CLI orchestrator — wires parsers → detectors → agent into one command.

Usage:
    python -m splunk.runner --input results/cert_errors.json
    python -m splunk.runner --input results/cert_errors.csv --no-llm
    python -m splunk.runner --live --spl "index=pki sourcetype=ocsp_error" --output reports/
    cat results.json | python -m splunk.runner --input -
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from splunk.detectors import (
    correlate_events,
    detect_cert_anomalies,
    detect_patterns,
    detect_spikes,
    host_error_ranking,
    severity_summary,
)
from splunk.db import init_db
from splunk.logger import RunLogger
from splunk.parsers import (
    build_timeline,
    extract_cert_fields,
    extract_timestamps,
    parse_splunk_csv,
    parse_splunk_json,
)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    df: pl.DataFrame,
    log: RunLogger,
    source: str = "",
    no_llm: bool = False,
    model: str | None = None,
) -> tuple[dict[str, Any], str]:
    """
    Parse → detect → (optionally) analyse.
    Returns (findings dict, markdown report string).
    """
    # Normalise — single DataFrame threaded through
    df = extract_timestamps(df)
    df = extract_cert_fields(df)
    df = build_timeline(df)

    fmt = "json" if source.endswith(".json") or source == "live" else "csv"
    log.parse_done(event_count=df.height, source=source, fmt=fmt)

    # Detect
    findings: dict[str, Any] = {
        "spikes": detect_spikes(df),
        "patterns": detect_patterns(df),
        "cert_anomalies": detect_cert_anomalies(df),
        "correlations": correlate_events(df),
        "severity": severity_summary(df),
        "host_ranking": host_error_ranking(df),
        "event_count": df.height,
    }
    log.detect_done(findings)

    from splunk.config import USE_LLM
    if no_llm or not USE_LLM:
        report = _findings_to_markdown(findings)
        return findings, report

    # Agent
    if model:
        import os
        os.environ["SPLUNK_LLM_MODEL"] = model

    from splunk.agent import analyse
    from splunk.config import LLM_MODEL
    report, _ = analyse(findings)

    # Detect whether format_report was called by checking for report header
    report_captured = report.startswith("# Splunk Investigation Report")
    log.agent_done(model=model or LLM_MODEL, iterations=-1, report_captured=report_captured)

    return findings, report


def _findings_to_markdown(findings: dict[str, Any]) -> str:
    """Minimal markdown report when --no-llm is set."""
    lines = [
        "# Splunk Findings (no-llm mode)",
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

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# I/O helpers
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


def _load_from_live(spl: str, earliest: str, latest: str) -> pl.DataFrame:
    from splunk.client import run_query
    return pl.DataFrame(run_query(spl, earliest=earliest, latest=latest))


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
    )
    top_host = findings["host_ranking"][0]["host"] if findings["host_ranking"] else "n/a"
    print(
        f"{n_findings} findings | {critical} CRITICAL {errors} ERROR | "
        f"top error host: {top_host} | report: {report_path}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="splunk.runner",
        description="Splunk log intelligence pipeline — parse, detect, analyse.",
    )
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", "-i", metavar="FILE", help="Splunk export file (JSON/CSV) or - for stdin")
    source.add_argument("--live", action="store_true", help="Fetch live from Splunk via REST API")

    p.add_argument("--spl", metavar="QUERY", help="SPL query string (required with --live)")
    p.add_argument("--earliest", default="-24h", help="Earliest time for --live query (default: -24h)")
    p.add_argument("--latest", default="now", help="Latest time for --live query (default: now)")
    p.add_argument("--output", "-o", default="reports/", metavar="DIR", help="Output directory (default: reports/)")
    p.add_argument("--model", metavar="MODEL", help="Override Ollama model (default: qwen2.5:14b). Requires --llm.")
    p.add_argument("--llm", action="store_true", help="Enable standalone LangGraph/Ollama agent (requires uv sync --extra llm and a running Ollama instance)")
    p.add_argument("--dump-findings", action="store_true", help="Print findings JSON to stdout (for pasting into Claude)")
    p.add_argument("--investigate", action="store_true", help="Run iterative investigator loop (requires Splunk REST access for follow-up queries)")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.live and not args.spl:
        parser.error("--live requires --spl")

    init_db()
    run_id = str(uuid.uuid4())

    with RunLogger(run_id) as log:
        log.info("run.start", mode="live" if args.live else "file",
                 source=args.spl if args.live else args.input)

        # Load
        if args.live:
            df = _load_from_live(args.spl, args.earliest, args.latest)
            input_name = "live"
        else:
            df = _load_from_file(args.input)
            input_name = args.input

        if args.dump_findings:
            findings, _ = run_pipeline(df, log, source=input_name, no_llm=not args.llm, model=args.model)
            import json
            print(json.dumps(findings, default=str, indent=2))
            return

        if args.investigate:
            # Try delegating to the server if it's running
            _server_accepted = False
            try:
                import requests as _req
                payload = {"source": args.input} if not args.live else {"spl": args.spl, "earliest": args.earliest, "latest": args.latest}
                resp = _req.post("http://127.0.0.1:8765/api/investigate", json=payload, timeout=3)
                if resp.status_code == 200:
                    data = resp.json()
                    print(f"Investigation started on server — run_id: {data['run_id']}")
                    print("Watch at http://127.0.0.1:8765/ui/")
                    _server_accepted = True
            except Exception:
                pass

            if _server_accepted:
                log.info("run.complete", run_id=run_id)
                print(f"[log] logs/{run_id}.jsonl")
                return

            # Fall back — run directly
            from splunk.investigator import investigate
            try:
                from splunk.server import set_active_run
                set_active_run(run_id, input_name)
            except Exception:
                pass
            log.info("investigator.start", source=input_name)
            report, queries = investigate(df, run_id)
            if queries:
                print(f"\n--- Follow-up queries ({len(queries)}) ---")
                for q in queries:
                    print(q)
                    print()
        else:
            # Pipeline
            findings, report = run_pipeline(
                df, log,
                source=input_name,
                no_llm=not args.llm,
                model=args.model,
            )

        # Write report
        report_path = _write_report(report, args.output, input_name, log)

        log.info("run.complete", run_id=run_id)

    if not args.investigate:
        _stdout_summary(findings, report_path)
    else:
        print(f"report: {report_path}")
    print(f"[log] logs/{run_id}.jsonl")


if __name__ == "__main__":
    main()
