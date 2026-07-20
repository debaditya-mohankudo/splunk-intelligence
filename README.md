# Splunk Intelligence

A local Splunk investigation stack that ingests exports (JSON/CSV) or runs live SPL queries, applies deterministic detectors, and drives a structured multi-iteration investigation loop via MCP tools exposed to AI agents (GitHub Copilot or Claude Code). Everything runs on-device — no data leaves the machine.

## How it works

```text
Splunk export (JSON/CSV)  ──or──  Splunk REST API
    └─> parsers.py        # Polars DataFrame: field extraction, timestamp normalisation
    └─> detectors.py      # rule-based: spikes, patterns, cert anomalies, correlations,
    │                     #   severity, host rankings, slow queries, numeric anomalies
    └─> mcp_server.py     # FastMCP: exposes investigation tools to Copilot / Claude
    └─> runner.py         # CLI orchestrator
    └─> reports/          # generated markdown reports
    └─> logs/             # per-run JSONL structured logs
    └─> splunk.db         # SQLite: events, findings, reports, queries per run_id
```

The investigation loop is self-contained — `splunk__submit_report` returns `{status, findings, next}` and the agent loops on its own without external hooks.

## Quick start

### 1. Install prerequisites

- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) — `brew install uv`
- Splunk instance URL (set `SPLUNK_URL` env var; required for live queries only)

### 2. Install dependencies

```bash
uv sync --extra dev
uv run playwright install chromium
```

### 3. Configure Splunk URL (live queries only)

```bash
echo "SPLUNK_URL=https://your-splunk-instance:8089" > .env
```

### 4. Authenticate to Splunk (live queries only)

```bash
uv run python -m splunk.auth
```

This opens a visible **Chromium** window via Playwright. Complete the SSO login manually. The session cookie is saved to `~/.splunk/auth.json` and loaded automatically on every live query. Re-run when your session expires (Splunk uses SSO/SAML — password login is not available).

### 5. Run an investigation

```bash
# From a local export file
uv run python -m splunk --input results/cert_errors.json

# Live SPL query
uv run python -m splunk --live --spl "index=pki sourcetype=ocsp_error" --earliest -6h
```

## Via AI agent (MCP tools)

Run both servers — the FastAPI UI server and the MCP tool server:

```bash
# Terminal 1 — FastAPI UI (http://127.0.0.1:8765)
./serve.sh

# Terminal 2 — MCP tool server
uv run python -m splunk.mcp_server
```

The UI at `http://127.0.0.1:8765/ui/runs/<run_id>` shows live investigation progress, findings, and the final report. The MCP server exposes the investigation tools to the agent.

Then ask Copilot or Claude: *"Start a Splunk investigation on results/cert_errors.json"*

The agent calls `splunk__investigate_start`, reasons over findings, and loops via `splunk__submit_report` until confident. See [AGENTS.md](AGENTS.md) for the full loop protocol.

### Claude Code skills

- `/splunk-analyze` — interactive front door; asks for the log file and whether a live SPL query is also needed, then hands off into the investigation loop
- `/splunk-investigate <input>` — same loop, invoked directly with a file path or SPL query already in hand

## MCP Tools

| Tool | Purpose |
| --- | --- |
| `splunk__investigate_start` | Load file or live SPL query, run detectors, return structured findings + `run_id` |
| `splunk__submit_report` | Submit a markdown report and follow-up SPL queries; returns `{status, findings}` |
| `splunk__get_findings` | Read current findings for an active run without advancing the loop |
| `splunk__pause` | Stop the loop after the current iteration |
| `splunk__hint` | Inject an analyst hint that shapes the next iteration |
| `splunk__query_examples` | Return past SPL queries from `splunk.db` to ground follow-up queries |
| `splunk__lsp_call_chain` | Trace a function/symbol through a microservice's call graph to find which code path produced a log error (requires `repo_path`) |

## Onboarding (new team members)

An interactive onboarding prompt is available for GitHub Copilot. In VS Code Copilot Chat, attach `.github/prompts/onboard.prompt.md` via the `#` file picker — Copilot will walk you through setup, auth, and running your first investigation.

## Tests

```bash
uv run pytest tests/
```

Tests are fully deterministic — no Splunk connection, no server required. Fixtures live in `tests/fixtures/`.

## Key files

| File | Purpose |
| --- | --- |
| `splunk/config.py` | All tunables — thresholds, paths, auth |
| `splunk/parsers.py` | `parse_splunk_json` / `parse_splunk_csv` → `pl.DataFrame` |
| `splunk/detectors.py` | `detect_spikes`, `detect_cert_anomalies`, `host_error_ranking`, `detect_slow_queries`, `detect_numeric_anomalies`, etc. |
| `splunk/mcp_server.py` | FastMCP server — 7 investigation tools |
| `splunk/runner.py` | CLI entry point |
| `splunk/client.py` | Splunk REST client (cookie-based, SSO-compatible) |
| `splunk/auth.py` | Playwright SSO — opens Chromium, saves cookie |
| `splunk/db.py` | SQLite store: events, findings, reports, queries |
| `splunk/logger.py` | Structured JSON-lines logging per run |

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `SPLUNK_URL` | — | Splunk base URL (required for live queries) |
| `SPLUNK_SPIKE_THRESHOLD` | `10` | Events/window to trigger a spike |
| `SPLUNK_SPIKE_WINDOW` | `60` | Spike detection window (seconds) |
| `SPLUNK_SLOW_QUERY_THRESHOLD_MS` | `1000` | Duration (ms) above which an event is flagged as a slow query |
| `SPLUNK_ANOMALY_WINDOW` | `20` | Rolling window size (events) for z-score anomaly detection |
| `SPLUNK_ANOMALY_Z_THRESHOLD` | `3.0` | \|z-score\| above which an event is flagged as a numeric anomaly |
| `SPLUNK_COOKIE_NAME` | `splunkd_8089` | Splunk session cookie name |
| `SPLUNK_AUTH_PATH` | `~/.splunk/auth.json` | Cookie persist path |
| `LOG_LEVEL` | `DEBUG` | Log verbosity |

Put these in a `.env` file at the repo root (gitignored).

## Agent instructions

- **GitHub Copilot** — see [AGENTS.md](AGENTS.md) for loop rules, MCP tool reference, and report format
- **Claude Code** — see [CLAUDE.md](CLAUDE.md) for project conventions and task backlog
- **Onboarding** — see [.github/prompts/onboard.prompt.md](.github/prompts/onboard.prompt.md)
