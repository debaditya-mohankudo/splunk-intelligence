# Investigation Loop — How It Works

## Setup

Two processes run independently:

```bash
# Terminal 1 — Splunk UI + investigation server
./serve.sh          # uvicorn splunk.server:app on port 8765

# Terminal 2 — MCP tool server
uv run python -m splunk.mcp_server   # FastMCP on stdio
```

The MCP server registers 5 tools (`splunk__*`). Any MCP-compatible agent (Claude Code, GitHub Copilot, etc.) can drive the loop.

---

## Loop

```
1. splunk__investigate_start(source or spl)
       ↓ returns {run_id, findings, event_count}

2. Agent reasons over findings → writes report + follow-up SPL queries

3. splunk__submit_report(run_id, report, queries)
       ↓ returns {status, findings, confidence, ...}

4. If status == "continue" → go to step 2
   If status == "done"     → present final summary + ui_url
```

The loop is **self-contained**: all findings are returned directly in the tool response. No external hooks, no injected system prompts. The agent reads `status` from each `submit_report` response and decides whether to continue.

---

## Done conditions

`submit_report` returns `status: "done"` when:

- Report contains `**Confidence:** High`
- `iteration >= SPLUNK_INVESTIGATOR_MAX_ITER` (default 3)
- No follow-up queries were generated
- Follow-up queries return no new events

---

## Tools

| Tool | Purpose |
|------|---------|
| `splunk__investigate_start` | Load file or live SPL, run detectors, return findings + run_id |
| `splunk__submit_report` | Submit report + queries, get next findings or done signal |
| `splunk__get_findings` | Read current findings mid-loop |
| `splunk__pause` | Signal the loop to stop after current iteration |
| `splunk__hint` | Inject an analyst hint for the next iteration |

---

## Files involved

| File | Role |
|------|------|
| `splunk/mcp_server.py` | FastMCP server — all 5 tools |
| `splunk/server.py` | FastAPI server — active run state, SSE queues, UI routes |
| `splunk/investigator.py` | `_build_findings`, `_prepare_df`, `_execute_queries` |
| `splunk/client.py` | Executes follow-up SPL queries via Splunk REST API |
