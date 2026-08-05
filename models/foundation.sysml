package Foundation {
    doc /* Cross-cutting: tunables and audit logging. A shared dependency of
         * every other part, not a peer subsystem.
         *
         * Corresponds to splunk/config.py and splunk/logger.py.
         *
         * Two properties of this part are load-bearing elsewhere in the
         * model and are recorded here because they are easy to lose in a
         * refactor:
         *
         *   1. config.py is the ONE place every tunable (thresholds, paths,
         *      model names, env var overrides) is declared. Every other
         *      module reads from it; none redefines a default locally.
         *
         *   2. RunLogger writes one structured JSON-lines file per run_id
         *      under logs/ — every entry point (connector.py, watcher.py,
         *      runner.py) that starts a unit of work opens its own logger
         *      instance rather than sharing global logging state, so a
         *      run's audit trail is always self-contained and disposable. */

    metadata def ModelProvenance {
        doc /* Which state of the codebase a package was written against.
             *
             * derivedFromCommit is the last commit that changed the CODE
             * this model describes — deliberately not the commit that last
             * touched the .sysml file. Stamped this way it changes exactly
             * when the model might have become wrong; stamped with the
             * model's own commit it would churn on every typo fix while
             * never once indicating staleness.
             *
             * To check a package for drift, compare its stamp against the
             * last commit touching the paths its docs cite:
             *
             *     git log -1 --format=%H -- <cited paths>
             *
             * A newer result means the model has not been re-examined
             * since that code moved. It does NOT mean the model is wrong
             * — only that nobody has checked.
             *
             * modelledPaths is that list of cited paths, declared here
             * rather than hardcoded in the checker, so a package states
             * its own scope and the test only enforces what the model
             * already claims.
             *
             * Enforced by tests/test_model_provenance.py. */
        attribute derivedFromCommit : String;
        attribute derivedAt : String;
        attribute modelledPaths : String[0..*];
    }

    @ModelProvenance {
        derivedFromCommit = "7ffc04d8f994c55adeaf954ba03ef9b04decfe0e";
        derivedAt = "2026-08-03";
        modelledPaths = (
            "splunk/config.py",
            "splunk/logger.py"
        );
    }

    part def Foundation {
        doc /* One declarative set of tunables (config.py), one per-run
             * disposable audit log (logger.py) — see the package doc for
             * why both are load-bearing. */
    }
}
