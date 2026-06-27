"""
Splunk SSO auth via Playwright — visible browser, user completes login manually.
Extracts session cookie and persists to ~/.splunk/auth.json.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from splunk.config import AUTH_JSON_PATH as AUTH_PATH, COOKIE_NAME, SPLUNK_URL

load_dotenv()

logger = logging.getLogger(__name__)


def run_auth_flow() -> None:
    """
    Open a visible browser, navigate to Splunk, wait for the user to complete
    SSO login, then extract and persist the session cookie.
    """
    if not SPLUNK_URL:
        raise ValueError("SPLUNK_URL is not set. Add it to .env or environment.")

    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=50)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        logger.info("Opening Splunk at %s — complete SSO login in the browser window.", SPLUNK_URL)
        page.goto(SPLUNK_URL)

        # Wait until the user lands on a Splunk page that has the session cookie.
        # We poll until the target cookie appears (up to 5 minutes).
        print(f"\n[splunk/auth] Browser opened. Please complete SSO login.\nWaiting for session cookie '{COOKIE_NAME}'...\n")

        cookie_value = _wait_for_cookie(context, COOKIE_NAME, timeout_ms=300_000)

        if not cookie_value:
            browser.close()
            raise RuntimeError(
                f"Cookie '{COOKIE_NAME}' not found after login. "
                "Check SPLUNK_COOKIE_NAME or complete the login flow."
            )

        _persist_cookie(COOKIE_NAME, cookie_value)
        print(f"[splunk/auth] Cookie captured and saved to {AUTH_PATH}")
        browser.close()


def _wait_for_cookie(context, name: str, timeout_ms: int = 300_000) -> str | None:
    """Poll the browser context for a named cookie, blocking until found or timeout."""
    import time
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        cookies = context.cookies()
        for c in cookies:
            if c["name"] == name:
                return c["value"]
        time.sleep(1)
    return None


def _persist_cookie(name: str, value: str) -> None:
    payload = {
        "cookie_name": name,
        "cookie_value": value,
    }
    AUTH_PATH.write_text(json.dumps(payload, indent=2))


def validate_session() -> bool:
    """
    Quick check — GET /services/server/info with stored cookie.
    Returns True if session is still valid.
    """
    import requests

    try:
        from splunk.client import _load_cookie, _session
        cookie = _load_cookie()
        session = _session(cookie)
        resp = session.get(f"{SPLUNK_URL}/services/server/info", params={"output_mode": "json"})
        return resp.status_code == 200
    except Exception as exc:
        logger.debug("Session validation failed: %s", exc)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_auth_flow()
