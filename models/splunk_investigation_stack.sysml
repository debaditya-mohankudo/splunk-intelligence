package SplunkInvestigationStack {
    doc /* Structural model of splunk_analysis: ten parts over a shared
         * Foundation, composed into one System.
         *
         * WHY THE DECOMPOSITION LOOKS LIKE THIS. DetectionCore
         * (parsers.py + detectors.py + investigator.py) is one part, not
         * three, because investigator.py is explicitly the glue that
         * composes parser output into detector input and detector output
         * into a findings dict (investigator.py:1-7 — "holds no
         * orchestration logic, that lives in connector.py") — splitting it
         * out would leave two halves of one pipeline with no reason to
         * move independently.
         *
         * Orchestrator (connector.py) is the single part every entry
         * surface calls through — MCPServer, Runner, TUI, and its own
         * embedded CLI all depend on it, and it alone owns the in-process
         * `_sessions` cache plus the write-through to Persistence's
         * `active_runs` table. There is no server process (connector.py:
         * 1-8 — "there is no server process anymore"); every "interface"
         * part below is an in-process caller, not a network client.
         *
         * The Standalone Agent Subsystem (AgentLoop, LLMBackendSeam,
         * InvestigationAreaRegistry) is modelled as three parts, not one,
         * because each is independently swappable in practice: a new chat
         * backend is a new LLMBackendSeam implementation, a new
         * investigation domain is a new InvestigationAreaRegistry entry,
         * and neither requires touching AgentLoop. It is reachable only
         * via the optional --investigate flag and depends on DetectionCore
         * for findings but nothing in the primary MCP path depends on it —
         * README.md and CLAUDE.md both call it "a fallback, not the
         * primary path."
         *
         * Watcher is modelled separately from Orchestrator despite doing
         * similar work (load, detect, persist) because it has its own
         * process lifecycle (a long-running poll loop, no request/response
         * run) and its own bookmark-based correctness invariant — see
         * WatcherNoGapRequirement in requirements.sysml.
         *
         * ThreadSaturation is composed into System because it exists in
         * the repo and has its own CLI entry point, but it is NOT wired
         * into any other part's dependencies — verified by grep, no other
         * module imports it. It is a parallel, currently-orphaned detector
         * set, and the model says so rather than hiding it inside
         * DetectionCore to make the graph look more connected than the
         * code is. */

    private import Foundation::Foundation;
    private import Foundation::ModelProvenance;

    @ModelProvenance {
        derivedFromCommit = "4f91e33e1a5dc926054d812cf6d81a30a7b93e00";
        derivedAt = "2026-08-03";
        modelledPaths = (
            "splunk/parsers.py",
            "splunk/detectors.py",
            "splunk/investigator.py",
            "splunk/db.py",
            "splunk/connector.py",
            "splunk/mcp_server.py",
            "splunk/runner.py",
            "splunk/tui.py",
            "splunk/client.py",
            "splunk/auth.py",
            "splunk/agent.py",
            "splunk/llm_backends.py",
            "splunk/cli_tool_protocol.py",
            "splunk/investigation_areas.py",
            "splunk/watcher.py",
            "splunk/thread_saturation.py"
        );
    }

    part def DetectionCore {
        doc /* splunk/parsers.py, splunk/detectors.py, splunk/investigator.py.
             * Pure, deterministic: raw Splunk export/REST result -> pl.DataFrame
             * -> findings dict. No LLM calls, no network (detectors.py:1-3).
             * investigator.py composes the other two; it owns no data and
             * no orchestration of its own — see the package doc. */
        part foundation : Foundation;
    }

    part def Persistence {
        doc /* splunk/db.py. SQLite store (splunk.db, gitignored — never
             * committed) owning every table: events, findings, reports,
             * sourcetype_schema, investigation_queries, active_runs,
             * alerts, watcher_state. Sits at the bottom of the dependency
             * graph — no imports besides polars/stdlib. */
        part foundation : Foundation;
    }

    part def Orchestrator {
        doc /* splunk/connector.py. The facade every entry surface calls
             * through in-process — no HTTP, no server process
             * (connector.py:1-8). Owns the in-process `_sessions` cache
             * (heavy per-run objects) and mirrors lightweight run state
             * (iteration, confidence, pause flag, hint) into Persistence's
             * `active_runs` table on every update so any process can
             * observe live progress. Also drives the standalone agent's
             * outer loop (run_standalone_agent). */
        part foundation : Foundation;
        part core : DetectionCore;
        part store : Persistence;
    }

    part def MCPServer {
        doc /* splunk/mcp_server.py. FastMCP server exposing 9 investigation
             * tools as thin wrappers over Orchestrator and Persistence:
             * investigate_start, submit_report, get_findings, pause, hint,
             * query_examples, lsp_call_chain, check_alerts, ack_alert.
             * Owns no state of its own. */
        part foundation : Foundation;
        part orchestrator : Orchestrator;
        part store : Persistence;
    }

    part def Runner {
        doc /* splunk/runner.py. CLI entry point for the non-agent,
             * non-MCP path (`python -m splunk`) — loads a file/live
             * source, runs DetectionCore directly, persists via
             * Persistence. */
        part foundation : Foundation;
        part orchestrator : Orchestrator;
        part core : DetectionCore;
        part store : Persistence;
    }

    part def TUI {
        doc /* splunk/tui.py. Textual terminal UI (`python -m splunk.tui`)
             * — reads Persistence directly for run history and report
             * rendering, polls `active_runs` every ~2s for live progress,
             * and can launch new investigations via Orchestrator. */
        part foundation : Foundation;
        part orchestrator : Orchestrator;
        part store : Persistence;
    }

    part def LiveConnectivity {
        doc /* splunk/client.py, splunk/auth.py. auth.py opens a visible
             * Playwright/Chromium SSO flow and persists the session
             * cookie to ~/.splunk/auth.json; client.py loads that cookie
             * for every REST call (submit/poll/fetch) and silently
             * re-authenticates on 401 (max 3 attempts). */
        part foundation : Foundation;
    }

    part def InvestigationAreaRegistry {
        doc /* splunk/investigation_areas.py. Static, data-driven registry
             * of investigation domains (prompt + SPL template per area),
             * exposed via get_prompt/get_spl_template. Pure data and
             * accessors — consumed only by AgentLoop. */
        part foundation : Foundation;
    }

    part def LLMBackendSeam {
        doc /* splunk/llm_backends.py, splunk/cli_tool_protocol.py.
             * Pluggable LLMBackend (ollama, claude_cli, copilot_cli) for
             * AgentLoop's ReAct tool-calling. The CLI backends shell out
             * non-interactively with the CLI's own tool use disabled and
             * bridge tool-calling by hand via cli_tool_protocol.py's JSON
             * extraction/message-dedup helpers, reusing one CLI session
             * (--resume) across a whole investigation. */
        part foundation : Foundation;
    }

    part def AgentLoop {
        doc /* splunk/agent.py. Optional standalone LangGraph ReAct agent
             * (--investigate flag) — an alternative reasoning loop to the
             * MCP/Copilot path. Receives a findings dict, never raw
             * events or a DataFrame (CLAUDE.md — "Agent lives in agent.py
             * only"). Depends on InvestigationAreaRegistry for domain
             * prompts/SPL templates and LLMBackendSeam for the chat model. */
        part foundation : Foundation;
        part areas : InvestigationAreaRegistry;
        part backends : LLMBackendSeam;
    }

    part def Watcher {
        doc /* splunk/watcher.py. Standalone long-running process
             * (`python -m splunk.watcher`) polling Splunk on an interval,
             * running a detector subset, and writing hits into
             * Persistence's `alerts` table with a sliding bookmark
             * (`watcher_state`) so poll-window boundaries don't drop
             * events. Alerts it writes are read back through MCPServer's
             * check_alerts/ack_alert. */
        part foundation : Foundation;
        part connectivity : LiveConnectivity;
        part core : DetectionCore;
        part store : Persistence;
    }

    part def ThreadSaturation {
        doc /* splunk/thread_saturation.py. A parallel, standalone
             * detector set (own CLI: `python -m splunk.thread_saturation`)
             * for thread-pool-saturation/connection-contention findings —
             * mirrors DetectionCore's conventions but is NOT imported by
             * any other part today (verified via grep). Modelled as its
             * own part rather than folded into DetectionCore so the model
             * doesn't claim a wiring that doesn't exist. */
        part foundation : Foundation;
    }

    part def System {
        doc /* The whole stack, composed for requirements.sysml to satisfy
             * against. */
        part foundation : Foundation;
        part core : DetectionCore;
        part store : Persistence;
        part orchestrator : Orchestrator;
        part mcpServer : MCPServer;
        part runner : Runner;
        part tui : TUI;
        part connectivity : LiveConnectivity;
        part areas : InvestigationAreaRegistry;
        part backends : LLMBackendSeam;
        part agent : AgentLoop;
        part watcher : Watcher;
        part threadSaturation : ThreadSaturation;
    }
}
