"""
Deterministic detectors over parsed Splunk events.
No LLM calls, no network — pure Polars-based structured findings extraction.
"""

from __future__ import annotations

from datetime import datetime, timezone

import logging

import polars as pl

from splunk.config import ANOMALY_NUMERIC_FIELDS as _ANOMALY_NUMERIC_FIELDS
from splunk.config import ANOMALY_ROLLING_WINDOW as _DEFAULT_ANOMALY_WINDOW
from splunk.config import ANOMALY_Z_THRESHOLD as _DEFAULT_ANOMALY_Z_THRESHOLD
from splunk.config import CERT_ANOMALY_KEYWORDS as _DEFAULT_CERT_KEYWORDS
from splunk.config import DURATION_FIELDS as _DURATION_FIELDS
from splunk.config import SLOW_QUERY_THRESHOLD_MS as _DEFAULT_SLOW_QUERY_THRESHOLD_MS

logger = logging.getLogger(__name__)

_SEVERITY_LEVELS = {"CRITICAL", "ERROR", "WARN", "WARNING", "INFO", "DEBUG"}


# ---------------------------------------------------------------------------
# Spike detection
# ---------------------------------------------------------------------------

def detect_spikes(
    df: pl.DataFrame,
    window_seconds: int = 60,
    threshold: int = 10,
) -> list[dict]:
    """Return windows where event frequency exceeds threshold."""
    logger.debug("detect_spikes: window=%ds threshold=%d events=%d", window_seconds, threshold, df.height)
    if "time" not in df.columns or df.is_empty():
        logger.debug("detect_spikes: skipped — no time column or empty DataFrame")
        return []

    timed = df.filter(pl.col("time").is_not_null()).sort("time")
    if timed.is_empty():
        return []

    times = timed["time"].to_list()
    hosts = timed["host"].to_list() if "host" in timed.columns else ["unknown"] * len(times)

    spikes = []
    seen_minutes: set[str] = set()

    for i, anchor in enumerate(times):
        window_hosts = [
            h for t, h in zip(times[i:], hosts[i:])
            if t is not None and (t - anchor).total_seconds() <= window_seconds
        ]
        if len(window_hosts) >= threshold:
            minute_key = anchor.isoformat()[:16]
            if minute_key not in seen_minutes:
                seen_minutes.add(minute_key)
                spikes.append({
                    "window_start": anchor.isoformat(),
                    "window_seconds": window_seconds,
                    "event_count": len(window_hosts),
                    "threshold": threshold,
                    "hosts": list(set(str(h) for h in window_hosts)),
                })

    logger.info("detect_spikes: found %d spike window(s)", len(spikes))
    return spikes


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------

def detect_patterns(df: pl.DataFrame) -> list[dict]:
    """Find repeating (sourcetype, error_code) tuples and common _raw prefixes."""
    logger.debug("detect_patterns: events=%d", df.height)
    findings = []

    # Repeated (sourcetype, error_code) tuples
    code_col = next(
        (c for c in ("error_code", "event_code", "EventCode") if c in df.columns), None
    )
    if "sourcetype" in df.columns and code_col:
        counts = (
            df.group_by(["sourcetype", code_col])
            .agg(pl.len().alias("count"))
            .filter(pl.col("count") > 1)
            .sort("count", descending=True)
        )
        for row in counts.to_dicts():
            findings.append({
                "type": "repeated_error",
                "sourcetype": row["sourcetype"],
                "error_code": str(row[code_col]),
                "count": row["count"],
            })

    # Common _raw prefix clusters (first 60 chars)
    if "_raw" in df.columns:
        prefix_counts = (
            df.with_columns(pl.col("_raw").str.slice(0, 60).alias("_prefix"))
            .group_by("_prefix")
            .agg(pl.len().alias("count"))
            .filter(pl.col("count") > 1)
            .sort("count", descending=True)
            .head(10)
        )
        for row in prefix_counts.to_dicts():
            findings.append({
                "type": "repeated_raw_prefix",
                "prefix": row["_prefix"],
                "count": row["count"],
            })

    logger.info("detect_patterns: found %d pattern(s)", len(findings))
    return findings


# ---------------------------------------------------------------------------
# Event correlation
# ---------------------------------------------------------------------------

def correlate_events(df: pl.DataFrame, window_seconds: int = 60) -> list[dict]:
    """Group cascading events that fall within window_seconds of each other."""
    logger.debug("correlate_events: window=%ds events=%d", window_seconds, df.height)
    if "time" not in df.columns or df.is_empty():
        logger.debug("correlate_events: skipped — no time column or empty DataFrame")
        return []

    timed = df.filter(pl.col("time").is_not_null()).sort("time")
    if timed.is_empty():
        return []

    times = timed["time"].to_list()
    hosts = timed["host"].to_list() if "host" in timed.columns else ["unknown"] * len(times)
    sourcetypes = timed["sourcetype"].to_list() if "sourcetype" in timed.columns else ["unknown"] * len(times)

    groups: list[list[int]] = []
    current = [0]

    for i in range(1, len(times)):
        if times[i] is not None and times[current[-1]] is not None:
            gap = (times[i] - times[current[-1]]).total_seconds()
        else:
            gap = window_seconds + 1
        if gap <= window_seconds:
            current.append(i)
        else:
            if len(current) > 1:
                groups.append(current)
            current = [i]
    if len(current) > 1:
        groups.append(current)

    result = [
        {
            "group_start": times[g[0]].isoformat(),
            "group_end": times[g[-1]].isoformat(),
            "span_seconds": (times[g[-1]] - times[g[0]]).total_seconds(),
            "event_count": len(g),
            "hosts": list({str(hosts[i]) for i in g}),
            "sourcetypes": list({str(sourcetypes[i]) for i in g}),
        }
        for g in groups
    ]
    logger.info("correlate_events: found %d cascading group(s)", len(result))
    return result


# ---------------------------------------------------------------------------
# Cert anomalies
# ---------------------------------------------------------------------------

def detect_cert_anomalies(
    df: pl.DataFrame,
    keywords: list[str] | None = None,
) -> list[dict]:
    """Flag events whose _raw or cert fields match known PKI error keywords."""
    kws = [k.lower() for k in (keywords or _DEFAULT_CERT_KEYWORDS)]
    logger.debug("detect_cert_anomalies: keywords=%s events=%d", kws, df.height)
    if "_raw" not in df.columns:
        logger.warning("detect_cert_anomalies: no '_raw' column — skipping")
        return []

    # Build a lowercase search column from _raw + tls_error + ocsp_status
    parts = [pl.col("_raw").cast(pl.String).fill_null("")]
    for col in ("tls_error", "ocsp_status"):
        if col in df.columns:
            parts.append(pl.col(col).cast(pl.String).fill_null(""))

    haystack_expr = parts[0]
    for p in parts[1:]:
        haystack_expr = haystack_expr + pl.lit(" ") + p

    flagged = df.with_columns(haystack_expr.str.to_lowercase().alias("_haystack"))

    findings = []
    for row in flagged.to_dicts():
        haystack = row.get("_haystack", "")
        matched = [k for k in kws if k in haystack]
        if matched:
            t = row.get("time")
            findings.append({
                "type": "cert_anomaly",
                "host": str(row.get("host") or "unknown"),
                "time": t.isoformat() if isinstance(t, datetime) else str(t),
                "matched_keywords": matched,
                "sourcetype": str(row.get("sourcetype") or "unknown"),
                "raw_excerpt": str(row.get("_raw") or "")[:120],
            })

    logger.info("detect_cert_anomalies: found %d anomalous event(s)", len(findings))
    return findings


# ---------------------------------------------------------------------------
# Severity summary
# ---------------------------------------------------------------------------

def severity_summary(df: pl.DataFrame) -> dict[str, int]:
    """Count events by severity level."""
    logger.debug("severity_summary: events=%d", df.height)
    if df.is_empty():
        logger.debug("severity_summary: empty DataFrame")
        return {}

    # Use explicit severity/level column if present
    level_col = next((c for c in ("severity", "level", "log_level") if c in df.columns), None)

    if level_col:
        counts = (
            df.with_columns(pl.col(level_col).cast(pl.String).str.to_uppercase().alias("_lvl"))
            .group_by("_lvl")
            .agg(pl.len().alias("count"))
        )
        return {row["_lvl"]: row["count"] for row in counts.to_dicts()}

    # Infer from _raw
    if "_raw" not in df.columns:
        return {}

    results: dict[str, int] = {}
    for level in _SEVERITY_LEVELS:
        n = df.filter(pl.col("_raw").cast(pl.String).str.to_uppercase().str.contains(level)).height
        if n:
            results[level] = n
    unknown = df.height - sum(results.values())
    if unknown:
        results["UNKNOWN"] = unknown
    return results


# ---------------------------------------------------------------------------
# Host error ranking
# ---------------------------------------------------------------------------

def host_error_ranking(df: pl.DataFrame) -> list[dict]:
    """Return hosts sorted by ERROR/CRITICAL event count descending."""
    logger.debug("host_error_ranking: events=%d", df.height)
    if "_raw" not in df.columns or df.is_empty():
        logger.debug("host_error_ranking: skipped — no '_raw' column or empty DataFrame")
        return []

    host_col = next((c for c in ("host", "src", "hostname") if c in df.columns), None)
    if not host_col:
        return []

    error_filter = pl.col("_raw").cast(pl.String).str.to_uppercase()
    is_error = error_filter.str.contains("ERROR") | error_filter.str.contains("CRITICAL")

    ranked = (
        df.filter(is_error)
        .group_by(host_col)
        .agg(pl.len().alias("error_count"))
        .sort("error_count", descending=True)
        .rename({host_col: "host"})
    )
    result = ranked.to_dicts()
    if result:
        logger.info("host_error_ranking: top host=%s (%d errors), %d hosts total",
                    result[0]["host"], result[0]["error_count"], len(result))
    else:
        logger.info("host_error_ranking: no ERROR/CRITICAL events found")
    return result


# ---------------------------------------------------------------------------
# Slow query detection
# ---------------------------------------------------------------------------

def detect_slow_queries(
    df: pl.DataFrame,
    threshold_ms: int = _DEFAULT_SLOW_QUERY_THRESHOLD_MS,
) -> list[dict]:
    """Return events whose duration exceeds threshold_ms, sorted slowest first."""
    logger.debug("detect_slow_queries: threshold_ms=%d events=%d", threshold_ms, df.height)
    if df.is_empty():
        logger.debug("detect_slow_queries: skipped — empty DataFrame")
        return []

    duration_col = next((c for c in _DURATION_FIELDS if c in df.columns), None)
    if not duration_col:
        logger.debug("detect_slow_queries: skipped — no duration field found (checked %s)", _DURATION_FIELDS)
        return []

    durations = df.with_columns(pl.col(duration_col).cast(pl.Float64, strict=False).alias("_duration_ms"))
    slow = durations.filter(pl.col("_duration_ms") > threshold_ms).sort("_duration_ms", descending=True)
    if slow.is_empty():
        logger.info("detect_slow_queries: no events exceeded %dms threshold", threshold_ms)
        return []

    host_col = next((c for c in ("host", "src", "hostname") if c in slow.columns), None)
    query_col = next((c for c in ("query", "search", "spl", "_raw") if c in slow.columns), None)
    time_col = "time" if "time" in slow.columns else None

    results = []
    for row in slow.to_dicts():
        entry = {"duration_ms": row["_duration_ms"], "field": duration_col}
        if host_col:
            entry["host"] = row[host_col]
        if query_col:
            entry["query"] = str(row[query_col])[:200]
        if time_col and row[time_col] is not None:
            entry["time"] = row[time_col].isoformat()
        results.append(entry)

    logger.info("detect_slow_queries: %d event(s) exceeded %dms (slowest=%.1fms)",
                len(results), threshold_ms, results[0]["duration_ms"])
    return results


# ---------------------------------------------------------------------------
# Rolling z-score numeric anomaly detection
# ---------------------------------------------------------------------------

def detect_numeric_anomalies(
    df: pl.DataFrame,
    field: str | None = None,
    window: int = _DEFAULT_ANOMALY_WINDOW,
    z_threshold: float = _DEFAULT_ANOMALY_Z_THRESHOLD,
) -> list[dict]:
    """
    Flag events whose value on a numeric field deviates more than z_threshold
    rolling standard deviations from the rolling mean. Requires 'time' column
    to establish event order; events are sorted chronologically first.

    If `field` is not given, auto-detects the first matching column from
    config.ANOMALY_NUMERIC_FIELDS.
    """
    logger.debug("detect_numeric_anomalies: field=%s window=%d z_threshold=%.1f events=%d",
                 field, window, z_threshold, df.height)
    if df.is_empty() or "time" not in df.columns:
        logger.debug("detect_numeric_anomalies: skipped — empty DataFrame or no 'time' column")
        return []

    numeric_col = field or next((c for c in _ANOMALY_NUMERIC_FIELDS if c in df.columns), None)
    if not numeric_col or numeric_col not in df.columns:
        logger.debug("detect_numeric_anomalies: skipped — no numeric field found (checked %s)",
                     _ANOMALY_NUMERIC_FIELDS)
        return []

    timed = df.filter(pl.col("time").is_not_null()).sort("time")
    if timed.height < window + 1:
        logger.debug("detect_numeric_anomalies: skipped — not enough events (%d) for window=%d",
                     timed.height, window)
        return []

    scored = timed.with_columns(
        pl.col(numeric_col).cast(pl.Float64, strict=False).alias("_value")
    ).with_columns(
        pl.col("_value").rolling_mean(window_size=window).alias("_roll_mean"),
        pl.col("_value").rolling_std(window_size=window).alias("_roll_std"),
    ).with_columns(
        ((pl.col("_value") - pl.col("_roll_mean")) / pl.col("_roll_std")).alias("_z")
    ).with_row_index("_idx")

    anomalies_chrono = scored.filter(
        pl.col("_z").is_finite() & pl.col("_z").abs().gt(z_threshold)
    ).sort("_idx")

    if anomalies_chrono.is_empty():
        logger.info("detect_numeric_anomalies: no anomalies found on field=%s", numeric_col)
        return []

    # A flagged event whose rolling window still contains an earlier flagged
    # event is likely an artifact of the spike polluting the rolling mean/std,
    # not a genuine independent anomaly. Cluster consecutive flags within
    # `window` rows of each other and tag/log every non-leading member.
    tainted_by: dict[int, int] = {}  # row idx -> idx of the cluster's leading (highest |z|) anomaly
    rows = anomalies_chrono.to_dicts()
    cluster: list[dict] = []

    def _flush_cluster(members: list[dict]) -> None:
        if len(members) <= 1:
            return
        leader = max(members, key=lambda r: abs(r["_z"]))
        for m in members:
            if m["_idx"] != leader["_idx"]:
                tainted_by[m["_idx"]] = leader["_idx"]
        logger.warning(
            "detect_numeric_anomalies: %d flagged event(s) within window=%d of a prior spike "
            "on field=%s — likely rolling-window contamination from the anomaly at %s (z=%.2f), "
            "not independent anomalies",
            len(members) - 1, window, numeric_col,
            leader["time"].isoformat() if leader["time"] is not None else "?",
            leader["_z"],
        )

    for row in rows:
        if cluster and row["_idx"] - cluster[-1]["_idx"] <= window:
            cluster.append(row)
        else:
            _flush_cluster(cluster)
            cluster = [row]
    _flush_cluster(cluster)

    anomalies = anomalies_chrono.sort(pl.col("_z").abs(), descending=True)
    host_col = next((c for c in ("host", "src", "hostname") if c in anomalies.columns), None)

    results = []
    for row in anomalies.to_dicts():
        entry = {
            "field": numeric_col,
            "value": row["_value"],
            "z_score": row["_z"],
            "rolling_mean": row["_roll_mean"],
            "rolling_std": row["_roll_std"],
            "time": row["time"].isoformat() if row["time"] is not None else None,
            "window_contaminated": row["_idx"] in tainted_by,
        }
        if host_col:
            entry["host"] = row[host_col]
        results.append(entry)

    n_tainted = len(tainted_by)
    logger.info(
        "detect_numeric_anomalies: %d anomal(y/ies) on field=%s (max |z|=%.2f), "
        "%d flagged as likely window-contamination artifacts",
        len(results), numeric_col, abs(results[0]["z_score"]), n_tainted,
    )
    return results
