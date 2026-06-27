"""
Outer investigator loop — plain Python, no LangGraph.

Each iteration:
  1. Run detectors on current DataFrame
  2. Call agent.analyse(findings) — inner ReAct loop handles its own reasoning
  3. Extract follow-up SPL queries from agent state
  4. Execute queries via client.run_query → new DataFrame
  5. Repeat until confidence is High or max iterations reached
"""

from __future__ import annotations

import logging
import re
from typing import Any

import polars as pl

from splunk.config import INVESTIGATOR_MAX_ITER
from splunk.detectors import (
    correlate_events,
    detect_cert_anomalies,
    detect_patterns,
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
        "severity": severity_summary(df),
        "host_ranking": host_error_ranking(df),
        "event_count": df.height,
    }


def _confidence_high(report: str) -> bool:
    return bool(_CONFIDENCE_HIGH_RE.search(report))


def _clean_spl(query_block: str) -> str:
    """Strip the leading `-- area` comment line from a query block."""
    return _SPL_COMMENT_RE.sub("", query_block).strip()


def _execute_queries(queries: list[str]) -> pl.DataFrame | None:
    """Run each SPL query and union results into a single DataFrame."""
    from splunk.client import run_query

    frames: list[pl.DataFrame] = []
    for block in queries:
        spl = _clean_spl(block)
        if not spl:
            continue
        logger.info("Executing follow-up query: %s", spl[:120])
        try:
            rows = run_query(spl)
            if rows:
                frames.append(pl.DataFrame(rows))
        except Exception as exc:
            logger.warning("Query failed — skipping: %s", exc)

    if not frames:
        return None
    return pl.concat(frames, how="diagonal")


def investigate(df: pl.DataFrame) -> tuple[str, list[str]]:
    """
    Run the iterative investigation loop.

    Args:
        df: Initial parsed DataFrame from parsers.py

    Returns:
        (final_report, all_followup_queries_across_iterations)
    """
    from splunk.agent import analyse

    df = extract_timestamps(df)
    df = extract_cert_fields(df)
    df = build_timeline(df)

    all_queries: list[str] = []
    report = ""

    for iteration in range(1, INVESTIGATOR_MAX_ITER + 1):
        logger.info("Investigator iteration %d/%d — %d events", iteration, INVESTIGATOR_MAX_ITER, df.height)

        findings = _build_findings(df)
        report, queries = analyse(findings)
        all_queries.extend(queries)

        logger.info(
            "Iteration %d complete — confidence_high=%s queries=%d",
            iteration, _confidence_high(report), len(queries),
        )

        if _confidence_high(report):
            logger.info("High confidence reached — stopping after %d iteration(s)", iteration)
            break

        if not queries:
            logger.info("No follow-up queries generated — stopping")
            break

        if iteration == INVESTIGATOR_MAX_ITER:
            logger.warning("Max investigator iterations (%d) reached", INVESTIGATOR_MAX_ITER)
            break

        new_df = _execute_queries(queries)
        if new_df is None or new_df.height == 0:
            logger.info("Follow-up queries returned no new events — stopping")
            break

        new_df = extract_timestamps(new_df)
        new_df = extract_cert_fields(new_df)
        new_df = build_timeline(new_df)
        df = pl.concat([df, new_df], how="diagonal")
        logger.info("DataFrame grown to %d events for next iteration", df.height)

    return report, all_queries
