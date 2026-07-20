"""
Outer investigator loop.

Two entry points:
- investigate(df, run_id)        — sync, called directly from CLI when server is not running
- investigate_task(df, run_id)   — async, spawned as asyncio.create_task() by the server
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import polars as pl

from splunk.config import INVESTIGATOR_MAX_ITER
from splunk.detectors import (
    correlate_events,
    detect_cert_anomalies,
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


def _execute_queries(queries: list[str]) -> pl.DataFrame | None:
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

    return pl.concat(frames, how="diagonal") if frames else None


def _prepare_df(df: pl.DataFrame) -> pl.DataFrame:
    df = extract_timestamps(df)
    df = extract_cert_fields(df)
    return build_timeline(df)


def _emit(run_id: str, event: dict) -> None:
    try:
        from splunk.server import emit, update_active_run
        update_active_run(**{k: v for k, v in event.items() if k in ("iteration", "confidence", "df", "findings")})
        emit(run_id, {k: v for k, v in event.items() if k not in ("df", "findings")})
    except Exception:
        pass


def _finish(run_id: str) -> None:
    try:
        from splunk.server import clear_active_run, close_stream
        close_stream(run_id)
        clear_active_run()
    except Exception:
        pass


def _get_hint(run_id: str) -> str | None:
    """Pull and clear the analyst hint from server state."""
    try:
        from splunk.server import _active_run
        hint = _active_run.pop("hint", None)
        return hint
    except Exception:
        return None


def _is_paused(run_id: str) -> bool:
    try:
        from splunk.server import _active_run
        return bool(_active_run.get("pause_requested"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Shared loop body
# ---------------------------------------------------------------------------

def _run_loop(df: pl.DataFrame, run_id: str, analyse_fn) -> tuple[str, list[str]]:
    """
    Core investigation loop. analyse_fn is either the sync analyse() or a wrapper
    that the async path provides via asyncio.to_thread.
    """
    from splunk.agent import analyse
    from splunk.db import store_queries

    df = _prepare_df(df)
    all_queries: list[str] = []
    report = ""

    for iteration in range(1, INVESTIGATOR_MAX_ITER + 1):
        logger.info("Investigator iteration %d/%d — %d events", iteration, INVESTIGATOR_MAX_ITER, df.height)

        findings = _build_findings(df)

        # Inject analyst hint into findings if provided
        hint = _get_hint(run_id)
        if hint:
            findings["analyst_hint"] = hint
            logger.info("Analyst hint injected: %s", hint)

        report, queries = analyse_fn(findings)
        all_queries.extend(queries)

        confidence = "High" if _confidence_high(report) else "Medium"
        logger.info("Iteration %d — confidence=%s queries=%d", iteration, confidence, len(queries))

        _emit(run_id, {
            "iteration": iteration,
            "confidence": confidence,
            "queries": len(queries),
            "events": df.height,
            "df": df,
            "findings": findings,
        })

        if _confidence_high(report):
            store_queries(run_id, iteration, queries)
            logger.info("High confidence — stopping after %d iteration(s)", iteration)
            break

        if not queries:
            logger.info("No follow-up queries — stopping")
            break

        if iteration == INVESTIGATOR_MAX_ITER:
            logger.warning("Max iterations (%d) reached", INVESTIGATOR_MAX_ITER)
            store_queries(run_id, iteration, queries)
            break

        new_df = _execute_queries(queries)
        result_rows = [new_df.height if new_df is not None else 0] * len(queries)
        store_queries(run_id, iteration, queries, result_rows)

        if new_df is None or new_df.height == 0:
            logger.info("Follow-up queries returned no new events — stopping")
            break

        df = pl.concat([df, _prepare_df(new_df)], how="diagonal")
        logger.info("DataFrame grown to %d events", df.height)

    _finish(run_id)
    return report, all_queries


# ---------------------------------------------------------------------------
# Sync entry point — CLI direct call
# ---------------------------------------------------------------------------

def investigate(df: pl.DataFrame, run_id: str) -> tuple[str, list[str]]:
    """Sync — called from CLI when server is not running."""
    from splunk.agent import analyse
    return _run_loop(df, run_id, analyse)


# ---------------------------------------------------------------------------
# Async entry point — server background task
# ---------------------------------------------------------------------------

async def investigate_task(df: pl.DataFrame, run_id: str) -> tuple[str, list[str]]:
    """
    Async — spawned via asyncio.create_task() by the server.
    Runs the blocking analyse() call in a thread so the event loop stays free.
    Checks pause_requested between iterations.
    """
    from splunk.agent import analyse

    df = _prepare_df(df)
    all_queries: list[str] = []
    report = ""
    from splunk.db import store_queries

    for iteration in range(1, INVESTIGATOR_MAX_ITER + 1):
        # Pause gate — idle until resumed
        while _is_paused(run_id):
            logger.debug("Investigation paused — waiting")
            await asyncio.sleep(1)

        logger.info("Async iteration %d/%d — %d events", iteration, INVESTIGATOR_MAX_ITER, df.height)
        findings = _build_findings(df)

        hint = _get_hint(run_id)
        if hint:
            findings["analyst_hint"] = hint
            logger.info("Analyst hint injected: %s", hint)

        # Run blocking Ollama call in a thread
        report, queries = await asyncio.to_thread(analyse, findings)
        all_queries.extend(queries)

        confidence = "High" if _confidence_high(report) else "Medium"
        logger.info("Async iteration %d — confidence=%s queries=%d", iteration, confidence, len(queries))

        _emit(run_id, {
            "iteration": iteration,
            "confidence": confidence,
            "queries": len(queries),
            "events": df.height,
            "df": df,
            "findings": findings,
        })

        if _confidence_high(report):
            store_queries(run_id, iteration, queries)
            break

        if not queries:
            break

        if iteration == INVESTIGATOR_MAX_ITER:
            store_queries(run_id, iteration, queries)
            break

        new_df = await asyncio.to_thread(_execute_queries, queries)
        result_rows = [new_df.height if new_df is not None else 0] * len(queries)
        store_queries(run_id, iteration, queries, result_rows)

        if new_df is None or new_df.height == 0:
            break

        df = pl.concat([df, _prepare_df(new_df)], how="diagonal")
        logger.info("DataFrame grown to %d events", df.height)

    _finish(run_id)
    return report, all_queries
