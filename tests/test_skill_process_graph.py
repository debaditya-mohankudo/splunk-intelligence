"""The splunk-investigate process graph must stay true to the code it maps."""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GRAPH_PATH = ROOT / ".claude" / "skills" / "splunk-investigate" / "splunk-investigate-process-domain.json"


@pytest.fixture(scope="module")
def graph():
    return json.loads(GRAPH_PATH.read_text())


def _slug(heading: str) -> str:
    """GitHub-style anchor for a Markdown heading."""
    s = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return s.replace(" ", "-")


def test_edges_reference_existing_nodes_with_known_types(graph):
    nodes, edge_types = graph["nodes"], graph["meta"]["edge_types"]
    for e in graph["edges"]:
        assert e["from"] in nodes and e["to"] in nodes, e
        assert e["type"] in edge_types, e


def test_every_node_is_reachable_from_the_entry_gate(graph):
    seen, stack = set(), ["gate_input_given"]
    while stack:
        n = stack.pop()
        if n not in seen:
            seen.add(n)
            stack += [e["to"] for e in graph["edges"] if e["from"] == n]
    assert seen == set(graph["nodes"])


def test_gates_declare_polarity(graph):
    polarities = set(graph["meta"]["gate_polarity"]) - {"_note"}
    for name, node in graph["nodes"].items():
        if node["type"] == "gate":
            assert node.get("polarity") in polarities, name


def test_done_rules_match_step_order(graph):
    rules = sorted((n for n in graph["nodes"].values() if n.get("polarity") == "done_rule"), key=lambda n: n["order"])
    step_src = (ROOT / "splunk" / "connector.py").read_text()
    reasons = ["high confidence", "no follow-up queries", "max iterations reached", "no new events"]
    positions = [step_src.index(r) for r in reasons]
    assert len(rules) == 4 and positions == sorted(positions)


def test_mcp_tools_are_registered(graph):
    server = (ROOT / "splunk" / "mcp_server.py").read_text()
    registered = set(re.findall(r"@mcp\.tool\(\)\s*\ndef (splunk__\w+)", server))
    for name, node in graph["nodes"].items():
        cited = re.findall(r"splunk__\w+", node.get("mcp_tool") or "") + node.get("optional_tools", [])
        assert set(cited) <= registered, name


def test_terms_exist_in_ontology(graph):
    terms = set(json.loads((ROOT / "ontology" / "splunk-investigation-domain.json").read_text())["terms"])
    for name, node in graph["nodes"].items():
        assert set(node.get("refers_to_term", [])) <= terms, name


def test_writes_name_real_tables(graph):
    db = (ROOT / "splunk" / "db.py").read_text()
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", db))
    for name, node in graph["nodes"].items():
        assert set(node.get("writes", [])) <= tables, name


def test_evidence_symbols_exist(graph):
    for name, node in graph["nodes"].items():
        if "evidence" in node:
            path, symbol = node["evidence"].split(":")
            assert re.search(rf"^def {symbol}\b", (ROOT / path).read_text(), re.MULTILINE), name


def test_doc_refs_resolve(graph):
    for name, node in graph["nodes"].items():
        if "doc_ref" not in node:
            continue
        path, anchor = node["doc_ref"].split("#")
        headings = re.findall(r"^#+ (.+)$", (ROOT / path).read_text(), re.MULTILINE)
        assert anchor in {_slug(h) for h in headings}, (name, node["doc_ref"])
