"""
Centralised configuration and tunable constants for the splunk pipeline.
Override via environment variables, a .env file at the repo root, or by
editing this file before running.

Uses pydantic-settings' BaseSettings so .env is loaded automatically
regardless of which module is imported first — previously config.py read
os.environ directly at import time with no load_dotenv() call of its own,
so .env values were silently ignored unless some other already-imported
module (auth.py/client.py/agent.py) happened to call load_dotenv() first.

Every public name below is still a flat module-level constant, assigned
from one _Settings() instance at import time — existing `from splunk.config
import X` imports and direct `config.X = value` mutation (e.g. splunk/tui.py's
Config screen) both keep working unchanged.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class _Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    SPLUNK_INDEX: str = "*"
    SPLUNK_INVESTIGATOR_MAX_ITER: int = 3
    # Comma-separated indexes relevant to this Splunk environment — reference
    # context only, surfaced to the user during the live-SPL preflight
    # (SKILL.md/AGENTS.md); does not change SPLUNK_INDEX or SPL generation.
    SPLUNK_KNOWN_INDEXES: str = ""

    # Standalone LangGraph/Ollama agent (splunk/agent.py) — optional fallback
    # for environments without Copilot/Claude Code. Requires `uv sync --extra llm`.
    SPLUNK_LLM_MODEL: str = "qwen2.5:14b"
    SPLUNK_AGENT_MAX_ITER: int = 10
    # Chat backend driving the ReAct loop in splunk/agent.py: "ollama" (default),
    # "claude_cli", or "copilot_cli" (see splunk/llm_backends.py).
    SPLUNK_AGENT_BACKEND: str = "ollama"
    SPLUNK_CLAUDE_CLI_MODEL: str = "sonnet"
    SPLUNK_COPILOT_CLI_MODEL: str = "claude-sonnet-4.5"

    # Detector thresholds
    SPLUNK_SPIKE_WINDOW: int = 60
    SPLUNK_SPIKE_THRESHOLD: int = 10
    SPLUNK_CORRELATE_WINDOW: int = 60
    SPLUNK_SLOW_QUERY_THRESHOLD_MS: int = 1000
    SPLUNK_ANOMALY_WINDOW: int = 20
    SPLUNK_ANOMALY_Z_THRESHOLD: float = 3.0

    # Auth
    SPLUNK_AUTH_PATH: Path = Path.home() / ".splunk" / "auth.json"
    SPLUNK_COOKIE_NAME: str = "splunkd_8089"
    SPLUNK_URL: str = ""

    # Job polling
    SPLUNK_POLL_INTERVAL: int = 2
    SPLUNK_POLL_TIMEOUT: int = 300
    SPLUNK_MAX_REAUTH: int = 3

    # Watcher (splunk/watcher.py) — standalone continuous-monitoring process
    SPLUNK_WATCH_SPL: str = ""
    SPLUNK_WATCH_INTERVAL: int = 60
    SPLUNK_WATCH_LOOKBACK: str = "-15m"
    SPLUNK_WATCH_OVERLAP: int = 30


_settings = _Settings()

SPLUNK_INDEX: str = _settings.SPLUNK_INDEX
INVESTIGATOR_MAX_ITER: int = _settings.SPLUNK_INVESTIGATOR_MAX_ITER
KNOWN_INDEXES: list[str] = [i.strip() for i in _settings.SPLUNK_KNOWN_INDEXES.split(",") if i.strip()]

LLM_MODEL: str = _settings.SPLUNK_LLM_MODEL
AGENT_MAX_ITER: int = _settings.SPLUNK_AGENT_MAX_ITER
AGENT_BACKEND: str = _settings.SPLUNK_AGENT_BACKEND
CLAUDE_CLI_MODEL: str = _settings.SPLUNK_CLAUDE_CLI_MODEL
COPILOT_CLI_MODEL: str = _settings.SPLUNK_COPILOT_CLI_MODEL

# ---------------------------------------------------------------------------
# PKI / cert field names — static defaults, no env var
# ---------------------------------------------------------------------------

CERT_FIELDS: frozenset[str] = frozenset({
    "ocsp_status", "cert_subject", "cert_issuer", "cert_expiry",
    "cert_serial", "tls_error", "tls_version", "chain_depth",
    "revocation_reason",
})

CERT_ANOMALY_KEYWORDS: list[str] = [
    "ocsp", "crl", "chain validation", "handshake failed",
    "revocation", "certificate expired", "cert expired",
]

# ---------------------------------------------------------------------------
# Detector thresholds
# ---------------------------------------------------------------------------

SPIKE_WINDOW_SECONDS: int = _settings.SPLUNK_SPIKE_WINDOW
SPIKE_THRESHOLD: int = _settings.SPLUNK_SPIKE_THRESHOLD
CORRELATE_WINDOW_SECONDS: int = _settings.SPLUNK_CORRELATE_WINDOW
SLOW_QUERY_THRESHOLD_MS: int = _settings.SPLUNK_SLOW_QUERY_THRESHOLD_MS

# Entity-keyed event-pair patterns for detect_event_pairs — each entry flags
# entity_field values where a first_pattern event precedes a second_pattern
# event (matched as lowercase substrings of _raw) within maxspan_seconds.
# Analogous to Splunk's `transaction <entity> maxspan=<n> startswith=<a> endswith=<b>`.
CORRELATE_PAIR_PATTERNS: list[dict] = [
    {
        "entity_field": "host",
        "first_pattern": "ocsp",
        "second_pattern": "handshake failed",
        "maxspan_seconds": 3600,
    },
    {
        "entity_field": "host",
        "first_pattern": "crl",
        "second_pattern": "handshake failed",
        "maxspan_seconds": 3600,
    },
]

# Candidate field names for query/request duration, checked in order.
DURATION_FIELDS: list[str] = [
    "duration_ms", "duration", "elapsed", "elapsed_ms",
    "response_time", "run_time", "query_time", "latency", "latency_ms",
]

# Candidate field names for HTTP status code, checked in order.
STATUS_CODE_FIELDS: list[str] = [
    "status", "status_code", "http_status", "response_code", "statuscode",
]

ANOMALY_ROLLING_WINDOW: int = _settings.SPLUNK_ANOMALY_WINDOW
ANOMALY_Z_THRESHOLD: float = _settings.SPLUNK_ANOMALY_Z_THRESHOLD

# Candidate numeric field names to scan for rolling z-score anomalies, checked in order.
ANOMALY_NUMERIC_FIELDS: list[str] = [
    "duration_ms", "duration", "elapsed", "response_time",
    "bytes", "bytes_out", "bytes_in", "status", "response_code",
]

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

AUTH_JSON_PATH: Path = _settings.SPLUNK_AUTH_PATH
COOKIE_NAME: str = _settings.SPLUNK_COOKIE_NAME
SPLUNK_URL: str = _settings.SPLUNK_URL.rstrip("/")

# ---------------------------------------------------------------------------
# Job polling
# ---------------------------------------------------------------------------

POLL_INTERVAL: int = _settings.SPLUNK_POLL_INTERVAL
POLL_TIMEOUT: int = _settings.SPLUNK_POLL_TIMEOUT
MAX_REAUTH_ATTEMPTS: int = _settings.SPLUNK_MAX_REAUTH

# ---------------------------------------------------------------------------
# Watcher (splunk/watcher.py) — standalone continuous-monitoring process
# ---------------------------------------------------------------------------

WATCH_SPL: str = _settings.SPLUNK_WATCH_SPL
WATCH_INTERVAL: int = _settings.SPLUNK_WATCH_INTERVAL
WATCH_LOOKBACK: str = _settings.SPLUNK_WATCH_LOOKBACK
WATCH_OVERLAP: int = _settings.SPLUNK_WATCH_OVERLAP
