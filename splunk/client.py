"""
Splunk REST client — auth via Playwright SSO cookie, query execution, results fetch.
Requires SPLUNK_URL in environment or .env file.
Session cookie loaded from ~/.splunk/auth.json (produced by splunk/auth.py).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from splunk import config
from splunk.config import (
    AUTH_JSON_PATH as AUTH_PATH,
    MAX_REAUTH_ATTEMPTS,
    POLL_INTERVAL,
    POLL_TIMEOUT,
)
from splunk.parsers import parse_splunk_json

load_dotenv()

logger = logging.getLogger(__name__)


class SplunkAuthError(Exception):
    pass


class SplunkQueryError(Exception):
    pass


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _load_cookie() -> dict[str, str]:
    """Load session cookie from ~/.splunk/auth.json."""
    if not AUTH_PATH.exists():
        raise SplunkAuthError(f"auth.json not found at {AUTH_PATH}. Run splunk/auth.py first.")
    data = json.loads(AUTH_PATH.read_text())
    name = data.get("cookie_name")
    value = data.get("cookie_value")
    if not name or not value:
        raise SplunkAuthError("auth.json is missing cookie_name or cookie_value.")
    logger.debug("Loaded cookie '%s' from %s", name, AUTH_PATH)
    return {name: value}


def _session(cookie: dict[str, str]) -> requests.Session:
    s = requests.Session()
    s.cookies.update(cookie)
    s.verify = False  # many on-prem Splunk instances use self-signed certs
    return s


def re_auth() -> None:
    """Trigger Playwright SSO re-login and reload auth.json."""
    from splunk.auth import run_auth_flow  # deferred import — Playwright dep
    logger.warning("Re-authenticating via Playwright SSO...")
    run_auth_flow()


# ---------------------------------------------------------------------------
# Core REST operations
# ---------------------------------------------------------------------------

def submit_query(spl: str, earliest: str = "-24h", latest: str = "now") -> str:
    """POST /services/search/jobs — returns SID."""
    logger.info("Submitting query earliest=%s latest=%s | %s", earliest, latest, spl[:120])
    cookie = _load_cookie()
    session = _session(cookie)
    url = f"{config.SPLUNK_URL}/services/search/jobs"
    payload = {
        "search": f"search {spl}" if not spl.strip().startswith("search") else spl,
        "earliest_time": earliest,
        "latest_time": latest,
        "output_mode": "json",
    }
    resp = session.post(url, data=payload)
    _check_response(resp)
    sid = resp.json()["sid"]
    logger.info("Job submitted — SID: %s", sid)
    return sid


def poll_job(
    sid: str,
    interval: int = POLL_INTERVAL,
    timeout: int = POLL_TIMEOUT,
) -> dict[str, Any]:
    """Poll /services/search/jobs/{sid} until dispatchState=DONE or timeout."""
    cookie = _load_cookie()
    session = _session(cookie)
    url = f"{config.SPLUNK_URL}/services/search/jobs/{sid}"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        resp = session.get(url, params={"output_mode": "json"})
        _check_response(resp)
        entry = resp.json()["entry"][0]["content"]
        state = entry.get("dispatchState", "")
        logger.debug("Job %s state: %s (%.0f%%)", sid, state, entry.get("doneProgress", 0) * 100)

        if state == "DONE":
            logger.info("Job %s DONE — %d events scanned", sid, entry.get("scanCount", 0))
            return entry
        if state in ("FAILED", "FINALIZED"):
            logger.error("Job %s ended with state %s", sid, state)
            raise SplunkQueryError(f"Job {sid} ended with state {state}")

        time.sleep(interval)

    raise SplunkQueryError(f"Job {sid} did not complete within {timeout}s")


def fetch_results(sid: str, count: int = 0) -> str:
    """GET /services/search/jobs/{sid}/results — returns raw JSON string."""
    logger.info("Fetching results for SID %s (count=%d)", sid, count)
    cookie = _load_cookie()
    session = _session(cookie)
    url = f"{config.SPLUNK_URL}/services/search/jobs/{sid}/results"
    params = {"output_mode": "json", "count": count}
    resp = session.get(url, params=params)
    _check_response(resp)
    logger.info("Fetched %d bytes for SID %s", len(resp.content), sid)
    return resp.text


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------

def run_query(
    spl: str,
    earliest: str = "-24h",
    latest: str = "now",
) -> list[dict[str, Any]]:
    """Submit → poll → fetch → parse. Returns list of normalised event dicts."""
    if not config.SPLUNK_URL:
        raise SplunkAuthError("SPLUNK_URL is not set. Add it to .env or environment.")

    logger.info("run_query: spl=%s earliest=%s latest=%s", spl[:80], earliest, latest)
    _reauth_attempts = 0

    while True:
        try:
            sid = submit_query(spl, earliest, latest)
            poll_job(sid)
            raw = fetch_results(sid)
            events = parse_splunk_json(raw)
            logger.info("run_query complete — %d events returned", len(events))
            return events

        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401:
                _reauth_attempts += 1
                if _reauth_attempts >= MAX_REAUTH_ATTEMPTS:
                    raise SplunkAuthError("Authentication failed after 3 attempts.") from exc
                logger.warning("Got 401 — re-auth attempt %d/%d", _reauth_attempts, MAX_REAUTH_ATTEMPTS)
                re_auth()
            else:
                raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_response(resp: requests.Response) -> None:
    if resp.status_code == 401:
        resp.raise_for_status()
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        raise SplunkQueryError(f"Splunk API error {resp.status_code}: {resp.text[:200]}") from exc
