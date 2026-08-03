# Splunk Intelligence — Investigation Stack

## Goal

Speed up the first pass of a production incident investigation — surface the patterns worth
looking at in the relevant logs, then get to a readable root-cause report faster than doing
it by hand. It's an assistant to the investigation, not a replacement for the human decision.

## Project Planning
use claude-hooks task framework

## Project Overview

Python tool that ingests Splunk exports (JSON/CSV) or fetches live via REST, runs deterministic Polars-based parsers and detectors, then exposes structured findings to an AI agent (GitHub Copilot or Claude Code) via FastMCP tools. The agent handles all reasoning and drives the investigation loop. Everything runs on-device — no data leaves the machine.

Ollama is **not required** for the primary path. The optional `--investigate` flag enables a standalone LangGraph/Qwen agent for environments without Copilot/Claude.

Architecture diagram: README.md. Formal SysML v2 structural model (parts, requirements,
run-state machine, each traced to source): models/ — stamped with `@ModelProvenance`;
staleness against the code is enforced by tests/test_model_provenance.py.

## Stack

Python 3.12, `uv` for dependency management. Dependencies: pyproject.toml (`llm` extra pulls in
langgraph/langchain — only needed for the optional `--investigate` standalone agent).

## Running

Setup, usage, and the full command reference: README.md

Key files and their purpose are derivable by reading the code — for an indexed table if you
want a map before diving in: README.md


## Auth

Splunk uses SSO/SAML — REST `/services/auth/login` is NOT available. Auth flow:
1. `uv run python -m splunk.auth` — opens visible browser, user completes SSO
2. Cookie (`splunkd_8089`) saved to `~/.splunk/auth.json` (never in repo)
3. `client.py` loads cookie for all REST calls; re-auths silently on 401 (max 3 attempts)

Override cookie name via `SPLUNK_COOKIE_NAME` env var. Full env var reference: README.md

## Tasks

Tracked in taskfw (see Project Planning above), not in this file.
