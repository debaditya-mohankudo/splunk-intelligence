# Pending: Workflow simplification (from 2026-09-23 review)

Source: [docs/review/2026-09-23-workflow-review.md](../review/2026-09-23-workflow-review.md)
Tracked in taskfw — epic `1ba6c14a`. Work in the order below.

## 1. `2ad936ec` — Unify findings builder and loop step function (S1+S3)
Fixes B1 (hint no-op), B2 (pause no-op), B4 (no result_rows), B6 (event_pairs drift).
Files: `splunk/runner.py`, `splunk/investigator.py`, `splunk/connector.py`, `splunk/db.py`, `tests/test_connector.py`
- [ ] `runner.run_pipeline` calls `_prepare_df`/`_build_findings`; duplicate detector dict removed
- [ ] `_build_findings` includes `event_pairs`
- [ ] Shared step function used by `submit_report` and `run_standalone_agent`
- [ ] MCP `submit_report` honours `pause_requested` and injects `pop_hint` as `analyst_hint`
- [ ] MCP path stores `result_rows` per executed query
- [ ] Tests for hint, pause, event_pairs, result_rows on MCP path

## 2. `e09c840f` — Accept `-- area: x` query label format (B3)
Files: `splunk/investigator.py`, `splunk/db.py`, `AGENTS.md`, `.claude/skills/splunk-investigate/SKILL.md`
- [ ] Both regexes accept optional `area:` prefix
- [ ] Test: `-- area: tls` strips to pure SPL and stores `area='tls'`
- [ ] Standardise AGENTS.md/SKILL.md on one label form

## 3. `28f07830` — Three-value confidence + enforced sparse-data cap (B5)
Files: `splunk/connector.py`, `splunk/investigator.py`, `ontology/splunk-investigation-domain.json`
- [ ] Parse High/Medium/Low from report
- [ ] **Decide** cap (Medium vs Low) and enforce when `event_count < 50`
- [ ] Align SKILL.md, AGENTS.md, ontology `ConfidenceLevel`
- [ ] Tests for Low passthrough and sparse cap

## 4. `d20adb1b` — Consolidate loop protocol into `docs/investigation-loop.md`
Files: `docs/investigation-loop.md`, `AGENTS.md`, `SKILL.md`, `README.md`
- [ ] Normative doc: 9 tools, done rules, report template, query format, confidence rubric
- [ ] AGENTS.md / SKILL.md keep only client-specific parts and link to it
- [ ] README tool table replaced with link
- [ ] Drop hard-coded `index=pki` default in favour of `SPLUNK_INDEX`

## 5. `cde7b109` — Fix ontology drift + evidence-existence test
Files: `ontology/splunk-investigation-domain.json`, `tests/test_ontology.py`
- [ ] `InvestigationArea` matches `INVESTIGATION_AREAS` (register or drop ocsp/crl; note standalone-only scope)
- [ ] `Detector` list matches `detectors.py` public functions
- [ ] Add Alert, AnalystHint, Nudge terms (or out-of-scope note)
- [ ] Revisit `Report persists Run` direction
- [ ] Test: evidence paths/symbols exist; area and detector lists match code

## 6. `1336d302` — Cleanup (S2, S4, S5, S6, B7)
Files: `splunk/runner.py`, `splunk/connector.py`, `splunk/dispatcher.py`, `splunk/mcp_server.py`, `splunk/db.py`
- [ ] Fold runner one-shot/`--investigate` into connector CLI; `python -m splunk` delegates
- [ ] Replace `dispatcher.tool_called`/`apply_*` with plain result updates
- [ ] Rename/fix `splunk__lsp_call_chain` (honour or remove `direction`); move logic into connector
- [ ] Persist `repo_path` on `active_runs` so rehydrate keeps it
- [ ] Drop `db.store_findings` + `findings` table, or use for per-iteration history
- [ ] Update README, concept_store, SysML stamps
