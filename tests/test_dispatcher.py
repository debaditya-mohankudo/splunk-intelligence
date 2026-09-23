"""Unit tests for splunk/dispatcher.py — the advisory nudge functions."""
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

