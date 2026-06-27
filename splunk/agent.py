"""
LangGraph ReAct agent — Qwen2.5 32B via Ollama.
Receives structured findings from detectors, reasons over them, emits markdown report.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict

from dotenv import load_dotenv

from splunk.config import AGENT_MAX_ITER as MAX_ITERATIONS, LLM_MODEL as MODEL, SPLUNK_INDEX

load_dotenv()

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior security engineer specialising in PKI and certificate infrastructure.
You are given structured findings extracted from Splunk logs — spikes, error patterns, cert anomalies, host rankings, and event correlations.

Your job:
1. Reason over the findings to identify the most likely root cause.
2. Reference specific timestamps, hosts, error codes, and sourcetypes from the data.
3. Form a root cause hypothesis and assign a confidence level (High / Medium / Low).
4. Suggest the next 2–3 investigation steps an analyst should take.
5. Emit your final answer as a structured markdown report using format_report.

Think step by step. Use tools to organise your reasoning before calling format_report.
Do not hallucinate field values — only reference data present in the findings."""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class LogAnalysisState(TypedDict):
    messages: Annotated[list, add_messages]
    findings: dict[str, Any]
    report: str
    followup_queries: list[str]
    iterations: int


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def summarise_findings(findings_json: str) -> str:
    """Produce a concise bullet-point summary of the structured findings."""
    try:
        f = json.loads(findings_json)
    except json.JSONDecodeError:
        return "Invalid findings JSON."

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


@tool
def rank_hypotheses(hypotheses_json: str) -> str:
    """
    Given a JSON list of hypothesis strings, rank them by likelihood based
    on the findings and return an ordered list with reasoning.
    This tool is a reasoning scaffold — return the input ranked with brief justification.
    """
    try:
        hypotheses = json.loads(hypotheses_json)
    except json.JSONDecodeError:
        return "Invalid JSON list of hypotheses."
    if not isinstance(hypotheses, list):
        return "Expected a JSON array of hypothesis strings."
    ranked = "\n".join(f"{i+1}. {h}" for i, h in enumerate(hypotheses))
    return f"Hypotheses to evaluate (rank these by evidence strength):\n{ranked}"


@tool
def request_deeper_analysis(area: str) -> str:
    """
    Signal that a specific area needs deeper investigation.
    Returns a prompt for the agent to focus its next reasoning step.
    area: one of 'cert_chain', 'ocsp', 'crl', 'tls_handshake', 'host_isolation', 'timeline'
    """
    prompts = {
        "cert_chain": "Focus on chain validation errors — look for patterns across hosts and timestamps.",
        "ocsp": "Examine OCSP timeout/failure patterns — check if failures are clustered by time or host.",
        "crl": "Review CRL distribution point failures — may indicate network connectivity to CA.",
        "tls_handshake": "Analyse TLS handshake failures — correlate with cert expiry or cipher mismatch.",
        "host_isolation": "Determine if errors are isolated to specific hosts or widespread — check host_ranking.",
        "timeline": "Build a precise timeline of first occurrence vs escalation — use correlations data.",
    }
    return prompts.get(area, f"Investigate '{area}' in detail using the available findings.")


@tool
def format_report(
    summary: str,
    root_cause: str,
    confidence: str,
    affected_hosts: str,
    timeline: str,
    next_steps: str,
) -> str:
    """
    Emit the final markdown investigation report.
    Call this once you have reached a conclusion.
    confidence: 'High', 'Medium', or 'Low'
    affected_hosts: comma-separated list
    next_steps: newline-separated list of 2-3 actions
    """
    steps = "\n".join(f"- {s.strip()}" for s in next_steps.strip().splitlines() if s.strip())
    return f"""# Splunk Investigation Report

## Summary
{summary}

## Root Cause Hypothesis
{root_cause}

**Confidence:** {confidence}

## Affected Hosts
{affected_hosts}

## Timeline
{timeline}

## Recommended Next Steps
{steps}
"""


_SPL_TEMPLATES: dict[str, str] = {
    "host_isolation": (
        "index={index} host IN ({hosts}) earliest={spike_start} latest=+2h"
        " | stats count by host, sourcetype, error_code | sort -count"
    ),
    "ocsp": (
        "index=network dest_port=80 OR dest_port=2560 src IN ({hosts})"
        " earliest={spike_start_minus5m} latest={spike_start_plus30m}"
        " | timechart count by src"
    ),
    "crl": (
        "index=network dest_port=80 src IN ({hosts})"
        " earliest={spike_start} latest=+1h"
        " | search url=*crl* OR url=*revocation*"
        " | stats count by src, dest, url"
    ),
    "timeline": (
        "index={index} sourcetype={sourcetype} ({error_filter})"
        " | timechart span=1m count by host"
    ),
    "first_occurrence": (
        "index={index} sourcetype={sourcetype} ({error_filter})"
        " | sort _time | head 1 | table _time, host, sourcetype, message"
    ),
}


@tool
def generate_followup_queries(
    hosts: str,
    error_codes: str,
    sourcetype: str,
    spike_start: str,
    areas: str,
) -> str:
    """
    Generate follow-up SPL queries for the next investigation iteration.
    hosts: comma-separated host names from findings
    error_codes: comma-separated error codes from findings
    sourcetype: primary sourcetype from findings
    spike_start: ISO timestamp of the first spike
    areas: comma-separated subset of: host_isolation, ocsp, crl, timeline, first_occurrence
    """
    from datetime import datetime, timedelta, timezone

    host_list = ", ".join(f'"{h.strip()}"' for h in hosts.split(",") if h.strip())
    error_filter = " OR ".join(f'error_code="{e.strip()}"' for e in error_codes.split(",") if e.strip()) or "*"

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

    queries = []
    for area in (a.strip() for a in areas.split(",") if a.strip()):
        tmpl = _SPL_TEMPLATES.get(area)
        if tmpl:
            queries.append(f"-- {area}\n{tmpl.format(**slots)}")
        else:
            logger.warning("Unknown area '%s' requested in generate_followup_queries", area)

    return "\n\n".join(queries) if queries else "No queries generated — check area names."


TOOLS = [summarise_findings, rank_hypotheses, request_deeper_analysis, format_report, generate_followup_queries]


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def _check_ollama_model(model: str) -> None:
    """Fail fast if Ollama is not running or model is not pulled."""
    import httpx
    logger.info("Checking Ollama for model '%s'", model)
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=5)
        resp.raise_for_status()
        names = [m["name"] for m in resp.json().get("models", [])]
        base = model.split(":")[0]
        if not any(base in n for n in names):
            logger.error("Model '%s' not found. Available: %s", model, names)
            raise RuntimeError(
                f"Model '{model}' not found in Ollama. Run: ollama pull {model}\n"
                f"Available: {names}"
            )
        logger.info("Model '%s' confirmed available in Ollama", model)
    except httpx.ConnectError:
        logger.error("Ollama not reachable at localhost:11434")
        raise RuntimeError("Ollama is not running. Start it with: ollama serve")


def agent_node(state: LogAnalysisState) -> dict:
    iteration = state.get("iterations", 0) + 1
    logger.debug("Agent iteration %d/%d", iteration, MAX_ITERATIONS)
    llm = ChatOllama(model=MODEL, temperature=0).bind_tools(TOOLS)
    response = llm.invoke(state["messages"])
    tool_calls = getattr(response, "tool_calls", [])
    logger.debug("Iteration %d — tool_calls: %s", iteration, [t["name"] for t in tool_calls])
    return {
        "messages": [response],
        "iterations": iteration,
    }


def tool_node_fn(state: LogAnalysisState) -> dict:
    node = ToolNode(TOOLS)
    result = node.invoke(state)
    report = state.get("report", "")
    followup_queries = state.get("followup_queries", [])
    for msg in result.get("messages", []):
        if not hasattr(msg, "name"):
            continue
        if msg.name == "format_report" and msg.content:
            logger.info("format_report called — report captured (%d chars)", len(msg.content))
            report = msg.content
        elif msg.name == "generate_followup_queries" and msg.content:
            new_queries = [q.strip() for q in msg.content.split("\n\n") if q.strip()]
            followup_queries = followup_queries + new_queries
            logger.info("generate_followup_queries called — %d queries captured", len(new_queries))
        else:
            logger.debug("Tool executed: %s", msg.name)
    return {**result, "report": report, "followup_queries": followup_queries}


def should_continue(state: LogAnalysisState) -> str:
    last = state["messages"][-1]
    iterations = state.get("iterations", 0)
    if iterations >= MAX_ITERATIONS:
        logger.warning("ReAct loop hit max iterations (%d) — forcing END.", MAX_ITERATIONS)
        return END
    if isinstance(last, AIMessage) and last.tool_calls:
        logger.debug("Continuing loop — %d tool call(s) requested", len(last.tool_calls))
        return "tools"
    logger.info("Agent reached conclusion after %d iteration(s)", iterations)
    return END


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def _build_graph() -> Any:
    g = StateGraph(LogAnalysisState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tool_node_fn)
    g.set_entry_point("agent")
    g.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")
    return g.compile()


_graph = None


def _get_graph() -> Any:
    global _graph
    if _graph is None:
        _check_ollama_model(MODEL)
        _graph = _build_graph()
    return _graph


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyse(findings: dict[str, Any]) -> tuple[str, list[str]]:
    """
    Run the ReAct agent over structured findings from detectors.
    Returns (markdown report, list of follow-up SPL query strings).
    """
    logger.info(
        "Starting agent analysis — model=%s max_iter=%d event_count=%d",
        MODEL, MAX_ITERATIONS, findings.get("event_count", 0),
    )
    graph = _get_graph()
    findings_str = json.dumps(findings, default=str, indent=2)

    initial_state: LogAnalysisState = {
        "messages": [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=f"Analyse these Splunk findings and produce an investigation report:\n\n```json\n{findings_str}\n```"),
        ],
        "findings": findings,
        "report": "",
        "followup_queries": [],
        "iterations": 0,
    }

    final_state = graph.invoke(initial_state)
    total_iterations = final_state.get("iterations", 0)
    queries = final_state.get("followup_queries", [])

    if final_state.get("report"):
        logger.info("Analysis complete — report from format_report (%d chars, %d iterations)", len(final_state["report"]), total_iterations)
        return final_state["report"], queries

    for msg in reversed(final_state["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            logger.warning("format_report not called — returning last AI message (%d iterations)", total_iterations)
            return str(msg.content), queries

    logger.error("Agent produced no output after %d iterations", total_iterations)
    return "No report generated.", queries
