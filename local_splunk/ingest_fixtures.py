"""
Push tests/fixtures/*.json into the dummy Splunk container via HEC, so
--live queries have real indexed data to hit. Splunk's transaction command
(and everything else in SPL) only works over server-indexed events -- it
can't run against a local Polars DataFrame -- so this is what makes local
--live testing meaningful rather than just exercising the file-input path
a second time.

Usage:
    uv run python local_splunk/ingest_fixtures.py
    uv run python local_splunk/ingest_fixtures.py --url https://localhost:8088 --token <hec-token>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures"


def ingest_file(hec_url: str, token: str, path: Path, sourcetype: str) -> int:
    payload = json.loads(path.read_text())
    events = payload.get("results", payload if isinstance(payload, list) else [])
    if not events:
        print(f"  {path.name}: no events found, skipping")
        return 0

    headers = {"Authorization": f"Splunk {token}"}
    sent = 0
    for event in events:
        # HEC's top-level "host" defaults to the connection source (e.g.
        # localhost:8088) unless set explicitly here -- without this every
        # ingested event collapses onto one fake host, breaking any
        # host-keyed detector (host_error_ranking, detect_event_pairs, etc.)
        body = {"event": event, "sourcetype": sourcetype}
        if isinstance(event, dict) and event.get("host"):
            body["host"] = event["host"]
        resp = requests.post(f"{hec_url}/services/collector/event", headers=headers, json=body, verify=False)
        resp.raise_for_status()
        sent += 1
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://localhost:8088", help="HEC URL")
    parser.add_argument("--token", default="00000000-0000-0000-0000-000000000000", help="Must match SPLUNK_HEC_TOKEN used to start the container")
    args = parser.parse_args()

    for fixture in sorted(FIXTURES_DIR.glob("*.json")):
        sourcetype = fixture.stem  # e.g. cert_errors, windows_events
        n = ingest_file(args.url, args.token, fixture, sourcetype)
        print(f"  {fixture.name}: sent {n} event(s) as sourcetype={sourcetype}")


if __name__ == "__main__":
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        main()
    except requests.HTTPError as exc:
        print(f"Ingest failed: {exc}", file=sys.stderr)
        sys.exit(1)
