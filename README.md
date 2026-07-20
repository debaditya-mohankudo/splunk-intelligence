# Splunk Intelligence

A local Splunk investigation stack that ingests exports (JSON/CSV) or runs live SPL queries, applies deterministic detectors, and drives a structured multi-iteration investigation loop via MCP tools exposed to AI agents (GitHub Copilot or Claude Code). Everything runs on-device — no data leaves the machine.

## How it works

```text
Splunk export (JSON/CSV)  ──or──  Splunk REST API
    └─> parsers.py        # Polars DataFrame: field extraction, timestamp normalisation
    └─> detectors.py      # rule-based: spikes, patterns, cert anomalies, correlations,
    │                     #   severity, host rankings, slow queries, numeric anomalies
    └─> connector.py      # facade: loading, detection, run state — no HTTP, no server
    │                     #   process; MCP tools, the TUI, runner.py, and its own CLI
    │                     #   all call into it directly
    └─> mcp_server.py     # FastMCP: exposes investigation tools to Copilot / Claude
    └─> tui.py            # terminal UI: run history + live progress, reads splunk.db directly
    └─> runner.py         # CLI orchestrator
    └─> reports/          # generated markdown reports
    └─> logs/             # per-run JSONL structured logs (audit trail — every
    │                     #   investigate/pause/hint/done action, not just the CLI pipeline)
    └─> splunk.db         # SQLite: events, findings, reports, queries, active_runs per run_id
```

The investigation loop is self-contained — `splunk__submit_report` returns `{status, findings, next}` and the agent loops on its own without external hooks.

Copilot/Claude via MCP is the primary reasoning path — no Ollama required. For environments without either, `splunk/agent.py` provides an optional standalone LangGraph ReAct agent over Ollama, enabled via `uv run python -m splunk --input <file> --investigate` (requires `uv sync --extra llm` and a running Ollama instance; model configurable via `SPLUNK_LLM_MODEL`).

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

No server process required — start the MCP tool server, and optionally the TUI:

```bash
# Terminal 1 — MCP tool server
uv run python -m splunk.mcp_server

# Terminal 2 (optional) — terminal UI for watching live investigation progress
uv run python -m splunk.tui
```

Then ask Copilot or Claude: *"Start a Splunk investigation on results/cert_errors.json"*

The agent calls `splunk__investigate_start`, reasons over findings, and loops via `splunk__submit_report` until confident. See [AGENTS.md](AGENTS.md) for the full loop protocol.

The TUI reads `splunk.db` directly for run history and the rendered report, and polls the `active_runs` table for live iteration/confidence/event-count every ~2s — no HTTP involved. Because every `connector` function writes to `active_runs` regardless of which process calls it, the TUI shows live per-iteration progress for **both** MCP/Claude-driven investigations and the standalone `--investigate` agent path — previously (before this design), MCP-driven progress was invisible to any other process since it only lived in an in-memory dict inside whichever process was running it.

### No MCP client available? Use the connector CLI

Same investigation engine, no MCP tool-calling required:

```bash
uv run python -m splunk.connector start --source results/cert_errors.json
uv run python -m splunk.connector submit-report --run-id <id> --report "..." --queries "-- area\nindex=pki ..."
uv run python -m splunk.connector get-findings --run-id <id>
uv run python -m splunk.connector pause --run-id <id>
uv run python -m splunk.connector hint --run-id <id> --text "focus on web-01 after 14:30 UTC"
```

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
| `splunk/connector.py` | Facade: loading, run state, standalone agent loop, `python -m splunk.connector` CLI |
| `splunk/mcp_server.py` | FastMCP server — 7 investigation tools (thin wrappers over connector.py) |
| `splunk/tui.py` | Terminal UI — `python -m splunk.tui`, reads `splunk.db` directly |
| `splunk/runner.py` | CLI entry point |
| `splunk/client.py` | Splunk REST client (cookie-based, SSO-compatible) |
| `splunk/auth.py` | Playwright SSO — opens Chromium, saves cookie |
| `splunk/db.py` | SQLite store: events, findings, reports, queries, active_runs |
| `splunk/logger.py` | Structured JSON-lines logging per run — audit trail for every connector action |

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
