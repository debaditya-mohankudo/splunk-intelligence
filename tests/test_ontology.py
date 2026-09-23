"""ontology/splunk-investigation-domain.json must not claim what the code no longer does."""
import ast
import json
from pathlib import Path

from splunk.investigation_areas import INVESTIGATION_AREAS

ROOT = Path(__file__).resolve().parent.parent
ONTOLOGY = json.loads((ROOT / "ontology" / "splunk-investigation-domain.json").read_text())
TERMS = ONTOLOGY["terms"]


def _evidence_refs():
    for name, term in TERMS.items():
        for ref in term["evidence"].split(","):
            path, _, symbol = ref.strip().partition(":")
            yield name, path, symbol


def test_every_evidence_path_and_symbol_exists():
    missing = []
    for name, path, symbol in _evidence_refs():
        file = ROOT / path
        if not file.is_file():
            missing.append(f"{name}: {path}")
        elif symbol and symbol not in file.read_text():
            missing.append(f"{name}: {path}:{symbol}")
    assert not missing, missing


def test_relations_only_use_defined_terms():
    for rel in ONTOLOGY["relations"]:
        assert rel["subject"] in TERMS and rel["object"] in TERMS, rel


def test_investigation_areas_match_registry():
    assert sorted(TERMS["InvestigationArea"]["members"]) == sorted(INVESTIGATION_AREAS)


def test_detectors_match_public_functions():
    tree = ast.parse((ROOT / "splunk" / "detectors.py").read_text())
    public = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert sorted(TERMS["Detector"]["members"]) == sorted(public)
