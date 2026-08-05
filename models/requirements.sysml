package Requirements {
    doc /* Requirements traced to the parts that satisfy them.
         *
         * ACCEPTANCE CRITERION: every requirement here has exactly one
         * `satisfy` statement, EXCEPT CrossProcessVisibilityRequirement,
         * which genuinely needs two. See that requirement's doc for why
         * it is not routed around — it is a real architectural finding,
         * not a defect being hidden.
         *
         * SOURCING: each requirement cites the code that implements it.
         * None is aspirational — every one below is satisfied by shipped,
         * tested code (see tests/test_parsers.py, tests/test_agent.py,
         * tests/test_llm_backends.py, tests/test_connector.py).
         *
         * DELIBERATELY ABSENT: the sequential-vs-parallel detector
         * scheduling note in CLAUDE.md ("Future optimization") is a
         * documented non-decision about performance, not a guarantee the
         * system satisfies — it names what has NOT been done and why
         * that's fine for now, which doesn't fit a requirement def. */

    private import SplunkInvestigationStack::*;
    private import Foundation::ModelProvenance;

    @ModelProvenance {
        derivedFromCommit = "c14e88d8257ef78bd8c43b65b1e9adeafb5ad502";
        derivedAt = "2026-08-03";
        modelledPaths = (
            "splunk/detectors.py",
            "splunk/parsers.py",
            "splunk/config.py",
            "splunk/connector.py",
            "standalone/agent.py",
            "splunk/cli_tool_protocol.py",
            "standalone/watcher.py",
            "splunk/db.py",
            "splunk/mcp_server.py",
            "splunk/client.py",
            "splunk/investigator.py"
        );
    }

    part system : System;

    requirement def DetectorsPurityRequirement {
        doc /* Source: splunk/detectors.py:1-3. Detectors are pure Polars
             * transforms — no LLM calls, no network. Findings are always
             * reproducible from the same input DataFrame, which is what
             * lets both Orchestrator's MCP path and AgentLoop's standalone
             * path share one detection core without either path being
             * able to introduce a side effect the other doesn't expect. */
    }
    satisfy requirement DetectorsPurityRequirement by system.core;

    requirement def SchemaCacheSoftHintRequirement {
        doc /* Source: splunk/parsers.py:parse_splunk_json. The per-
             * sourcetype schema cache is a performance hint, not a
             * correctness guarantee — the same sourcetype can return
             * structurally different shapes depending on the SPL applied
             * (e.g. transaction/stats producing multivalue fields where a
             * plain search returns scalars). On a Polars ComputeError,
             * parse_splunk_json falls back to inference and calls
             * db.reset_schema (delete-then-insert) rather than
             * db.save_schema (upsert), so stale fields from an abandoned
             * shape don't linger to collide with a third query shape
             * later. */
    }
    satisfy requirement SchemaCacheSoftHintRequirement by system.core;

    requirement def TunablesSingleSourceRequirement {
        doc /* Source: splunk/config.py. Every threshold, path, and model
             * name is declared exactly once here and read by every other
             * module — no module redefines a default locally, so a tuning
             * change has one place to happen. */
    }
    satisfy requirement TunablesSingleSourceRequirement by system.foundation;

    requirement def DataFrameBoundaryRequirement {
        doc /* Source: splunk/connector.py (json.loads(json.dumps(findings,
             * default=str)) at the MCP/agent tool-return boundary).
             * pl.DataFrame is threaded through the full pipeline —
             * DetectionCore, Orchestrator, Watcher all pass DataFrames —
             * and is converted to plain dicts only at this boundary, never
             * earlier. Keeps the detection core's output shape from
             * leaking into two independently-drifting serialisations. */
    }
    satisfy requirement DataFrameBoundaryRequirement by system.orchestrator;

    requirement def AgentFindingsOnlyRequirement {
        doc /* Source: standalone/agent.py. AgentLoop receives a findings
             * dict and never touches a raw event or a DataFrame directly
             * — it imports InvestigationAreaRegistry and LLMBackendSeam,
             * not DetectionCore. Keeps the standalone agent's reasoning
             * surface identical in shape to what the MCP path hands
             * Copilot/Claude, so the two reasoning paths stay comparable. */
    }
    satisfy requirement AgentFindingsOnlyRequirement by system.agent;

    requirement def NoServerProcessRequirement {
        doc /* Source: splunk/connector.py:1-8 ("All callers ... go through
             * here instead of talking to each other over HTTP. There is
             * no server process anymore."). MCPServer, Runner, and TUI all
             * call Orchestrator in-process; there is no daemon to keep
             * alive and no network hop between them. */
    }
    satisfy requirement NoServerProcessRequirement by system.orchestrator;

    requirement def CrossProcessVisibilityRequirement {
        doc /* Source: splunk/connector.py (module docstring: heavy
             * per-run objects are cached in-process as a fast path, but
             * lightweight fields — iteration, confidence, pause flag,
             * hint — are mirrored to Persistence's `active_runs` table on
             * every update, "so any other process ... can observe live
             * progress"); splunk/db.py (owns the `active_runs` table
             * itself, and get_active_run_row/upsert_active_run/
             * clear_active_run_row are what Orchestrator calls).
             *
             * GENUINELY DUAL-SATISFIED, not a fake single allocation
             * avoided for tidiness: the guarantee holds only because
             * Orchestrator writes through on every update AND Persistence
             * durably holds a table any process can read. Taking either
             * part alone does not give you cross-process visibility —
             * Orchestrator without a durable table degrades to the old
             * in-memory-dict design the docstring explicitly says this
             * replaced; Persistence without a disciplined writer is just
             * an unpopulated table. Unlike a rule-and-its-data split, this
             * is deliberate joint responsibility by design, not drift —
             * flagged here per the skill's instruction not to hide a
             * requirement that genuinely needs two satisfiers. */
    }
    satisfy requirement CrossProcessVisibilityRequirement by system.orchestrator;
    satisfy requirement CrossProcessVisibilityRequirement by system.store;

    requirement def RunStateMachineRequirement {
        doc /* Source: splunk/connector.py (start_investigation,
             * submit_report, request_pause, resume), modelled in
             * RunLifecycle. activeState -> pausedState -> activeState via
             * request_pause/resume; activeState -> doneState (terminal)
             * when submit_report's confidence/iteration/query conditions
             * are met. State is inferred from active_runs row presence
             * plus pause_requested, not a stored enum. */
    }
    satisfy requirement RunStateMachineRequirement by system.orchestrator;

    requirement def WatcherNoGapRequirement {
        doc /* Source: standalone/watcher.py:_next_earliest. Each poll cycle's
             * earliest time is the previous bookmark minus an overlap
             * window, "so sliding-window detectors don't miss events
             * straddling the previous poll cycle's boundary" — an
             * explicit anti-gap invariant for continuous monitoring. */
    }
    satisfy requirement WatcherNoGapRequirement by system.watcher;

    requirement def CLIBackendDeliveryRequirement {
        doc /* Source: splunk/cli_tool_protocol.py:select_new_messages.
             * A message is marked "already sent" to a CLI-backed session
             * only after the CLI call it was flattened into actually
             * succeeds — so a failed call doesn't wrongly mark messages
             * delivered and silently drop them from the next retry's
             * prompt. */
    }
    satisfy requirement CLIBackendDeliveryRequirement by system.backends;

    requirement def RunDataNotCommittedRequirement {
        doc /* Source: splunk/db.py:1-4 ("DB lives at <repo_root>/
             * splunk.db — not committed"), consistent with CLAUDE.md
             * (splunk.db, logs/, reports/, results/ gitignored). No
             * investigated log data or generated report ever leaves the
             * machine via git — a structural property of where
             * Persistence writes, not a policy someone has to remember. */
    }
    satisfy requirement RunDataNotCommittedRequirement by system.store;

    requirement def MCPPortabilityRequirement {
        doc /* Source: splunk/mcp_server.py. 9 investigation tools
             * (investigate_start, submit_report, get_findings, pause,
             * hint, query_examples, lsp_call_chain, check_alerts,
             * ack_alert) are the only interface MCPServer exposes — any
             * MCP host reaches the whole investigation loop through them,
             * with no host-specific adapter on top. */
    }
    satisfy requirement MCPPortabilityRequirement by system.mcpServer;

    requirement def LiveAuthRetryRequirement {
        doc /* Source: splunk/client.py. Every REST call loads the SSO
             * cookie auth.py persisted to ~/.splunk/auth.json and
             * silently re-authenticates on a 401, up to 3 attempts,
             * before surfacing an error — since Splunk's SSO/SAML flow
             * means there is no password-based retry available. */
    }
    satisfy requirement LiveAuthRetryRequirement by system.connectivity;
}
