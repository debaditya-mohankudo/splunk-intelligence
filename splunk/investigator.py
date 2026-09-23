"""
Pure, reusable pieces of the investigation pipeline — building findings from a
DataFrame, executing follow-up SPL, and preparing raw events.

No orchestration lives here anymore: the standalone agent loop and the
MCP-driven step functions both live in connector.py, which imports these.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import polars as pl

from splunk.db import split_area_label
from splunk.detectors import (
    correlate_events,
    detect_cert_anomalies,
    detect_event_pair_patterns,
    detect_numeric_anomalies,
    detect_patterns,
    detect_slow_queries,
    detect_spikes,
    host_error_ranking,
    severity_summary,
)
from splunk.parsers import build_timeline, extract_cert_fields, extract_timestamps

logger = logging.getLogger(__name__)

_CONFIDENCE_RE = re.compile(r"\*\*Confidence:\*\*\s*(High|Medium|Low)\b", re.IGNORECASE)

# A report that states no recognisable level is treated as Medium — the
# pre-parse behaviour — so it neither ends the run nor reads as Low.
CONFIDENCE_DEFAULT = "Medium"
# Below this many events a single detector can look consistent by chance,
# so a claimed High is capped at SPARSE_CONFIDENCE_CAP.
SPARSE_EVENT_THRESHOLD = 50
SPARSE_CONFIDENCE_CAP = "Medium"


def _build_findings(df: pl.DataFrame) -> dict[str, Any]:
    return {
        "spikes": detect_spikes(df),
        "patterns": detect_patterns(df),
        "cert_anomalies": detect_cert_anomalies(df),
        "correlations": correlate_events(df),
        "event_pairs": detect_event_pair_patterns(df),
        "severity": severity_summary(df),
        "host_ranking": host_error_ranking(df),
        "slow_queries": detect_slow_queries(df),
        "numeric_anomalies": detect_numeric_anomalies(df),
        "event_count": df.height,
    }


def _parse_confidence(report: str, event_count: int) -> str:
    """High/Medium/Low as stated in the report, with the sparse-data cap applied."""
    m = _CONFIDENCE_RE.search(report)
    level = m.group(1).capitalize() if m else CONFIDENCE_DEFAULT
    if level == "High" and event_count < SPARSE_EVENT_THRESHOLD:
        return SPARSE_CONFIDENCE_CAP
    return level


def _clean_spl(query_block: str) -> str:
    return split_area_label(query_block)[1]


def _execute_queries(queries: list[str]) -> tuple[pl.DataFrame | None, list[int]]:
    """Run each follow-up block; return the combined events plus a row count
    per block (parallel to `queries`) so callers can record result_rows."""
    from splunk.client import run_query

    frames: list[pl.DataFrame] = []
    counts: list[int] = []
    for block in queries:
        spl = _clean_spl(block)
        rows: list = []
        if spl:
            logger.info("Executing follow-up query: %s", spl[:120])
            try:
                rows = run_query(spl) or []
            except Exception as exc:
                logger.warning("Query failed — skipping: %s", exc)
        counts.append(len(rows))
        if rows:
            frames.append(pl.DataFrame(rows))

    return (pl.concat(frames, how="diagonal") if frames else None), counts


def _prepare_df(df: pl.DataFrame) -> pl.DataFrame:
    df = extract_timestamps(df)
    df = extract_cert_fields(df)
    return build_timeline(df)
