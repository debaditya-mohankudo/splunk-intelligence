"""
Unit tests for splunk/connector.py — the facade wrapping data loading,
detection, MCP-driven step functions, and DB persistence (no HTTP/server).

DB-touching calls (init_db, store_*, upsert/clear_active_run_row, RunLogger)
are patched out via an autouse fixture to keep tests hermetic and fast — the
`_sessions` in-memory dict is real, so start_investigation/submit_report
still exercise real detector/parsing logic end to end. The active_runs
round-trip (upsert/get/clear/pop_hint) is covered separately in
tests/test_db.py-equivalent coverage below (TestActiveRunsDB).
"""
from __future__ import annotations

import json
import pathlib
import shutil
from unittest.mock import patch

import polars as pl
import pytest

from splunk import connector

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> list[dict]:
    data = json.loads((FIXTURES / name).read_text())
    return data["results"]


@pytest.fixture(autouse=True)
def _no_db_side_effects():
    with (
        patch("splunk.connector.init_db"),
        patch("splunk.connector.store_events"),
        patch("splunk.connector.upsert_active_run"),
        patch("splunk.connector.store_report"),
        patch("splunk.connector.store_queries"),
        patch("splunk.connector.clear_active_run_row"),
        patch("splunk.connector.RunLogger"),
    ):
        yield
    connector._sessions.clear()


# ---------------------------------------------------------------------------
# start_investigation
# ---------------------------------------------------------------------------

class TestStartInvestigation:
    def test_missing_source_and_spl_returns_error(self):
        result = connector.start_investigation()
        assert "error" in result

    def test_load_from_file_cert_errors(self, tmp_path):
        dest = tmp_path / "cert_errors.json"
        shutil.copy(FIXTURES / "cert_errors.json", dest)

        result = connector.start_investigation(source=str(dest))
        assert "error" not in result
        assert "run_id" in result
        assert result["event_count"] > 0
        assert "findings" in result
        assert result["next"]

    def test_load_from_file_access_logs(self, tmp_path):
        dest = tmp_path / "access_logs.json"
        shutil.copy(FIXTURES / "access_logs.json", dest)

        result = connector.start_investigation(source=str(dest))
        assert "error" not in result
        assert result["event_count"] == 3

    def test_file_not_found_returns_error(self):
        result = connector.start_investigation(source="/nonexistent/path.json")
        assert "error" in result

    def test_repo_path_included_in_result(self, tmp_path):
        dest = tmp_path / "cert_errors.json"
        shutil.copy(FIXTURES / "cert_errors.json", dest)

        result = connector.start_investigation(source=str(dest), repo_path="/some/repo")
        assert result["repo_path"] == "/some/repo"
        assert "code_context" in result

    def test_multiple_concurrent_runs_allowed(self, tmp_path):
        # No singleton lock (the old FastAPI 409-on-concurrent-run behavior is
        # gone) — each call gets its own run_id and they don't collide.
        dest = tmp_path / "cert_errors.json"
        shutil.copy(FIXTURES / "cert_errors.json", dest)

        r1 = connector.start_investigation(source=str(dest))
        r2 = connector.start_investigation(source=str(dest))
        assert r1["run_id"] != r2["run_id"]
        assert "error" not in r1 and "error" not in r2


# ---------------------------------------------------------------------------
# submit_report
# ---------------------------------------------------------------------------

class TestSubmitReport:
    def _start(self) -> str:
        result = connector.start_investigation(source=str(FIXTURES / "cert_errors.json"))
        return result["run_id"]

    def test_wrong_run_id_returns_error(self):
        result = connector.submit_report("nonexistent-run", "report")
        assert "error" in result

    def test_high_confidence_returns_done(self):
        run_id = self._start()
        report = "## Report\n**Confidence:** High\n\nFound root cause."
        result = connector.submit_report(run_id, report, queries=["index=pki"])
        assert result["status"] == "done"
        assert result["confidence"] == "High"

    def test_no_queries_returns_done(self):
        run_id = self._start()
        report = "## Report\n**Confidence:** Medium\n\nIncomplete."
        result = connector.submit_report(run_id, report, queries=[])
        assert result["status"] == "done"

    def test_max_iterations_returns_done(self):
        from splunk.config import INVESTIGATOR_MAX_ITER

        run_id = self._start()
        report = "## Report\n**Confidence:** Medium\n\nStill investigating."
        new_df = pl.DataFrame(_load_fixture("access_logs.json"))

        result = None
        with patch("splunk.connector._execute_queries", return_value=new_df):
            for _ in range(INVESTIGATOR_MAX_ITER):
                result = connector.submit_report(run_id, report, queries=["index=pki"])

        assert result["status"] == "done"
        assert result["iterations"] == INVESTIGATOR_MAX_ITER

    def test_no_new_events_from_queries_returns_done(self):
        run_id = self._start()
        report = "## Report\n**Confidence:** Medium\n\nInvestigating."
        with patch("splunk.connector._execute_queries", return_value=None):
            result = connector.submit_report(run_id, report, queries=["index=pki"])
        assert result["status"] == "done"
        assert result.get("reason") == "no new events from follow-up queries"

    def test_new_events_returns_continue(self):
        run_id = self._start()
        report = "## Report\n**Confidence:** Medium\n\nInvestigating."
        new_df = pl.DataFrame(_load_fixture("access_logs.json"))
        with patch("splunk.connector._execute_queries", return_value=new_df):
            result = connector.submit_report(run_id, report, queries=["index=web"])
        assert result["status"] == "continue"
        assert "findings" in result
        assert result["next"]

    def test_continue_response_has_correct_shape(self):
        run_id = self._start()
        report = "## Report\n**Confidence:** Low\n\nEarly stage."
        new_df = pl.DataFrame(_load_fixture("windows_events.json"))
        with patch("splunk.connector._execute_queries", return_value=new_df):
            result = connector.submit_report(run_id, report, queries=["index=win"])
        assert result["status"] == "continue"
        assert "run_id" in result
        assert "iteration" in result
        assert "confidence" in result
        assert "event_count" in result
        assert isinstance(result["findings"], dict)


# ---------------------------------------------------------------------------
# get_findings
# ---------------------------------------------------------------------------

class TestGetFindings:
    def test_returns_findings_for_active_run(self):
        run_id = connector.start_investigation(source=str(FIXTURES / "cert_errors.json"))["run_id"]
        result = connector.get_findings(run_id)
        assert "error" not in result
        assert result["run_id"] == run_id
        assert "findings" in result

    def test_wrong_run_id_returns_error(self):
        result = connector.get_findings("nonexistent-run")
        assert "error" in result


# ---------------------------------------------------------------------------
# pause / resume / hint
# ---------------------------------------------------------------------------

class TestPauseResumeHint:
    def test_pause_and_resume_on_real_session(self):
        run_id = connector.start_investigation(source=str(FIXTURES / "cert_errors.json"))["run_id"]
        assert connector.request_pause(run_id)["status"] == "paused"
        assert connector.resume(run_id)["status"] == "resumed"

    def test_pause_wrong_run_id_returns_error(self):
        assert "error" in connector.request_pause("nonexistent-run")

    def test_resume_wrong_run_id_returns_error(self):
        assert "error" in connector.resume("nonexistent-run")

    def test_hint_sets_on_real_session(self):
        run_id = connector.start_investigation(source=str(FIXTURES / "cert_errors.json"))["run_id"]
        result = connector.set_hint(run_id, "focus on api-gateway-01 cert chain errors")
        assert result["status"] == "hint set"
        assert result["hint"] == "focus on api-gateway-01 cert chain errors"

    def test_hint_wrong_run_id_returns_error(self):
        assert "error" in connector.set_hint("nonexistent-run", "some hint")


# ---------------------------------------------------------------------------
# active_runs DB round-trip — no mocking here, exercises real splunk.db
# ---------------------------------------------------------------------------

class TestActiveRunsDB:
    RUN_ID = "test-active-runs-roundtrip"

    def setup_method(self):
        from splunk.db import clear_active_run_row, init_db
        init_db()
        clear_active_run_row(self.RUN_ID)

    def teardown_method(self):
        from splunk.db import clear_active_run_row
        clear_active_run_row(self.RUN_ID)

    def test_upsert_then_get_round_trip(self):
        from splunk.db import get_active_run_row, upsert_active_run

        upsert_active_run(self.RUN_ID, source="test.json", iteration=1, confidence="Medium", events=10)
        row = get_active_run_row(self.RUN_ID)
        assert row is not None
        assert row["source"] == "test.json"
        assert row["iteration"] == 1
        assert row["confidence"] == "Medium"
        assert row["events"] == 10

    def test_upsert_updates_existing_row(self):
        from splunk.db import get_active_run_row, upsert_active_run

        upsert_active_run(self.RUN_ID, iteration=1)
        upsert_active_run(self.RUN_ID, iteration=2, confidence="High")
        row = get_active_run_row(self.RUN_ID)
        assert row["iteration"] == 2
        assert row["confidence"] == "High"

    def test_clear_removes_row(self):
        from splunk.db import clear_active_run_row, get_active_run_row, upsert_active_run

        upsert_active_run(self.RUN_ID, iteration=1)
        clear_active_run_row(self.RUN_ID)
        assert get_active_run_row(self.RUN_ID) is None

    def test_pop_hint_reads_then_clears(self):
        from splunk.db import get_active_run_row, pop_hint, upsert_active_run

        upsert_active_run(self.RUN_ID, hint="focus on X")
        hint = pop_hint(self.RUN_ID)
        assert hint == "focus on X"
        assert pop_hint(self.RUN_ID) is None
        assert get_active_run_row(self.RUN_ID)["hint"] is None

    def test_get_without_run_id_returns_most_recent(self):
        from splunk.db import get_active_run_row, upsert_active_run

        upsert_active_run(self.RUN_ID, source="most-recent")
        row = get_active_run_row()
        assert row is not None
        assert row["run_id"] == self.RUN_ID


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_start_command_prints_json(self, tmp_path, capsys):
        dest = tmp_path / "cert_errors.json"
        shutil.copy(FIXTURES / "cert_errors.json", dest)

        connector._cli_main(["start", "--source", str(dest)])
        out = json.loads(capsys.readouterr().out)
        assert "error" not in out
        assert "run_id" in out

    def test_get_findings_command_errors_on_unknown_run(self, capsys):
        connector._cli_main(["get-findings", "--run-id", "nonexistent-run"])
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

    def test_missing_required_arg_raises_systemexit(self):
        with pytest.raises(SystemExit):
            connector._cli_main(["submit-report", "--run-id", "x"])  # missing --report
