"""
Terminal UI for the Splunk investigation engine. Reads splunk.db directly for
run history/detail and polls the active_runs table for live progress — no
HTTP, no server process. Also drives investigations interactively: a launch
flow lets you pick a file or run a live SPL query from inside the TUI, in
addition to just watching progress on runs started elsewhere (MCP/CLI).

Usage:
    uv run python -m splunk.tui
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    Static,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.0

# Home -> Flow -> Results, matching this app's actual screen flow
# (HomeScreen -> LaunchScreen(+picker/live-form) -> RunningScreen ->
# PrevRunsScreen).
STEP_NAMES = ["Home", "Flow", "Results"]

_CONFIDENCE_RE = re.compile(r"\*\*Confidence:\*\*\s*(High|Medium|Low)", re.IGNORECASE)


def _extract_confidence(report_md: str) -> str:
    m = _CONFIDENCE_RE.search(report_md or "")
    return m.group(1) if m else "—"


def _log_action(event: str, **kwargs: object) -> None:
    """Per-interaction audit line for TUI-level user actions (screen
    transitions, key presses) — separate from RunLogger's per-run JSONL
    audit trail, which covers engine state changes (splunk/connector.py)."""
    extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
    logger.info("tui.%s %s", event, extra)


# ---------------------------------------------------------------------------
# CustomStatic — shared base for this app's Static widgets, mirroring
# CustomScreen's role for screens: one control point for logging widget-level
# state transitions (which screen bindings alone don't cover — a widget can
# change state from a background worker, not just a user keypress).
# ---------------------------------------------------------------------------

class CustomStatic(Static):
    def _log(self, event: str, **kwargs: object) -> None:
        _log_action(f"{type(self).__name__}.{event}", **kwargs)


# ---------------------------------------------------------------------------
# Shared widgets
# ---------------------------------------------------------------------------

class Cockpit(CustomStatic):
    """Live status strip for the most-recently-updated in-progress run."""

    run_id: reactive[str | None] = reactive(None)
    source: reactive[str] = reactive("")
    iteration: reactive[int] = reactive(0)
    confidence: reactive[str] = reactive("—")
    events: reactive[int | None] = reactive(None)
    paused: reactive[bool] = reactive(False)

    def render(self) -> str:
        if not self.run_id:
            return "[dim]No active investigation — press 'n' to start one[/]"
        status = "[yellow]PAUSED[/]" if self.paused else "[green]running[/]"
        events_str = self.events if self.events is not None else "—"
        return (
            f"[bold]INVESTIGATING[/] {self.run_id[:8]} · iter {self.iteration} · "
            f"conf [bold]{self.confidence}[/] · {events_str} events · {status}  "
            f"[dim]{self.source}[/]"
        )


class BreadcrumbBar(Horizontal):
    """"1 · Home > 2 · Flow > 3 · Results" stepper bar, current step
    highlighted — mounted at the top of every screen via CustomScreen's
    compose_head(step_index), mirroring docker_log_analyzer/tui.py's
    BreadcrumbBar convention so both apps share the same house design."""

    def __init__(self, current_index: int) -> None:
        self._current_index = current_index
        super().__init__(classes="breadcrumb-bar")

    def compose(self) -> ComposeResult:
        for i, name in enumerate(STEP_NAMES):
            classes = "breadcrumb-chip active" if i == self._current_index else "breadcrumb-chip"
            yield Static(f"{i + 1} · {name}", classes=classes)
            if i < len(STEP_NAMES) - 1:
                yield Static("›", classes="breadcrumb-sep")


class ClickableCard(Container):
    """A Container that also responds to a mouse click, running the same
    action its key binding already triggers — every card is reachable by
    key OR click, never click-only (no mouse-only Button widgets), mirroring
    docker_log_analyzer/tui.py's ClickableCard."""

    can_focus = True

    def __init__(self, *children: Any, on_activate: Any, **kwargs: Any) -> None:
        super().__init__(*children, **kwargs)
        self._on_activate = on_activate

    def on_click(self) -> None:
        self._on_activate()


class StatusChip(CustomStatic):
    """Reusable live status indicator — mirrors the docker-log-analyzer
    ConnectScreen convention: testing/success/failure, color-coded."""

    state: reactive[str] = reactive("idle")  # idle | testing | success | failure
    message: reactive[str] = reactive("")

    def __init__(self, message: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.message = message

    def set_status(self, state: str, message: str) -> None:
        """Update state + message together and log the transition — the
        single control point every call site should go through instead of
        assigning `.state`/`.message` separately (easy to update one and
        forget the other otherwise)."""
        self.state = state
        self.message = message
        self._log("status", state=state, message=message)

    def render(self) -> str:
        if self.state == "testing":
            return f"[yellow]◌ {self.message}[/]"
        if self.state == "success":
            return f"[green]● {self.message}[/]"
        if self.state == "failure":
            return f"[red]✗ {self.message}[/]"
        return self.message or ""


# ---------------------------------------------------------------------------
# CustomScreen — shared base for every screen in this app. Mirrors
# docker_log_analyzer/tui.py's CustomScreen convention (task:0d8f0ca1):
# factors Header/Footer composition out of every screen and gives each one
# an auto-scoped `_log` helper (prefixed with the screen's own class name,
# so call sites never have to hand-type "dashboard.foo" / "launch.foo").
# ---------------------------------------------------------------------------

class CustomScreen(Screen):
    def _log(self, event: str, **kwargs: object) -> None:
        _log_action(f"{type(self).__name__}.{event}", **kwargs)

    def compose_head(self, step_index: int = 0) -> ComposeResult:
        yield Header()
        yield BreadcrumbBar(step_index)

    def compose_foot(self) -> ComposeResult:
        yield Footer()


# ---------------------------------------------------------------------------
# Home — lean landing screen: live cockpit + nav cards to the rest of the app
# ---------------------------------------------------------------------------

class HomeScreen(CustomScreen):
    CSS = """
    Cockpit {
        height: 3;
        padding: 0 1;
        border: solid $accent;
        content-align: left middle;
    }
    #home-nav { height: auto; margin-top: 1; }
    .home-card {
        width: 1fr; height: auto; margin-right: 2; padding: 1 2;
        border: round $accent 50%; background: $panel;
        align: center middle;
    }
    .home-card:last-of-type { margin-right: 0; }
    .home-card-icon { text-align: center; width: 100%; color: $accent; margin-bottom: 1; }
    .home-card-label { text-style: bold; text-align: center; width: 100%; }
    .home-card-hint { color: $text-muted; text-align: center; width: 100%; }
    #controls {
        height: 3;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("n", "new_investigation", "New investigation"),
        ("v", "prev_runs", "Prev runs"),
        ("p", "toggle_pause", "Pause/Resume"),
        ("h", "focus_hint", "Hint"),
        ("c", "config", "Config"),
    ]

    def compose(self) -> ComposeResult:
        yield from self.compose_head(0)
        yield Cockpit(id="cockpit")
        with Horizontal(id="home-nav"):
            with ClickableCard(classes="home-card", on_activate=self.action_new_investigation):
                yield Static("🔎", classes="home-card-icon")
                yield Label("(N) New Investigation", classes="home-card-label")
                yield Static("Start analyzing a file or live SPL query", classes="home-card-hint")
            with ClickableCard(classes="home-card", on_activate=self.action_prev_runs):
                yield Static("🗂", classes="home-card-icon")
                yield Label("(V) Prev Runs", classes="home-card-label")
                yield Static("Browse past investigation results", classes="home-card-hint")
        with Horizontal(id="controls"):
            yield Input(placeholder="inject analyst hint… (enter to send, 'h' to focus)", id="hint-input")
        yield from self.compose_foot()

    def on_mount(self) -> None:
        self.run_worker(self._poll_active_run_loop(), thread=False, exclusive=True, name="poll-active")

    # -----------------------------------------------------------------
    # Actions (key-bound — no Button widgets; the previous hint/pause
    # Buttons were also silently unrenderable, since the hint Input
    # expands to fill the whole #controls row and pushed them offscreen)
    # -----------------------------------------------------------------

    def action_new_investigation(self) -> None:
        self._log("new_investigation")
        self.app.push_screen(LaunchScreen())

    def action_prev_runs(self) -> None:
        self._log("prev_runs")
        self.app.push_screen(PrevRunsScreen())

    def action_config(self) -> None:
        self._log("config")
        self.app.push_screen(ConfigScreen())

    def action_toggle_pause(self) -> None:
        cockpit = self.query_one("#cockpit", Cockpit)
        run_id = cockpit.run_id
        if not run_id:
            return
        from splunk import connector

        action = connector.resume if cockpit.paused else connector.request_pause
        self._log("pause_toggle", run_id=run_id[:8], to_paused=not cockpit.paused)
        self.run_worker(asyncio.to_thread(action, run_id), thread=False, exclusive=False)

    def action_focus_hint(self) -> None:
        self.query_one("#hint-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "hint-input":
            return
        cockpit = self.query_one("#cockpit", Cockpit)
        run_id = cockpit.run_id
        hint = event.value.strip()
        if run_id and hint:
            from splunk import connector

            self._log("hint_submitted", run_id=run_id[:8])
            self.run_worker(
                asyncio.to_thread(connector.set_hint, run_id, hint), thread=False, exclusive=False,
            )
        event.input.value = ""

    # -----------------------------------------------------------------
    # Worker
    # -----------------------------------------------------------------

    async def _poll_active_run_loop(self) -> None:
        from splunk.db import get_active_run_row

        while True:
            row = await asyncio.to_thread(get_active_run_row)
            cockpit = self.query_one("#cockpit", Cockpit)
            if row is None:
                cockpit.run_id = None
            else:
                cockpit.run_id = row.get("run_id")
                cockpit.source = row.get("source") or ""
                cockpit.iteration = row.get("iteration", 0)
                cockpit.confidence = row.get("confidence", "—")
                cockpit.events = row.get("events")
                cockpit.paused = bool(row.get("pause_requested"))
            await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Prev Runs — run history, report detail. Reached from Home's "Prev Runs"
# nav card, not the app's root screen (see HomeScreen).
# ---------------------------------------------------------------------------

class PrevRunsScreen(CustomScreen):
    CSS = """
    #body {
        height: 1fr;
    }
    #sidebar {
        width: 40;
        border: solid $panel;
    }
    #detail {
        width: 1fr;
        border: solid $panel;
    }
    #queries {
        height: 10;
        border: solid $panel;
    }
    """

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, initial_run_id: str | None = None) -> None:
        super().__init__()
        self._selected_run_id: str | None = None
        self._initial_run_id = initial_run_id

    def compose(self) -> ComposeResult:
        yield from self.compose_head(2)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield DataTable(id="runs-table", cursor_type="row")
            with Vertical(id="detail"):
                yield Markdown("Select a run from the sidebar.", id="report")
                yield DataTable(id="queries")
        yield from self.compose_foot()

    def on_mount(self) -> None:
        runs_table = self.query_one("#runs-table", DataTable)
        runs_table.add_columns("Run", "Source", "Confidence", "Created")

        queries_table = self.query_one("#queries", DataTable)
        queries_table.add_columns("Iter", "Area", "SPL", "Rows")

        self.action_refresh()
        if self._initial_run_id:
            self.run_worker(self._load_run_detail(self._initial_run_id), thread=False, exclusive=False)

    # -----------------------------------------------------------------
    # Actions
    # -----------------------------------------------------------------

    def action_refresh(self) -> None:
        self._log("refresh")
        self.run_worker(self._load_runs(), thread=False, exclusive=False)

    # -----------------------------------------------------------------
    # DB reads (sync sqlite calls, run in a thread so the UI stays responsive)
    # -----------------------------------------------------------------

    @staticmethod
    def _fetch_runs() -> list[dict]:
        from splunk.db import _connect

        with _connect() as conn:
            rows = conn.execute(
                "SELECT run_id, source_file, created_at, report_md FROM reports ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "source": r["source_file"] or "—",
                "created_at": (r["created_at"] or "")[:16],
                "confidence": _extract_confidence(r["report_md"] or ""),
            }
            for r in rows
        ]

    @staticmethod
    def _fetch_run_detail(run_id: str) -> dict | None:
        from splunk.db import _connect, get_queries

        with _connect() as conn:
            row = conn.execute(
                "SELECT run_id, source_file, created_at, report_md FROM reports WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "run_id": run_id,
            "source": row["source_file"] or "—",
            "report_md": row["report_md"] or "",
            "queries": get_queries(run_id),
        }

    # -----------------------------------------------------------------
    # Workers
    # -----------------------------------------------------------------

    async def _load_runs(self) -> None:
        runs = await asyncio.to_thread(self._fetch_runs)
        table = self.query_one("#runs-table", DataTable)
        table.clear()
        for run in runs:
            table.add_row(
                run["run_id"][:8],
                run["source"],
                run["confidence"],
                run["created_at"],
                key=run["run_id"],
            )

    async def _load_run_detail(self, run_id: str) -> None:
        data = await asyncio.to_thread(self._fetch_run_detail, run_id)
        if data is None:
            return

        report = self.query_one("#report", Markdown)
        await report.update(data.get("report_md") or "*No report yet.*")

        table = self.query_one("#queries", DataTable)
        table.clear()
        for q in data.get("queries", []):
            spl = (q.get("spl") or "")[:80]
            table.add_row(str(q.get("iteration")), q.get("area") or "", spl, str(q.get("result_rows")))

    # -----------------------------------------------------------------
    # Event handlers
    # -----------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "runs-table":
            return
        run_id = str(event.row_key.value)
        self._selected_run_id = run_id
        self._log("run_selected", run_id=run_id[:8])
        self.run_worker(self._load_run_detail(run_id), thread=False, exclusive=False)


# ---------------------------------------------------------------------------
# Launch flow — start a new investigation from inside the TUI
# ---------------------------------------------------------------------------

class LaunchScreen(CustomScreen):
    """Entry point for starting a new investigation: file or live SPL."""

    BINDINGS = [
        ("f", "analyze_file", "Analyze file"),
        ("l", "live_analyze", "Live analyze"),
        ("escape", "app.pop_screen", "Back"),
    ]

    CSS = """
    #launch-choices { height: auto; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        yield from self.compose_head(1)
        yield Label("New Investigation", classes="title")
        yield Static("Pick how to feed this investigation.", classes="hint-bar")
        with Horizontal(id="launch-choices"):
            with ClickableCard(classes="launch-card", on_activate=self.action_analyze_file):
                yield Static("📄", classes="launch-icon")
                yield Label("(F) Analyze a log file", classes="launch-label")
                yield Static("Load a Splunk .json/.csv export", classes="launch-hint")
            with ClickableCard(classes="launch-card", on_activate=self.action_live_analyze):
                yield Static("⚡", classes="launch-icon")
                yield Label("(L) Live analyze", classes="launch-label")
                yield Static("Run a live SPL query via Splunk", classes="launch-hint")
        yield from self.compose_foot()

    def action_analyze_file(self) -> None:
        self._log("analyze_file")
        self.app.push_screen(FilePickerScreen())

    def action_live_analyze(self) -> None:
        self._log("live_analyze")
        self.app.push_screen(LiveAnalyzeScreen())


class ConfigScreen(CustomScreen):
    """Set SPLUNK_URL from inside the app instead of hand-editing .env —
    reached via HomeScreen's 'c' binding. Writes to .env (so it
    survives a restart) AND mutates splunk.config.SPLUNK_URL + os.environ
    directly so it takes effect for the rest of this session without one —
    auth.py/client.py read config.SPLUNK_URL as a live module attribute
    (not a frozen `from ... import SPLUNK_URL` binding) specifically so
    this works."""

    BINDINGS = [
        ("s", "save", "Save"),
        ("escape", "app.pop_screen", "Cancel"),
    ]

    CSS = """
    #config-form { padding: 1 2; }
    #config-form Input { margin-bottom: 1; }
    #config-save-card {
        width: auto; height: auto; margin-top: 1; padding: 0 2;
        border: round $accent 50%; background: $panel;
        align: center middle;
    }
    #config-save-card:focus, #config-save-card:hover { border: round $accent; }
    #config-save-label { width: auto; text-style: bold; color: $accent; }
    #config-status { margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        from splunk import config

        yield from self.compose_head(0)
        with Vertical(id="config-form"):
            yield Label("Splunk URL")
            yield Input(
                value=config.SPLUNK_URL,
                placeholder="https://splunk.example.com:8089",
                id="splunk-url-input",
            )
            with ClickableCard(id="config-save-card", on_activate=self.action_save):
                yield Static("💾 Save (s)", id="config-save-label")
            yield StatusChip("", id="config-status")
        yield from self.compose_foot()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "splunk-url-input":
            return
        self.action_save()

    def action_save(self) -> None:
        from splunk import config

        url = self.query_one("#splunk-url-input", Input).value.strip().rstrip("/")
        status = self.query_one("#config-status", StatusChip)
        if not url:
            status.set_status("failure", "SPLUNK_URL can't be empty.")
            return

        config.SPLUNK_URL = url
        os.environ["SPLUNK_URL"] = url
        self._write_env_var("SPLUNK_URL", url)
        self._log("saved", splunk_url=url)
        status.set_status("success", f"Saved — SPLUNK_URL={url}")

    @staticmethod
    def _write_env_var(key: str, value: str) -> None:
        """Create/update a KEY=value line in .env, preserving every other
        line untouched — same file client.py/auth.py's load_dotenv() reads
        on the next run."""
        env_path = Path.cwd() / ".env"
        lines = env_path.read_text().splitlines() if env_path.exists() else []
        prefix = f"{key}="
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                lines[i] = f"{key}={value}"
                env_path.write_text("\n".join(lines) + "\n")
                return
        lines.append(f"{key}={value}")
        env_path.write_text("\n".join(lines) + "\n")


class FilePickerScreen(CustomScreen):
    """Browser-style file picker for the analyze-file flow — modeled on
    seniordevagent tui/app.py's BrowseScreen(purpose=...) pattern, using
    Textual's built-in DirectoryTree rather than reinventing one."""

    BINDINGS = [("escape", "app.pop_screen", "Cancel")]

    CSS = """
    #picker-status {
        height: 3;
        padding: 0 1;
        border: solid $accent;
    }
    DirectoryTree {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield from self.compose_head(1)
        yield StatusChip("Select a .json or .csv Splunk export", id="picker-status")
        yield DirectoryTree(str(Path.cwd()), id="file-tree")
        yield from self.compose_foot()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        path = str(event.path)
        if not (path.endswith(".json") or path.endswith(".csv")):
            status = self.query_one("#picker-status", StatusChip)
            status.set_status("failure", f"Not a .json/.csv file: {path}")
            return
        self._log("selected", path=path)
        self.app.push_screen(RunningScreen(source=path))


class LiveAnalyzeScreen(CustomScreen):
    """SPL query entry for the live-analyze flow. No file picker here —
    a live SPL query has no file input; this goes straight to entering the
    query, then the Playwright SSO login (in RunningScreen)."""

    BINDINGS = [
        ("r", "run", "Run"),
        ("escape", "app.pop_screen", "Cancel"),
    ]

    CSS = """
    #live-form {
        padding: 1 2;
    }
    #live-form Input {
        margin-bottom: 1;
    }
    #run-card {
        width: auto; height: auto; margin-top: 1; padding: 0 2;
        border: round $accent 50%; background: $panel;
        align: center middle;
    }
    #run-card:focus, #run-card:hover {
        border: round $accent;
    }
    #run-card-label { width: auto; text-style: bold; color: $accent; }
    """

    def compose(self) -> ComposeResult:
        yield from self.compose_head(1)
        with Vertical(id="live-form"):
            yield Label("SPL query")
            yield Input(placeholder="index=pki sourcetype=ocsp_error", id="spl-input")
            yield Label("Earliest (default -24h)")
            yield Input(placeholder="-24h", id="earliest-input")
            yield Label("Latest (default now)")
            yield Input(placeholder="now", id="latest-input")
            with ClickableCard(id="run-card", on_activate=self.action_run):
                yield Static("▶ Run Live Analysis (r)", id="run-card-label")
            yield Label(
                "[dim]This triggers a Splunk SSO login in a browser window "
                "if your session has expired.[/]"
            )
        yield from self.compose_foot()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "spl-input":
            return
        self.action_run()

    def action_run(self) -> None:
        spl = self.query_one("#spl-input", Input).value.strip()
        if not spl:
            self.query_one("#spl-input", Input).focus()
            return
        earliest = self.query_one("#earliest-input", Input).value.strip() or "-24h"
        latest = self.query_one("#latest-input", Input).value.strip() or "now"
        self._log("submitted", spl=spl[:60])
        self.app.push_screen(RunningScreen(spl=spl, earliest=earliest, latest=latest))


class RunningScreen(CustomScreen):
    """Shared worker screen for both flows: (live-only) Splunk SSO login,
    then start_investigation, then run_standalone_agent — all in one
    background worker with a live status chip. Pops back to the dashboard
    on completion (success or failure alike; failure just shows the error
    and lets the user retry via 'n' again)."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(
        self,
        *,
        source: str = "",
        spl: str = "",
        earliest: str = "-24h",
        latest: str = "now",
    ) -> None:
        super().__init__()
        self._source = source
        self._spl = spl
        self._earliest = earliest
        self._latest = latest

    def compose(self) -> ComposeResult:
        yield from self.compose_head(2)
        yield StatusChip("Starting…", id="run-status")
        yield from self.compose_foot()

    def on_mount(self) -> None:
        status = self.query_one("#run-status", StatusChip)
        status.set_status("testing", "Starting…")
        self.run_worker(self._run(), thread=False, exclusive=True, name="investigation")

    async def _run(self) -> None:
        from splunk import connector

        status = self.query_one("#run-status", StatusChip)

        # Preflight: the standalone agent needs Ollama + `uv sync --extra llm`.
        # Fail fast with a clear message instead of hanging on the first
        # analyse() call deep inside run_standalone_agent.
        try:
            import splunk.agent  # noqa: F401
        except Exception as exc:
            status.set_status(
                "failure",
                f"Standalone agent unavailable ({exc}). "
                "Run `uv sync --extra llm` and make sure Ollama is running.",
            )
            return

        # Live-analyze: Splunk SSO login before anything else, same
        # status-chip pattern. Skip the browser popup if the existing
        # session cookie is still valid.
        if self._spl:
            status.set_status("testing", "Checking Splunk session…")
            valid = await asyncio.to_thread(self._validate_session)
            if not valid:
                status.set_status("testing", "Opening browser for Splunk SSO login…")
                try:
                    await asyncio.to_thread(self._run_auth_flow)
                except Exception as exc:
                    status.set_status("failure", f"Splunk login failed: {exc}")
                    return
            status.set_status("testing", "Splunk session ready.")

        status.set_status("testing", "Loading events…")
        result = await asyncio.to_thread(
            connector.start_investigation,
            source=self._source, spl=self._spl, earliest=self._earliest, latest=self._latest,
        )
        if "error" in result:
            status.set_status("failure", result["error"])
            return

        run_id = result["run_id"]
        event_count = result["event_count"]
        status.set_status("testing", f"Loaded {event_count} events — running analysis agent…")

        session = connector.get_session(run_id)
        df = session["df"] if session else None
        source_label = result.get("source") or self._source or f"live: {self._spl[:60]}"

        try:
            report, queries = await asyncio.to_thread(
                connector.run_standalone_agent, df, run_id, source_label,
            )
        except Exception as exc:
            status.set_status("failure", f"Analysis agent failed: {exc}")
            from splunk.db import clear_active_run_row

            clear_active_run_row(run_id)
            return

        n = len(queries)
        status.set_status("success", f"Done — {n} follow-up quer{'y' if n == 1 else 'ies'} generated")

        await asyncio.sleep(1.5)
        # Pop all the way back to Home regardless of which flow got here
        # (Home->Launch->Picker/Live->Running is 4 deep; Home->Launch->Live
        # is also possible) — "pop until len==2" previously left an
        # intermediate screen on top for the 4-deep case, so the isinstance
        # check below it could never fire. Push a fresh PrevRunsScreen
        # instead of relying on whatever's left on the stack.
        while len(self.app.screen_stack) > 1:
            self.app.pop_screen()
        self.app.push_screen(PrevRunsScreen(initial_run_id=run_id))

    @staticmethod
    def _validate_session() -> bool:
        from splunk.auth import validate_session

        return validate_session()

    @staticmethod
    def _run_auth_flow() -> None:
        from splunk.auth import run_auth_flow

        run_auth_flow()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class SplunkTUI(App):
    """Textual front end for the Splunk investigation engine."""

    # 'q' must live on the App, not a Screen — a Screen-level Binding whose
    # action isn't defined on that Screen does NOT reliably bubble up to
    # App.action_quit (confirmed: it silently no-ops with a DataTable
    # focused). Screens should never bind "quit" themselves.
    BINDINGS = [("q", "quit", "Quit")]

    # House design ported from Analyze_docker_logs_with_copilot/docker_log_analyzer/tui.py
    # (breadcrumb-bar/chip and card tokens) so both TUIs share one visual language.
    CSS = """
    .title { text-style: bold; margin-bottom: 1; }
    .hint-bar { height: auto; padding: 0 1; color: $text-muted; }

    .breadcrumb-bar { height: auto; padding: 1 2; border-bottom: solid $accent 30%; align: left middle; }
    .breadcrumb-chip { width: auto; height: 3; color: $text-muted; padding: 1 1; }
    .breadcrumb-chip.active { color: $accent; text-style: bold; border: round $accent; padding: 0 1; }
    .breadcrumb-sep { width: auto; height: 3; color: $text-muted; padding: 1 1; }

    .launch-card {
        width: 1fr; height: auto; margin-right: 2; padding: 1 2;
        border: round $accent 50%; background: $panel;
        align: center middle;
    }
    .launch-card:last-of-type { margin-right: 0; }
    .launch-icon { text-align: center; width: 100%; color: $accent; margin-bottom: 1; }
    .launch-label { text-style: bold; text-align: center; width: 100%; }
    .launch-hint { color: $text-muted; text-align: center; width: 100%; }
    """

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())


def main() -> None:
    SplunkTUI().run()


if __name__ == "__main__":
    main()
