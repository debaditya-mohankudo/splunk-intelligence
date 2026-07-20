"""
Terminal UI for the Splunk investigation server. Replaces the old browser dashboard
(splunk/ui/) — talks to the same REST/SSE surface in splunk/server.py that a browser
would, just rendered with Textual instead of Jinja2/htmx.

Requires ./serve.sh (or `uv run uvicorn splunk.server:app ...`) running first.

Usage:
    uv run python -m splunk.tui
"""
from __future__ import annotations

import json
import logging

import requests
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Markdown,
    Static,
)

logger = logging.getLogger(__name__)

BASE_URL = "http://127.0.0.1:8765"
POLL_INTERVAL = 2.0


class Cockpit(Static):
    """Live status strip — mirrors the browser UI's cockpit-strip partial."""

    run_id: reactive[str | None] = reactive(None)
    source: reactive[str] = reactive("")
    iteration: reactive[int] = reactive(0)
    confidence: reactive[str] = reactive("—")
    events: reactive[int | None] = reactive(None)
    paused: reactive[bool] = reactive(False)
    connected: reactive[bool] = reactive(True)

    def render(self) -> str:
        if not self.connected:
            return f"[bold red]Cannot reach {BASE_URL} — is ./serve.sh running?[/]"
        if not self.run_id:
            return "[dim]No active investigation[/]"
        status = "[yellow]PAUSED[/]" if self.paused else "[green]running[/]"
        events_str = self.events if self.events is not None else "—"
        return (
            f"[bold]INVESTIGATING[/] {self.run_id[:8]} · iter {self.iteration} · "
            f"conf [bold]{self.confidence}[/] · {events_str} events · {status}  "
            f"[dim]{self.source}[/]"
        )


class SplunkTUI(App):
    """Textual front end for the Splunk investigation server."""

    CSS = """
    Cockpit {
        height: 3;
        padding: 1;
        border: solid $accent;
        content-align: left middle;
    }
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
    #controls {
        height: 3;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._selected_run_id: str | None = None
        self._sse_run_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Cockpit(id="cockpit")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield DataTable(id="runs-table", cursor_type="row")
            with Vertical(id="detail"):
                yield Markdown("Select a run from the sidebar.", id="report")
                yield DataTable(id="queries")
        with Horizontal(id="controls"):
            yield Input(placeholder="inject analyst hint…", id="hint-input")
            yield Button("Hint", id="hint-btn")
            yield Button("Pause/Resume", id="pause-btn")
        yield Footer()

    def on_mount(self) -> None:
        runs_table = self.query_one("#runs-table", DataTable)
        runs_table.add_columns("Run", "Source", "Confidence", "Created")

        queries_table = self.query_one("#queries", DataTable)
        queries_table.add_columns("Iter", "Area", "SPL", "Rows")

        self.run_worker(self._poll_active_run_loop(), thread=False, exclusive=True, name="poll-active")
        self.action_refresh()

    # -----------------------------------------------------------------
    # Actions
    # -----------------------------------------------------------------

    def action_refresh(self) -> None:
        self.run_worker(self._load_runs(), thread=False, exclusive=False)

    # -----------------------------------------------------------------
    # HTTP helpers (sync requests, called from async workers via to_thread)
    # -----------------------------------------------------------------

    def _get(self, path: str, timeout: float = 5.0) -> dict | None:
        try:
            resp = requests.get(f"{BASE_URL}{path}", timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.warning("GET %s failed: %s", path, exc)
            return None

    def _post(self, path: str, payload: dict | None = None, timeout: float = 5.0) -> dict | None:
        try:
            resp = requests.post(f"{BASE_URL}{path}", json=payload or {}, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.warning("POST %s failed: %s", path, exc)
            return None

    # -----------------------------------------------------------------
    # Workers
    # -----------------------------------------------------------------

    async def _load_runs(self) -> None:
        import asyncio

        data = await asyncio.to_thread(self._get, "/api/runs")
        cockpit = self.query_one("#cockpit", Cockpit)
        if data is None:
            cockpit.connected = False
            return
        cockpit.connected = True

        table = self.query_one("#runs-table", DataTable)
        table.clear()
        for run in data.get("runs", []):
            table.add_row(
                run["run_id"][:8],
                run["source"],
                run["confidence"],
                run["created_at"],
                key=run["run_id"],
            )

    async def _load_run_detail(self, run_id: str) -> None:
        import asyncio

        data = await asyncio.to_thread(self._get, f"/api/runs/{run_id}")
        if data is None:
            return

        report = self.query_one("#report", Markdown)
        await report.update(data.get("report_md") or "*No report yet.*")

        table = self.query_one("#queries", DataTable)
        table.clear()
        for q in data.get("queries", []):
            spl = (q.get("spl") or "")[:80]
            table.add_row(str(q.get("iteration")), q.get("area") or "", spl, str(q.get("result_rows")))

    async def _poll_active_run_loop(self) -> None:
        import asyncio

        while True:
            data = await asyncio.to_thread(self._get, "/api/runs/active")
            cockpit = self.query_one("#cockpit", Cockpit)
            if data is None:
                cockpit.connected = False
            else:
                cockpit.connected = True
                if data.get("status") == "idle":
                    cockpit.run_id = None
                else:
                    cockpit.run_id = data.get("run_id")
                    cockpit.source = data.get("source") or ""
                    cockpit.iteration = data.get("iteration", 0)
                    cockpit.confidence = data.get("confidence", "—")
                    cockpit.events = data.get("events")
                    cockpit.paused = bool(data.get("pause_requested"))

                    if cockpit.run_id and cockpit.run_id != self._sse_run_id:
                        self._sse_run_id = cockpit.run_id
                        self.run_worker(
                            self._stream_run(cockpit.run_id), thread=False, exclusive=True, name="sse"
                        )

            await asyncio.sleep(POLL_INTERVAL)

    async def _stream_run(self, run_id: str) -> None:
        """Consume the SSE stream for one run, pushing live updates into the cockpit."""
        import asyncio

        def _iter_events():
            try:
                with requests.get(
                    f"{BASE_URL}/api/runs/{run_id}/stream", stream=True, timeout=60
                ) as resp:
                    for line in resp.iter_lines(decode_unicode=True):
                        if line and line.startswith("data: "):
                            yield line[len("data: "):]
                        if line and line.startswith("event: done"):
                            yield None
            except requests.RequestException as exc:
                logger.warning("SSE stream for %s failed: %s", run_id, exc)

        def _consume() -> list[dict | None]:
            return list(_iter_events())

        events = await asyncio.to_thread(_consume)
        cockpit = self.query_one("#cockpit", Cockpit)
        for raw in events:
            if raw is None:
                break
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if "iteration" in d:
                cockpit.iteration = d["iteration"]
            if "confidence" in d:
                cockpit.confidence = d["confidence"]
            if "events" in d:
                cockpit.events = d["events"]

        if self._sse_run_id == run_id:
            self._sse_run_id = None
        self.run_worker(self._load_runs(), thread=False, exclusive=False)

    # -----------------------------------------------------------------
    # Event handlers
    # -----------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "runs-table":
            return
        run_id = str(event.row_key.value)
        self._selected_run_id = run_id
        self.run_worker(self._load_run_detail(run_id), thread=False, exclusive=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        import asyncio

        if event.button.id == "hint-btn":
            hint_input = self.query_one("#hint-input", Input)
            hint = hint_input.value.strip()
            if hint:
                self.run_worker(
                    asyncio.to_thread(self._post, "/api/investigate/hint", {"hint": hint}),
                    thread=False,
                    exclusive=False,
                )
                hint_input.value = ""
        elif event.button.id == "pause-btn":
            cockpit = self.query_one("#cockpit", Cockpit)
            path = "/api/investigate/resume" if cockpit.paused else "/api/investigate/pause"
            self.run_worker(asyncio.to_thread(self._post, path), thread=False, exclusive=False)


def main() -> None:
    SplunkTUI().run()


if __name__ == "__main__":
    main()
