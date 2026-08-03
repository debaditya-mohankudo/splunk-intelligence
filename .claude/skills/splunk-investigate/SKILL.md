---
name: splunk-investigate
description: Investigate a Splunk production issue. Loads events, runs deterministic detectors, then drives an iterative investigation loop via MCP tools — Claude is the reasoning engine. No Ollama, no API key, no server process. Watch live progress via the TUI (`uv run python -m splunk.tui`).
user-invocable: true
cwd: /Users/debaditya/workspace/splunk_analysis
---

# Splunk Investigate

Claude-session investigation loop via MCP tools. Each tool call is self-sufficient: `splunk__submit_report` returns `{status, findings, next}` directly in its own JSON result, so Claude sees the next findings on the next turn and loops without any manual intervention or external hook.

## Repo

`/Users/debaditya/workspace/splunk_analysis`  
No server process required. Terminal UI for watching live progress (optional): `uv run python -m splunk.tui`

## Invocation

```
/splunk-investigate <input>
```

`<input>` — **mandatory**, one of:
- File path: `results/cert_errors.json`, `results/ocsp.csv`
- Live SPL: `"index=pki sourcetype=ocsp_error" --earliest -6h` (see Step 0 preflight below before running)

If neither is given, ask the user for one before calling `splunk__investigate_start` —
`source` or `spl` is required (`connector.py::start_investigation` returns
`{"error": "Provide 'source' (file path) or 'spl' (live SPL query)"}` otherwise).

`repo_path` (optional) — path to the microservice source repo, enables
`splunk__lsp_call_chain` for code cross-referencing. Omit to skip; the tool
result will carry a `repo_path_nudge` reminding you it's unavailable this run.

---

## Loop — how it works (MCP path, primary)

```
splunk__investigate_start(source)   →  {run_id, findings}
Claude reasons                      →  report + SPL queries
splunk__submit_report(run_id, ...)  →  {status: continue, findings, next}
Claude reasons again                →  ...
until: confidence=High | no new events | max 3 iterations
```

Every tool call is self-sufficient — `splunk__submit_report`'s own JSON result carries the next findings and a `next` instruction directly. Claude sees them on the next turn and continues automatically; no external hook process is involved.

---

## Step 0 — Live SPL preflight (skip for file input)

Only applies when `<input>` is a live SPL query, not a file path. Three checks before calling
`splunk__investigate_start(spl=...)`:

1. **Cached login.** Check the session cookie file exists at `$SPLUNK_AUTH_PATH` (default
   `~/.splunk/auth.json`):
   ```bash
   ls ~/.splunk/auth.json
   ```
   If missing, tell the user to run `uv run python -m splunk.auth` first (opens a browser for
   SSO login) rather than attempting the live call — it will fail deep in the REST client
   otherwise. Note: this only confirms a cookie was captured at some point, not that the
   session is still valid — an expired session still needs a live call to discover (client.py
   silently re-auths on 401, up to 3 attempts).

2. **Confirm the target instance.** Read the configured `SPLUNK_URL` (from `.env`/environment)
   and show it to the user for confirmation before running the query — a live SPL query
   against the wrong Splunk instance is a silent wrong-environment footgun, not something
   any tool call will catch for you.

3. **Surface known indexes.** Read `SPLUNK_KNOWN_INDEXES` (from `.env`/environment) — if
   non-empty, show the list to the user alongside the SPLUNK_URL confirmation so they can
   pick/confirm the right index without recalling names from memory. This is reference
   context only; it doesn't change what SPL gets run.

Only proceed to Step 1 once all three are confirmed.

---

## Step 1 — Start the investigation

```python
splunk__investigate_start(source="results/cert_errors.json")
# OR for live query (after Step 0 preflight):
splunk__investigate_start(spl="index=pki sourcetype=ocsp_error", earliest="-6h")
```

Returns `{run_id, findings, event_count, ui_url}`. Note the `run_id`.

Fallback (if MCP server not running):
```bash
uv run python -m splunk.connector start --source "<file_path>"
```

---

## Step 2 — Reason over findings

Analyse the findings dict and produce a structured report:

```markdown
## Summary
<2-3 sentences on what the data shows>

## Root Cause Hypothesis
<most likely root cause based on evidence>

**Confidence:** High | Medium | Low

## Affected Hosts
<from findings.host_ranking>

## Timeline
<from findings.spikes — first spike timestamp → now>

## Recommended Next Steps
- <action 1>
- <action 2>
- <action 3>
```

Rules:
- Only reference hosts, error codes, timestamps, sourcetypes present in findings — never invent values
- High = consistent signal across multiple detectors; Medium = partial; Low = sparse data
- If `event_count` < 50 — cap confidence at Medium

---

## Step 3 — Generate follow-up SPL queries

Produce concrete SPL using only fields/values from findings. Default index: `pki`.

Format each as a string with `-- area` comment prefix:
```
-- host_isolation
index=pki host IN ("web-01") earliest=2024-01-15T14:32:00 latest=+2h
| stats count by host, sourcetype, error_code | sort -count
```

Areas to cover based on findings:
- `host_isolation` — errors concentrated on specific hosts
- `timeline` — spike detected, get per-minute breakdown
- `first_occurrence` — pin exact first event
- `ocsp` — cert_anomalies contain ocsp keywords
- `crl` — crl keywords in cert_anomalies

---

## Step 4 — Submit report

```python
splunk__submit_report(
    run_id="<run_id>",
    report="<markdown report>",
    queries=["-- host_isolation\nindex=pki ...", "-- timeline\nindex=pki ..."]
)
```

**The tool's own JSON result carries the next findings** — no external hook needed.
You will see them in the tool result on this turn — just continue reasoning and call `splunk__submit_report` again.

Response is either:
- `{status: "continue", findings: {...}, next: "..."}` → Claude reasons and loops
- `{status: "done", ui_url: "...", confidence_nudge?: "...", followup_nudge?: "..."}` → investigation complete; check for advisory nudge keys before writing the final summary

Fallback (if MCP server not running):
```bash
uv run python -m splunk.connector submit-report --run-id "<run_id>" --report "..." --queries "..." "..."
```

---

## Step 5 — Finish

When `status: done` or confidence is High:
- Present final summary to user
- Point to the TUI for the full report: `uv run python -m splunk.tui` (select run `<run_id>`)

---

## Pause / hint mid-loop

```python
splunk__pause(run_id="<run_id>")   # pause after current iteration
splunk__hint(run_id="<run_id>", hint="focus on web-01 cert chain errors after 14:30 UTC")
```

---

## Key constraints

- Never hallucinate field names — all SPL uses fields/values from findings JSON only
- No Ollama, no Anthropic API key — Claude Code session is the reasoning engine
- No data leaves the machine — findings stay local; Claude reasons in this conversation
- MCP path is primary; `python -m splunk.connector` CLI is the fallback when the MCP server is not registered — no server process involved either way
