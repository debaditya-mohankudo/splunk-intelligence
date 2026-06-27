---
name: splunk-investigate
description: Investigate a Splunk production issue. Runs the local splunk_analysis pipeline to extract structured findings, then reasons over them to identify root cause, confidence, and follow-up SPL queries. Iterates until High confidence or no new signal.
user-invocable: true
cwd: /Users/debaditya/workspace/splunk_analysis
---

# Splunk Investigate

Runs the local `splunk_analysis` pipeline against a Splunk export or live query, reasons over structured findings, and produces an investigation report with follow-up SPL queries.

## Repo

`/Users/debaditya/workspace/splunk_analysis` — Python 3.12, `uv`, Polars parsers + detectors, SQLite store. Run all commands with `uv run` from that directory.

## How It Works

1. **Get findings** — run the pipeline in `--dump-findings` mode (no LLM, no Ollama needed). Output is a structured JSON dict from deterministic Polars detectors.
2. **Reason** — Claude analyses the findings inline: root cause hypothesis, confidence level (High / Medium / Low), affected hosts, timeline.
3. **Generate follow-up queries** — Claude produces concrete SPL queries using only field names and values present in the findings (no hallucination).
4. **Iterate** — if confidence is not High and the user wants to go deeper, run the follow-up queries (via `--live` or manually in Splunk), feed new findings back, repeat.

## Invocation

```
/splunk-investigate <input>
```

`<input>` is one of:
- A file path: `results/cert_errors.json`, `results/ocsp.csv`
- A live SPL query: `"index=pki sourcetype=ocsp_error" --earliest -6h`
- Nothing — Claude will ask

## Step-by-Step

### Step 1 — Get findings

If input is a file:
```bash
cd /Users/debaditya/workspace/splunk_analysis && uv run python -m splunk --input <file> --dump-findings
```

If input is a live SPL query:
```bash
cd /Users/debaditya/workspace/splunk_analysis && uv run python -m splunk --live --spl "<query>" --earliest <window> --dump-findings
```

Capture the JSON output. This is the findings dict — it contains:
- `spikes` — frequency spikes with timestamps and affected hosts
- `patterns` — repeating error codes per sourcetype
- `cert_anomalies` — events matching cert/OCSP/CRL keywords
- `correlations` — events clustered within 60s windows
- `severity` — count breakdown by severity level
- `host_ranking` — hosts ranked by error count
- `event_count` — total events parsed

### Step 2 — Reason over findings

Analyse the findings dict and produce a structured report:

```markdown
## Summary
<2-3 sentences on what the data shows>

## Root Cause Hypothesis
<most likely root cause based on evidence>

**Confidence:** High | Medium | Low

## Affected Hosts
<comma-separated list from findings>

## Timeline
<first occurrence → escalation → current state>

## Recommended Next Steps
- <action 1>
- <action 2>
- <action 3>
```

Rules:
- Only reference hosts, error codes, timestamps, and sourcetypes that appear in the findings JSON — never invent values
- Assign confidence based on signal strength: High = consistent pattern across multiple detectors, Medium = partial signal, Low = sparse data
- If `event_count` < 50, note that the sample is small and confidence should be capped at Medium

### Step 3 — Generate follow-up SPL queries

After the report, produce concrete SPL queries the analyst can run next. Use only index names, sourcetypes, hosts, and error codes from the findings. Default index is `pki` unless the findings suggest otherwise.

Template areas to cover based on what the findings show:
- **host_isolation** — if errors are concentrated on specific hosts
- **timeline** — if a spike was detected, get per-minute breakdown
- **first_occurrence** — pin the exact first event
- **ocsp** — if cert_anomalies mention ocsp keywords, check network traffic to OCSP responders
- **crl** — if crl keywords appear, check CRL distribution point reachability

Format each query as a code block with a comment header:
```
-- host_isolation
index=pki host IN ("host1", "host2") earliest=2024-01-15T10:00:00 latest=+2h
| stats count by host, sourcetype, error_code | sort -count
```

### Step 4 — Iterate (if needed)

If confidence is not High:
- Ask the user: "Want me to run these queries and go deeper?"
- If yes and `--live` is available: run each query via `uv run python -m splunk --live --spl "<query>" --dump-findings`, collect new findings, merge with prior findings context, repeat from Step 2
- If not live: present queries for the analyst to run manually in Splunk, ask them to paste results back

Stop iterating when:
- Confidence reaches High
- Follow-up queries return no new signal
- 3 iterations reached

## Key constraints

- **Never hallucinate field names** — all SPL must use fields and values from the findings JSON
- **No Ollama needed** — `--dump-findings` is pure Python/Polars, no LLM dependency
- **No data leaves the machine** — this skill runs entirely locally; Claude reasons over the findings in this conversation only

## Example

```
User: /splunk-investigate results/cert_errors.json

Claude: [runs --dump-findings, gets findings JSON]
        [reasons over it]
        [produces report: root cause = OCSP responder unreachable, Confidence: Medium]
        [produces 3 follow-up SPL queries]
        "Want me to run these queries and go deeper?"

User: yes

Claude: [runs follow-up queries via --live]
        [merges new findings]
        [produces updated report: Confidence: High — OCSP responder at 10.0.0.5:2560 unreachable from hosts web-01, web-02 since 14:32 UTC]
```
