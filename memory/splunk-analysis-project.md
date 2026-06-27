---
name: splunk-analysis-project
description: Project context for splunk_analysis repo — active epic, stack, and conventions
metadata:
  type: project
  domain: splunk
  tags: splunk, project, epic, langgraph, qwen, ollama, parsers, detectors, agent
---

Active repo: `/Users/debaditya/workspace/splunk_analysis`
Active epic: `d142e45a` — Local LLM Splunk Intelligence

Pipeline: Splunk export (JSON/CSV) → `splunk/parsers.py` → `splunk/detectors.py` → LangGraph ReAct agent (Qwen2.5 32B via Ollama) → markdown reports in `reports/`

**Why:** Single repo for on-device Splunk log intelligence. Playwright-based approach (`bf4ad7d0`) is deprecated/abandoned in favour of this stack.

**How to apply:** Frame all suggestions around this pipeline. Parsers and detectors must be pure/deterministic. LLM calls only happen in `splunk/agent.py`. Tests in `tests/` use fixtures only — no network, no Ollama.
