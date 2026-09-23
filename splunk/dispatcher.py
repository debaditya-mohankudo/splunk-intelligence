"""dispatcher — advisory, non-blocking nudges attached to MCP tool results.

Three pure functions (repo_path omitted, confidence never reached High, no
follow-up queries submitted). connector.py calls them on its success paths
and adds the returned text to the result under one extra key; an
{"error": ...} result is never nudged.

A nudge is advisory because it never blocks or changes the tool's result
beyond adding one extra key — it only makes a true, otherwise-easy-to-miss
fact about the result visible to the calling agent.
"""
from __future__ import annotations


def repo_path_nudge(repo_path: str) -> str | None:
    """Advisory nudge for investigate_start, or None when there's nothing to say.

    Fires when repo_path was omitted — the agent otherwise has no signal that
    splunk__find_symbol_refs (code cross-referencing) is unavailable this run.
    """
    if repo_path:
        return None
    return (
        "No repo_path given — splunk__find_symbol_refs (code cross-referencing) "
        "is unavailable for this run. Call splunk__investigate_start again "
        "with repo_path if you need to trace error log sites through the call graph."
    )


def confidence_nudge(status: str, confidence: str) -> str | None:
    """Advisory nudge for submit_report, or None when there's nothing to say.

    Fires only once the run is done and confidence never reached High —
    flags that the final summary should note the remaining uncertainty.
    """
    if status != "done" or confidence == "High":
        return None
    return (
        f"Investigation ended with confidence={confidence}, not High. "
        "Note the remaining uncertainty in your final summary to the user."
    )


def no_followup_nudge(status: str, queries: list[str]) -> str | None:
    """Advisory nudge for submit_report, or None when there's nothing to say.

    Fires when the run ended specifically because no follow-up queries were
    submitted (connector.py's `not queries` done-condition) — distinct from
    ending on high confidence or hitting the iteration cap.
    """
    if status != "done" or queries:
        return None
    return (
        "No follow-up queries were submitted, ending the investigation early. "
        "If deeper SPL follow-up (e.g. host_isolation, timeline) would have "
        "sharpened the root cause, consider running another investigation."
    )
