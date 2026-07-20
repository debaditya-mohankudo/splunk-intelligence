"""
Screenshot regression tests for splunk/tui.py, via pytest-textual-snapshot.

These run headlessly inside pytest (no tmux, no real terminal) and diff a
rendered SVG screenshot against a saved reference — catching the class of
bug that only manifests in actual rendered output, not internal widget
state (e.g. a message string silently dropped by a widget's render()
override, or a CSS height/padding/border combination that leaves zero rows
for content). Both bugs were found via manual tmux testing during
task:85fae9f9's implementation; these tests exist so a regression would be
caught automatically instead of requiring another manual tmux session.

First run (or after an intentional visual change) needs:
    uv run pytest tests/test_tui_snapshots.py --snapshot-update
to (re)generate the reference SVGs under tests/__snapshots__/.
"""
from __future__ import annotations

from pathlib import Path

TUI_APP_PATH = str(Path(__file__).parent.parent / "splunk" / "tui.py")


def test_dashboard_renders(snap_compare):
    """The idle-state cockpit message ("No active investigation — press
    'n' to start one") must actually render — this is the message that was
    silently empty before the Cockpit CSS fix (height:3 + padding:1 +
    border left zero rows for content)."""
    assert snap_compare(TUI_APP_PATH, terminal_size=(120, 40))


def test_launch_screen_renders(snap_compare):
    assert snap_compare(TUI_APP_PATH, press=["n"], terminal_size=(120, 40))


def test_file_picker_renders(snap_compare):
    """The picker's status message ("Select a .json or .csv Splunk
    export") must actually render — this is the message that was silently
    empty before the StatusChip constructor fix (message string passed to
    Static.__init__ but never assigned to the `message` reactive that
    render() actually reads)."""
    assert snap_compare(TUI_APP_PATH, press=["n", "f"], terminal_size=(120, 40))


def test_live_analyze_screen_renders(snap_compare):
    assert snap_compare(TUI_APP_PATH, press=["n", "l"], terminal_size=(120, 40))
