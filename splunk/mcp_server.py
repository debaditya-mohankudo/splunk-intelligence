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

from splunk import connector, db

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
                   can call splunk__find_symbol_refs to locate where error log sites are
                   defined and referenced. Leave empty to skip code cross-referencing.

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
    If the analyst paused the run, returns status=paused without storing
    anything; resubmit the same report after they resume.

    Args:
        run_id:  The run_id from splunk__investigate_start.
        report:  Your markdown investigation report including **Confidence:** High/Medium/Low.
        queries: List of follow-up SPL query strings. Each starts with a '-- <area>' label line, e.g. '-- tls'.

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
    """Pause the investigation: the next splunk__submit_report returns status=paused until the analyst resumes (TUI or `connector resume`)."""
    return json.dumps(connector.request_pause(run_id))


@mcp.tool()
def splunk__query_examples(area: str = "", limit: int = 20, sort: str = "recent") -> str:
    """
    Return example SPL queries from past investigations stored in splunk.db.
    Use this to ground follow-up queries in field names and patterns that have
    actually worked against this Splunk environment.

    Args:
        area:  Filter by area label (e.g. "tls", "cert", "auth"). Empty = all areas.
        limit: Max number of examples to return (default 20).
        sort:  "recent" (default) orders by most recently stored first.
               "effective" orders executed queries by result_rows descending
               (queries that returned more data surface first; unexecuted/null
               queries are ordered last) — use this to find SPL patterns that
               have actually worked, not just what was tried most recently.

    Returns JSON {examples, count, area_stats}. Each example is
    {area, spl, result_rows, run_id, iteration}; result_rows is the event
    count the query returned, or null if it was never executed. area_stats
    maps area -> {executed_count, avg_result_rows, median_result_rows},
    computed over all matching history (not just the returned page), so you
    can tell which areas reliably return data.
    """
    try:
        from splunk.db import get_query_examples
        examples, area_stats = get_query_examples(area=area, limit=limit, sort=sort)
        return json.dumps({"examples": examples, "count": len(examples), "area_stats": area_stats})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
def splunk__find_symbol_refs(run_id: str, symbol: str, max_refs: int = 15) -> str:
    """
    Find where a function or class is defined, and every line that mentions it, in the
    repo_path given to splunk__investigate_start. A whole-word text search (grep),
    not a call graph: use it to locate the code that emits an error log line.

    Args:
        run_id:   Active investigation run_id.
        symbol:   Function or class name (e.g. "validate_cert", "TLSHandler").
        max_refs: Maximum reference lines returned per definition. Default: 15.

    Returns JSON with {symbol, repo, results: [{definition, references}]}, or an error if
    repo_path was not provided at investigate_start or the symbol is not found.
    """
    return json.dumps(connector.find_symbol_refs(run_id, symbol, max_refs))


@mcp.tool()
def splunk__hint(run_id: str, hint: str) -> str:
    """
    Inject an analyst hint into the investigation for the next iteration.
    The hint is included in the findings passed to the next reasoning step.
    Example: "focus on web-01 cert chain errors after 14:30 UTC"
    """
    return json.dumps(connector.set_hint(run_id, hint))


@mcp.tool()
def splunk__check_alerts(severity: str = "") -> str:
    """
    Read unacknowledged alerts written by the standalone watcher (standalone/watcher.py),
    which polls Splunk continuously and independently of any agent session.

    Args:
        severity: Optional filter — "critical", "warning", or "info". Empty = all severities.

    Returns JSON list of alerts (most recent first), each with id, run_id, ts, severity,
    summary, and detail (the full detector hit). Call splunk__ack_alert on each id you've
    acted on — alerts are never auto-acknowledged.
    """
    alerts = db.get_alerts(acked=False, severity=severity or None)
    for a in alerts:
        a["detail"] = json.loads(a.pop("detail_json"))
    return json.dumps(alerts)


@mcp.tool()
def splunk__ack_alert(alert_id: int) -> str:
    """Mark a watcher alert as acknowledged so it no longer appears in splunk__check_alerts."""
    db.ack_alert(alert_id)
    return json.dumps({"status": "ok"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
