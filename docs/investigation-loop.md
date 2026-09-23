# Investigation Loop

The normative protocol for the MCP investigation loop driven by Claude Code. The
`/splunk-investigate` skill (`.claude/skills/splunk-investigate/SKILL.md`) adds only the
Claude-specific parts — invocation, live-SPL preflight, TUI — and defers to this file for
everything below. Behaviour described here is implemented in `splunk/connector.py`; where
the two disagree, the code wins and this file is the bug.

## Setup

No server process to manage:

```bash
uv run python -m splunk.mcp_server   # FastMCP on stdio (registered with the MCP client)
uv run python -m splunk.tui          # optional — watch live progress (reads splunk.db)
```

## Loop

```
1. splunk__investigate_start(source | spl)
       → {run_id, findings, event_count, ui_url}

2. Reason over findings → report + follow-up SPL queries

3. splunk__submit_report(run_id, report, queries)
       → status "continue" | "done" | "paused"

4. continue → back to 2 with the returned findings
   done     → present the final summary + ui_url
   paused   → wait for the analyst to resume, then resubmit the same report and queries
```

Each tool result carries everything needed for the next step; no hook or injected prompt
is involved.

## Tools

| Tool | Purpose |
| --- | --- |
| `splunk__investigate_start` | Load a file or live SPL query, run detectors, return findings + `run_id` |
| `splunk__submit_report` | Submit a report + follow-up queries; returns the next findings or a done/paused signal |
| `splunk__get_findings` | Read current findings for an active run without advancing the loop |
| `splunk__pause` | Ask the loop to pause — the next `submit_report` is refused until resumed |
| `splunk__hint` | Queue an analyst hint; it appears as `findings.analyst_hint` on the next `continue` |
| `splunk__query_examples` | Past SPL queries from `splunk.db` (filter by `area`) to ground follow-ups |
| `splunk__find_symbol_refs` | Find where a symbol is defined and referenced in the microservice repo (requires `repo_path`) |
| `splunk__check_alerts` | Read unacknowledged alerts written by `standalone/watcher.py` |
| `splunk__ack_alert` | Acknowledge a watcher alert so it stops appearing in `check_alerts` |

No MCP client? The same loop is available as a CLI:

```bash
uv run python -m splunk.connector start --source results/x.json
uv run python -m splunk.connector submit-report --run-id <id> --report "..." --queries "..."
```

## submit_report responses

| `status` | Keys | Meaning |
| --- | --- | --- |
| `continue` | `iteration`, `confidence`, `event_count`, `findings`, `next` | Follow-ups returned new events; reason again |
| `done` | `confidence`, `iterations`, `reason`, `ui_url` | A done rule fired (below) |
| `paused` | `next` | Analyst paused the run. Nothing was stored and the iteration did not advance |

Any response may also carry advisory `confidence_nudge`, `followup_nudge` or
`repo_path_nudge` keys. They never block; mention them in the final summary.

## Done rules

Checked in this order on every `submit_report` (`connector._step`):

1. Parsed confidence is `High` — after the sparse-data cap below
2. No follow-up queries were submitted
3. `iteration >= SPLUNK_INVESTIGATOR_MAX_ITER` (default 3)
4. The follow-up queries returned no events

## Report

Only the confidence line is machine-parsed, and it must appear exactly in this form:

```
**Confidence:** High | Medium | Low
```

A missing or unrecognised line is read as `Medium`. Use this template:

```markdown
## Summary
<2-3 sentences on what the data shows>

## Root Cause Hypothesis
<one falsifiable hypothesis>

**Confidence:** High | Medium | Low

## Affected Hosts
<from findings.host_ranking>

## Timeline
<from findings.spikes — first spike → now>

## Recommended Next Steps
- <action>
```

Only reference hosts, error codes, timestamps and sourcetypes present in the findings — never
invent values.

## Confidence rubric

- **High** — consistent signal across multiple detectors
- **Medium** — partial signal
- **Low** — sparse or contradictory data

**Sparse-data cap:** with fewer than 50 events (`SPARSE_EVENT_THRESHOLD`), a stated High is
recorded as Medium. It will not end the run.

## Follow-up queries

- 1–3 queries per iteration, cheapest discriminating query first
- Each query string starts with a `-- <area>` label line (`-- area: <area>` is also
  accepted); the label is stored with the query and is what `splunk__query_examples(area=...)`
  filters on
- Use only fields and values present in the findings
- Index: reuse the index from the starting SPL (or the one the events came from); fall back
  to `$SPLUNK_INDEX` (default `*`). There is no built-in default index.

```
-- host_isolation
index=<index> host IN ("web-01") earliest=2024-01-15T14:32:00 latest=+2h
| stats count by host, sourcetype, error_code | sort -count
```

Common areas: `host_isolation`, `timeline`, `first_occurrence`, `ocsp`, `crl`.

## Files involved

| File | Role |
| --- | --- |
| `splunk/mcp_server.py` | FastMCP server — the 9 tools, thin wrappers over connector.py |
| `splunk/connector.py` | Loads data, runs the loop step (`_step`), persists run state to splunk.db, own CLI |
| `splunk/investigator.py` | `_build_findings`, `_prepare_df`, `_execute_queries`, `_parse_confidence` |
| `splunk/client.py` | Executes follow-up SPL via the Splunk REST API |
| `splunk/tui.py` | Terminal UI — reads splunk.db for history and live `active_runs` state |
