"""
Unit tests for splunk/llm_backends.py — the pluggable chat-backend seam for
standalone/agent.py's ReAct loop. No network calls, no real subprocess: httpx and
subprocess.run are mocked; shutil.which is mocked so check_available/_invoke
don't depend on `claude`/`copilot` actually being installed on the test host.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from splunk.llm_backends import (
    ClaudeCLIBackend,
    CopilotCLIBackend,
    OllamaBackend,
    _CLIToolCallingModel,
    get_backend,
)


class TestGetBackend:
    def test_returns_ollama_backend(self):
        backend = get_backend("ollama", "qwen2.5:14b")
        assert isinstance(backend, OllamaBackend)
        assert backend.model == "qwen2.5:14b"

    def test_returns_claude_cli_backend(self):
        backend = get_backend("claude_cli", "sonnet")
        assert isinstance(backend, ClaudeCLIBackend)
        assert backend.model == "sonnet"

    def test_returns_copilot_cli_backend(self):
        backend = get_backend("copilot_cli", "claude-sonnet-4.5")
        assert isinstance(backend, CopilotCLIBackend)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown SPLUNK_AGENT_BACKEND"):
            get_backend("nonexistent", "unused")


class TestOllamaBackend:
    def test_check_available_passes_when_model_present(self):
        backend = OllamaBackend("qwen2.5:14b")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"models": [{"name": "qwen2.5:14b"}]}
        with patch("httpx.get", return_value=fake_resp):
            backend.check_available()

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


class TestClaudeCLIBackend:
    def test_check_available_raises_when_binary_missing(self):
        backend = ClaudeCLIBackend()
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="`claude` binary not found"):
                backend.check_available()

    def test_check_available_passes_when_binary_present(self):
        backend = ClaudeCLIBackend()
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            backend.check_available()

    def test_bind_tools_returns_cli_tool_calling_model(self):
        backend = ClaudeCLIBackend()
        result = backend.bind_tools([])
        assert isinstance(result, _CLIToolCallingModel)

    def test_invoke_mints_session_on_first_call(self):
        backend = ClaudeCLIBackend(model="sonnet")
        fake_proc = MagicMock(returncode=0, stdout=json.dumps({"result": "hi", "session_id": "sess-1"}))
        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("subprocess.run", return_value=fake_proc) as mock_run:
            text, session_id = backend._invoke("sys", "user prompt", None)
        assert text == "hi"
        assert session_id == "sess-1"
        cmd = mock_run.call_args.args[0]
        assert "--session-id" in cmd
        assert "--resume" not in cmd

    def test_invoke_resumes_existing_session(self):
        backend = ClaudeCLIBackend(model="sonnet")
        fake_proc = MagicMock(returncode=0, stdout=json.dumps({"result": "hi again", "session_id": "sess-1"}))
        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("subprocess.run", return_value=fake_proc) as mock_run:
            backend._invoke("", "next turn", "sess-1")
        cmd = mock_run.call_args.args[0]
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == "sess-1"

    def test_invoke_raises_on_cli_error(self):
        backend = ClaudeCLIBackend()
        fake_proc = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("subprocess.run", return_value=fake_proc):
            with pytest.raises(RuntimeError, match="claude CLI exited 1"):
                backend._invoke("", "user", None)


class TestCopilotCLIBackend:
    def test_check_available_raises_when_binary_missing(self):
        backend = CopilotCLIBackend()
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="`copilot` binary not found"):
                backend.check_available()

    def test_invoke_parses_jsonl_event_stream(self):
        backend = CopilotCLIBackend(model="claude-sonnet-4.5")
        stdout = "\n".join([
            json.dumps({"type": "assistant.message", "data": {"content": "the answer"}}),
            json.dumps({"type": "result", "sessionId": "sess-2"}),
        ])
        fake_proc = MagicMock(returncode=0, stdout=stdout)
        with patch("shutil.which", return_value="/usr/local/bin/copilot"), \
             patch("subprocess.run", return_value=fake_proc):
            text, session_id = backend._invoke("sys", "user", None)
        assert text == "the answer"
        assert session_id == "sess-2"

    def test_invoke_raises_when_no_assistant_message(self):
        backend = CopilotCLIBackend()
        fake_proc = MagicMock(returncode=0, stdout=json.dumps({"type": "result", "sessionId": "sess-3"}))
        with patch("shutil.which", return_value="/usr/local/bin/copilot"), \
             patch("subprocess.run", return_value=fake_proc):
            with pytest.raises(RuntimeError, match="produced no assistant.message"):
                backend._invoke("", "user", None)


class TestCLIToolCallingModel:
    def test_invoke_parses_tool_call_into_ai_message(self):
        from langchain_core.messages import HumanMessage

        def fake_invoke_fn(system_prompt, user_prompt, session_id):
            return json.dumps({"tool": "summarise_findings", "args": {"findings_json": "{}"}}), "sess-x"

        model = _CLIToolCallingModel(fake_invoke_fn, tools=[])
        result = model.invoke([HumanMessage(content="go")])
        assert result.tool_calls[0]["name"] == "summarise_findings"
        assert result.tool_calls[0]["args"] == {"findings_json": "{}"}
        assert model._session_id == "sess-x"

    def test_invoke_parses_final_answer_into_plain_content(self):
        from langchain_core.messages import HumanMessage

        def fake_invoke_fn(system_prompt, user_prompt, session_id):
            return json.dumps({"final_answer": "done"}), "sess-y"

        model = _CLIToolCallingModel(fake_invoke_fn, tools=[])
        result = model.invoke([HumanMessage(content="go")])
        assert result.content == "done"
        assert not result.tool_calls

    def test_invoke_only_sends_new_messages_on_second_call(self):
        from langchain_core.messages import HumanMessage

        seen_prompts = []

        def fake_invoke_fn(system_prompt, user_prompt, session_id):
            seen_prompts.append(user_prompt)
            return json.dumps({"final_answer": "ok"}), "sess-z"

        model = _CLIToolCallingModel(fake_invoke_fn, tools=[])
        msg1 = HumanMessage(content="first")
        msg2 = HumanMessage(content="second")
        model.invoke([msg1])
        model.invoke([msg1, msg2])
        assert "first" in seen_prompts[0]
        assert "first" not in seen_prompts[1]
        assert "second" in seen_prompts[1]
