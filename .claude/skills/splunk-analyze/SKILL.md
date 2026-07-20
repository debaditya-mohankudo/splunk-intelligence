---
name: splunk-analyze
description: Interactive front door for a Splunk investigation — asks for the log file and whether live SPL analysis is also needed, then hands off into the splunk-investigate MCP loop. Use when the user wants to analyze Splunk logs but hasn't already named a file or SPL query.
user-invocable: true
---

# Splunk Analyze

Interactive entry point for a Splunk investigation. Where `/splunk-investigate <input>`
expects the file path or SPL query up front, this skill gathers that from the user first,
then drives the exact same MCP loop.

## Invocation

```
/splunk-analyze
```

No arguments — this skill's whole job is to ask.

---

## Step 1 — Ask for the Splunk log file

Ask the user directly (plain question, not a multiple-choice tool — this is a free-text path):

> "What's the path to the Splunk log export you want analyzed? (JSON or CSV, e.g. `results/cert_errors.json`)"

If they don't have a file yet, note that one can be produced via a live query instead (see
Step 2) and file export isn't required.

## Step 2 — Ask if live analysis is also required

Use `AskUserQuestion` (this is a discrete yes/no choice, not free text):

```
question: "Do you also need a live Splunk SPL query run against the instance, or is the file export enough?"
options:
  - "File only" — analyze the provided export, no live query
  - "Live query" — run an SPL query against SPLUNK_URL in addition to (or instead of) the file
```

If "Live query" is chosen, ask for the SPL string and time range:

> "What SPL query should I run, and over what time range? (defaults: earliest=-24h, latest=now)"

Live queries require a valid Splunk session cookie — if `splunk__investigate_start` returns
an auth error, tell the user to run `uv run python -m splunk.auth` (opens a browser for SSO)
and retry.

## Step 3 — Analyze and report

Hand off into the same loop `/splunk-investigate` uses — do not reimplement it:

```python
splunk__investigate_start(source="<file path>")
# OR, if live query requested:
splunk__investigate_start(spl="<SPL>", earliest="<earliest>", latest="<latest>")
```

Then follow **Steps 2–5 of `/splunk-investigate`** verbatim: reason over `findings`
(spikes, patterns, cert_anomalies, correlations, severity, host_ranking, slow_queries,
numeric_anomalies — check `window_contaminated` before treating consecutive z-score flags
as independent signal), draft a report with a confidence level, write follow-up SPL grounded
only in fields present in `findings`, submit via `splunk__submit_report`, and loop until
`status: done`. Present the final summary and the `ui_url` link.

If the user provided both a file and asked for a live query, start with the file
(`splunk__investigate_start(source=...)`), then use the live SPL as one of the Step 3
follow-up queries in `submit_report` rather than a second `investigate_start` call —
`/splunk-investigate`'s working rules forbid calling `investigate_start` more than once
per user request.
