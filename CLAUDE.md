# Splunk Intelligence — Investigation Stack

## Project Planning
use claude-hooks task framework

## Project Overview

Python tool that ingests Splunk exports (JSON/CSV) or fetches live via REST, runs deterministic Polars-based parsers and detectors, then exposes structured findings to an AI agent (GitHub Copilot or Claude Code) via FastMCP tools. The agent handles all reasoning and drives the investigation loop. Everything runs on-device — no data leaves the machine.

Ollama is **not required** for the primary path. The optional `--investigate` flag enables a standalone LangGraph/Qwen agent for environments without Copilot/Claude.

## Architecture

```
Splunk export (JSON/CSV)  ──or──  Splunk REST API (via auth.py + client.py)
    └─> splunk/parsers.py       # Polars DataFrame: field extraction, timestamp normalisation, timeline
    └─> splunk/detectors.py     # rule-based detection: spikes, patterns, cert anomalies, host ranking, HTTP errors
    └─> splunk/mcp_server.py    # FastMCP tools — Copilot/Claude drives the investigation loop
    └─> splunk/agent.py         # optional: LangGraph ReAct via Ollama (--investigate flag only)
    └─> splunk/runner.py        # CLI orchestrator — wires everything, emits run_id
    └─> reports/<stem>_<ts>.md  # markdown investigation report
    └─> logs/<run_id>.jsonl     # structured JSON-lines log per run
    └─> splunk.db               # SQLite: events, findings, reports, alerts keyed by run_id

splunk/watcher.py               # standalone process (python -m splunk.watcher) —
    └─> polls Splunk on an interval, runs detectors on each new slice,
        writes hits to splunk.db's alerts table. Independent of any agent
        session — Copilot has no self-scheduling mechanism to drive this
        loop itself. splunk__check_alerts / splunk__ack_alert (mcp_server.py)
        are how the agent consumes its output.
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
uv run python -m splunk --input results/cert_errors.json --investigate

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
| `splunk/detectors.py` | `detect_spikes`, `detect_patterns`, `detect_cert_anomalies`, `correlate_events`, `detect_event_pairs`/`detect_event_pair_patterns`, `severity_summary`, `host_error_ranking`, `detect_slow_queries`, `detect_numeric_anomalies`, `detect_http_errors` |
| `splunk/agent.py` | LangGraph ReAct graph, 5 tools, `analyse(findings) -> tuple[str, list[str]]` |
| `splunk/investigation_areas.py` | Data-driven registry of investigation domains (prompt + SPL template per area) consumed by `agent.py`'s `request_deeper_analysis`/`generate_followup_queries` — add a domain here, not by editing `agent.py` |
| `splunk/llm_backends.py` | Pluggable chat backend for `agent.py`'s ReAct loop, selected via `SPLUNK_AGENT_BACKEND`. Only `ollama` is implemented; `claude_cli`/`copilot_cli` are registered seams (raise `NotImplementedError`) since those CLIs are agentic and don't support the same tool-calling handshake as `ChatOllama` — see `splunk/llm_backends.py` module docstring |
| `splunk/client.py` | `run_query(spl)` → submit → poll → fetch → parse |
| `splunk/auth.py` | Playwright SSO, extracts cookie → `~/.splunk/auth.json` |
| `splunk/runner.py` | CLI entry point, `run_pipeline(df)`, `RunLogger`, DB store |
| `splunk/logger.py` | `RunLogger` — JSON-lines to `logs/<run_id>.jsonl`, default DEBUG |
| `splunk/db.py` | SQLite store: `init_db`, `store_events`, `store_findings`, `store_report`, `store_alerts`, `get_alerts`, `ack_alert`, `get_watch_bookmark`/`set_watch_bookmark`, `save_schema`/`load_schema`/`reset_schema` (per-sourcetype schema cache for `parsers.py`) |
| `splunk/watcher.py` | Standalone `python -m splunk.watcher` process — polls Splunk on an interval, runs detectors on each new slice, writes hits to the `alerts` table. Exists because Copilot has no self-scheduling mechanism to drive polling itself. |
| `local_splunk/` | Throwaway single-instance Splunk container (`docker-splunk`-based) for testing the `--live` path against a real Splunk REST API + real SPL, bypassing `auth.py`'s SSO flow via plain basic auth. Dev/test only — not part of the shipped pipeline. See `local_splunk/README.md`. |

## Code conventions

- Parsers and detectors must be **pure and deterministic** — no LLM calls, no network
- `pl.DataFrame` is threaded through the full pipeline; `.to_dicts()` only at the agent boundary
- Agent lives in `agent.py` only — receives findings dict, never raw events
- All tunables live in `splunk/config.py`; override via env vars or `.env`
- `LOG_LEVEL` defaults to `DEBUG` — every run is disposable, log freely
- Tests use `tests/fixtures/` for input data; never call Ollama or Splunk in tests
- `splunk.db`, `logs/`, `reports/`, `results/` are gitignored

### Future optimization: parallel detectors on large data

`_build_findings` (in `investigator.py` / `runner.py`) currently calls each detector in
`detectors.py` sequentially in Python. Not worth changing until real dataset sizes make
this a measured bottleneck — premature otherwise.

When it is warranted, prefer **Polars-native parallelism** over LangGraph nodes or
`concurrent.futures`: convert the frame-native detectors (`detect_patterns`,
`severity_summary`, `host_error_ranking`, `detect_cert_anomalies`, `detect_slow_queries`,
`detect_numeric_anomalies`)
to `pl.LazyFrame` query chains and run them together via `pl.collect_all([...])`. This
schedules on Polars' own Rust-side thread pool, sidestepping the GIL entirely rather than
fighting it through Python threads/async.

Note: `detect_spikes`, `correlate_events`, and `detect_event_pairs` use Python `for` loops
over rows (sliding time windows / per-entity pair scanning) and can't be expressed as lazy
Polars chains — they'd stay sequential even after this change.

## Schema cache (parsers.py + db.py)

`parse_splunk_json` caches a per-sourcetype column→dtype schema in `splunk.db` to speed up
repeated parses. This is a **soft performance hint, not a correctness guarantee** — the same
sourcetype can return structurally different result shapes depending on the SPL applied (e.g.
`transaction`/`stats` producing multivalue list fields where a plain search returns scalars for
the same field name). On a schema mismatch, `parse_splunk_json` catches the Polars
`ComputeError`, falls back to inference, and calls `db.reset_schema` (delete-then-insert) rather
than `db.save_schema` (upsert) so stale fields from the abandoned shape don't linger to collide
with a third query shape later. See `tests/test_parsers.py` for the regression coverage.

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
| `SPLUNK_LLM_MODEL` | `qwen2.5:14b` | Ollama model (only used with the `--investigate` flag, requires `--extra llm`) |
| `SPLUNK_AGENT_BACKEND` | `ollama` | Chat backend for `agent.py`'s ReAct loop — `ollama`, `claude_cli` (not yet implemented), `copilot_cli` (not yet implemented) |
| `SPLUNK_AGENT_MAX_ITER` | `10` | ReAct loop cap (only used with the `--investigate` flag) |
| `SPLUNK_SPIKE_THRESHOLD` | `10` | Events/window to trigger spike |
| `SPLUNK_SPIKE_WINDOW` | `60` | Spike detection window (seconds) |
| `SPLUNK_SLOW_QUERY_THRESHOLD_MS` | `1000` | Duration (ms) above which an event is flagged as a slow query |
| `SPLUNK_ANOMALY_WINDOW` | `20` | Rolling window size (events) for z-score anomaly detection |
| `SPLUNK_ANOMALY_Z_THRESHOLD` | `3.0` | \|z-score\| above which an event is flagged as a numeric anomaly |
| `SPLUNK_COOKIE_NAME` | `splunkd_8089` | Splunk session cookie name |
| `SPLUNK_AUTH_PATH` | `~/.splunk/auth.json` | Cookie persist path |
| `LOG_LEVEL` | `DEBUG` | Logging verbosity |
| `SPLUNK_WATCH_SPL` | — | SPL query the watcher (`splunk/watcher.py`) polls on a loop |
| `SPLUNK_WATCH_INTERVAL` | `60` | Seconds between watcher poll cycles |
| `SPLUNK_WATCH_LOOKBACK` | `-15m` | Earliest time for the watcher's first cycle (no bookmark yet) |
| `SPLUNK_WATCH_OVERLAP` | `30` | Seconds subtracted from the bookmark each cycle so boundary-straddling events aren't missed |

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
