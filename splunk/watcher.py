"""
Standalone continuous-monitoring process — polls Splunk on an interval,
runs the existing deterministic detectors on each new slice, and persists
hits to splunk.db's alerts table for splunk__check_alerts to serve.

Decoupled from the MCP request lifecycle on purpose: Copilot (unlike Claude
Code) has no self-scheduling mechanism, so continuous monitoring has to run
server-side, independent of any agent session.

Usage (run alongside splunk/mcp_server.py, same as splunk/tui.py):
    uv run python -m splunk.watcher
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import polars as pl

from splunk import config, db
from splunk.client import run_query
from splunk.detectors import (
    detect_cert_anomalies,
    detect_http_errors,
    detect_patterns,
    detect_slow_queries,
    detect_spikes,
)
from splunk.logger import RunLogger
from splunk.parsers import build_timeline, extract_timestamps

_RUN_ID = f"watch-{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _initial_earliest() -> str:
    """No bookmark yet — fall back to the configured lookback window."""
    return config.WATCH_LOOKBACK


def _next_earliest(bookmark: str) -> str:
    """Bookmark minus the overlap window, so sliding-window detectors don't
    miss events straddling the previous poll cycle's boundary."""
    ts = datetime.fromisoformat(bookmark)
    return (ts - timedelta(seconds=config.WATCH_OVERLAP)).isoformat()


def _severity_for_slow_query(hit: dict, threshold_ms: int) -> str:
    return "critical" if hit["duration_ms"] >= threshold_ms * 3 else "warning"


def _run_detectors(df: pl.DataFrame) -> list[dict]:
    """Normalize the slice the same way runner.run_pipeline does, then run
    the watcher's detector set, tagging each hit with severity + summary."""
    df = extract_timestamps(df)
    df = build_timeline(df)

    hits: list[dict] = []

    for hit in detect_slow_queries(df, threshold_ms=config.SLOW_QUERY_THRESHOLD_MS):
        hits.append({
            **hit,
            "detector": "slow_query",
            "severity": _severity_for_slow_query(hit, config.SLOW_QUERY_THRESHOLD_MS),
            "summary": f"Slow query: {hit['duration_ms']:.0f}ms on {hit.get('host', 'unknown')}",
        })

    for hit in detect_spikes(df, window_seconds=config.SPIKE_WINDOW_SECONDS, threshold=config.SPIKE_THRESHOLD):
        hits.append({
            **hit,
            "detector": "spike",
            "severity": "warning",
            "summary": f"Spike: {hit['event_count']} events in {hit['window_seconds']}s starting {hit['window_start']}",
        })

    for hit in detect_patterns(df):
        hits.append({
            **hit,
            "detector": "pattern",
            "severity": "info",
            "summary": f"Repeated {hit['type']}: count={hit.get('count')}",
        })

    for hit in detect_cert_anomalies(df):
        hits.append({
            **hit,
            "detector": "cert_anomaly",
            "severity": "warning",
            "summary": f"Cert anomaly on {hit['host']}: {', '.join(hit['matched_keywords'])}",
        })

    for hit in detect_http_errors(df):
        hits.append({
            **hit,
            "detector": "http_error",
            "severity": "critical" if hit["class"] == "5xx" else "warning",
            "summary": f"HTTP {hit['status_code']} on {hit.get('host', 'unknown')}"
                       + (f" {hit['path']}" if hit.get("path") else ""),
        })

    return hits


def _run_cycle(log: RunLogger) -> None:
    bookmark = db.get_watch_bookmark()
    earliest = _next_earliest(bookmark) if bookmark else _initial_earliest()

    events = run_query(config.WATCH_SPL, earliest=earliest, latest="now")
    log.info("watch.cycle", event_count=len(events), earliest=earliest)

    if not events:
        return

    df = pl.DataFrame(events)
    hits = _run_detectors(df)

    if hits:
        db.store_alerts(hits, run_id=_RUN_ID)
        log.info("watch.alerts", count=len(hits))

    newest = max((str(ev["_time"]) for ev in events if ev.get("_time")), default=None)
    if newest:
        db.set_watch_bookmark(newest)


def main() -> None:
    if not config.WATCH_SPL:
        raise SystemExit("SPLUNK_WATCH_SPL is not set. Add it to .env or environment.")

    db.init_db()
    log = RunLogger(_RUN_ID)
    log.info("watch.start", spl=config.WATCH_SPL, interval=config.WATCH_INTERVAL)

    try:
        while True:
            try:
                _run_cycle(log)
            except Exception as exc:  # noqa: BLE001 — one bad cycle must not kill the loop
                log.error("watch.cycle_failed", error=str(exc))
            time.sleep(config.WATCH_INTERVAL)
    except KeyboardInterrupt:
        log.info("watch.stop")
    finally:
        log.close()


if __name__ == "__main__":
    main()
