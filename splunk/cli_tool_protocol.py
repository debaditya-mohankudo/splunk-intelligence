"""
Text-bridge tool-calling protocol for CLI-backed chat backends
(splunk/llm_backends.py's ClaudeCLIBackend/CopilotCLIBackend).

Both shell out to a coding-agent CLI with its own native tool use disabled and
need the same hand-rolled {"tool": ..., "args": {...}} / {"final_answer": ...}
JSON convention so agent.py's LangGraph ReAct loop can drive tool calls
instead of the CLI's own. Ported (and trimmed — no multi-role session
forking, single agent role here) from bee-bug-hunter's
bee_bug_hunter/cli_tool_protocol.py, which uses this same protocol to bridge
`claude -p`/`copilot -p` into BeeAI's agent loop.
"""

from __future__ import annotations

import json
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

CODE_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

TOOL_PROTOCOL_INSTRUCTIONS = """
You have access to tools. To call one, respond with ONLY this JSON (no other text):
{{"tool": "<tool_name>", "args": {{...}}}}

When you have the final answer and don't need any more tools, respond with ONLY this JSON:
{{"final_answer": "<your answer>"}}

Available tools:
{tool_descriptions}
""".strip()


def find_balanced_json_objects(text: str) -> list[str]:
    """Scans for every top-level {...} span via brace-depth counting
    (string/escape aware) — a greedy first-'{'-to-last-'}' regex would swallow
    the whole response into one unparseable blob whenever surrounding prose
    happens to quote braces of its own."""
    spans = []
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    spans.append(text[start:i + 1])
    return spans


def extract_json_object(text: str) -> dict | None:
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fence_match = CODE_FENCE_PATTERN.search(text)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # Several brace spans can be in play at once (the real tool-call JSON plus
    # prose that happens to quote its own valid-JSON braces) — require the
    # object to look like our protocol before accepting it; only fall back to
    # "first parseable" if nothing matches the protocol shape.
    parsed_candidates = []
    for candidate in find_balanced_json_objects(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and ("tool" in parsed or "final_answer" in parsed):
            return parsed
        parsed_candidates.append(parsed)
    if parsed_candidates:
        return parsed_candidates[0]
    return None


def describe_tools(tools: list) -> str:
    if not tools:
        return "(none)"
    lines = []
    for t in tools:
        try:
            schema = json.dumps(t.args)
        except Exception:
            schema = "{}"
        lines.append(f"- {t.name}: {t.description}\n  parameters schema: {schema}")
    return "\n".join(lines)


def _render_message(m) -> tuple[str, str]:
    if isinstance(m, SystemMessage):
        return "system", m.content
    if isinstance(m, ToolMessage):
        return "tool", f"Tool '{getattr(m, 'name', '')}' result:\n{m.content}"
    if isinstance(m, AIMessage):
        if getattr(m, "tool_calls", None):
            chunks = [
                json.dumps({"tool": c["name"], "args": c.get("args", {})})
                for c in m.tool_calls
            ]
            return "assistant", "\n".join(chunks)
        return "assistant", m.content
    if isinstance(m, HumanMessage):
        return "user", m.content
    return "user", str(getattr(m, "content", m))


def flatten_messages(messages: list) -> tuple[str, str]:
    """Returns (system_prompt, conversation_prompt) — CLI backends here take
    one system string and one user string per call, so the given message
    history is flattened into role-prefixed text."""
    system_parts = []
    convo_parts = []
    for m in messages:
        role, text = _render_message(m)
        if role == "system":
            system_parts.append(text)
        else:
            convo_parts.append(f"{role.upper()}: {text}")
    return "\n\n".join(system_parts), "\n\n".join(convo_parts)


def message_signature(m) -> str:
    """Content-based identity for a message (role + rendered text) — used by
    select_new_messages to dedupe which messages have already been sent to a
    CLI session, instead of a position-based cursor (robust to LangGraph
    occasionally handing back a message list that isn't a strict superset of
    the previous call's, e.g. after any future state-trimming)."""
    role, text = _render_message(m)
    return f"{role}:{text}"


def select_new_messages(messages: list, already_sent: set[str]) -> list:
    """Returns the subset of `messages` whose content signature isn't already
    in `already_sent`. Pure — callers own committing signatures to
    `already_sent` only after the CLI call those messages were flattened into
    actually succeeds, so a failed call doesn't wrongly mark them delivered."""
    return [m for m in messages if message_signature(m) not in already_sent]
