"""
NOOA-based agent — ollama backend only.

Receives structured findings from detectors, reasons over them, emits a
markdown report. The agent is a plain Python object (nooa.Agent): findings,
report, followup_queries and hypotheses live on `self` as real typed state;
the deterministic methods below are automatically callable by the model as
tools; `investigate()`'s `...` body is LLM-driven under CodeActStrategy,
which replaces the old hand-rolled StateGraph/should_continue loop — the
model writes ordinary Python against `self` and decides for itself when the
investigation is done (e.g. by checking `self.hypotheses`) instead of that
being a graph edge evaluated after every tool call.

claude_cli/copilot_cli still run through standalone/agent_cli_bridge.py's old
LangGraph loop — see that module's docstring for why.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nooa import Agent, strategy
from nooa.config import CodeActConfig
from nooa.strategies import CodeActStrategy
from nooa.unifiedllm.registry import get_llm_client

from splunk import config
from splunk.config import AGENT_MAX_ITER as MAX_ITERATIONS, SPLUNK_INDEX
from splunk.investigation_areas import get_prompt, get_spl_template
from splunk.llm_backends import OllamaBackend

logger = logging.getLogger(__name__)

AGENT_PERSONA = """You are a senior site reliability / security engineer investigating an incident from Splunk log data.
`self.findings` holds structured findings extracted from Splunk logs — frequency spikes, repeating error patterns,
cert/PKI anomalies, entity-keyed event-pair correlations, host error rankings, slow queries, HTTP errors,
numeric anomalies, and severity/timeline summaries. Not every investigation will have every finding type —
reason only over what's actually present in the data, whatever domain it comes from (PKI, web traffic,
application errors, infrastructure metrics, etc.). Inspect self.findings directly (e.g. print it, or call
self.summarise_findings()) before reasoning — do not hallucinate field values, only reference data present
in the findings.

Your job:
1. Reason over the findings to identify candidate root causes.
2. Register them with self.rank_hypotheses(...) so they're tracked through the investigation — use
   self.request_deeper_analysis(...) and self.generate_followup_queries(...) (naming the relevant area
   for each) to gather evidence for or against each one.
3. Reference specific timestamps, hosts, error codes, and sourcetypes from the data.
4. Once one hypothesis is clearly supported, assign a confidence level (High / Medium / Low) and call
   self.format_report(...), passing that hypothesis's claim text as confirmed_hypothesis so it's marked
   resolved. Once every tracked hypothesis is resolved (not "open"), stop iterating.
5. Suggest the next 2-3 investigation steps an analyst should take.

Think step by step. Use the methods below to organise your reasoning before calling format_report."""


def _build_agent_class(llm: Any) -> type[Agent]:
    """llm is bound once per analyse() call and constructed fresh each time —
    unlike the CLI bridge there's no subprocess session to keep alive across
    iterations, ollama is a stateless HTTP call per turn."""

    class LogAnalysisAgent(Agent, llm=llm):
        __doc__ = AGENT_PERSONA

        def __init__(self, findings: dict[str, Any]):
            super().__init__()
            self.findings = findings
            self.report = ""
            self.followup_queries: list[str] = []
            self.hypotheses: list[dict[str, Any]] = []

        def summarise_findings(self) -> str:
            """Produce a concise bullet-point summary of self.findings."""
            f = self.findings
            lines = []
            if spikes := f.get("spikes"):
                lines.append(f"- {len(spikes)} frequency spike(s) detected")
                for s in spikes[:3]:
                    lines.append(f"  • {s['window_start']} — {s['event_count']} events in {s['window_seconds']}s on hosts: {', '.join(s['hosts'])}")

            if patterns := f.get("patterns"):
                lines.append(f"- {len(patterns)} repeating pattern(s)")
                for p in patterns[:3]:
                    if p.get("type") == "repeated_error":
                        lines.append(f"  • {p['sourcetype']} / error {p['error_code']} — {p['count']}x")

            if cert_anomalies := f.get("cert_anomalies"):
                lines.append(f"- {len(cert_anomalies)} cert anomaly event(s)")
                for c in cert_anomalies[:3]:
                    lines.append(f"  • [{c['host']}] {', '.join(c['matched_keywords'])} at {c['time']}")

            if severity := f.get("severity"):
                lines.append(f"- Severity breakdown: {severity}")

            if host_ranking := f.get("host_ranking"):
                top = host_ranking[:3]
                lines.append(f"- Top error hosts: {', '.join(h['host'] + '(' + str(h['error_count']) + ')' for h in top)}")

            return "\n".join(lines) if lines else "No significant findings."

        def rank_hypotheses(self, hypotheses: list[str | dict[str, str]]) -> str:
            """
            Register root-cause hypotheses for tracking across the investigation.
            Each item is either a plain claim string, or a dict {"claim": str, "area": str
            (optional, an area from request_deeper_analysis), "evidence": str (optional,
            brief supporting reference)}. Registered hypotheses start "open" and are
            confirmed/rejected as the investigation proceeds (via format_report).
            """
            existing_claims = {h["claim"] for h in self.hypotheses}
            added = []
            for h in hypotheses:
                if isinstance(h, str) and h not in existing_claims:
                    entry = {"claim": h, "area": "", "evidence": "", "status": "open"}
                elif isinstance(h, dict) and h.get("claim") and h["claim"] not in existing_claims:
                    entry = {
                        "claim": h["claim"],
                        "area": h.get("area", ""),
                        "evidence": h.get("evidence", ""),
                        "status": "open",
                    }
                else:
                    continue
                self.hypotheses.append(entry)
                existing_claims.add(entry["claim"])
                added.append(entry)
            logger.info("rank_hypotheses called — %d hypothesis(es) tracked", len(self.hypotheses))
            ranked = "\n".join(f"{i + 1}. {h['claim']}" for i, h in enumerate(self.hypotheses))
            return f"Hypotheses registered, ranked by evidence strength:\n{ranked}" if ranked else "No hypotheses registered."

        def request_deeper_analysis(self, area: str) -> str:
            """
            Signal that a specific area needs deeper investigation.
            Returns a prompt for the agent to focus its next reasoning step.
            area: a domain from splunk.investigation_areas.INVESTIGATION_AREAS (e.g.
            'api_errors', 'database_slowdown', 'cascading_failure', 'host_isolation',
            'timeline') — any other string is accepted too and falls back to a generic
            investigation prompt.
            """
            return get_prompt(area)

        def format_report(
            self,
            summary: str,
            root_cause: str,
            confidence: str,
            affected_hosts: list[str],
            timeline: str,
            next_steps: list[str],
            confirmed_hypothesis: str = "",
        ) -> str:
            """
            Emit the final markdown investigation report and store it on self.report.
            Call this once you have reached a conclusion.
            confidence: 'High', 'Medium', or 'Low'
            next_steps: 2-3 actions
            confirmed_hypothesis: optional — the claim text of a hypothesis registered via
            rank_hypotheses that this report's root_cause confirms. If set, that hypothesis
            is marked confirmed and every other still-open hypothesis is marked rejected.
            """
            steps = "\n".join(f"- {s.strip()}" for s in next_steps if s.strip())
            self.report = f"""# Splunk Investigation Report

## Summary
{summary}

## Root Cause Hypothesis
{root_cause}

**Confidence:** {confidence}

## Affected Hosts
{', '.join(affected_hosts)}

## Timeline
{timeline}

## Recommended Next Steps
{steps}
"""
            if confirmed_hypothesis:
                for h in self.hypotheses:
                    if h["claim"] == confirmed_hypothesis:
                        h["status"] = "confirmed"
                    elif h["status"] == "open":
                        h["status"] = "rejected"
                logger.info("Hypothesis confirmed: %r", confirmed_hypothesis)
            logger.info("format_report called — report captured (%d chars)", len(self.report))
            return self.report

        def generate_followup_queries(
            self,
            hosts: list[str],
            error_codes: list[str],
            sourcetype: str,
            spike_start: str,
            areas: list[str],
        ) -> str:
            """
            Generate follow-up SPL queries for the next investigation iteration and
            append them to self.followup_queries.
            hosts: host names from findings
            error_codes: error codes from findings
            sourcetype: primary sourcetype from findings
            spike_start: ISO timestamp of the first spike
            areas: area names from splunk.investigation_areas.INVESTIGATION_AREAS that
            have a spl_template (e.g. host_isolation, api_errors, database_slowdown,
            cascading_failure, timeline, first_occurrence)
            """
            from datetime import datetime, timedelta

            host_list = ", ".join(f'"{h}"' for h in hosts if h)
            error_filter = " OR ".join(f'error_code="{e}"' for e in error_codes if e) or "*"

            try:
                t = datetime.fromisoformat(spike_start.replace("Z", "+00:00"))
                spike_start_minus5m = (t - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
                spike_start_plus30m = (t + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
            except (ValueError, AttributeError):
                spike_start_minus5m = spike_start
                spike_start_plus30m = spike_start

            slots = {
                "index": SPLUNK_INDEX,
                "hosts": host_list,
                "sourcetype": sourcetype.strip() or "*",
                "error_filter": error_filter,
                "spike_start": spike_start,
                "spike_start_minus5m": spike_start_minus5m,
                "spike_start_plus30m": spike_start_plus30m,
            }

            new_queries = []
            for area in (a.strip() for a in areas if a.strip()):
                tmpl = get_spl_template(area)
                if tmpl:
                    new_queries.append(f"-- {area}\n{tmpl.format(**slots)}")
                else:
                    logger.warning("Unknown area '%s' (or area has no spl_template) in generate_followup_queries", area)

            self.followup_queries.extend(new_queries)
            logger.info("generate_followup_queries called — %d queries captured", len(new_queries))
            return "\n\n".join(new_queries) if new_queries else "No queries generated — check area names."

        @strategy(CodeActStrategy(config=CodeActConfig(max_iterations=MAX_ITERATIONS)))
        async def investigate(self) -> str:
            """Analyse self.findings and produce the final markdown investigation
            report via format_report, then return self.report."""
            ...

    return LogAnalysisAgent


async def _analyse_nooa(findings: dict[str, Any]) -> tuple[str, list[str]]:
    model = config.LLM_MODEL
    logger.info(
        "Starting agent analysis — backend=ollama model=%s max_iter=%d event_count=%d",
        model, MAX_ITERATIONS, findings.get("event_count", 0),
    )
    OllamaBackend(model).check_available()
    llm = get_llm_client(f"ollama_chat/{model}", api_base="http://localhost:11434")
    agent_cls = _build_agent_class(llm)
    agent = agent_cls(findings)

    result = await agent.investigate()

    if agent.hypotheses:
        logger.info("Hypotheses tracked: %s", [(h["claim"], h["status"]) for h in agent.hypotheses])

    if agent.report:
        logger.info("Analysis complete — report from format_report (%d chars)", len(agent.report))
        return agent.report, agent.followup_queries

    logger.warning("format_report not called — returning investigate()'s own return value")
    return (result if isinstance(result, str) else str(result)) or "No report generated.", agent.followup_queries


def analyse(findings: dict[str, Any]) -> tuple[str, list[str]]:
    """
    Run the agent over structured findings from detectors.
    Returns (markdown report, list of follow-up SPL query strings).
    """
    if config.AGENT_BACKEND == "ollama":
        return asyncio.run(_analyse_nooa(findings))

    from standalone.agent_cli_bridge import analyse_via_cli_bridge

    return analyse_via_cli_bridge(findings)
