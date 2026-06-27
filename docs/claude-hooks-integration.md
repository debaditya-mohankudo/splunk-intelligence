# Claude-Hooks Integration — How the Splunk Investigation Loop Works

## How it starts

Two processes run independently:

```bash
# Terminal 1 — Splunk UI + investigation server
./serve.sh          # uvicorn splunk.server:app on port 8765

# Terminal 2 — MCP tool server
uv run python -m splunk.mcp_server   # FastMCP on stdio
```

The MCP server registers 5 tools (`splunk__*`) into Claude Code's tool registry via `~/.claude/mcp_servers.json`. Once registered, Claude sees those tools in every session.

You kick it off by saying something like:

> *"Investigate results/cert_errors.json"*

Claude calls `splunk__investigate_start(source="results/cert_errors.json")`.

---

## What investigate_start does

```
splunk__investigate_start
  └─> _load_from_file(source)        # Polars reads JSON/CSV
  └─> _prepare_df(df)                # normalise timestamps, cert fields
  └─> _build_findings(df)            # run all detectors → structured dict
  └─> set_active_run(run_id, ...)    # register in _active_run server session
  └─> return {run_id, findings, ui_url}
```

Claude receives `findings` directly in the tool response — spikes, patterns, cert anomalies, host rankings, timeline — all structured.

---

## Claude reasons and submits

Claude reads the findings, writes a markdown investigation report, generates follow-up SPL queries (each prefixed with a `-- area` comment line), and calls:

```
splunk__submit_report(run_id, report, queries)
```

---

## How the hook sends findings back automatically

This is where **claude-hooks** closes the loop. The `PostToolUse` hook fires after every tool call. The hook server's `run_post_tool()` invokes the session graph, which routes `splunk__submit_report` → `SplunkPostToolNode`:

```
Claude calls splunk__submit_report
       │
       ▼
  [splunk/mcp_server.py]
  - stores report in splunk.db
  - executes follow-up SPL queries via client.py
  - builds new findings from fresh events
  - returns {status: "continue", findings: {...}}
       │
       ▼
  [claude-hooks: PostToolUse hook fires]
  hooks/server.py → run_post_tool() → session_graph
       │
       ▼
  [session_graph _post_tool_route]
  tool == "splunk__submit_report" → "splunk_post_tool"
       │
       ▼
  [SplunkPostToolNode.__call__]
  extracts findings from tool_result
  builds additionalSystemPrompt:
    "## Splunk Investigation — Iteration 1 complete
     run_id: `abc` · Confidence: Medium · Events: 320
     New findings: {...json...}
     Call splunk__submit_report again."
       │
       ▼
  returns {"pending_hook_output": {"additionalSystemPrompt": "..."}}
       │
       ▼
  [hooks/server.py]
  returns additionalSystemPrompt in HTTP response to Claude Code
       │
       ▼
  Claude Code injects it into the system prompt for the next turn
```

Claude sees the new findings **without any manual intervention** — it reads the injected system prompt and calls `splunk__submit_report` again.

---

## Loop termination

`submit_report` returns `status: "done"` when any of these are true:

- Report contains `**Confidence:** High`
- `iteration >= INVESTIGATOR_MAX_ITER` (default 3, set via `SPLUNK_INVESTIGATOR_MAX_ITER`)
- No follow-up queries were generated

`SplunkPostToolNode` then injects a completion message with the UI link. Claude wraps up.

---

## The key insight

The PostToolUse hook is **the only reason Claude loops automatically**. Without it, Claude would call `submit_report` once and stop — it wouldn't know there were new findings waiting. The hook bridges the gap by injecting those findings into Claude's next context window as if you'd pasted them yourself.

---

## Files involved

| File | Role |
|------|------|
| `splunk/mcp_server.py` | FastMCP server — 5 `splunk__` tools, server session is the state layer |
| `splunk/server.py` | FastAPI server — owns `_active_run`, SSE queues, UI routes |
| `splunk/investigator.py` | `_build_findings`, `_prepare_df`, `_execute_queries` helpers |
| `splunk/client.py` | Executes follow-up SPL queries via Splunk REST API |
| `claude-hooks: nodes/splunk_post_tool.py` | `SplunkPostToolNode` — injects next findings into `additionalSystemPrompt` |
| `claude-hooks: session_graph.py` | Routes `splunk__submit_report` → `splunk_post_tool` node |
