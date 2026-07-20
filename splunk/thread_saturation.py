"""
Detects service slowness caused by excess network thread handling
(thread-pool saturation / connection-handling contention) from Splunk log exports.

No LLM calls, no network — pure Polars-based structured findings extraction.
Mirrors the conventions in splunk/detectors.py so it can be dropped into the
same pipeline (same input shape: a Polars DataFrame with `time`, `host`,
`sourcetype`, `_raw`, and optionally `response_time` / `latency_ms` columns).

Usage:
    python -m splunk.thread_saturation results/app_logs.json
    python -m splunk.thread_saturation results/app_logs.csv --window 60
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

import polars as pl

logger = logging.getLogger(__name__)

# Keywords that indicate thread-pool / connection-handling exhaustion across
# common stacks (Tomcat, Jetty, Netty, HikariCP, gRPC, generic app logs).
_THREAD_EXHAUSTION_KEYWORDS = [
    "threadpoolexecutor",
    "rejectedexecutionexception",
    "pool exhausted",
    "pool is exhausted",
    "queue full",
    "queue capacity",
    "max threads",
    "maxthreads",
    "thread starvation",
    "no available threads",
    "connection pool exhausted",
    "timeout waiting for connection",
    "timeout waiting for idle object",
    "too many open files",
    "socket accept queue",
    "backlog exceeded",
    "cannot accept connection",
]

# Fields that, if present, carry an explicit thread-count/pool-size reading.
_THREAD_COUNT_FIELDS = ["active_threads", "thread_count", "pool_active", "active_connections"]
_LATENCY_FIELDS = ["response_time", "latency_ms", "duration_ms", "elapsed_ms"]

# Socket-state keywords whose sustained buildup indicates connections are
# being accepted faster than worker threads can drain them.
_SOCKET_BACKLOG_KEYWORDS = ["close_wait", "syn_recv", "time_wait"]

# GC-pause keywords — thread starvation is often masked as "the GC is slow"
# when in fact worker threads are contending for CPU with a bloated pool.
_GC_PAUSE_KEYWORDS = ["full gc", "stop-the-world", "gc pause", "allocation stall"]


def _find_col(df: pl.DataFrame, candidates: list[str]) -> str | None:
    return next((c for c in candidates if c in df.columns), None)


# ---------------------------------------------------------------------------
# Keyword-based exhaustion detection
# ---------------------------------------------------------------------------

def detect_thread_exhaustion_events(df: pl.DataFrame) -> list[dict]:
    """Flag raw log lines matching known thread/connection exhaustion keywords."""
    if "_raw" not in df.columns or df.is_empty():
        logger.debug("detect_thread_exhaustion_events: no '_raw' column or empty DataFrame")
        return []

    haystack = pl.col("_raw").cast(pl.String).fill_null("").str.to_lowercase()
    flagged = df.with_columns(haystack.alias("_haystack"))

    findings = []
    for row in flagged.to_dicts():
        text = row.get("_haystack", "")
        matched = [kw for kw in _THREAD_EXHAUSTION_KEYWORDS if kw in text]
        if matched:
            t = row.get("time")
            findings.append({
                "type": "thread_exhaustion",
                "host": str(row.get("host") or "unknown"),
                "time": t.isoformat() if isinstance(t, datetime) else str(t),
                "matched_keywords": matched,
                "sourcetype": str(row.get("sourcetype") or "unknown"),
                "raw_excerpt": str(row.get("_raw") or "")[:160],
            })

    logger.info("detect_thread_exhaustion_events: found %d event(s)", len(findings))
    return findings


# ---------------------------------------------------------------------------
# Socket backlog buildup
# ---------------------------------------------------------------------------

def detect_socket_backlog(df: pl.DataFrame, min_count: int = 5) -> list[dict]:
    """Flag hosts with repeated CLOSE_WAIT/SYN_RECV/TIME_WAIT mentions in
    raw lines — a sign connections are piling up faster than threads drain them."""
    if "_raw" not in df.columns or df.is_empty():
        return []

    haystack = pl.col("_raw").cast(pl.String).fill_null("").str.to_lowercase()
    flagged = df.with_columns(haystack.alias("_haystack"))
    host_col = _find_col(df, ["host", "src", "hostname"]) or "host"

    counts: dict[tuple[str, str], int] = {}
    for row in flagged.to_dicts():
        text = row.get("_haystack", "")
        for kw in _SOCKET_BACKLOG_KEYWORDS:
            if kw in text:
                key = (str(row.get(host_col) or "unknown"), kw)
                counts[key] = counts.get(key, 0) + 1

    findings = [
        {"type": "socket_backlog", "host": host, "state": kw, "count": c}
        for (host, kw), c in counts.items()
        if c >= min_count
    ]
    findings.sort(key=lambda f: f["count"], reverse=True)
    logger.info("detect_socket_backlog: found %d backlog signal(s)", len(findings))
    return findings


# ---------------------------------------------------------------------------
# GC pause correlation
# ---------------------------------------------------------------------------

def detect_gc_pressure(df: pl.DataFrame) -> list[dict]:
    """Flag GC-pause log lines — high thread counts often show up first as
    GC pressure since worker threads compete for CPU/heap."""
    if "_raw" not in df.columns or df.is_empty():
        return []

    haystack = pl.col("_raw").cast(pl.String).fill_null("").str.to_lowercase()
    flagged = df.with_columns(haystack.alias("_haystack"))

    findings = []
    for row in flagged.to_dicts():
        text = row.get("_haystack", "")
        matched = [kw for kw in _GC_PAUSE_KEYWORDS if kw in text]
        if matched:
            t = row.get("time")
            findings.append({
                "type": "gc_pressure",
                "host": str(row.get("host") or "unknown"),
                "time": t.isoformat() if isinstance(t, datetime) else str(t),
                "matched_keywords": matched,
                "raw_excerpt": str(row.get("_raw") or "")[:160],
            })
    logger.info("detect_gc_pressure: found %d event(s)", len(findings))
    return findings


# ---------------------------------------------------------------------------
# Rising thread-count trend
# ---------------------------------------------------------------------------

def detect_thread_count_growth(
    df: pl.DataFrame,
    window_seconds: int = 300,
    growth_ratio: float = 1.5,
) -> list[dict]:
    """Detect windows where an explicit thread/connection count field rises
    by >= growth_ratio compared to the window before it."""
    count_col = _find_col(df, _THREAD_COUNT_FIELDS)
    if count_col is None or "time" not in df.columns or df.is_empty():
        logger.debug("detect_thread_count_growth: no thread-count field or time column")
        return []

    timed = (
        df.filter(pl.col("time").is_not_null() & pl.col(count_col).is_not_null())
        .sort("time")
        .with_columns(pl.col(count_col).cast(pl.Float64))
    )
    if timed.is_empty():
        return []

    buckets = (
        timed.with_columns(pl.col("time").dt.truncate(f"{window_seconds}s").alias("_bucket"))
        .group_by("_bucket")
        .agg(pl.col(count_col).mean().alias("avg_count"))
        .sort("_bucket")
    )

    rows = buckets.to_dicts()
    findings = []
    for prev, cur in zip(rows, rows[1:]):
        if prev["avg_count"] and cur["avg_count"] / prev["avg_count"] >= growth_ratio:
            findings.append({
                "type": "thread_count_growth",
                "window_start": cur["_bucket"].isoformat(),
                "window_seconds": window_seconds,
                "prev_avg": round(prev["avg_count"], 1),
                "current_avg": round(cur["avg_count"], 1),
                "growth_ratio": round(cur["avg_count"] / prev["avg_count"], 2),
            })

    logger.info("detect_thread_count_growth: found %d growth window(s)", len(findings))
    return findings


# ---------------------------------------------------------------------------
# Latency correlation
# ---------------------------------------------------------------------------

def correlate_latency_with_thread_load(
    df: pl.DataFrame,
    window_seconds: int = 300,
) -> list[dict]:
    """Bucket latency and thread/connection count together to show whether
    latency rises alongside active-thread count."""
    count_col = _find_col(df, _THREAD_COUNT_FIELDS)
    latency_col = _find_col(df, _LATENCY_FIELDS)
    if not count_col or not latency_col or "time" not in df.columns or df.is_empty():
        logger.debug("correlate_latency_with_thread_load: missing required columns")
        return []

    timed = (
        df.filter(
            pl.col("time").is_not_null()
            & pl.col(count_col).is_not_null()
            & pl.col(latency_col).is_not_null()
        )
        .with_columns([
            pl.col(count_col).cast(pl.Float64),
            pl.col(latency_col).cast(pl.Float64),
        ])
        .sort("time")
    )
    if timed.is_empty():
        return []

    buckets = (
        timed.with_columns(pl.col("time").dt.truncate(f"{window_seconds}s").alias("_bucket"))
        .group_by("_bucket")
        .agg([
            pl.col(count_col).mean().alias("avg_threads"),
            pl.col(latency_col).mean().alias("avg_latency_ms"),
            pl.len().alias("event_count"),
        ])
        .sort("_bucket")
    )

    result = [
        {
            "window_start": row["_bucket"].isoformat(),
            "avg_threads": round(row["avg_threads"], 1),
            "avg_latency_ms": round(row["avg_latency_ms"], 1),
            "event_count": row["event_count"],
        }
        for row in buckets.to_dicts()
    ]
    logger.info("correlate_latency_with_thread_load: %d window(s)", len(result))
    return result


# ---------------------------------------------------------------------------
# Top-level findings assembly
# ---------------------------------------------------------------------------

def analyze(df: pl.DataFrame, window_seconds: int = 300) -> dict:
    """Run all thread-saturation detectors and assemble a findings dict."""
    exhaustion_events = detect_thread_exhaustion_events(df)
    growth_windows = detect_thread_count_growth(df, window_seconds=window_seconds)
    latency_corr = correlate_latency_with_thread_load(df, window_seconds=window_seconds)
    socket_backlog = detect_socket_backlog(df)
    gc_pressure = detect_gc_pressure(df)

    host_counts: dict[str, int] = {}
    for ev in exhaustion_events:
        host_counts[ev["host"]] = host_counts.get(ev["host"], 0) + 1
    host_ranking = sorted(
        [{"host": h, "exhaustion_events": c} for h, c in host_counts.items()],
        key=lambda r: r["exhaustion_events"],
        reverse=True,
    )

    signals_present = sum([
        bool(exhaustion_events),
        bool(growth_windows),
        bool(latency_corr and any(w["avg_threads"] > 0 for w in latency_corr)),
        bool(socket_backlog),
        bool(gc_pressure),
    ])
    confidence = "High" if signals_present >= 2 else ("Medium" if signals_present == 1 else "Low")

    return {
        "confidence": confidence,
        "event_count": df.height,
        "thread_exhaustion_events": exhaustion_events,
        "thread_count_growth": growth_windows,
        "latency_vs_thread_load": latency_corr,
        "socket_backlog": socket_backlog,
        "gc_pressure": gc_pressure,
        "host_ranking": host_ranking,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load(path: str) -> pl.DataFrame:
    if path.endswith(".json"):
        return pl.read_json(path)
    if path.endswith(".csv"):
        return pl.read_csv(path, try_parse_dates=True)
    raise ValueError(f"Unsupported file type: {path} (expected .json or .csv)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Path to a Splunk export (.json or .csv)")
    parser.add_argument("--window", type=int, default=300, help="Bucket window in seconds (default 300)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    df = _load(args.source)
    if "time" in df.columns and df["time"].dtype != pl.Datetime:
        df = df.with_columns(pl.col("time").str.to_datetime(strict=False))

    findings = analyze(df, window_seconds=args.window)
    json.dump(findings, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
