"""
Unit tests for splunk__ MCP tools.

Strategy: since mcp_server.py's tools are thin wrappers around
splunk.connector.* (no more per-tool state dict), patch the connector
functions directly with controlled return values and verify the tool passes
arguments through correctly and returns the exact JSON-serialized result.

Deep behavior (real file loading, DB persistence, iteration/confidence
logic) is covered in tests/test_connector.py instead — that's where the
actual business logic now lives.
"""
from __future__ import annotations

import json
from unittest.mock import patch


class TestInvestigateStart:
    def test_passes_args_through_to_connector(self):
        with patch(
            "splunk.mcp_server.connector.start_investigation",
            return_value={"run_id": "abc", "event_count": 5},
        ) as mock_start:
            from splunk.mcp_server import splunk__investigate_start
            result = json.loads(splunk__investigate_start(source="x.json", repo_path="/repo"))

        mock_start.assert_called_once_with(
            source="x.json", spl="", earliest="-24h", latest="now", repo_path="/repo",
        )
        assert result == {"run_id": "abc", "event_count": 5}

    def test_returns_connector_error_unchanged(self):
        with patch("splunk.mcp_server.connector.start_investigation", return_value={"error": "boom"}):
            from splunk.mcp_server import splunk__investigate_start
            result = json.loads(splunk__investigate_start())
        assert result == {"error": "boom"}


class TestSubmitReport:
    def test_passes_args_through_to_connector(self):
        with patch(
            "splunk.mcp_server.connector.submit_report",
            return_value={"status": "done"},
        ) as mock_submit:
            from splunk.mcp_server import splunk__submit_report
            result = json.loads(splunk__submit_report("run-1", "report text", queries=["q1"]))

        mock_submit.assert_called_once_with("run-1", "report text", ["q1"])
        assert result == {"status": "done"}

    def test_continue_status_passed_through(self):
        payload = {"status": "continue", "run_id": "run-1", "iteration": 2, "findings": {}}
        with patch("splunk.mcp_server.connector.submit_report", return_value=payload):
            from splunk.mcp_server import splunk__submit_report
            result = json.loads(splunk__submit_report("run-1", "report"))
        assert result == payload


class TestGetFindings:
    def test_passes_through(self):
        with patch(
            "splunk.mcp_server.connector.get_findings",
            return_value={"run_id": "run-1", "findings": {}},
        ) as mock_get:
            from splunk.mcp_server import splunk__get_findings
            result = json.loads(splunk__get_findings("run-1"))

        mock_get.assert_called_once_with("run-1")
        assert result == {"run_id": "run-1", "findings": {}}

    def test_error_passed_through(self):
        with patch("splunk.mcp_server.connector.get_findings", return_value={"error": "not active"}):
            from splunk.mcp_server import splunk__get_findings
            result = json.loads(splunk__get_findings("wrong-id"))
        assert "error" in result


class TestPause:
    def test_passes_through(self):
        with patch(
            "splunk.mcp_server.connector.request_pause",
            return_value={"status": "paused", "run_id": "run-1"},
        ) as mock_pause:
            from splunk.mcp_server import splunk__pause
            result = json.loads(splunk__pause("run-1"))

        mock_pause.assert_called_once_with("run-1")
        assert result["status"] == "paused"

    def test_error_passed_through(self):
        with patch("splunk.mcp_server.connector.request_pause", return_value={"error": "not active"}):
            from splunk.mcp_server import splunk__pause
            result = json.loads(splunk__pause("wrong-id"))
        assert "error" in result


class TestHint:
    def test_passes_through(self):
        with patch(
            "splunk.mcp_server.connector.set_hint",
            return_value={"status": "hint set", "run_id": "run-1", "hint": "focus on X"},
        ) as mock_hint:
            from splunk.mcp_server import splunk__hint
            result = json.loads(splunk__hint("run-1", "focus on X"))

        mock_hint.assert_called_once_with("run-1", "focus on X")
        assert result["status"] == "hint set"
        assert result["hint"] == "focus on X"

    def test_error_passed_through(self):
        with patch("splunk.mcp_server.connector.set_hint", return_value={"error": "not active"}):
            from splunk.mcp_server import splunk__hint
            result = json.loads(splunk__hint("wrong-id", "some hint"))
        assert "error" in result


class TestLspCallChain:
    def test_returns_error_when_session_not_found(self):
        with patch("splunk.mcp_server.connector.get_session", return_value=None):
            from splunk.mcp_server import splunk__lsp_call_chain
            result = json.loads(splunk__lsp_call_chain("run-1", "some_func"))
        assert "error" in result

    def test_returns_error_when_no_repo_path(self):
        with patch("splunk.mcp_server.connector.get_session", return_value={"repo_path": ""}):
            from splunk.mcp_server import splunk__lsp_call_chain
            result = json.loads(splunk__lsp_call_chain("run-1", "some_func"))
        assert "error" in result
