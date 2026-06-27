#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
exec uv run uvicorn splunk.server:app --host 127.0.0.1 --port 8765 --reload
