"""
Centralised configuration and tunable constants for the splunk pipeline.
Override via environment variables or by editing this file before running.
"""

from __future__ import annotations

import os

SPLUNK_INDEX: str = os.environ.get("SPLUNK_INDEX", "pki")
INVESTIGATOR_MAX_ITER: int = int(os.environ.get("SPLUNK_INVESTIGATOR_MAX_ITER", "3"))

# ---------------------------------------------------------------------------
# Standalone LangGraph/Ollama agent (splunk/agent.py) — optional fallback for
# environments without Copilot/Claude Code. Requires `uv sync --extra llm`.
# ---------------------------------------------------------------------------

LLM_MODEL: str = os.environ.get("SPLUNK_LLM_MODEL", "qwen2.5:14b")
AGENT_MAX_ITER: int = int(os.environ.get("SPLUNK_AGENT_MAX_ITER", "10"))

# ---------------------------------------------------------------------------
# PKI / cert field names
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

SPIKE_WINDOW_SECONDS: int = int(os.environ.get("SPLUNK_SPIKE_WINDOW", "60"))
SPIKE_THRESHOLD: int = int(os.environ.get("SPLUNK_SPIKE_THRESHOLD", "10"))
CORRELATE_WINDOW_SECONDS: int = int(os.environ.get("SPLUNK_CORRELATE_WINDOW", "60"))
SLOW_QUERY_THRESHOLD_MS: int = int(os.environ.get("SPLUNK_SLOW_QUERY_THRESHOLD_MS", "1000"))

# Candidate field names for query/request duration, checked in order.
DURATION_FIELDS: list[str] = [
    "duration_ms", "duration", "elapsed", "elapsed_ms",
    "response_time", "run_time", "query_time", "latency", "latency_ms",
]

# Candidate field names for HTTP status code, checked in order.
STATUS_CODE_FIELDS: list[str] = [
    "status", "status_code", "http_status", "response_code", "statuscode",
]

ANOMALY_ROLLING_WINDOW: int = int(os.environ.get("SPLUNK_ANOMALY_WINDOW", "20"))
ANOMALY_Z_THRESHOLD: float = float(os.environ.get("SPLUNK_ANOMALY_Z_THRESHOLD", "3.0"))

# Candidate numeric field names to scan for rolling z-score anomalies, checked in order.
ANOMALY_NUMERIC_FIELDS: list[str] = [
    "duration_ms", "duration", "elapsed", "response_time",
    "bytes", "bytes_out", "bytes_in", "status", "response_code",
]

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

AUTH_JSON_PATH: Path = Path(os.environ.get("SPLUNK_AUTH_PATH", str(Path.home() / ".splunk" / "auth.json")))
COOKIE_NAME: str = os.environ.get("SPLUNK_COOKIE_NAME", "splunkd_8089")
SPLUNK_URL: str = os.environ.get("SPLUNK_URL", "").rstrip("/")

# ---------------------------------------------------------------------------
# Job polling
# ---------------------------------------------------------------------------

POLL_INTERVAL: int = int(os.environ.get("SPLUNK_POLL_INTERVAL", "2"))
POLL_TIMEOUT: int = int(os.environ.get("SPLUNK_POLL_TIMEOUT", "300"))
MAX_REAUTH_ATTEMPTS: int = int(os.environ.get("SPLUNK_MAX_REAUTH", "3"))

# ---------------------------------------------------------------------------
# Watcher (splunk/watcher.py) — standalone continuous-monitoring process
# ---------------------------------------------------------------------------

WATCH_SPL: str = os.environ.get("SPLUNK_WATCH_SPL", "")
WATCH_INTERVAL: int = int(os.environ.get("SPLUNK_WATCH_INTERVAL", "60"))
WATCH_LOOKBACK: str = os.environ.get("SPLUNK_WATCH_LOOKBACK", "-15m")
WATCH_OVERLAP: int = int(os.environ.get("SPLUNK_WATCH_OVERLAP", "30"))
