"""FastAPI router — all /ui/* routes for the Splunk investigation UI."""
from __future__ import annotations

import asyncio
import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from splunk.ui.deps import render

ui_router = APIRouter()

_CONFIDENCE_RE = re.compile(r"\*\*Confidence:\*\*\s*(High|Medium|Low)", re.IGNORECASE)


def _extract_confidence(report_md: str) -> str:
    m = _CONFIDENCE_RE.search(report_md or "")
    return m.group(1) if m else "—"


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@ui_router.get("/ui/", response_class=HTMLResponse)
async def ui_root():
    return RedirectResponse(url="/ui/runs/", status_code=302)


# ---------------------------------------------------------------------------
# Cockpit strip — polled every 10s
# ---------------------------------------------------------------------------

@ui_router.get("/ui/cockpit", response_class=HTMLResponse)
async def ui_cockpit():
    from splunk.server import _active_run
    return render("ui/partials/cockpit.html", active=_active_run)


# ---------------------------------------------------------------------------
# Runs — sidebar + detail
# ---------------------------------------------------------------------------

@ui_router.get("/ui/runs/", response_class=HTMLResponse)
async def ui_runs(request: Request):
    from splunk.db import _connect
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, source_file, created_at, report_md FROM reports ORDER BY created_at DESC"
        ).fetchall()

    runs = []
    for r in rows:
        runs.append({
            "run_id": r["run_id"],
            "source": r["source_file"] or "—",
            "created_at": (r["created_at"] or "")[:16],
            "confidence": _extract_confidence(r["report_md"] or ""),
        })

    return render("ui/runs/list.html", runs=runs)


@ui_router.get("/ui/runs/{run_id}", response_class=HTMLResponse)
async def ui_run_detail(run_id: str):
    from splunk.db import _connect, get_queries
    import markdown as md

    with _connect() as conn:
        row = conn.execute(
            "SELECT run_id, source_file, created_at, report_md FROM reports WHERE run_id = ?",
            (run_id,),
        ).fetchone()

    if not row:
        return HTMLResponse("<div class='empty-state'>Run not found</div>")

    report_html = md.markdown(row["report_md"] or "", extensions=["fenced_code", "tables"])
    confidence = _extract_confidence(row["report_md"] or "")
    queries = get_queries(run_id)

    return render(
        "ui/partials/run_detail.html",
        run_id=run_id,
        source=row["source_file"] or "—",
        created_at=(row["created_at"] or "")[:16],
        confidence=confidence,
        report_html=report_html,
        queries=queries,
    )


# ---------------------------------------------------------------------------
# Queries right panel
# ---------------------------------------------------------------------------

@ui_router.get("/ui/runs/{run_id}/queries", response_class=HTMLResponse)
async def ui_run_queries(run_id: str):
    from splunk.db import get_queries
    queries = get_queries(run_id)
    return render("ui/partials/queries.html", run_id=run_id, queries=queries)


# ---------------------------------------------------------------------------
# SSE stream — living session
# ---------------------------------------------------------------------------

@ui_router.get("/ui/runs/{run_id}/stream")
async def ui_run_stream(run_id: str):
    from splunk.server import _sse_queues

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
