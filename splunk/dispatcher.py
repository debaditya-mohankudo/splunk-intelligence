"""dispatcher — advisory, non-blocking nudges attached to MCP tool calls.

Mirrors task-framework's taskfw/dispatcher.py::tool_called: a self-contained
pre/post hook around one tool call, with no dependency on an external hook
process. splunk_analysis previously documented (in SKILL.md) a dependency on
claude-hooks' PostToolUse hook to inject "next findings" after
splunk__submit_report — but connector.py's start_investigation/submit_report
already return everything the agent needs (status, findings, next) in the
tool's own JSON result, so no external hook was ever load-bearing for the
loop to continue. This module exists for a distinct, real purpose: three
advisory nudges (repo_path omitted, confidence never reached High, no
follow-up queries submitted) that previously had nowhere in-repo to live.

A nudge is advisory because it never blocks or changes the tool's result
beyond adding one extra key — it only makes a true, otherwise-easy-to-miss
fact about the result visible to the calling agent.
"""
from __future__ import annotations

from typing import Any, Callable


def repo_path_nudge(repo_path: str) -> str | None:
    """Advisory nudge for investigate_start, or None when there's nothing to say.

    Fires when repo_path was omitted — the agent otherwise has no signal that
    splunk__lsp_call_chain (code cross-referencing) is unavailable this run.
    """
    if repo_path:
        return None
    return (
        "No repo_path given — splunk__lsp_call_chain (code cross-referencing) "
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


def apply_repo_path_nudge(result: dict[str, Any], repo_path: str) -> None:
    nudge = repo_path_nudge(repo_path)
    if nudge:
        result["repo_path_nudge"] = nudge


def apply_confidence_nudge(result: dict[str, Any], status: str, confidence: str) -> None:
    nudge = confidence_nudge(status, confidence)
    if nudge:
        result["confidence_nudge"] = nudge


def apply_no_followup_nudge(result: dict[str, Any], status: str, queries: list[str]) -> None:
    nudge = no_followup_nudge(status, queries)
    if nudge:
        result["followup_nudge"] = nudge


class tool_called:
    """Pre/post hook around one MCP tool call.

    Usage:

        with dispatcher.tool_called(post=lambda r: ...) as call:
            call.result = {...}
            return call.result

    `pre` runs on entry. `post` runs on exit, but only when the block raised
    nothing and `call.result` is a dict with no "error" key — a refusal
    ({"error": ...}) is never nudged. Mutating `call.result` in `post` is
    visible in what the function actually returns, the same way as
    taskfw/dispatcher.py::tool_called.
    """

    def __init__(
        self,
        pre: Callable[[], None] | None = None,
        post: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.pre = pre
        self.post = post
        self.result: dict[str, Any] = {}

    def __enter__(self) -> "tool_called":
        if self.pre:
            self.pre()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None and self.post and "error" not in self.result:
            self.post(self.result)
