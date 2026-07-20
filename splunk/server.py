"""FastAPI server for the Splunk investigation API. Consumed by the TUI (splunk/tui.py),
MCP tools (mcp_server.py), and the REST/curl fallback documented in AGENTS.md."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

import polars as pl
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Splunk Investigation API", docs_url=None, redoc_url=None)

_CONFIDENCE_RE = re.compile(r"\*\*Confidence:\*\*\s*(High|Medium|Low)", re.IGNORECASE)


def _extract_confidence(report_md: str) -> str:
    m = _CONFIDENCE_RE.search(report_md or "")
    return m.group(1) if m else "—"


# ---------------------------------------------------------------------------
# Shared live state
# ---------------------------------------------------------------------------

_active_run: dict[str, Any] = {}           # live investigation state
_sse_queues: dict[str, asyncio.Queue] = {} # run_id → SSE event queue
_active_task: asyncio.Task | None = None   # running investigate_task


def set_active_run(run_id: str, source: str) -> None:
    _active_run.clear()
    _active_run.update({
        "run_id": run_id,
        "source": source,
        "iteration": 0,
        "confidence": "—",
        "pause_requested": False,
        "hint": None,
        "df": None,
        "findings": None,
    })


def update_active_run(**kwargs) -> None:
    _active_run.update(kwargs)


def clear_active_run() -> None:
    _active_run.clear()


def emit(run_id: str, event: dict) -> None:
    q = _sse_queues.get(run_id)
    if q:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def close_stream(run_id: str) -> None:
    emit(run_id, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# API — investigation control
# ---------------------------------------------------------------------------

class InvestigateRequest(BaseModel):
    source: str | None = None   # file path
    spl: str | None = None       # live SPL query
    earliest: str = "-24h"
    latest: str = "now"


class HintRequest(BaseModel):
    hint: str


class ReportRequest(BaseModel):
    run_id: str
    report: str
    queries: list[str] = []


@app.post("/api/investigate")
async def api_investigate(req: InvestigateRequest):
    global _active_task

    if _active_run.get("run_id") and _active_task and not _active_task.done():
        raise HTTPException(status_code=409, detail="Investigation already running")

    if not req.source and not req.spl:
        raise HTTPException(status_code=400, detail="Provide 'source' (file path) or 'spl' (live query)")

    run_id = str(uuid.uuid4())

    # Load DataFrame
    if req.source:
        from splunk.runner import _load_from_file
        df = _load_from_file(req.source)
        source_label = req.source
    else:
        from splunk.runner import _load_from_live
        df = _load_from_live(req.spl, req.earliest, req.latest)
        source_label = f"live: {req.spl[:60]}"

    # Init DB + logging
    from splunk.db import init_db
    from splunk.logger import RunLogger
    init_db()

    set_active_run(run_id, source_label)
    _sse_queues[run_id] = asyncio.Queue(maxsize=100)

    async def _run():
        from splunk.investigator import investigate_task
        from splunk.db import store_report
        from splunk.runner import _write_report
        import logging as _logging
        with RunLogger(run_id) as log:
            log.info("investigator.start", source=source_label)
            report, _ = await investigate_task(df, run_id)
            store_report(report, run_id, source_label)

    _active_task = asyncio.create_task(_run())
    return JSONResponse({"run_id": run_id, "status": "started"})


@app.get("/api/runs/active")
async def api_active_run():
    if not _active_run:
        return JSONResponse({"status": "idle"})
    return JSONResponse({
        "run_id": _active_run.get("run_id"),
        "source": _active_run.get("source"),
        "iteration": _active_run.get("iteration", 0),
        "confidence": _active_run.get("confidence", "—"),
        "pause_requested": _active_run.get("pause_requested", False),
        "events": _active_run["df"].height if isinstance(_active_run.get("df"), pl.DataFrame) else None,
        "status": "paused" if _active_run.get("pause_requested") else "running",
    })


@app.get("/api/runs")
async def api_runs():
    """List past runs (finished + in-progress reports), most recent first."""
    from splunk.db import _connect

    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, source_file, created_at, report_md FROM reports ORDER BY created_at DESC"
        ).fetchall()

    runs = [
        {
            "run_id": r["run_id"],
            "source": r["source_file"] or "—",
            "created_at": (r["created_at"] or "")[:16],
            "confidence": _extract_confidence(r["report_md"] or ""),
        }
        for r in rows
    ]
    return JSONResponse({"runs": runs})


@app.get("/api/runs/{run_id}")
async def api_run_detail(run_id: str):
    """Report + follow-up queries for a single run."""
    from splunk.db import _connect, get_queries

    with _connect() as conn:
        row = conn.execute(
            "SELECT run_id, source_file, created_at, report_md FROM reports WHERE run_id = ?",
            (run_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Run not found")

    return JSONResponse({
        "run_id": run_id,
        "source": row["source_file"] or "—",
        "created_at": (row["created_at"] or "")[:16],
        "confidence": _extract_confidence(row["report_md"] or ""),
        "report_md": row["report_md"] or "",
        "queries": get_queries(run_id),
    })


@app.get("/api/runs/{run_id}/stream")
async def api_run_stream(run_id: str):
    """SSE stream of live iteration events for a run."""
    queue: asyncio.Queue = _sse_queues.setdefault(run_id, asyncio.Queue())

    async def event_generator():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30)
                if event is None:
                    yield "event: done\ndata: {}\n\n"
                    break
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/investigate/start")
async def api_investigate_start(req: InvestigateRequest):
    """Claude-session mode: load df, build initial findings, return them for Claude to reason over."""
    if _active_run.get("run_id"):
        raise HTTPException(status_code=409, detail="Investigation already running")
    if not req.source and not req.spl:
        raise HTTPException(status_code=400, detail="Provide 'source' or 'spl'")

    run_id = str(uuid.uuid4())

    if req.source:
        from splunk.runner import _load_from_file
        df = _load_from_file(req.source)
        source_label = req.source
    else:
        from splunk.runner import _load_from_live
        df = _load_from_live(req.spl, req.earliest, req.latest)
        source_label = f"live: {req.spl[:60]}"

    from splunk.db import init_db
    from splunk.investigator import _build_findings, _prepare_df
    init_db()

    df = _prepare_df(df)
    findings = _build_findings(df)

    set_active_run(run_id, source_label)
    _active_run["df"] = df
    _active_run["findings"] = findings
    _active_run["iteration"] = 0
    _sse_queues[run_id] = asyncio.Queue(maxsize=100)

    import json
    return JSONResponse({
        "run_id": run_id,
        "iteration": 0,
        "findings": json.loads(json.dumps(findings, default=str)),
        "ui_url": f"Run: uv run python -m splunk.tui  (select run {run_id[:8]})",
    })


@app.post("/api/investigate/report")
async def api_investigate_report(req: ReportRequest):
    """
    Claude-session mode: receive report + queries from Claude, store them,
    execute follow-up queries, build next findings. Returns next findings
    or {status: done} when confidence is High or max iterations reached.
    """
    import json, re
    from splunk.config import INVESTIGATOR_MAX_ITER
    from splunk.db import store_report, store_queries
    from splunk.investigator import _build_findings, _confidence_high, _execute_queries, _prepare_df

    if _active_run.get("run_id") != req.run_id:
        raise HTTPException(status_code=404, detail="run_id not found or not active")

    iteration = _active_run.get("iteration", 0) + 1
    _active_run["iteration"] = iteration
    confidence = "High" if _confidence_high(req.report) else "Medium"
    _active_run["confidence"] = confidence

    # Persist report
    source = _active_run.get("source", "")
    store_report(req.report, req.run_id, source)

    # Persist queries
    if req.queries:
        store_queries(req.run_id, iteration, req.queries)

    emit(req.run_id, {
        "iteration": iteration,
        "confidence": confidence,
        "queries": len(req.queries),
        "events": _active_run["df"].height if _active_run.get("df") is not None else 0,
    })

    if _confidence_high(req.report) or iteration >= INVESTIGATOR_MAX_ITER or not req.queries:
        close_stream(req.run_id)
        clear_active_run()
        return JSONResponse({
            "status": "done",
            "run_id": req.run_id,
            "confidence": confidence,
            "iterations": iteration,
            "ui_url": f"Run: uv run python -m splunk.tui  (select run {req.run_id[:8]})",
        })

    # Execute follow-up queries → new findings
    new_df = await asyncio.to_thread(_execute_queries, req.queries)
    if new_df is None or new_df.height == 0:
        close_stream(req.run_id)
        clear_active_run()
        return JSONResponse({
            "status": "done",
            "run_id": req.run_id,
            "confidence": confidence,
            "iterations": iteration,
            "reason": "no new events from follow-up queries",
            "ui_url": f"Run: uv run python -m splunk.tui  (select run {req.run_id[:8]})",
        })

    import polars as pl
    new_df = _prepare_df(new_df)
    df = pl.concat([_active_run["df"], new_df], how="diagonal")
    _active_run["df"] = df
    findings = _build_findings(df)
    _active_run["findings"] = findings

    return JSONResponse({
        "status": "continue",
        "run_id": req.run_id,
        "iteration": iteration,
        "confidence": confidence,
        "findings": json.loads(json.dumps(findings, default=str)),
    })


@app.post("/api/investigate/pause")
async def api_pause():
    if not _active_run:
        raise HTTPException(status_code=404, detail="No active investigation")
    _active_run["pause_requested"] = True
    return JSONResponse({"status": "paused"})


@app.post("/api/investigate/resume")
async def api_resume():
    if not _active_run:
        raise HTTPException(status_code=404, detail="No active investigation")
    _active_run["pause_requested"] = False
    return JSONResponse({"status": "resumed"})


@app.post("/api/investigate/hint")
async def api_hint(req: HintRequest):
    if not _active_run:
        raise HTTPException(status_code=404, detail="No active investigation")
    _active_run["hint"] = req.hint
    return JSONResponse({"status": "hint set", "hint": req.hint})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("splunk.server:app", host="127.0.0.1", port=8765, reload=True)
