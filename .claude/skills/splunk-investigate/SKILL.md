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
- Live SPL: `"index=<index> sourcetype=ocsp_error" --earliest -6h` (see Step 0 preflight below before running)

If neither is given, ask the user for one before calling `splunk__investigate_start` —
`source` or `spl` is required (`connector.py::start_investigation` returns
`{"error": "Provide 'source' (file path) or 'spl' (live SPL query)"}` otherwise).

`repo_path` (optional) — path to the microservice source repo, enables
`splunk__lsp_call_chain` for code cross-referencing. Omit to skip; the tool
result will carry a `repo_path_nudge` reminding you it's unavailable this run.

---

## Protocol

**Read `docs/investigation-loop.md` before the first tool call.** It is the normative loop:
tools, `submit_report` responses (`continue` / `done` / `paused`), done rules, report
template, confidence rubric and sparse-data cap, and the follow-up query format. This skill
adds only the Claude-specific steps below.

The one line the server parses — put it in every report exactly like this:

```
**Confidence:** High | Medium | Low
```

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

## Step 1 — Run the loop

```python
splunk__investigate_start(source="results/cert_errors.json")
# OR for live query (after Step 0 preflight):
splunk__investigate_start(spl="<the user's SPL>", earliest="-6h")
```

Then reason → `splunk__submit_report` → repeat, as `docs/investigation-loop.md` describes,
until `status` is `done`. Don't stop to ask for confirmation between iterations.

---

## Step 2 — Finish

When `status: done`:
- Present the final summary, including any advisory nudge keys from the last result
- Point to the TUI for the full report: `uv run python -m splunk.tui` (select run `<run_id>`)

---

## Key constraints

- Never hallucinate field names — all SPL uses fields/values from findings JSON only
- No Ollama, no Anthropic API key — Claude Code session is the reasoning engine
- No data leaves the machine — findings stay local; Claude reasons in this conversation
- MCP path is primary; `python -m splunk.connector` CLI is the fallback when the MCP server is not registered — no server process involved either way
