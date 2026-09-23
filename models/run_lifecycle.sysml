package RunLifecycle {
    doc /* State machine for an investigation run, transcribed from
         * splunk/connector.py's start_investigation/submit_report/
         * request_pause/resume and splunk/db.py's `active_runs` table.
         *
         * There is no explicit status enum in the code — state is
         * inferred from row presence in `active_runs` plus its
         * `pause_requested` flag. This is modelled as three states anyway
         * because the transitions ARE explicit and real, just implicit in
         * their representation:
         *
         *   - activeState: an `active_runs` row exists, pause_requested=0.
         *     Entered by start_investigation (connector.py, writes the row
         *     via upsert_active_run).
         *   - pausedState: the row still exists but pause_requested=1.
         *     Entered by request_pause. Neither loop is interrupted:
         *     the standalone agent's outer loop polls the flag before
         *     every iteration, and the MCP path's submit_report refuses
         *     the step (status=paused, nothing stored, iteration
         *     unchanged) until resume clears it.
         *   - doneState: the row is cleared (clear_active_run_row).
         *     Entered from either loop via connector._step (shared by
         *     submit_report and run_standalone_agent) when parsed confidence
         *     is High (after the sparse-data cap: <50 events caps High
         *     to Medium),
         *     iteration >= INVESTIGATOR_MAX_ITER, no queries were
         *     submitted, or follow-up queries returned no new events.
         *     Terminal — nothing transitions out of doneState.
         *
         * NOT MODELLED, deliberately: the exact confidence/iteration/
         * query conditions that trigger activeToDone — those are a
         * disjunction of four conditions (connector.py, _step),
         * not a state distinction, and are cited directly in
         * requirements.sysml instead of turned into four fake states. */

    private import Foundation::ModelProvenance;

    @ModelProvenance {
        derivedFromCommit = "265e3764d79a9d1382f21ed834db8949e31a1fe5";
        derivedAt = "2026-09-23";
        modelledPaths = (
            "splunk/connector.py",
            "splunk/db.py"
        );
    }

    state def RunLifecycle {
        doc /* Transcribed from splunk/connector.py — see the package doc
             * for what is deliberately not modelled here. */
        entry;

        state activeState;
        state pausedState;
        state doneState;

        transition activeToPaused
            first activeState
            then pausedState;

        transition pausedToActive
            first pausedState
            then activeState;

        transition activeToDone
            first activeState
            then doneState;
    }
}
