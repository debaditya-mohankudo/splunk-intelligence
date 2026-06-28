"""
Unit tests for splunk__ MCP tools.

Strategy:
- No Ollama, no Splunk REST, no FastAPI server required.
- _server_state() is patched to return a controlled in-memory dict.
- DB calls (init_db, store_report, store_queries) are patched out.
- _execute_queries is patched to return a controlled DataFrame or None.
"""
from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock, patch

import polars as pl
import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_fixture(name: str) -> list[dict]:
    data = json.loads((FIXTURES / name).read_text())
    return data["results"]


def _make_df(fixture_name: str) -> pl.DataFrame:
    from splunk.parsers import parse_splunk_json
    raw = (FIXTURES / fixture_name).read_text()
    return parse_splunk_json(raw)


def _active_state(run_id: str, df: pl.DataFrame, iteration: int = 0) -> dict:
    from splunk.investigator import _build_findings, _prepare_df
    df = _prepare_df(df)
    return {
        "run_id": run_id,
        "source": "test",
        "iteration": iteration,
        "confidence": "Medium",
        "df": df,
        "findings": _build_findings(df),
    }


# ---------------------------------------------------------------------------
# splunk__investigate_start
# ---------------------------------------------------------------------------

class TestInvestigateStart:
    def test_missing_source_and_spl(self):
        from splunk.mcp_server import splunk__investigate_start
        result = json.loads(splunk__investigate_start())
        assert "error" in result

    def test_load_from_file_cert_errors(self, tmp_path):
        import shutil
        src = FIXTURES / "cert_errors.json"
        dest = tmp_path / "cert_errors.json"
        shutil.copy(src, dest)

        empty_state: dict = {}
        with (
            patch("splunk.mcp_server._server_state", return_value=empty_state),
            patch("splunk.db.init_db"),
            patch("splunk.mcp_server._emit_sse"),
        ):
            from splunk.mcp_server import splunk__investigate_start
            result = json.loads(splunk__investigate_start(source=str(dest)))

        assert "error" not in result
        assert "run_id" in result
        assert result["event_count"] > 0
        assert "findings" in result
        assert result["next"]

    def test_load_from_file_access_logs(self, tmp_path):
        import shutil
        src = FIXTURES / "access_logs.json"
        dest = tmp_path / "access_logs.json"
        shutil.copy(src, dest)

        empty_state: dict = {}
        with (
            patch("splunk.mcp_server._server_state", return_value=empty_state),
            patch("splunk.db.init_db"),
            patch("splunk.mcp_server._emit_sse"),
        ):
            from splunk.mcp_server import splunk__investigate_start
            result = json.loads(splunk__investigate_start(source=str(dest)))

        assert "error" not in result
        assert result["event_count"] == 3

    def test_blocks_if_run_already_active(self):
        active_state = {"run_id": "existing-run"}
        with patch("splunk.mcp_server._server_state", return_value=active_state):
            from splunk.mcp_server import splunk__investigate_start
            result = json.loads(splunk__investigate_start(source="anything.json"))
        assert "error" in result
        assert "existing-run" in result["error"]

    def test_file_not_found_returns_error(self):
        empty_state: dict = {}
        with (
            patch("splunk.mcp_server._server_state", return_value=empty_state),
            patch("splunk.db.init_db"),
        ):
            from splunk.mcp_server import splunk__investigate_start
            result = json.loads(splunk__investigate_start(source="/nonexistent/path.json"))
        assert "error" in result


# ---------------------------------------------------------------------------
# splunk__submit_report
# ---------------------------------------------------------------------------

class TestSubmitReport:
    RUN_ID = "test-run-001"

    def _state(self, iteration: int = 0) -> dict:
        df = _make_df("cert_errors.json")
        return _active_state(self.RUN_ID, df, iteration)

    def test_wrong_run_id_returns_error(self):
        state = self._state()
        with patch("splunk.mcp_server._server_state", return_value=state):
            from splunk.mcp_server import splunk__submit_report
            result = json.loads(splunk__submit_report("wrong-id", "report"))
        assert "error" in result

    def test_high_confidence_returns_done(self):
        state = self._state()
        report = "## Report\n**Confidence:** High\n\nFound root cause."
        with (
            patch("splunk.mcp_server._server_state", return_value=state),
            patch("splunk.db.store_report"),
            patch("splunk.db.store_queries"),
            patch("splunk.mcp_server._emit_sse"),
        ):
            from splunk.mcp_server import splunk__submit_report
            result = json.loads(splunk__submit_report(self.RUN_ID, report, queries=["index=pki"]))
        assert result["status"] == "done"
        assert result["confidence"] == "High"

    def test_no_queries_returns_done(self):
        state = self._state()
        report = "## Report\n**Confidence:** Medium\n\nIncomplete."
        with (
            patch("splunk.mcp_server._server_state", return_value=state),
            patch("splunk.db.store_report"),
            patch("splunk.db.store_queries"),
            patch("splunk.mcp_server._emit_sse"),
        ):
            from splunk.mcp_server import splunk__submit_report
            result = json.loads(splunk__submit_report(self.RUN_ID, report, queries=[]))
        assert result["status"] == "done"

    def test_max_iterations_returns_done(self):
        from splunk.config import INVESTIGATOR_MAX_ITER
        state = self._state(iteration=INVESTIGATOR_MAX_ITER - 1)
        report = "## Report\n**Confidence:** Medium\n\nStill investigating."
        with (
            patch("splunk.mcp_server._server_state", return_value=state),
            patch("splunk.db.store_report"),
            patch("splunk.db.store_queries"),
            patch("splunk.mcp_server._emit_sse"),
        ):
            from splunk.mcp_server import splunk__submit_report
            result = json.loads(splunk__submit_report(self.RUN_ID, report, queries=["index=pki"]))
        assert result["status"] == "done"
        assert result["iterations"] == INVESTIGATOR_MAX_ITER

    def test_no_new_events_from_queries_returns_done(self):
        state = self._state()
        report = "## Report\n**Confidence:** Medium\n\nInvestigating."
        with (
            patch("splunk.mcp_server._server_state", return_value=state),
            patch("splunk.db.store_report"),
            patch("splunk.db.store_queries"),
            patch("splunk.mcp_server._emit_sse"),
            patch("splunk.investigator._execute_queries", return_value=None),
        ):
            from splunk.mcp_server import splunk__submit_report
            result = json.loads(splunk__submit_report(self.RUN_ID, report, queries=["index=pki"]))
        assert result["status"] == "done"
        assert result.get("reason") == "no new events from follow-up queries"

    def test_new_events_returns_continue(self):
        state = self._state()
        report = "## Report\n**Confidence:** Medium\n\nInvestigating."
        new_rows = _load_fixture("access_logs.json")
        new_df = pl.DataFrame(new_rows)
        with (
            patch("splunk.mcp_server._server_state", return_value=state),
            patch("splunk.db.store_report"),
            patch("splunk.db.store_queries"),
            patch("splunk.mcp_server._emit_sse"),
            patch("splunk.investigator._execute_queries", return_value=new_df),
        ):
            from splunk.mcp_server import splunk__submit_report
            result = json.loads(splunk__submit_report(self.RUN_ID, report, queries=["index=web"]))
        assert result["status"] == "continue"
        assert "findings" in result
        assert result["next"]

    def test_continue_response_has_correct_shape(self):
        state = self._state()
        report = "## Report\n**Confidence:** Low\n\nEarly stage."
        new_df = _make_df("windows_events.json")
        with (
            patch("splunk.mcp_server._server_state", return_value=state),
            patch("splunk.db.store_report"),
            patch("splunk.db.store_queries"),
            patch("splunk.mcp_server._emit_sse"),
            patch("splunk.investigator._execute_queries", return_value=new_df),
        ):
            from splunk.mcp_server import splunk__submit_report
            result = json.loads(splunk__submit_report(self.RUN_ID, report, queries=["index=win"]))
        assert result["status"] == "continue"
        assert "run_id" in result
        assert "iteration" in result
        assert "confidence" in result
        assert "event_count" in result
        assert isinstance(result["findings"], dict)


# ---------------------------------------------------------------------------
# splunk__get_findings
# ---------------------------------------------------------------------------

class TestGetFindings:
    RUN_ID = "test-run-002"

    def test_returns_findings_for_active_run(self):
        df = _make_df("cert_errors.json")
        state = _active_state(self.RUN_ID, df, iteration=1)
        with patch("splunk.mcp_server._server_state", return_value=state):
            from splunk.mcp_server import splunk__get_findings
            result = json.loads(splunk__get_findings(self.RUN_ID))
        assert "error" not in result
        assert result["run_id"] == self.RUN_ID
        assert result["iteration"] == 1
        assert "findings" in result

    def test_wrong_run_id_returns_error(self):
        state = {"run_id": self.RUN_ID}
        with patch("splunk.mcp_server._server_state", return_value=state):
            from splunk.mcp_server import splunk__get_findings
            result = json.loads(splunk__get_findings("wrong-id"))
        assert "error" in result

    def test_no_findings_yet_returns_error(self):
        state = {"run_id": self.RUN_ID}
        with patch("splunk.mcp_server._server_state", return_value=state):
            from splunk.mcp_server import splunk__get_findings
            result = json.loads(splunk__get_findings(self.RUN_ID))
        assert "error" in result


# ---------------------------------------------------------------------------
# splunk__pause
# ---------------------------------------------------------------------------

class TestPause:
    RUN_ID = "test-run-003"

    def test_sets_pause_flag(self):
        state: dict = {"run_id": self.RUN_ID}
        with patch("splunk.mcp_server._server_state", return_value=state):
            from splunk.mcp_server import splunk__pause
            result = json.loads(splunk__pause(self.RUN_ID))
        assert result["status"] == "paused"
        assert state["pause_requested"] is True

    def test_wrong_run_id_returns_error(self):
        state = {"run_id": self.RUN_ID}
        with patch("splunk.mcp_server._server_state", return_value=state):
            from splunk.mcp_server import splunk__pause
            result = json.loads(splunk__pause("wrong-id"))
        assert "error" in result


# ---------------------------------------------------------------------------
# splunk__hint
# ---------------------------------------------------------------------------

class TestHint:
    RUN_ID = "test-run-004"

    def test_sets_hint(self):
        state: dict = {"run_id": self.RUN_ID}
        hint_text = "focus on api-gateway-01 cert chain errors after 14:30 UTC"
        with patch("splunk.mcp_server._server_state", return_value=state):
            from splunk.mcp_server import splunk__hint
            result = json.loads(splunk__hint(self.RUN_ID, hint_text))
        assert result["status"] == "hint set"
        assert state["hint"] == hint_text
        assert result["hint"] == hint_text

    def test_wrong_run_id_returns_error(self):
        state = {"run_id": self.RUN_ID}
        with patch("splunk.mcp_server._server_state", return_value=state):
            from splunk.mcp_server import splunk__hint
            result = json.loads(splunk__hint("wrong-id", "some hint"))
        assert "error" in result
