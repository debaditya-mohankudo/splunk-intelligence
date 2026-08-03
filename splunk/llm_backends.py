"""
Pluggable LLM backend for splunk/agent.py's standalone ReAct loop.

Selected via SPLUNK_AGENT_BACKEND (config.AGENT_BACKEND) — "ollama" is the only
implemented backend today; "claude_cli" and "copilot_cli" are registered as named
seams so a future task can wire them in without touching agent.py or the graph.

Only "ollama" supports the LangChain tool-calling handshake (bind_tools/invoke
returning an AIMessage with .tool_calls) that agent_node's LangGraph ReAct loop
relies on — Claude CLI and Copilot CLI are themselves agentic tools invoked
non-interactively (e.g. `claude -p "..."`) and don't emit structured tool calls
the same way, so they'll need a different call shape (likely single-shot:
findings in, finished report out) rather than driving the same graph. See
task a65820ae's grooming/decisions for why this was scoped to "ollama first."
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class LLMBackend(ABC):
    """A chat model capable of driving agent.py's LangGraph ReAct loop."""

    @abstractmethod
    def check_available(self) -> None:
        """Fail fast with a clear message if the backend can't be used right now."""

    @abstractmethod
    def bind_tools(self, tools: list) -> Any:
        """Return a LangChain-compatible runnable: .invoke(messages) -> AIMessage."""


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


class _UnimplementedCLIBackend(LLMBackend):
    """Named seam for a CLI-driven backend — not yet wired up.

    Claude CLI / Copilot CLI are agentic on their own and don't expose the
    bind_tools/invoke tool-calling handshake ChatOllama does; integrating them
    means giving agent.py a single-shot call path (findings -> finished report)
    rather than routing them through the existing ReAct graph. That's future work.
    """

    def __init__(self, name: str, cli_command: str):
        self.name = name
        self.cli_command = cli_command

    def check_available(self) -> None:
        raise NotImplementedError(
            f"Backend '{self.name}' is not implemented yet — only 'ollama' is wired up. "
            f"(Would shell out to `{self.cli_command}` non-interactively.)"
        )

    def bind_tools(self, tools: list) -> Any:
        raise NotImplementedError(f"Backend '{self.name}' is not implemented yet.")


_BACKENDS = {
    "ollama": lambda model: OllamaBackend(model),
    "claude_cli": lambda model: _UnimplementedCLIBackend("claude_cli", "claude"),
    "copilot_cli": lambda model: _UnimplementedCLIBackend("copilot_cli", "copilot"),
}


def get_backend(name: str, model: str) -> LLMBackend:
    factory = _BACKENDS.get(name)
    if factory is None:
        raise ValueError(f"Unknown SPLUNK_AGENT_BACKEND '{name}'. Choices: {sorted(_BACKENDS)}")
    return factory(model)
