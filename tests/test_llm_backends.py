"""
Unit tests for splunk/llm_backends.py — the pluggable chat-backend seam for
splunk/agent.py's ReAct loop. No network calls: OllamaBackend.check_available's
httpx call is mocked; the CLI backends are stubs and raise before touching a
subprocess.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from splunk.llm_backends import OllamaBackend, _UnimplementedCLIBackend, get_backend


class TestGetBackend:
    def test_returns_ollama_backend(self):
        backend = get_backend("ollama", "qwen2.5:14b")
        assert isinstance(backend, OllamaBackend)
        assert backend.model == "qwen2.5:14b"

    def test_returns_claude_cli_stub(self):
        backend = get_backend("claude_cli", "unused")
        assert isinstance(backend, _UnimplementedCLIBackend)

    def test_returns_copilot_cli_stub(self):
        backend = get_backend("copilot_cli", "unused")
        assert isinstance(backend, _UnimplementedCLIBackend)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown SPLUNK_AGENT_BACKEND"):
            get_backend("nonexistent", "unused")


class TestUnimplementedCLIBackends:
    def test_check_available_raises_not_implemented(self):
        backend = get_backend("claude_cli", "unused")
        with pytest.raises(NotImplementedError, match="not implemented"):
            backend.check_available()

    def test_bind_tools_raises_not_implemented(self):
        backend = get_backend("copilot_cli", "unused")
        with pytest.raises(NotImplementedError, match="not implemented"):
            backend.bind_tools([])


class TestOllamaBackend:
    def test_check_available_passes_when_model_present(self):
        backend = OllamaBackend("qwen2.5:14b")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"models": [{"name": "qwen2.5:14b"}]}
        with patch("httpx.get", return_value=fake_resp):
            backend.check_available()  # no raise

    def test_check_available_raises_when_model_missing(self):
        backend = OllamaBackend("qwen2.5:14b")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"models": [{"name": "llama3:8b"}]}
        with patch("httpx.get", return_value=fake_resp):
            with pytest.raises(RuntimeError, match="not found in Ollama"):
                backend.check_available()

    def test_check_available_raises_when_ollama_not_running(self):
        import httpx as httpx_mod

        backend = OllamaBackend("qwen2.5:14b")
        with patch("httpx.get", side_effect=httpx_mod.ConnectError("refused")):
            with pytest.raises(RuntimeError, match="Ollama is not running"):
                backend.check_available()

    def test_bind_tools_returns_bound_chat_model(self):
        backend = OllamaBackend("qwen2.5:14b")
        with patch("langchain_ollama.ChatOllama") as MockChatOllama:
            mock_instance = MagicMock()
            MockChatOllama.return_value = mock_instance
            result = backend.bind_tools(["tool1", "tool2"])
            MockChatOllama.assert_called_once_with(model="qwen2.5:14b", temperature=0)
            mock_instance.bind_tools.assert_called_once_with(["tool1", "tool2"])
            assert result == mock_instance.bind_tools.return_value
