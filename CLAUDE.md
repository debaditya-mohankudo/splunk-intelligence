# Splunk Intelligence — Investigation Stack

## Project Overview

Python tool that ingests Splunk exports (JSON/CSV) or fetches live via REST, runs deterministic Polars-based parsers and detectors, then exposes structured findings to an AI agent (GitHub Copilot or Claude Code) via FastMCP tools. The agent handles all reasoning and drives the investigation loop. Everything runs on-device — no data leaves the machine.

Ollama is **not required** for the primary path. The optional `--llm` flag enables a standalone LangGraph/Qwen agent for environments without Copilot/Claude.

## Architecture

```
Splunk export (JSON/CSV)  ──or──  Splunk REST API (via auth.py + client.py)
    └─> splunk/parsers.py       # Polars DataFrame: field extraction, timestamp normalisation, timeline
    └─> splunk/detectors.py     # rule-based detection: spikes, patterns, cert anomalies, host ranking
    └─> splunk/mcp_server.py    # FastMCP tools — Copilot/Claude drives the investigation loop
    └─> splunk/agent.py         # optional: LangGraph ReAct via Ollama (--llm flag only)
    └─> splunk/runner.py        # CLI orchestrator — wires everything, emits run_id
    └─> reports/<stem>_<ts>.md  # markdown investigation report
    └─> logs/<run_id>.jsonl     # structured JSON-lines log per run
    └─> splunk.db               # SQLite: events, findings, reports keyed by run_id
```

## Stack

- Python 3.12, `uv` for dependency management
- `polars` — DataFrame-based parsing and detection (threaded through the full pipeline)
- `mcp[cli]` + `fastmcp` — MCP tool server, Copilot/Claude is the reasoning layer
- `playwright` for Splunk SSO auth (non-headless — user completes login manually)
- `requests` for Splunk REST API calls
- `pytest` for tests (deterministic — no Ollama, no network, no Splunk connection needed)
- Optional (`uv sync --extra llm`): `langgraph` + `langchain-ollama` + `langchain-core`

## Running

```bash
# Install deps (includes pytest)
uv sync --extra dev

# Pipeline from file (parsers + detectors, Copilot/Claude handles reasoning via MCP)
uv run python -m splunk --input results/cert_errors.json

# With standalone Ollama agent (requires --extra llm and Ollama running)
uv run python -m splunk --input results/cert_errors.json --llm

# Live query from Splunk
uv run python -m splunk --live --spl "index=pki sourcetype=ocsp_error" --earliest -6h

# Tests
uv run pytest tests/

# One-time: install Playwright browser
uv run playwright install chromium

# Authenticate to Splunk (opens browser for SSO)
uv run python -m splunk.auth
```

## Key files

| File | Purpose |
|------|---------|
| `splunk/config.py` | All tunables — cert fields, keywords, thresholds, auth paths, model name |
| `splunk/parsers.py` | `parse_splunk_json` / `parse_splunk_csv` → `pl.DataFrame`; timestamp, cert, timeline transforms |
| `splunk/detectors.py` | `detect_spikes`, `detect_patterns`, `detect_cert_anomalies`, `correlate_events`, `severity_summary`, `host_error_ranking` |
| `splunk/agent.py` | LangGraph ReAct graph, 4 tools, `analyse(findings) -> str` |
| `splunk/client.py` | `run_query(spl)` → submit → poll → fetch → parse |
| `splunk/auth.py` | Playwright SSO, extracts cookie → `~/.splunk/auth.json` |
| `splunk/runner.py` | CLI entry point, `run_pipeline(df)`, `RunLogger`, DB store |
| `splunk/logger.py` | `RunLogger` — JSON-lines to `logs/<run_id>.jsonl`, default DEBUG |
| `splunk/db.py` | SQLite store: `init_db`, `store_events`, `store_findings`, `store_report` |

## Code conventions

- Parsers and detectors must be **pure and deterministic** — no LLM calls, no network
- `pl.DataFrame` is threaded through the full pipeline; `.to_dicts()` only at the agent boundary
- Agent lives in `agent.py` only — receives findings dict, never raw events
- All tunables live in `splunk/config.py`; override via env vars or `.env`
- `LOG_LEVEL` defaults to `DEBUG` — every run is disposable, log freely
- Tests use `tests/fixtures/` for input data; never call Ollama or Splunk in tests
- `splunk.db`, `logs/`, `reports/`, `results/` are gitignored

## Auth

Splunk uses SSO/SAML — REST `/services/auth/login` is NOT available. Auth flow:
1. `uv run python -m splunk.auth` — opens visible browser, user completes SSO
2. Cookie (`splunkd_8089`) saved to `~/.splunk/auth.json` (never in repo)
3. `client.py` loads cookie for all REST calls; re-auths silently on 401 (max 3 attempts)

Override cookie name via `SPLUNK_COOKIE_NAME` env var.

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `SPLUNK_URL` | — | Splunk base URL (required) |
| `SPLUNK_USE_LLM` | `false` | Set `true` to enable standalone Ollama agent (requires `--extra llm`) |
| `SPLUNK_LLM_MODEL` | `qwen2.5:14b` | Ollama model (only used when `SPLUNK_USE_LLM=true`) |
| `SPLUNK_AGENT_MAX_ITER` | `10` | ReAct loop cap (only used when `SPLUNK_USE_LLM=true`) |
| `SPLUNK_SPIKE_THRESHOLD` | `10` | Events/window to trigger spike |
| `SPLUNK_SPIKE_WINDOW` | `60` | Spike detection window (seconds) |
| `SPLUNK_COOKIE_NAME` | `splunkd_8089` | Splunk session cookie name |
| `SPLUNK_AUTH_PATH` | `~/.splunk/auth.json` | Cookie persist path |
| `LOG_LEVEL` | `DEBUG` | Logging verbosity |

## Task backlog

| ID | Description | Status |
|----|-------------|--------|
| `7d5a25bf` | splunk/parsers.py | ✅ done |
| `b1b7370a` | splunk/detectors.py | ✅ done |
| `387b32b3` | splunk/agent.py — LangGraph ReAct + Qwen2.5 32B | ✅ done |
| `1b0a842b` | splunk/runner.py — CLI orchestrator | ✅ done |
| `feba7531` | splunk/client.py — Splunk REST client | ✅ done |
| `fe073cce` | splunk/auth.py — Playwright SSO auth | ✅ done |
| `9528ce17` | splunk/logger.py — structured logging with run_id | ✅ done |
| `3fa83d03` | tests/ — unit tests for parsers and detectors | 🔲 open |

Epic: `d142e45a` — Local LLM Splunk Intelligence
