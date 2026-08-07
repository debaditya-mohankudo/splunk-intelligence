"""
Unit tests for standalone/agent_cli_bridge.py's deterministic tool functions
(claude_cli/copilot_cli backends only — see that module's docstring) and
splunk/investigation_areas.py's registry.

Scoped to what's testable without a live CLI call: the LangChain @tool-wrapped
functions are plain deterministic functions under the decorator (invoke via
.func(...) to bypass the tool-calling machinery), plus tool_node_fn's/
should_continue's state-merging logic exercised directly against
LogAnalysisState dicts. No graph invocation, no LLM calls.
"""
from __future__ import annotations

import json

from langchain_core.messages import AIMessage, ToolMessage

from standalone import agent_cli_bridge as agent
from splunk.investigation_areas import INVESTIGATION_AREAS, get_prompt, get_spl_template


class TestInvestigationAreasRegistry:
    def test_get_prompt_known_area(self):
        assert "4xx/5xx" in get_prompt("api_errors")

    def test_get_prompt_unknown_area_falls_back(self):
        assert get_prompt("some_new_domain") == "Investigate 'some_new_domain' in detail using the available findings."

    def test_get_spl_template_known_area(self):
        tmpl = get_spl_template("host_isolation")
        assert tmpl is not None
        assert "{index}" in tmpl

    def test_get_spl_template_unknown_area_is_none(self):
        assert get_spl_template("nonexistent") is None

    def test_first_occurrence_is_query_only(self):
        # Documented asymmetry: first_occurrence has a template but no bespoke prompt.
        assert "spl_template" in INVESTIGATION_AREAS["first_occurrence"]
        assert "prompt" not in INVESTIGATION_AREAS["first_occurrence"]
        assert get_prompt("first_occurrence").startswith("Investigate 'first_occurrence'")


class TestRequestDeeperAnalysis:
    def test_uses_registry(self):
        result = agent.request_deeper_analysis.func("database_slowdown")
        assert result == get_prompt("database_slowdown")

    def test_unknown_area_generic_fallback(self):
        result = agent.request_deeper_analysis.func("made_up_area")
        assert "made_up_area" in result


class TestGenerateFollowupQueries:
    def test_generates_query_for_known_area(self):
        result = agent.generate_followup_queries.func(
            hosts="host1,host2",
            error_codes="500",
            sourcetype="access",
            spike_start="2026-08-01T00:00:00",
            areas="host_isolation",
        )
        assert "host_isolation" in result
        assert "host1" in result

    def test_unknown_area_produces_no_queries(self):
        result = agent.generate_followup_queries.func(
            hosts="host1",
            error_codes="500",
            sourcetype="access",
            spike_start="2026-08-01T00:00:00",
            areas="not_a_real_area",
        )
        assert result == "No queries generated — check area names."


class TestRankHypotheses:
    def test_registers_string_hypotheses(self):
        result = agent.rank_hypotheses.func(json.dumps(["disk full", "network partition"]))
        assert "disk full" in result
        assert "network partition" in result

    def test_invalid_json(self):
        assert agent.rank_hypotheses.func("not json") == "Invalid JSON list of hypotheses."


class TestToolNodeFnHypothesisTracking:
    """Exercises tool_node_fn's state-merging logic directly, without a live LangGraph run."""

    def _state_with_tool_call(self, tool_name: str, args: dict, tool_call_id="call_1"):
        ai_msg = AIMessage(
            content="",
            tool_calls=[{"name": tool_name, "args": args, "id": tool_call_id}],
        )
        return {
            "messages": [ai_msg],
            "findings": {},
            "report": "",
            "followup_queries": [],
            "iterations": 1,
            "hypotheses": [],
        }

    def test_rank_hypotheses_populates_state(self, monkeypatch):
        from langgraph.prebuilt import ToolNode

        state = self._state_with_tool_call(
            "rank_hypotheses", {"hypotheses_json": json.dumps(["disk full"])}
        )

        def fake_invoke(self, state):
            return {"messages": [ToolMessage(content="ranked", name="rank_hypotheses", tool_call_id="call_1")]}

        monkeypatch.setattr(ToolNode, "invoke", fake_invoke)
        result = agent.tool_node_fn(state)
        assert result["hypotheses"] == [{"claim": "disk full", "area": "", "evidence": "", "status": "open"}]

    def test_format_report_confirms_hypothesis_and_rejects_others(self, monkeypatch):
        from langgraph.prebuilt import ToolNode

        state = self._state_with_tool_call(
            "format_report",
            {
                "summary": "s", "root_cause": "disk full", "confidence": "High",
                "affected_hosts": "h1", "timeline": "t", "next_steps": "check disk",
                "confirmed_hypothesis": "disk full",
            },
        )
        state["hypotheses"] = [
            {"claim": "disk full", "area": "", "evidence": "", "status": "open"},
            {"claim": "network partition", "area": "", "evidence": "", "status": "open"},
        ]

        def fake_invoke(self, state):
            return {"messages": [ToolMessage(content="# report", name="format_report", tool_call_id="call_1")]}

        monkeypatch.setattr(ToolNode, "invoke", fake_invoke)
        result = agent.tool_node_fn(state)
        statuses = {h["claim"]: h["status"] for h in result["hypotheses"]}
        assert statuses == {"disk full": "confirmed", "network partition": "rejected"}
        assert result["report"] == "# report"


class TestShouldContinue:
    def _base_state(self, **overrides):
        state = {
            "messages": [AIMessage(content="done", tool_calls=[])],
            "findings": {},
            "report": "",
            "followup_queries": [],
            "iterations": 1,
            "hypotheses": [],
        }
        state.update(overrides)
        return state

    def test_ends_when_all_hypotheses_resolved(self):
        state = self._base_state(
            messages=[AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}])],
            hypotheses=[{"claim": "a", "area": "", "evidence": "", "status": "confirmed"}],
        )
        assert agent.should_continue(state) == agent.END

    def test_continues_when_hypothesis_open_and_tool_call_pending(self):
        state = self._base_state(
            messages=[AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}])],
            hypotheses=[{"claim": "a", "area": "", "evidence": "", "status": "open"}],
        )
        assert agent.should_continue(state) == "tools"

    def test_max_iterations_forces_end(self):
        state = self._base_state(
            iterations=agent.MAX_ITERATIONS,
            messages=[AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}])],
        )
        assert agent.should_continue(state) == agent.END
