"""Unit tests for splunk/dispatcher.py — the advisory nudge context manager."""
from __future__ import annotations

from splunk import dispatcher


class TestRepoPathNudge:
    def test_fires_when_omitted(self):
        assert dispatcher.repo_path_nudge("") is not None

    def test_silent_when_given(self):
        assert dispatcher.repo_path_nudge("/some/repo") is None


class TestConfidenceNudge:
    def test_fires_on_done_below_high(self):
        assert dispatcher.confidence_nudge("done", "Medium") is not None

    def test_silent_on_done_high(self):
        assert dispatcher.confidence_nudge("done", "High") is None

    def test_silent_when_not_done(self):
        assert dispatcher.confidence_nudge("continue", "Medium") is None


class TestNoFollowupNudge:
    def test_fires_on_done_with_no_queries(self):
        assert dispatcher.no_followup_nudge("done", []) is not None

    def test_silent_when_queries_present(self):
        assert dispatcher.no_followup_nudge("done", ["index=pki"]) is None

    def test_silent_when_not_done(self):
        assert dispatcher.no_followup_nudge("continue", []) is None


class TestToolCalled:
    def test_post_runs_on_success(self):
        seen = {}
        with dispatcher.tool_called(post=lambda r: seen.update(fired=True)) as call:
            call.result = {"status": "done"}
        assert seen.get("fired") is True

    def test_post_skipped_on_error_result(self):
        seen = {}
        with dispatcher.tool_called(post=lambda r: seen.update(fired=True)) as call:
            call.result = {"error": "nope"}
        assert "fired" not in seen

    def test_post_skipped_on_exception(self):
        seen = {}
        try:
            with dispatcher.tool_called(post=lambda r: seen.update(fired=True)) as call:
                call.result = {"status": "done"}
                raise ValueError("boom")
        except ValueError:
            pass
        assert "fired" not in seen

    def test_pre_runs_on_entry(self):
        seen = {}
        with dispatcher.tool_called(pre=lambda: seen.update(entered=True)):
            pass
        assert seen.get("entered") is True
