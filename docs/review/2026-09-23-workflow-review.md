# Workflow Review — Splunk Intelligence + Domain Ontology

**Date:** 2026-09-23 · **Scope:** `splunk/`, `standalone/`, agent docs (`AGENTS.md`, `SKILL.md`, `docs/investigation-loop.md`, `README.md`), `ontology/splunk-investigation-domain.json` · **Commit:** `15987ae`

## Verdict

The **core idea is streamlined**: one facade (`connector.py`), thin MCP wrappers, a self-contained
`submit_report → {status, findings, next}` loop, and no server process. That shape is right.

It **does need simplification** around the edges. The project has grown three entry points, two
copies of the findings pipeline, and four documents that each describe the loop slightly
differently. Several controls exposed to the agent (`pause`, `hint`, `Low` confidence) do nothing
on the primary MCP path. The ontology is a good idea, but it already disagrees with the code and
with the other docs on the terms that matter most: confidence rules, investigation areas and
detectors.

---

## 1. Bugs on the primary (MCP) path — fix first

| # | Finding | Evidence | Effect |
|---|---|---|---|
| B1 | **`splunk__hint` is a no-op for MCP runs.** `pop_hint` is only called in `run_standalone_agent`, and `submit_report` never reads the hint. | `connector.py:367` (only caller) | The tool docstring says the hint "is included in the findings passed to the next reasoning step". That is false on the primary path. |
| B2 | **`splunk__pause` is a no-op for MCP runs.** Only the standalone loop polls `pause_requested`. | `connector.py:359` | The agent keeps looping. Only the TUI shows "paused". |
| B3 | **Follow-up query labels in the `AGENTS.md` format break the query.** `AGENTS.md` tells Copilot to write `-- area: tls`, but `_SPL_COMMENT_RE = ^--\s*\w+\s*$` does not match the colon form, so the comment line is sent to Splunk as SPL. `store_queries` also fails to parse the area. | `investigator.py:_SPL_COMMENT_RE`, `db.py:store_queries`, `AGENTS.md` Step 2 | Copilot-driven follow-ups fail or return nothing → the run ends as "no new events". The area is stored as `""`. |
| B4 | **MCP runs never record `result_rows`.** `submit_report` calls `store_queries(run_id, iteration, queries)` *before* executing the queries and never updates the counts. | `connector.py:submit_report` | `splunk__query_examples(sort="effective")` and `area_stats` only reflect standalone-agent runs, so the grounding feature is blind for the primary path. |
| B5 | **Confidence collapses to High/Medium.** A report saying `**Confidence:** Low` is recorded as `Medium`. | `connector.py` `confidence = "High" if … else "Medium"` | The TUI, `confidence_nudge` and the DB overstate confidence. This contradicts the ontology's three-value `ConfidenceLevel`. |
| B6 | **Two findings pipelines have drifted apart.** `runner.run_pipeline` includes `event_pairs`. `investigator._build_findings`, which feeds MCP, the standalone agent and the rehydrate path, does not. | `runner.py:63-74` vs `investigator.py:_build_findings` | The agent never sees the entity-keyed A→B correlation detector, which is the one best suited to cascade root causes. |
| B7 | **`repo_path` is lost on rehydrate.** | `connector.py:_get_or_rehydrate_session` (`"repo_path": ""`) | CLI-fallback runs cannot use `lsp_call_chain` after the first call, even though `repo_path_nudge` said it was available. |

## 2. Simplification opportunities

### S1. One findings builder
Delete the detector dict in `runner.run_pipeline` and call `investigator._prepare_df` + `_build_findings`.
This fixes B6 and removes about 30 duplicated lines. Consider promoting both functions to public names
(`prepare_df`, `build_findings`), since three modules import them.

### S2. Collapse entry points
Right now there are three CLIs over the same engine:
- `python -m splunk` / `splunk.runner` — one-shot findings + `--investigate` + `--dump-findings`
- `python -m splunk.connector` — step-wise loop (start / submit-report / …)
- `python -m splunk.mcp_server`

Recommendation: keep `mcp_server` (primary) and **one** CLI. Fold `runner`'s one-shot report into
`connector` as `connector findings --source X [--markdown]` and `connector agent --source X`
(the standalone loop). `--dump-findings` then becomes redundant with `start`.

### S3. Share one done-rule between the two loops
`submit_report` and `run_standalone_agent` each re-implement the four termination conditions,
with small differences: the standalone loop stores queries on some exits and not others, and the
MCP path skips pause/hint. Extract `_step(session, report, queries) -> (status, reason, new_df)`
and have both loops call it. That fixes B1, B2 and B4 in one place.

### S4. Retire `dispatcher.py`'s context manager
`tool_called` wraps three one-line `if` checks in a pre/post hook class. Its own docstring explains
that it mirrors taskfw's design rather than meeting a need here. Inline the three nudges as plain
`result.update(...)` calls, or keep the pure `*_nudge` functions and drop `tool_called`/`apply_*`.
That saves about 50 lines and one indirection when reading `submit_report`.

### S5. Rename or harden `splunk__lsp_call_chain`
It is two `rg` calls, not LSP. `direction` is ignored (callers and callees return the same refs),
and `depth` only truncates the output. Rename it to `splunk__find_symbol_refs` with honest args, or
implement direction. Also move it into `connector.py` so every tool stays a thin wrapper.

### S6. Remove dead storage
`db.store_findings` and the `findings` table have no callers, since findings live in
`active_runs.findings_json`. Drop them, or start using the table for per-iteration findings
history, which the TUI could show.

## 3. Documentation drift — too many sources for one loop

The loop protocol is described in **four** places: `AGENTS.md`, `SKILL.md`,
`docs/investigation-loop.md` and `README.md`. They disagree:

| Topic | SKILL.md | AGENTS.md | investigation-loop.md | Ontology | Code |
|---|---|---|---|---|---|
| Query label | `-- area` | `-- area: tls` ❌ | — | — | only `-- area` works |
| Sparse data cap | event_count<50 → **Medium** | "sparse" → **Medium** | — | event_count<50 → **Low** | not enforced |
| Report sections | Summary / Root Cause / Affected Hosts / Timeline / Next Steps | Hypothesis / Evidence / Follow-up | — | matches SKILL | only `**Confidence:** High` is parsed |
| Tool count | — | 9 | **7** ❌ | — | 9 |
| Default index | `pki` hard-coded | — | — | — | `SPLUNK_INDEX` (default `*`) |

**Recommendation:** make `docs/investigation-loop.md` the single normative protocol (tools, done
rules, report template, query format, confidence rubric). `AGENTS.md` and `SKILL.md` should keep
only their client-specific bits (Copilot's "ask for repo_path once"; Claude's live-SPL preflight)
and link to it. The MCP server's `instructions=` string can also point the agent at the rubric.
README's "How it works" can drop its tool table and link there too.

## 4. Ontology review (`ontology/splunk-investigation-domain.json`)

**Good:** bounded contexts are well chosen. Detection, Reasoning, Follow-up Inquiry, Reporting and Run Lifecycle
line up with real seams (detectors → agent → investigation_areas → store_report → active_runs). The
"Finding is the only evidence" invariant is the most valuable rule in the whole project. Having it
as a named term helps.

**Inaccuracies against code:**
1. **`InvestigationArea`** lists `ocsp` / `crl` areas. They do not exist in `investigation_areas.py`.
   It also calls the registry *the* vocabulary, but only the standalone agent uses it. The MCP
   path's areas are free text from SKILL.md/AGENTS.md. Either register `ocsp`/`crl` or drop them,
   and state that the area registry is standalone-only (or wire it into the MCP path, for example
   return valid area names and templates in `investigate_start`'s result).
2. **`Detector`** omits `detect_event_pair_patterns` and `detect_http_errors`, which the watcher uses.
   It also lists `correlate_events` as if it were distinct from event pairs, without noting the overlap.
3. **`ConfidenceLevel`** says event_count<50 caps at **Low**. The skill says **Medium**, and the code
   enforces neither (B5). Pick one rule and enforce it in `submit_report`, which is cheap: `if
   event_count < 50 and confidence == "High": confidence = "Medium"`.
4. **`Finding`**'s evidence is `_build_findings`, which lacks `event_pairs` (B6). The term also
   leaves out `analyst_hint`, which the standalone path injects into findings.
5. **`Run`** says state is inferred from row presence plus `pause_requested`. That is correct, but "done"
   deletes the row, so a finished run can only be told apart from a never-started one via
   `reports`. Worth stating, since the TUI depends on it.

**Structural:**
- `RootCause is-a Hypothesis` plus `ConfidenceLevel determines RootCause` is fine. But `Report
  persists Run` reads backwards (a Report is persisted *for* a Run). Consider `Report part-of Run`.
- Missing terms that the workflow actually turns on: **Alert** (watcher output, has its own
  lifecycle: unacked → acked), **AnalystHint**, **Nudge**. Alert is a real domain object with MCP
  tools and a DB table. It deserves a place in the vocabulary, or an explicit out-of-scope note.
- There are now **three model layers** (SysML `models/`, `concept_store/`, `ontology/`) plus
  README. Only SysML has a staleness test (`test_model_provenance.py`). The ontology already
  drifted within five days of creation. Add a small test that checks each `evidence` path/symbol
  exists and that the `InvestigationArea`/`Detector` lists match `INVESTIGATION_AREAS` and
  `detectors.py`'s public functions. Without that, the ontology will keep making confident claims
  that are no longer true.

## 5. Suggested order of work

1. **S1 + S3** (one findings builder, one step function). This fixes B1, B2, B4 and B6 structurally.
2. **B3**: accept `-- area: x` in both regexes (`^--\s*(?:area:\s*)?(\w+)`), then standardise the docs on one form.
3. **B5 + ontology #3**: a three-value confidence parse and an enforced sparse-data cap.
4. **Docs consolidation (§3)**: one normative loop doc.
5. **Ontology fixes (§4)** plus an evidence-existence test.
6. **S2, S4, S5, S6**: cleanup with no behaviour change.

Items 1–3 change what the agent actually experiences. The rest reduce what a new reader or
agent has to reconcile.
