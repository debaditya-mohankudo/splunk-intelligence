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

from splunk.tui import SplunkTUI

TUI_APP_PATH = str(Path(__file__).parent.parent / "splunk" / "tui.py")


def test_dashboard_renders(snap_compare):
    """The idle-state cockpit message ("No active investigation — press
    'n' to start one") must actually render — this is the message that was
    silently empty before the Cockpit CSS fix (height:3 + padding:1 +
    border left zero rows for content)."""
    assert snap_compare(TUI_APP_PATH, terminal_size=(120, 40))


def test_launch_screen_renders(snap_compare):
    assert snap_compare(TUI_APP_PATH, press=["n"], terminal_size=(120, 40))


def test_file_picker_renders(snap_compare, tmp_path):
    """The picker's status message ("Select a .json or .csv Splunk
    export") must actually render — this is the message that was silently
    empty before the StatusChip constructor fix (message string passed to
    Static.__init__ but never assigned to the `message` reactive that
    render() actually reads).

    Pinned to a fixed tmp_path (not Path.cwd()) so the rendered DirectoryTree
    listing is hermetic — this test used to drift every time a file or
    directory was added/removed at the repo root, since FilePickerScreen
    rendered the live cwd. `.hidden` here also exercises SplunkFileTree's
    dotfile filtering."""
    (tmp_path / "cert_errors.json").write_text("{}")
    (tmp_path / "access_logs.csv").write_text("")
    (tmp_path / "notes.txt").write_text("")
    (tmp_path / ".hidden").write_text("")

    app = SplunkTUI(picker_root=tmp_path)
    assert snap_compare(app, press=["n", "f"], terminal_size=(120, 40))


def test_live_analyze_screen_renders(snap_compare):
    assert snap_compare(TUI_APP_PATH, press=["n", "l"], terminal_size=(120, 40))
