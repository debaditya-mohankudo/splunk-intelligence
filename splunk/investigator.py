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

_CONFIDENCE_HIGH_RE = re.compile(r"\*\*Confidence:\*\*\s*High", re.IGNORECASE)
_SPL_COMMENT_RE = re.compile(r"^--\s*\w+\s*$", re.MULTILINE)


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


def _confidence_high(report: str) -> bool:
    return bool(_CONFIDENCE_HIGH_RE.search(report))


def _clean_spl(query_block: str) -> str:
    return _SPL_COMMENT_RE.sub("", query_block).strip()


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
