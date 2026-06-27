---
name: splunk-runner-cli
description: CLI entry point for splunk_analysis — flags, pipeline order, output format
metadata:
  type: project
  domain: splunk
  tags: splunk, runner, cli, pipeline, no-llm, live, flags
---

Main CLI entry point is `splunk/runner.py`. Invoked as `python -m splunk` or `python -m splunk.runner`.

Key flags:
- `--input FILE` or `--live` (mutually exclusive, required)
- `--live` requires `--spl` (SPL query string)
- `--no-llm` — runs parsers + detectors only, skips agent; useful when Ollama is not running
- `--model` — overrides SPLUNK_LLM_MODEL for that run
- `--output DIR` — report output directory (default: reports/)

Pipeline order: `extract_timestamps → extract_cert_fields → build_timeline → detect_spikes → detect_patterns → detect_cert_anomalies → correlate_events → severity_summary → host_error_ranking → agent.analyse`

Report written to `reports/<stem>_<timestamp>.md`. One-line summary printed to stdout.

**How to apply:** When debugging without Ollama, always suggest `--no-llm` flag. When onboarding real Splunk data, use `--live --spl` with `--earliest`/`--latest`.
