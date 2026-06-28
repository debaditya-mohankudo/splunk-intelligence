"""
splunk__ MCP server — exposes the investigation pipeline as MCP tools.

The investigation loop is self-contained: splunk__submit_report returns
{status, findings} in its JSON response. The calling agent reads status
("continue" or "done") and loops on its own — no external hooks required.

Start:
    uv run python -m splunk.mcp_server

Or alongside the UI server — run both:
    ./serve.sh &
    uv run python -m splunk.mcp_server
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="splunk",
    instructions=(
        "Splunk investigation tools. Call splunk__investigate_start to begin, "
        "reason over the returned findings, then call splunk__submit_report with "
        "your report and follow-up queries. Repeat until status=done."
    ),
)


def _server_state() -> dict[str, Any]:
    """Return the live server _active_run dict, or {} if server not running."""
    try:
        from splunk.server import _active_run
        return _active_run
    except Exception:
        return {}


def _emit_sse(run_id: str, event: dict) -> None:
    try:
        from splunk.server import emit, update_active_run
        update_active_run(**{k: v for k, v in event.items() if k in ("iteration", "confidence")})
        emit(run_id, event)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def splunk__investigate_start(
    source: str = "",
    spl: str = "",
    earliest: str = "-24h",
    latest: str = "now",
    repo_path: str = "",
) -> str:
    """
    Start a Splunk investigation. Loads events, runs detectors, returns structured
    findings for Claude to reason over.

    Args:
        source:    Path to a Splunk export file (JSON or CSV). Use this OR spl.
        spl:       SPL query string for a live Splunk query. Requires SPLUNK_URL configured.
        earliest:  Earliest time for live query (default: -24h).
        latest:    Latest time for live query (default: now).
        repo_path: Optional path to the microservice source repo. When provided, the agent
                   can call splunk__lsp_call_chain to trace error log sites back through
                   the call graph. Leave empty to skip code cross-referencing.

    Returns JSON with run_id and findings dict.
    """
    if not source and not spl:
        return json.dumps({"error": "Provide 'source' (file path) or 'spl' (live SPL query)"})

    state = _server_state()
    if state.get("run_id"):
        return json.dumps({"error": f"Investigation already running: {state['run_id']}. Call splunk__pause or wait for it to finish."})

    run_id = str(uuid.uuid4())

    try:
        if source:
            from splunk.runner import _load_from_file
            df = _load_from_file(source)
            source_label = source
        else:
            from splunk.runner import _load_from_live
            df = _load_from_live(spl, earliest, latest)
            source_label = f"live: {spl[:60]}"

        from splunk.db import init_db
        from splunk.investigator import _build_findings, _prepare_df
        init_db()

        df = _prepare_df(df)
        findings = _build_findings(df)

        # Register in server session
        try:
            import asyncio
            from splunk.server import set_active_run, _sse_queues
            set_active_run(run_id, source_label)
            _sse_queues[run_id] = asyncio.Queue(maxsize=100)
            state = _server_state()
            state["df"] = df
            state["findings"] = findings
            if repo_path:
                state["repo_path"] = repo_path
        except Exception:
            pass

        result: dict = {
            "run_id": run_id,
            "source": source_label,
            "event_count": findings["event_count"],
            "findings": json.loads(json.dumps(findings, default=str)),
            "ui_url": f"http://127.0.0.1:8765/ui/runs/{run_id}",
            "next": "Reason over these findings and call splunk__submit_report with your report and follow-up SPL queries.",
        }
        if repo_path:
            result["repo_path"] = repo_path
            result["code_context"] = "splunk__lsp_call_chain is available — use it to trace error log sites back through the call graph before writing follow-up queries."
        return json.dumps(result)

    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
def splunk__submit_report(
    run_id: str,
    report: str,
    queries: list[str] | None = None,
) -> str:
    """
    Submit your investigation report and follow-up SPL queries to the server.
    The server stores the report, executes the queries, builds new findings,
    and returns either next findings (status=continue) or completion (status=done).

    Args:
        run_id:  The run_id from splunk__investigate_start.
        report:  Your markdown investigation report including **Confidence:** High/Medium/Low.
        queries: List of follow-up SPL query strings. Each starts with a '-- area' comment line.

    Returns JSON with status=continue+findings or status=done+ui_url.
    """
    import re
    from splunk.config import INVESTIGATOR_MAX_ITER
    from splunk.db import store_report, store_queries
    from splunk.investigator import _build_findings, _confidence_high, _execute_queries, _prepare_df

    queries = queries or []
    state = _server_state()

    if state.get("run_id") != run_id:
        return json.dumps({"error": f"run_id {run_id!r} not found in active session"})

    iteration = state.get("iteration", 0) + 1
    state["iteration"] = iteration
    confidence = "High" if _confidence_high(report) else "Medium"
    state["confidence"] = confidence

    store_report(report, run_id, state.get("source", ""))
    if queries:
        store_queries(run_id, iteration, queries)

    _emit_sse(run_id, {
        "iteration": iteration,
        "confidence": confidence,
        "queries": len(queries),
        "events": state["df"].height if state.get("df") is not None else 0,
    })

    # Done conditions
    if _confidence_high(report) or iteration >= INVESTIGATOR_MAX_ITER or not queries:
        try:
            from splunk.server import clear_active_run, close_stream
            close_stream(run_id)
            clear_active_run()
        except Exception:
            pass
        ui_url = f"http://127.0.0.1:8765/ui/runs/{run_id}"
        return json.dumps({
            "status": "done",
            "run_id": run_id,
            "confidence": confidence,
            "iterations": iteration,
            "ui_url": ui_url,
        })

    # Execute queries → new findings
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                new_df = pool.submit(_execute_queries, queries).result()
        else:
            new_df = _execute_queries(queries)
    except Exception:
        new_df = None

    if new_df is None or new_df.height == 0:
        try:
            from splunk.server import clear_active_run, close_stream
            close_stream(run_id)
            clear_active_run()
        except Exception:
            pass
        ui_url = f"http://127.0.0.1:8765/ui/runs/{run_id}"
        return json.dumps({
            "status": "done",
            "run_id": run_id,
            "confidence": confidence,
            "iterations": iteration,
            "reason": "no new events from follow-up queries",
            "ui_url": ui_url,
        })

    import polars as pl
    new_df = _prepare_df(new_df)
    df = pl.concat([state["df"], new_df], how="diagonal")
    state["df"] = df
    findings = _build_findings(df)
    state["findings"] = findings

    findings_json = json.loads(json.dumps(findings, default=str))
    event_count = findings["event_count"]
    return json.dumps({
        "status": "continue",
        "run_id": run_id,
        "iteration": iteration,
        "confidence": confidence,
        "event_count": event_count,
        "findings": findings_json,
        "next": "Reason over these findings and call splunk__submit_report again with your updated report and next follow-up queries.",
    })


@mcp.tool()
def splunk__get_findings(run_id: str) -> str:
    """
    Get current findings from the active investigation session.
    Use this to inspect the latest detector output mid-loop.
    """
    state = _server_state()
    if state.get("run_id") != run_id:
        return json.dumps({"error": f"run_id {run_id!r} not active"})
    findings = state.get("findings")
    if findings is None:
        return json.dumps({"error": "No findings yet for this run"})
    return json.dumps({
        "run_id": run_id,
        "iteration": state.get("iteration", 0),
        "confidence": state.get("confidence", "—"),
        "findings": json.loads(json.dumps(findings, default=str)),
    })


@mcp.tool()
def splunk__pause(run_id: str) -> str:
    """Pause the investigation after the current iteration completes."""
    state = _server_state()
    if state.get("run_id") != run_id:
        return json.dumps({"error": f"run_id {run_id!r} not active"})
    state["pause_requested"] = True
    return json.dumps({"status": "paused", "run_id": run_id})


@mcp.tool()
def splunk__query_examples(area: str = "", limit: int = 20) -> str:
    """
    Return example SPL queries from past investigations stored in splunk.db.
    Use this to ground follow-up queries in field names and patterns that have
    actually worked against this Splunk environment.

    Args:
        area:  Filter by area label (e.g. "tls", "cert", "auth"). Empty = all areas.
        limit: Max number of examples to return (default 20).

    Returns JSON list of {area, spl, result_rows, run_id, iteration} sorted by
    most recent first. result_rows is the event count the query returned, or null
    if it was never executed.
    """
    try:
        from splunk.db import _connect
        with _connect() as conn:
            if area:
                rows = conn.execute(
                    "SELECT area, spl, result_rows, run_id, iteration "
                    "FROM investigation_queries "
                    "WHERE area = ? "
                    "ORDER BY rowid DESC LIMIT ?",
                    (area, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT area, spl, result_rows, run_id, iteration "
                    "FROM investigation_queries "
                    "ORDER BY rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        examples = [
            {"area": r[0], "spl": r[1], "result_rows": r[2], "run_id": r[3], "iteration": r[4]}
            for r in rows
        ]
        return json.dumps({"examples": examples, "count": len(examples)})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
def splunk__lsp_call_chain(
    run_id: str,
    symbol: str,
    file_path: str = "",
    line: int = 0,
    direction: str = "callers",
    depth: int = 3,
) -> str:
    """
    Trace a function or symbol through the microservice call graph using LSP.
    Use this during the Reason step to find which code path produced a log error.

    Args:
        run_id:    Active investigation run_id.
        symbol:    Function or class name to look up (e.g. "validate_cert", "TLSHandler").
        file_path: Optional absolute path to the file containing the symbol. Speeds up lookup.
        line:      Optional 1-based line number of the symbol definition.
        direction: "callers" (who calls this?) or "callees" (what does this call?). Default: callers.
        depth:     How many levels up/down to trace. Default: 3.

    Returns JSON with the call chain and file locations, or an error if repo_path was not
    provided at investigate_start or if the symbol cannot be resolved.
    """
    import subprocess, pathlib

    state = _server_state()
    if state.get("run_id") != run_id:
        return json.dumps({"error": f"run_id {run_id!r} not active"})

    repo_path = state.get("repo_path", "")
    if not repo_path:
        return json.dumps({
            "error": "No repo_path in session. Re-start the investigation with repo_path set to the microservice repo.",
            "hint": "Call splunk__investigate_start again with repo_path='<path to repo>'.",
        })

    repo = pathlib.Path(repo_path)
    if not repo.exists():
        return json.dumps({"error": f"repo_path does not exist: {repo_path}"})

    try:
        # Use ripgrep to locate the symbol definition across the repo
        rg_cmd = ["rg", "--json", "-n", f"def {symbol}|class {symbol}", str(repo)]
        rg = subprocess.run(rg_cmd, capture_output=True, text=True, timeout=10)

        definitions: list[dict] = []
        for line_raw in rg.stdout.splitlines():
            try:
                obj = json.loads(line_raw)
                if obj.get("type") == "match":
                    data = obj["data"]
                    definitions.append({
                        "file": data["path"]["text"],
                        "line": data["line_number"],
                        "text": data["lines"]["text"].strip(),
                    })
            except Exception:
                continue

        if not definitions:
            return json.dumps({"error": f"Symbol '{symbol}' not found in {repo_path}"})

        # For each definition, find callers (references) or callees (outgoing calls)
        results = []
        for defn in definitions[:3]:
            ref_cmd = ["rg", "--json", "-n", symbol, str(repo)]
            ref = subprocess.run(ref_cmd, capture_output=True, text=True, timeout=10)

            refs: list[dict] = []
            for line_raw in ref.stdout.splitlines():
                try:
                    obj = json.loads(line_raw)
                    if obj.get("type") == "match":
                        data = obj["data"]
                        text = data["lines"]["text"].strip()
                        # Skip the definition itself and comments
                        if f"def {symbol}" in text or f"class {symbol}" in text:
                            continue
                        if text.lstrip().startswith("#"):
                            continue
                        refs.append({
                            "file": data["path"]["text"],
                            "line": data["line_number"],
                            "text": text,
                        })
                except Exception:
                    continue

            results.append({
                "definition": defn,
                direction: refs[:depth * 5],
            })

        return json.dumps({
            "symbol": symbol,
            "repo": repo_path,
            "direction": direction,
            "depth": depth,
            "results": results,
        })

    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Symbol search timed out"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
def splunk__hint(run_id: str, hint: str) -> str:
    """
    Inject an analyst hint into the investigation for the next iteration.
    The hint is included in the findings passed to the next reasoning step.
    Example: "focus on web-01 cert chain errors after 14:30 UTC"
    """
    state = _server_state()
    if state.get("run_id") != run_id:
        return json.dumps({"error": f"run_id {run_id!r} not active"})
    state["hint"] = hint
    return json.dumps({"status": "hint set", "run_id": run_id, "hint": hint})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
