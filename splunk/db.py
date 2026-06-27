"""
Local SQLite store for Splunk pipeline data.
DB lives at <repo_root>/splunk.db — not committed.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "splunk.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    logger.info("Initialising DB at %s", DB_PATH)
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
                host        TEXT,
                sourcetype  TEXT,
                source      TEXT,
                _time       TEXT,
                _raw        TEXT,
                extra_json  TEXT
            );

            CREATE TABLE IF NOT EXISTS findings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                type        TEXT NOT NULL,
                severity    TEXT,
                host        TEXT,
                body_json   TEXT
            );

            CREATE TABLE IF NOT EXISTS reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                source_file TEXT,
                report_md   TEXT
            );

            CREATE TABLE IF NOT EXISTS sourcetype_schema (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sourcetype  TEXT NOT NULL,
                field_name  TEXT NOT NULL,
                dtype       TEXT NOT NULL,
                first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen   TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(sourcetype, field_name)
            );

            CREATE TABLE IF NOT EXISTS investigation_queries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                iteration   INTEGER NOT NULL,
                area        TEXT,
                spl         TEXT NOT NULL,
                executed    INTEGER NOT NULL DEFAULT 0,
                result_rows INTEGER,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_events_run      ON events(run_id);
            CREATE INDEX IF NOT EXISTS idx_findings_run    ON findings(run_id);
            CREATE INDEX IF NOT EXISTS idx_reports_run     ON reports(run_id);
            CREATE INDEX IF NOT EXISTS idx_schema_stype    ON sourcetype_schema(sourcetype);
            CREATE INDEX IF NOT EXISTS idx_iquery_run      ON investigation_queries(run_id);
        """)


def store_events(df: pl.DataFrame, run_id: str) -> int:
    """Persist a parsed events DataFrame. Returns row count inserted."""
    import json

    known = {"host", "sourcetype", "source", "_time", "_raw"}
    rows = []
    for ev in df.to_dicts():
        extra = {k: v for k, v in ev.items() if k not in known}
        rows.append((
            run_id,
            str(ev.get("host") or ""),
            str(ev.get("sourcetype") or ""),
            str(ev.get("source") or ""),
            str(ev.get("_time") or ""),
            str(ev.get("_raw") or ""),
            json.dumps(extra, default=str),
        ))

    with _connect() as conn:
        conn.executemany(
            "INSERT INTO events (run_id, host, sourcetype, source, _time, _raw, extra_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    logger.info("store_events: inserted %d rows for run_id=%s", len(rows), run_id)
    return len(rows)


def store_findings(findings: dict[str, Any], run_id: str) -> None:
    """Persist findings dict (from detectors) as individual rows."""
    import json

    rows = []
    for ftype, items in findings.items():
        if not isinstance(items, list):
            continue
        for item in items:
            rows.append((
                run_id,
                ftype,
                item.get("severity") or item.get("type") or "",
                item.get("host") or "",
                json.dumps(item, default=str),
            ))

    with _connect() as conn:
        conn.executemany(
            "INSERT INTO findings (run_id, type, severity, host, body_json) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    logger.info("store_findings: inserted %d finding rows for run_id=%s", len(rows), run_id)


def store_report(report_md: str, run_id: str, source_file: str = "") -> None:
    """Persist the final markdown report."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO reports (run_id, source_file, report_md) VALUES (?, ?, ?)",
            (run_id, source_file, report_md),
        )
    logger.info("store_report: saved %d chars for run_id=%s", len(report_md), run_id)


def save_schema(sourcetype: str, schema: dict[str, str]) -> None:
    """Upsert field names + dtypes for a sourcetype."""
    rows = [(sourcetype, field, dtype) for field, dtype in schema.items()]
    with _connect() as conn:
        conn.executemany(
            """INSERT INTO sourcetype_schema (sourcetype, field_name, dtype)
               VALUES (?, ?, ?)
               ON CONFLICT(sourcetype, field_name) DO UPDATE SET
                   dtype     = excluded.dtype,
                   last_seen = datetime('now')""",
            rows,
        )
    logger.debug("save_schema: upserted %d fields for sourcetype=%s", len(rows), sourcetype)


def load_schema(sourcetype: str) -> dict[str, str] | None:
    """Return cached {field: dtype} for a sourcetype, or None if unseen."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT field_name, dtype FROM sourcetype_schema WHERE sourcetype = ?",
            (sourcetype,),
        ).fetchall()
    if not rows:
        logger.debug("load_schema: no cached schema for sourcetype=%s", sourcetype)
        return None
    schema = {r["field_name"]: r["dtype"] for r in rows}
    logger.debug("load_schema: loaded %d fields for sourcetype=%s", len(schema), sourcetype)
    return schema


def store_queries(run_id: str, iteration: int, query_blocks: list[str], result_rows: list[int | None] | None = None) -> None:
    """
    Persist follow-up SPL queries for one investigation iteration.
    query_blocks: list of strings in '-- area\\nSPL' format from generate_followup_queries tool.
    result_rows: optional parallel list of row counts returned by each query (None = not yet executed).
    """
    import re
    _comment_re = re.compile(r"^--\s*(\w+)\s*\n", re.MULTILINE)

    rows = []
    for i, block in enumerate(query_blocks):
        block = block.strip()
        m = _comment_re.match(block)
        area = m.group(1) if m else ""
        spl = _comment_re.sub("", block).strip()
        executed = result_rows is not None
        rrows = result_rows[i] if result_rows and i < len(result_rows) else None
        rows.append((run_id, iteration, area, spl, int(executed), rrows))

    with _connect() as conn:
        conn.executemany(
            "INSERT INTO investigation_queries (run_id, iteration, area, spl, executed, result_rows) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
    logger.info("store_queries: saved %d queries for run_id=%s iteration=%d", len(rows), run_id, iteration)


def get_queries(run_id: str) -> list[dict]:
    """Return all investigation queries for a run, ordered by iteration."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT iteration, area, spl, executed, result_rows, created_at "
            "FROM investigation_queries WHERE run_id = ? ORDER BY iteration, id",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def query_events(run_id: str) -> pl.DataFrame:
    """Load events for a run back into a DataFrame."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY _time", (run_id,)
        ).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame([dict(r) for r in rows])
