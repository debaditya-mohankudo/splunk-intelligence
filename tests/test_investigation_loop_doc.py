"""docs/investigation-loop.md is the normative tool list — keep it equal to the server."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_loop_doc_lists_exactly_the_registered_tools():
    server = (ROOT / "splunk" / "mcp_server.py").read_text()
    registered = set(re.findall(r"@mcp\.tool\(\)\s*\ndef (splunk__\w+)", server))
    doc = (ROOT / "docs" / "investigation-loop.md").read_text()
    documented = set(re.findall(r"^\| `(splunk__\w+)` \|", doc, re.MULTILINE))
    assert registered and documented == registered
