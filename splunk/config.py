"""
Centralised configuration and tunable constants for the splunk pipeline.
Override via environment variables or by editing this file before running.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

LLM_MODEL: str = os.environ.get("SPLUNK_LLM_MODEL", "qwen2.5:14b")
AGENT_MAX_ITER: int = int(os.environ.get("SPLUNK_AGENT_MAX_ITER", "10"))
SPLUNK_INDEX: str = os.environ.get("SPLUNK_INDEX", "pki")
INVESTIGATOR_MAX_ITER: int = int(os.environ.get("SPLUNK_INVESTIGATOR_MAX_ITER", "3"))

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
