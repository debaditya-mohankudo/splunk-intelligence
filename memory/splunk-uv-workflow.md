---
name: splunk-uv-workflow
description: uv dependency workflow for splunk_analysis — sync, dev extras, run commands
metadata:
  type: project
  domain: splunk
  tags: splunk, uv, venv, dependencies, dev, pytest
---

Dependency management uses `uv`. Runtime deps in `[project.dependencies]`, dev deps (pytest) in `[project.optional-dependencies] dev`.

Common commands:
- `uv sync --extra dev` — install all including pytest
- `uv sync` — runtime only
- `uv run pytest tests/` — run tests without activating venv
- `uv run playwright install chromium` — install browser (one-time)

**How to apply:** Always use `uv run` prefix for commands in this repo rather than activating venv manually.
