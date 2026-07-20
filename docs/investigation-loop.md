# Investigation Loop — How It Works

## Setup

One process, no server required:

```bash
# MCP tool server
uv run python -m splunk.mcp_server   # FastMCP on stdio

# Optional — terminal UI for watching live progress (reads splunk.db directly)
uv run python -m splunk.tui
```

The MCP server registers 7 tools (`splunk__*`). Any MCP-compatible agent (Claude Code, GitHub Copilot, etc.) can drive the loop. All tools are thin wrappers over `splunk/connector.py`, which owns loading data, running detectors, and persisting run state — no HTTP involved anywhere in this loop.

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
| `splunk__query_examples` | Look up past SPL queries to ground follow-up queries |
| `splunk__lsp_call_chain` | Trace a symbol through a microservice's call graph (requires `repo_path`) |

No MCP client available? The same operations are exposed as a CLI:

```bash
uv run python -m splunk.connector start --source results/x.json
uv run python -m splunk.connector submit-report --run-id <id> --report "..." --queries "..."
```

---

## Files involved

| File | Role |
|------|------|
| `splunk/mcp_server.py` | FastMCP server — all 7 tools, thin wrappers over connector.py |
| `splunk/connector.py` | Facade — loads data, runs detectors, persists run state to splunk.db, own CLI |
| `splunk/investigator.py` | `_build_findings`, `_prepare_df`, `_execute_queries` (imported by connector.py) |
| `splunk/client.py` | Executes follow-up SPL queries via Splunk REST API |
| `splunk/tui.py` | Terminal UI — reads splunk.db directly for history + live `active_runs` state |
