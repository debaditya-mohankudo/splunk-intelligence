"""
splunk__ MCP server — exposes the investigation pipeline as MCP tools.

The investigation loop is self-contained: splunk__submit_report returns
{status, findings} in its JSON response. The calling agent reads status
("continue" or "done") and loops on its own — no external hooks required.

Tools are thin wrappers around splunk/connector.py, which owns loading data,
running detectors, and persisting run state (no server process involved).

Start:
    uv run python -m splunk.mcp_server

Optionally alongside the TUI, for watching live progress:
    uv run python -m splunk.tui &
    uv run python -m splunk.mcp_server
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from splunk import connector

mcp = FastMCP(
    name="splunk",
    instructions=(
        "Splunk investigation tools. Call splunk__investigate_start to begin, "
        "reason over the returned findings, then call splunk__submit_report with "
        "your report and follow-up queries. Repeat until status=done."
    ),
)


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
    return json.dumps(connector.start_investigation(
        source=source, spl=spl, earliest=earliest, latest=latest, repo_path=repo_path,
    ))


@mcp.tool()
def splunk__submit_report(
    run_id: str,
    report: str,
    queries: list[str] | None = None,
) -> str:
    """
    Submit your investigation report and follow-up SPL queries.
    Stores the report, executes the queries, builds new findings, and returns
    either next findings (status=continue) or completion (status=done).

    Args:
        run_id:  The run_id from splunk__investigate_start.
        report:  Your markdown investigation report including **Confidence:** High/Medium/Low.
        queries: List of follow-up SPL query strings. Each starts with a '-- area' comment line.

    Returns JSON with status=continue+findings or status=done+ui_url.
    """
    return json.dumps(connector.submit_report(run_id, report, queries))


@mcp.tool()
def splunk__get_findings(run_id: str) -> str:
    """
    Get current findings from the active investigation session.
    Use this to inspect the latest detector output mid-loop.
    """
    return json.dumps(connector.get_findings(run_id))


@mcp.tool()
def splunk__pause(run_id: str) -> str:
    """Pause the investigation after the current iteration completes."""
    return json.dumps(connector.request_pause(run_id))


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

    session = connector.get_session(run_id)
    if session is None:
        return json.dumps({"error": f"run_id {run_id!r} not active"})

    repo_path = session.get("repo_path", "")
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
    return json.dumps(connector.set_hint(run_id, hint))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
