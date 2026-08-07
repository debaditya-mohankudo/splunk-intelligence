"""
Unit tests for standalone/agent.py's NOOA-based LogAnalysisAgent deterministic
methods (ollama backend only — see standalone/agent_cli_bridge.py for
claude_cli/copilot_cli, tested separately in tests/test_agent_cli_bridge.py).

Scoped to what's testable without an LLM: constructs the agent class with a
throwaway llm object (never invoked, since we only exercise plain methods
directly — no .investigate() call, no CodeAct loop, no network).
"""
from __future__ import annotations

from nooa.unifiedllm.fake import FakeLLMClient

from standalone.agent import _build_agent_class

_AGENT_CLASS = _build_agent_class(llm=FakeLLMClient())


def _agent(findings=None):
    return _AGENT_CLASS(findings or {})


class TestSummariseFindings:
    def test_no_findings(self):
        assert _agent().summarise_findings() == "No significant findings."

    def test_spikes_and_severity(self):
        a = _agent({
            "spikes": [{"window_start": "t0", "event_count": 5, "window_seconds": 60, "hosts": ["h1"]}],
            "severity": {"error": 3},
        })
        result = a.summarise_findings()
        assert "1 frequency spike(s)" in result
        assert "h1" in result
        assert "Severity breakdown" in result


class TestRequestDeeperAnalysis:
    def test_known_area(self):
        from splunk.investigation_areas import get_prompt

        assert _agent().request_deeper_analysis("database_slowdown") == get_prompt("database_slowdown")

    def test_unknown_area_generic_fallback(self):
        assert "made_up_area" in _agent().request_deeper_analysis("made_up_area")


class TestGenerateFollowupQueries:
    def test_generates_query_for_known_area(self):
        a = _agent()
        result = a.generate_followup_queries(
            hosts=["host1", "host2"], error_codes=["500"], sourcetype="access",
            spike_start="2026-08-01T00:00:00", areas=["host_isolation"],
        )
        assert "host_isolation" in result
        assert "host1" in result
        assert a.followup_queries == [result]

    def test_unknown_area_produces_no_queries(self):
        a = _agent()
        result = a.generate_followup_queries(
            hosts=["host1"], error_codes=["500"], sourcetype="access",
            spike_start="2026-08-01T00:00:00", areas=["not_a_real_area"],
        )
        assert result == "No queries generated — check area names."
        assert a.followup_queries == []


class TestRankHypotheses:
    def test_registers_string_hypotheses(self):
        a = _agent()
        result = a.rank_hypotheses(["disk full", "network partition"])
        assert "disk full" in result
        assert "network partition" in result
        assert [h["claim"] for h in a.hypotheses] == ["disk full", "network partition"]
        assert all(h["status"] == "open" for h in a.hypotheses)

    def test_dedupes_existing_claims(self):
        a = _agent()
        a.rank_hypotheses(["disk full"])
        a.rank_hypotheses(["disk full", "network partition"])
        assert [h["claim"] for h in a.hypotheses] == ["disk full", "network partition"]

    def test_no_hypotheses(self):
        assert _agent().rank_hypotheses([]) == "No hypotheses registered."


class TestFormatReport:
    def test_confirms_hypothesis_and_rejects_others(self):
        a = _agent()
        a.rank_hypotheses(["disk full", "network partition"])
        report = a.format_report(
            summary="s", root_cause="disk full", confidence="High",
            affected_hosts=["h1"], timeline="t", next_steps=["check disk"],
            confirmed_hypothesis="disk full",
        )
        statuses = {h["claim"]: h["status"] for h in a.hypotheses}
        assert statuses == {"disk full": "confirmed", "network partition": "rejected"}
        assert a.report == report
        assert "disk full" in report
        assert "check disk" in report
        assert "h1" in report

    def test_no_confirmed_hypothesis_leaves_them_open(self):
        a = _agent()
        a.rank_hypotheses(["disk full"])
        a.format_report(
            summary="s", root_cause="unclear", confidence="Low",
            affected_hosts=[], timeline="t", next_steps=[],
        )
        assert a.hypotheses[0]["status"] == "open"
