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
                app         TEXT,
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
                report_md   TEXT,
                spl         TEXT,
                earliest    TEXT,
                latest      TEXT
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

            CREATE TABLE IF NOT EXISTS active_runs (
                run_id           TEXT PRIMARY KEY,
                source           TEXT,
                iteration        INTEGER NOT NULL DEFAULT 0,
                confidence       TEXT,
                events           INTEGER,
                pause_requested  INTEGER NOT NULL DEFAULT 0,
                hint             TEXT,
                findings_json    TEXT,
                updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                ts          TEXT NOT NULL DEFAULT (datetime('now')),
                severity    TEXT,
                summary     TEXT,
                detail_json TEXT,
                acked       INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS watcher_state (
                id          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                last_time   TEXT,
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_events_run      ON events(run_id);
            CREATE INDEX IF NOT EXISTS idx_findings_run    ON findings(run_id);
            CREATE INDEX IF NOT EXISTS idx_reports_run     ON reports(run_id);
            CREATE INDEX IF NOT EXISTS idx_schema_stype    ON sourcetype_schema(sourcetype);
            CREATE INDEX IF NOT EXISTS idx_iquery_run      ON investigation_queries(run_id);
            CREATE INDEX IF NOT EXISTS idx_alerts_acked    ON alerts(acked);
        """)
        # Migration for databases created before spl/earliest/latest existed
        # on reports — CREATE TABLE IF NOT EXISTS above only helps fresh DBs.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(reports)")}
        for col in ("spl", "earliest", "latest"):
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE reports ADD COLUMN {col} TEXT")

        # Migration for databases created before app existed on events.
        existing_event_cols = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
        if "app" not in existing_event_cols:
            conn.execute("ALTER TABLE events ADD COLUMN app TEXT")


def store_events(df: pl.DataFrame, run_id: str) -> int:
    """Persist a parsed events DataFrame. Returns row count inserted."""
    import json

    known = {"host", "sourcetype", "source", "app", "_time", "_raw"}
    rows = []
    for ev in df.to_dicts():
        extra = {k: v for k, v in ev.items() if k not in known}
        rows.append((
            run_id,
            str(ev.get("host") or ""),
            str(ev.get("sourcetype") or ""),
            str(ev.get("source") or ""),
            str(ev.get("app") or ""),
            str(ev.get("_time") or ""),
            str(ev.get("_raw") or ""),
            json.dumps(extra, default=str),
        ))

    with _connect() as conn:
        conn.executemany(
            "INSERT INTO events (run_id, host, sourcetype, source, app, _time, _raw, extra_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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


def store_report(
    report_md: str,
    run_id: str,
    source_file: str = "",
    spl: str = "",
    earliest: str = "",
    latest: str = "",
) -> None:
    """Persist the final markdown report. spl/earliest/latest are the full,
    untruncated live-query params (when this run came from a live SPL
    search) — kept as a durable reference distinct from source_file, which
    upstream callers may pass in truncated (e.g. "live: <spl[:60]>")."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO reports (run_id, source_file, report_md, spl, earliest, latest) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, source_file, report_md, spl, earliest, latest),
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


_ACTIVE_RUN_FIELDS = {"source", "iteration", "confidence", "events", "pause_requested", "hint", "findings_json"}


def upsert_active_run(run_id: str, **fields: Any) -> None:
    """Insert or update a live-run status row. Cross-process substitute for the
    old in-memory _active_run dict — any process can read current progress."""
    fields = {k: v for k, v in fields.items() if k in _ACTIVE_RUN_FIELDS}
    with _connect() as conn:
        if not fields:
            conn.execute(
                "INSERT INTO active_runs (run_id) VALUES (?) "
                "ON CONFLICT(run_id) DO UPDATE SET updated_at = datetime('now')",
                (run_id,),
            )
            return
        columns = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{k} = excluded.{k}" for k in fields.keys())
        conn.execute(
            f"""INSERT INTO active_runs (run_id, {columns})
                VALUES (?, {placeholders})
                ON CONFLICT(run_id) DO UPDATE SET
                    {updates},
                    updated_at = datetime('now')""",
            (run_id, *fields.values()),
        )
    logger.debug("upsert_active_run: run_id=%s fields=%s", run_id, list(fields.keys()))


def clear_active_run_row(run_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM active_runs WHERE run_id = ?", (run_id,))
    logger.debug("clear_active_run_row: run_id=%s", run_id)


def get_active_run_row(run_id: str | None = None) -> dict | None:
    """With run_id: look up that row. Without: return the most-recently-updated
    active run (used by the TUI's cockpit — this tool assumes one analyst at a time)."""
    with _connect() as conn:
        if run_id:
            row = conn.execute("SELECT * FROM active_runs WHERE run_id = ?", (run_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM active_runs ORDER BY updated_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def pop_hint(run_id: str) -> str | None:
    """Read the analyst hint for a run, then clear it — same pop semantics the
    old in-memory _active_run dict had."""
    with _connect() as conn:
        row = conn.execute("SELECT hint FROM active_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row and row["hint"]:
            conn.execute("UPDATE active_runs SET hint = NULL WHERE run_id = ?", (run_id,))
            return row["hint"]
    return None


def query_events(run_id: str) -> pl.DataFrame:
    """Load events for a run back into a DataFrame."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY _time", (run_id,)
        ).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Watcher — alerts + bookmark (splunk/watcher.py, splunk__check_alerts)
# ---------------------------------------------------------------------------

def get_watch_bookmark() -> str | None:
    """Return the watcher's last-seen _time, or None if it has never run."""
    with _connect() as conn:
        row = conn.execute("SELECT last_time FROM watcher_state WHERE id = 1").fetchone()
    return row["last_time"] if row else None


def set_watch_bookmark(last_time: str) -> None:
    """Persist the watcher's last-seen _time so a restart resumes from here."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO watcher_state (id, last_time) VALUES (1, ?)
               ON CONFLICT(id) DO UPDATE SET
                   last_time  = excluded.last_time,
                   updated_at = datetime('now')""",
            (last_time,),
        )
    logger.debug("set_watch_bookmark: last_time=%s", last_time)


def store_alerts(hits: list[dict[str, Any]], run_id: str) -> None:
    """Persist detector hits from a watcher cycle as alert rows.
    Each hit must carry a 'severity' and 'summary' key (assigned by the caller);
    the full hit dict is kept in detail_json for splunk__check_alerts."""
    import json

    rows = [
        (run_id, hit.get("severity") or "warning", hit.get("summary") or "", json.dumps(hit, default=str))
        for hit in hits
    ]
    if not rows:
        return
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO alerts (run_id, severity, summary, detail_json) VALUES (?, ?, ?, ?)",
            rows,
        )
    logger.info("store_alerts: inserted %d alert(s) for run_id=%s", len(rows), run_id)


def get_alerts(acked: bool = False, severity: str | None = None) -> list[dict]:
    """Return alert rows, most recent first. acked=False (default) returns only unacked rows."""
    query = "SELECT * FROM alerts WHERE acked = ?"
    params: list[Any] = [int(acked)]
    if severity:
        query += " AND severity = ?"
        params.append(severity)
    query += " ORDER BY ts DESC"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def ack_alert(alert_id: int) -> None:
    """Mark an alert as acknowledged."""
    with _connect() as conn:
        conn.execute("UPDATE alerts SET acked = 1 WHERE id = ?", (alert_id,))
    logger.debug("ack_alert: id=%d", alert_id)
