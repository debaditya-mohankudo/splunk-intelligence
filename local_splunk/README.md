# Local dummy Splunk instance

A throwaway single-instance Splunk container for testing the `--live` path
(`splunk/client.py`, `runner.py --live`, `mcp_server.py`) against a real
Splunk REST API and real SPL execution -- without needing production
credentials or SSO.

Bypasses `splunk/auth.py`'s Playwright/SSO flow entirely: that flow exists
for production Splunk behind SAML. This container uses plain basic auth,
so `get_local_cookie.py` logs in directly via `/services/auth/login` and
writes the same `~/.splunk/auth.json` format `client.py` expects. Nothing
in the production code path is touched.

Note: docker-splunk's REST login endpoint (:8089) returns the session key
in the JSON response body rather than a `Set-Cookie` header (Splunk Web on
:8000 does set cookies; the management API doesn't by default). Splunk
accepts that same session key as a raw cookie value on later requests, so
`get_local_cookie.py` uses it directly as `cookie_value` -- verified
against a live container.

No native arm64 Splunk image exists -- on Apple Silicon this runs under
Docker's amd64 emulation (`platform: linux/amd64` in the compose file).
First boot is slower than on x86_64; subsequent starts are faster.

## Setup

```bash
# 1. Start the container (first boot takes 2-5 min under emulation)
cd local_splunk
docker compose up -d
docker compose logs -f splunk   # watch for "Ansible playbook complete"

# 2. Get a session cookie (writes ~/.splunk/auth.json)
uv run python get_local_cookie.py

# 3. Enable HEC and create a token (SPLUNK_HEC_TOKEN env var alone does not
#    enable HEC on this image -- verified against a live container: the
#    global http-inputs setting stays disabled=1 until you enable it explicitly)
docker exec splunk-dummy /opt/splunk/bin/splunk http-event-collector enable \
  -uri https://localhost:8089 -auth admin:Changeme123!
docker exec splunk-dummy /opt/splunk/bin/splunk http-event-collector create local-test-token \
  -uri https://localhost:8089 -auth admin:Changeme123! -description "local test"
# copy the `token=` value printed above

# 4. Load the existing test fixtures into it via HEC
uv run python ingest_fixtures.py --token <token-from-step-3>

# 5. Query it for real
uv run python -m splunk.runner --live --spl 'sourcetype=cert_errors' --earliest -24h
```

## Testing SPL directly (e.g. the transaction command)

Once fixtures are ingested, you can validate SPL patterns that a local
Polars detector can't express (server-side `transaction`, `tstats`, etc.)
for real:

```bash
uv run python -m splunk.runner --live --spl \
  'sourcetype=cert_errors | transaction host maxspan=1h startswith="ocsp" endswith="handshake failed"' \
  --dump-findings
```

## Teardown

```bash
docker compose down -v   # -v also drops the indexed data volume
```

## Notes

- Default creds: `admin` / `Changeme123!` (override via `SPLUNK_PASSWORD` env var before `docker compose up`).
- Splunk Web UI available at http://localhost:8000 if you want to poke around interactively.
- This directory is for local dev/testing only -- not part of the shipped pipeline.
