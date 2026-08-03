"""
Pluggable LLM backend for splunk/agent.py's standalone ReAct loop.

Selected via SPLUNK_AGENT_BACKEND (config.AGENT_BACKEND): "ollama" (default),
"claude_cli", or "copilot_cli".

Ollama plugs into LangGraph's native tool-calling (ChatOllama.bind_tools/invoke
returns an AIMessage with .tool_calls). Claude CLI / Copilot CLI are themselves
agentic tools invoked non-interactively (`claude -p` / `copilot -p`) and don't
expose that handshake, so ClaudeCLIBackend/CopilotCLIBackend bridge tool-calling
by hand: tool schemas go in the system prompt, the model is asked to reply with
a small JSON protocol ({"tool": ..., "args": {...}} or {"final_answer": ...}),
and the reply is translated into a LangChain-shaped AIMessage so agent.py's
graph doesn't need to know the difference. Ported from bee-bug-hunter's
bee_bug_hunter/{cli_tool_protocol,claude_cli_llm,copilot_cli_llm}.py, trimmed
down: no multi-role session forking/persistence-to-disk (agent.py has a single
role and analyse() is one-shot per investigation) — just one CLI session,
minted on first call and resumed via --resume for the rest of that
analyse() invocation. See agent.py's _build_graph/_make_agent_node: the bound
object returned by bind_tools() is constructed once per analyse() call and
reused across every ReAct iteration, which is what makes that session reuse
possible.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from abc import ABC, abstractmethod
from typing import Any

from langchain_core.messages import AIMessage

from splunk.cli_tool_protocol import (
    TOOL_PROTOCOL_INSTRUCTIONS,
    describe_tools,
    extract_json_object,
    flatten_messages,
    message_signature,
    select_new_messages,
)

logger = logging.getLogger(__name__)

CLI_TIMEOUT_SECONDS = 240


class LLMBackend(ABC):
    """A chat model capable of driving agent.py's LangGraph ReAct loop."""

    @abstractmethod
    def check_available(self) -> None:
        """Fail fast with a clear message if the backend can't be used right now."""

    @abstractmethod
    def bind_tools(self, tools: list) -> Any:
        """Return an object with .invoke(messages) -> AIMessage, bound to `tools`.

        Called once per analyse() invocation and reused across every ReAct
        iteration — CLI backends rely on this to keep one session alive for the
        whole investigation rather than starting cold every turn.
        """


class OllamaBackend(LLMBackend):
    def __init__(self, model: str):
        self.model = model

    def check_available(self) -> None:
        import httpx

        logger.info("Checking Ollama for model '%s'", self.model)
        try:
            resp = httpx.get("http://localhost:11434/api/tags", timeout=5)
            resp.raise_for_status()
            names = [m["name"] for m in resp.json().get("models", [])]
            base = self.model.split(":")[0]
            if not any(base in n for n in names):
                logger.error("Model '%s' not found. Available: %s", self.model, names)
                raise RuntimeError(
                    f"Model '{self.model}' not found in Ollama. Run: ollama pull {self.model}\n"
                    f"Available: {names}"
                )
            logger.info("Model '%s' confirmed available in Ollama", self.model)
        except httpx.ConnectError:
            logger.error("Ollama not reachable at localhost:11434")
            raise RuntimeError("Ollama is not running. Start it with: ollama serve")

    def bind_tools(self, tools: list) -> Any:
        from langchain_ollama import ChatOllama

        return ChatOllama(model=self.model, temperature=0).bind_tools(tools)


class _CLIToolCallingModel:
    """Stateful .invoke(messages) -> AIMessage bound to one CLI session.

    Constructed once per analyse() call (see OllamaBackend's docstring analog
    in ClaudeCLIBackend/CopilotCLIBackend.bind_tools) and reused across every
    ReAct iteration, so `self._session_id` and `self._sent_message_signatures`
    persist for the life of one investigation. `invoke_fn` is
    (system_prompt, user_prompt, session_id | None) -> (result_text, session_id).
    """

    def __init__(self, invoke_fn, tools: list):
        self._invoke_fn = invoke_fn
        self._tools = tools
        self._session_id: str | None = None
        self._sent_message_signatures: set[str] = set()

    def invoke(self, messages: list) -> AIMessage:
        # Only flatten what's new since the last call — the CLI session already
        # holds everything sent on prior turns; re-sending the full history each
        # time both wastes tokens and defeats the CLI's own prompt-cache reuse.
        new_messages = select_new_messages(messages, self._sent_message_signatures)
        system_prompt, prompt = flatten_messages(new_messages)
        if self._tools:
            system_prompt = (
                system_prompt + "\n\n" +
                TOOL_PROTOCOL_INSTRUCTIONS.format(tool_descriptions=describe_tools(self._tools))
            )

        raw, returned_session_id = self._invoke_fn(system_prompt, prompt, self._session_id)
        self._session_id = returned_session_id
        # Only commit as "sent" once the call actually succeeded, so a failed
        # call doesn't wrongly mark these messages delivered.
        self._sent_message_signatures.update(message_signature(m) for m in new_messages)

        parsed = extract_json_object(raw)
        if parsed and "tool" in parsed:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": parsed["tool"],
                    "args": parsed.get("args", {}),
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "tool_call",
                }],
            )
        text = parsed["final_answer"] if parsed and "final_answer" in parsed else raw
        if not isinstance(text, str):
            text = json.dumps(text)
        return AIMessage(content=text)


class ClaudeCLIBackend(LLMBackend):
    """Shells to `claude -p --tools none` — reuses the caller's existing Claude
    Code login, no ANTHROPIC_API_KEY needed."""

    def __init__(self, model: str = "sonnet"):
        self.model = model

    def check_available(self) -> None:
        if not shutil.which("claude"):
            raise RuntimeError(
                "claude_cli backend: `claude` binary not found on PATH. "
                "Install Claude Code, or set SPLUNK_AGENT_BACKEND=ollama."
            )

    def bind_tools(self, tools: list) -> Any:
        return _CLIToolCallingModel(self._invoke, tools)

    def _invoke(self, system_prompt: str, user_prompt: str, session_id: str | None) -> tuple[str, str]:
        claude_path = shutil.which("claude")
        if not claude_path:
            raise RuntimeError("claude_cli backend: `claude` binary not found on PATH")

        cmd = [claude_path, "-p", "--output-format", "json", "--tools", "none", "--model", self.model]
        if session_id is not None:
            cmd += ["--resume", session_id]
        else:
            session_id = str(uuid.uuid4())
            cmd += ["--session-id", session_id]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]

        proc = subprocess.run(
            cmd, input=user_prompt, capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            # API-level failures (rate limits, auth) land in stdout's JSON `result`
            # field with is_error=true and empty stderr — surface whichever has content.
            detail = proc.stderr.strip()
            if not detail:
                try:
                    detail = json.loads(proc.stdout).get("result", proc.stdout[:500])
                except json.JSONDecodeError:
                    detail = proc.stdout[:500]
            raise RuntimeError(f"claude CLI exited {proc.returncode}: {detail}")

        data = json.loads(proc.stdout)
        if data.get("is_error"):
            raise RuntimeError(f"claude CLI reported an error: {data.get('result')}")
        return data["result"], data.get("session_id", session_id)


# Every built-in tool `copilot -p` currently offers, excluded so the CLI can never
# actually execute anything on this host — only agent.py's own ReAct loop, via the
# hand-rolled JSON protocol above, should be running tools. `--available-tools=`
# (empty) alone does not disable them; this plus --disable-builtin-mcps does.
_COPILOT_EXCLUDED_TOOLS = (
    "bash,read_bash,stop_bash,list_bash,view,create,edit,web_fetch,"
    "fetch_copilot_cli_documentation,skill,sql,session_store_sql,read_agent,"
    "list_agents,grep,glob,task,web_search"
)


class CopilotCLIBackend(LLMBackend):
    """Shells to `copilot -p` with every built-in tool excluded — reuses the
    caller's existing `copilot` CLI login, no API key needed."""

    def __init__(self, model: str = "claude-sonnet-4.5"):
        self.model = model

    def check_available(self) -> None:
        if not shutil.which("copilot"):
            raise RuntimeError(
                "copilot_cli backend: `copilot` binary not found on PATH. "
                "Install GitHub Copilot CLI, or set SPLUNK_AGENT_BACKEND=ollama."
            )

    def bind_tools(self, tools: list) -> Any:
        return _CLIToolCallingModel(self._invoke, tools)

    def _invoke(self, system_prompt: str, user_prompt: str, session_id: str | None) -> tuple[str, str]:
        copilot_path = shutil.which("copilot")
        if not copilot_path:
            raise RuntimeError("copilot_cli backend: `copilot` binary not found on PATH")

        # No --append-system-prompt equivalent on this CLI — system and user text
        # are folded into one combined -p argument instead of split across stdin.
        combined_prompt = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt
        cmd = [
            copilot_path, "-p", combined_prompt,
            "--model", self.model,
            "--output-format", "json",
            "--excluded-tools", _COPILOT_EXCLUDED_TOOLS,
            "--disable-builtin-mcps",
        ]
        if session_id is not None:
            cmd += ["--resume", session_id]
        else:
            session_id = str(uuid.uuid4())
            cmd += ["--session-id", session_id]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout[:500]
            raise RuntimeError(f"copilot CLI exited {proc.returncode}: {detail}")

        result_text = None
        returned_session_id = session_id
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "assistant.message":
                # Later events overwrite earlier ones — the last assistant.message
                # in the stream is the model's actual final reply for this call.
                result_text = event["data"]["content"]
            elif event.get("type") == "result":
                returned_session_id = event.get("sessionId", returned_session_id)

        if result_text is None:
            raise RuntimeError(f"copilot CLI produced no assistant.message event: {proc.stdout[:500]}")
        return result_text, returned_session_id


_BACKENDS = {
    "ollama": OllamaBackend,
    "claude_cli": ClaudeCLIBackend,
    "copilot_cli": CopilotCLIBackend,
}


def get_backend(name: str, model: str) -> LLMBackend:
    cls = _BACKENDS.get(name)
    if cls is None:
        raise ValueError(f"Unknown SPLUNK_AGENT_BACKEND '{name}'. Choices: {sorted(_BACKENDS)}")
    return cls(model)
