"""
Data-driven registry of investigation "areas" used by splunk/agent.py's ReAct tools.

Each area is a single entry with everything needed for both `request_deeper_analysis`
(a reasoning prompt) and `generate_followup_queries` (an SPL template). New domains are
added here, not by editing agent.py's tool bodies.

`spl_template` is optional — an area can be prompt-only (nothing to query for yet) or
query-only (no dedicated reasoning nudge beyond the generic default).
"""

from __future__ import annotations

from typing import TypedDict


class InvestigationArea(TypedDict, total=False):
    prompt: str
    spl_template: str


INVESTIGATION_AREAS: dict[str, InvestigationArea] = {
    "api_errors": {
        "prompt": "Examine 4xx/5xx HTTP error spikes — check if failures cluster by endpoint, status code, or host.",
        "spl_template": (
            "index={index} sourcetype={sourcetype} host IN ({hosts}) status>=400"
            " earliest={spike_start} latest=+1h"
            " | stats count by host, status, uri | sort -count"
        ),
    },
    "database_slowdown": {
        "prompt": "Analyse slow queries and elevated latency — correlate with specific hosts, endpoints, or time windows.",
        "spl_template": (
            "index={index} sourcetype={sourcetype} host IN ({hosts})"
            " earliest={spike_start} latest=+1h"
            " | where duration_ms > 1000"
            " | stats avg(duration_ms) as avg_ms, max(duration_ms) as max_ms, count by host"
            " | sort -max_ms"
        ),
    },
    "cascading_failure": {
        "prompt": "Look for an entity-keyed chain of failures (e.g. one service's error preceding another's on the same host) — build a dependency timeline of what failed first.",
        "spl_template": (
            "index={index} host IN ({hosts})"
            " earliest={spike_start_minus5m} latest={spike_start_plus30m}"
            " | transaction host maxspan=1h"
            " | table _time, host, duration, eventcount"
        ),
    },
    "host_isolation": {
        "prompt": "Determine if errors are isolated to specific hosts or widespread — check host_ranking.",
        "spl_template": (
            "index={index} host IN ({hosts}) earliest={spike_start} latest=+2h"
            " | stats count by host, sourcetype, error_code | sort -count"
        ),
    },
    "timeline": {
        "prompt": "Build a precise timeline of first occurrence vs escalation — use correlations data.",
        "spl_template": (
            "index={index} sourcetype={sourcetype} ({error_filter})"
            " | timechart span=1m count by host"
        ),
    },
    "first_occurrence": {
        "spl_template": (
            "index={index} sourcetype={sourcetype} ({error_filter})"
            " | sort _time | head 1 | table _time, host, sourcetype, message"
        ),
    },
}


def get_prompt(area: str) -> str:
    entry = INVESTIGATION_AREAS.get(area)
    if entry and entry.get("prompt"):
        return entry["prompt"]
    return f"Investigate '{area}' in detail using the available findings."


def get_spl_template(area: str) -> str | None:
    entry = INVESTIGATION_AREAS.get(area)
    return entry.get("spl_template") if entry else None
