---
name: splunk-investigate
description: Investigate a Splunk production issue. Loads events, runs deterministic detectors, then drives an iterative investigation loop via MCP tools — Claude is the reasoning engine. No Ollama, no API key. Results live in the UI at http://127.0.0.1:8765/ui/
user-invocable: true
cwd: /Users/debaditya/workspace/splunk_analysis
---

# Splunk Investigate

Claude-session investigation loop via MCP tools. `splunk__submit_report` returns findings directly in its response — Claude reads `status` and loops on its own. No external hooks required.

## Repo

`/Users/debaditya/workspace/splunk_analysis`  
Server (optional but recommended for UI): `./serve.sh` → `http://127.0.0.1:8765/ui/`

## Invocation

```
/splunk-investigate <input>
```

`<input>` is one of:
- File path: `results/cert_errors.json`, `results/ocsp.csv`
- Live SPL: `"index=pki sourcetype=ocsp_error" --earliest -6h`
- Nothing — ask the user

---

## Loop — how it works

```
splunk__investigate_start(source)   →  {run_id, findings}
Claude reasons                      →  report + SPL queries
splunk__submit_report(run_id, ...)  →  {status, findings} — read directly from response
Claude reasons again                →  ...
until: status=done | confidence=High | no new events | max 3 iterations
```

No external hooks needed. The response from `splunk__submit_report` contains `status` ("continue" or "done") and the next `findings`. Claude reads them and loops.

---

## Step 1 — Start the investigation

```python
splunk__investigate_start(source="results/cert_errors.json")
# OR for live query:
splunk__investigate_start(spl="index=pki sourcetype=ocsp_error", earliest="-6h")
```

Returns `{run_id, findings, event_count, ui_url}`. Note the `run_id`.

Fallback (if MCP server not running):
```bash
curl -s -X POST http://127.0.0.1:8765/api/investigate/start \
  -H "Content-Type: application/json" \
  -d '{"source": "<file_path>"}'
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

Response is either:
- `{status: "continue", findings: {...}}` → read findings, loop back to Step 2
- `{status: "done", ui_url: "..."}` → investigation complete

---

## Step 5 — Finish

When `status: done` or confidence is High:
- Present final summary to user
- Link to UI: `http://127.0.0.1:8765/ui/runs/<run_id>`

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
- MCP path is primary; curl/REST path is the fallback when the MCP server is not registered
