"""
Local-only auth helper for the dummy Splunk container (docker-compose.yml in
this directory). Bypasses splunk/auth.py's Playwright/SSO flow entirely --
that flow exists for production Splunk instances behind SAML, which a local
single-instance container does not use. This script logs in with plain HTTP
basic auth instead and writes ~/.splunk/auth.json in the exact format
splunk/client.py._load_cookie expects, so the rest of the pipeline
(client.py, runner.py --live, mcp_server.py) works unmodified against it.

Usage:
    uv run python local_splunk/get_local_cookie.py
    uv run python local_splunk/get_local_cookie.py --url https://localhost:8089 --password Changeme123!
"""

from __future__ import annotations

import argparse
import json
import sys

import requests

from splunk.config import AUTH_JSON_PATH, COOKIE_NAME


def get_cookie(url: str, username: str, password: str) -> str:
    """
    docker-splunk's REST login endpoint (:8089) returns the session key in
    the JSON body rather than a Set-Cookie header (unlike Splunk Web on
    :8000, which does set cookies). Splunk accepts that same session key
    value as a raw cookie on subsequent requests, so we use it directly --
    verified against a live container that both `Cookie: splunkd_8089=<key>`
    and `Authorization: Splunk <key>` return 200 on /services/server/info.
    """
    resp = requests.post(
        f"{url}/services/auth/login",
        data={"username": username, "password": password, "output_mode": "json"},
        verify=False,
    )
    resp.raise_for_status()
    session_key = resp.json().get("sessionKey")
    if not session_key:
        raise RuntimeError(f"Login succeeded but response had no sessionKey: {resp.text[:200]}")
    return session_key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://localhost:8089", help="Splunk management/REST URL")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="Changeme123!", help="Must match SPLUNK_PASSWORD used to start the container")
    args = parser.parse_args()

    cookie = get_cookie(args.url, args.username, args.password)

    AUTH_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_JSON_PATH.write_text(json.dumps({"cookie_name": COOKIE_NAME, "cookie_value": cookie}, indent=2))
    print(f"Cookie captured and saved to {AUTH_JSON_PATH}")


if __name__ == "__main__":
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        main()
    except requests.HTTPError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        sys.exit(1)
