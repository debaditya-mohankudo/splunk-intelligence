---
name: splunk-auth-decisions
description: Splunk SSO auth design decisions — non-headless Playwright, cookie name, auth.json location
metadata:
  type: project
  domain: splunk
  tags: splunk, auth, playwright, cookie, sso, headless, auth.json
---

Splunk SSO auth uses non-headless Playwright — browser opens visibly, user completes login manually. This is intentional: SSO/SAML flows involve MFA and org-specific redirects that can't be automated.

Cookie name: `splunkd_8089` (default), overridable via `SPLUNK_COOKIE_NAME` env var.
Cookie persisted to `~/.splunk/auth.json` (never in repo).

**Why:** Non-headless chosen explicitly — headless opted out because SSO login requires human interaction.
**How to apply:** Do not attempt to automate the login click flow. auth.py opens the browser and waits; user drives SSO; Playwright captures the cookie after.
