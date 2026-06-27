# Task IDs — Splunk Analysis

## Epic: Local LLM Splunk Intelligence
**ID:** `d142e45a` — Local LLM Splunk Intelligence — Python parsers + LangGraph ReAct + Qwen 32B for on-device log analysis

| ID | Title | Status |
|----|-------|--------|
| `7d5a25bf` | splunk/parsers.py — raw log parsing, field extraction, timeline reconstruction | open |
| `b1b7370a` | splunk/detectors.py — spike detection, pattern matching, host grouping, cert anomalies | open |
| `387b32b3` | splunk/agent.py — LangGraph ReAct loop wired to Qwen2.5 32B via Ollama | open |
| `1b0a842b` | splunk/runner.py — CLI entry point, end-to-end pipeline orchestration | open |
| `3fa83d03` | tests/ — unit tests for parsers and detectors (deterministic, no LLM required) | open |
| `feba7531` | splunk/client.py — Splunk REST client, auth bridge, live query to results pipeline | open |
| `fe073cce` | splunk/auth.py — Playwright SSO login, cookie extraction, REST API session (moved from bf4ad7d0) | open |
| `9528ce17` | splunk/logger.py — structured pipeline logging with run_id tracing | open |
| `50a40cbb` | splunk/parsers.py — cache discovered field keys per sourcetype in splunk.db | open |

---

## Epic: Splunk Playwright Query Runner ~~[DEPRECATED]~~

**ID:** `bf4ad7d0` — Splunk Playwright Query Runner — file-driven SPL evaluation and iteration loop

| ID | Title | Status |
|----|-------|--------|
| `121108b2` | File-driven SPL query loader and submitter via Playwright | open |
| `56f5f8f8` | Result extractor and evaluator — parse Splunk output, score against baseline | open |
| `0e3f7137` | Iteration loop and reporting — run→evaluate→mutate→re-run cycle with summary diff | open |
| `fe073cce` | Playwright + Splunk auth — browser SSO login, cookie extraction, REST API session | open |
| `243c7e6f` | LangGraph ReAct loop for Splunk log analysis — Mistral Small local LLM with tool calling | open |
