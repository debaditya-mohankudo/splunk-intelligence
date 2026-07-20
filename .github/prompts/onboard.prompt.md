---
mode: ask
---

# Splunk Intelligence — Onboarding Guide

You are helping a new team member get up and running with this repo. Walk them through each section below and answer any questions along the way.

## What this repo does

This is a local Splunk investigation stack. It ingests Splunk exports (JSON/CSV) or runs live SPL queries, runs deterministic detectors (spikes, patterns, cert anomalies, correlations, severity, host rankings, slow queries, rolling z-score numeric anomalies), and drives a multi-iteration investigation loop via MCP tools exposed to you (the Copilot agent).

Everything runs on-device — no data leaves the machine.

## Prerequisites

- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) — `brew install uv`
- Access to the team's Splunk instance URL (set as `SPLUNK_URL` env var)

## First-time setup

```bash
# Install all dependencies (including dev/test extras)
uv sync --extra dev

# Install Playwright Chromium (needed for Splunk SSO auth)
uv run playwright install chromium
```

## Splunk authentication (one-time per session)

Splunk uses SSO/SAML — password login via REST is not available. You must authenticate through the browser:

```bash
uv run python -m splunk.auth
```

This opens a visible **Chromium** window (via Playwright). Complete the SSO login manually. The session cookie is saved to `~/.splunk/auth.json` and loaded automatically by all live query tools.

Repeat this when your session expires (usually after 8–24 hours).

## Repo layout

```
splunk/
  config.py        — all tunables (thresholds, paths)
  parsers.py       — parse Splunk JSON/CSV exports → Polars DataFrame
  detectors.py     — rule-based detectors (spikes, cert anomalies, rankings, slow queries, numeric anomalies)
  investigator.py  — builds findings dict from DataFrame
  mcp_server.py    — FastMCP server: exposes investigation tools to Copilot
  runner.py        — CLI orchestrator (file or live mode)
  client.py        — Splunk REST client
  auth.py          — Playwright SSO auth
  server.py        — FastAPI UI server (optional)
  db.py            — SQLite store (events, findings, reports, queries)
  logger.py        — structured JSON-lines logging

tests/
  test_mcp_tools.py   — unit tests for all MCP tools (no Splunk needed)
  fixtures/           — sample Splunk exports for tests

reports/            — generated markdown reports (gitignored)
logs/               — per-run JSONL logs (gitignored)
results/            — Splunk export files to analyse (gitignored)
```

## Running an investigation

### From a file

```bash
uv run python -m splunk --input results/cert_errors.json
```

### Live query

```bash
uv run python -m splunk --live --spl "index=pki sourcetype=ocsp_error" --earliest -6h
```

### Via Copilot (MCP tools)

Start both servers — the FastAPI UI and the MCP tool server:

```bash
# Terminal 1 — FastAPI UI (http://127.0.0.1:8765)
./serve.sh

# Terminal 2 — MCP tool server
uv run python -m splunk.mcp_server
```

The UI at `http://127.0.0.1:8765/ui/runs/<run_id>` shows live investigation progress and the final report. The MCP server exposes investigation tools to Copilot.

Then ask Copilot: *"Start a Splunk investigation on results/cert_errors.json"*

Copilot will call `splunk__investigate_start`, reason over findings, and loop via `splunk__submit_report` until confident. See `AGENTS.md` for the full loop rules.

## Running tests

```bash
uv run pytest tests/
```

Tests are fully deterministic — no Splunk connection, no server needed.

## Key environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SPLUNK_URL` | Yes (live) | — | Splunk base URL |
| `SPLUNK_COOKIE_NAME` | No | `splunkd_8089` | Splunk session cookie name |
| `LOG_LEVEL` | No | `DEBUG` | Log verbosity |

Put these in a `.env` file at the repo root — it is gitignored.

## Where to go next

- `AGENTS.md` — investigation loop rules and MCP tool reference for Copilot
- `splunk/config.py` — tune thresholds, paths
- `CLAUDE.md` — instructions for Claude Code sessions (same repo, different agent)
