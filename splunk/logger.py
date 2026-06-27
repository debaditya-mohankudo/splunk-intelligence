"""
Structured pipeline logger — JSON-lines to logs/<run_id>.jsonl.
Each log entry is a flat JSON object with at minimum: ts, level, run_id, event, plus arbitrary kwargs.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG").upper()

# stdlib logger for console output
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
_console = logging.getLogger("splunk")


class RunLogger:
    """
    Scoped logger for a single pipeline run.
    Writes JSON-lines to logs/<run_id>.jsonl and echoes to console.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._path = _LOG_DIR / f"{run_id}.jsonl"
        self._fh = self._path.open("a", encoding="utf-8")

    # ------------------------------------------------------------------
    # Public logging methods
    # ------------------------------------------------------------------

    def info(self, event: str, **kwargs: Any) -> None:
        self._write("INFO", event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._write("WARNING", event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._write("ERROR", event, **kwargs)

    def debug(self, event: str, **kwargs: Any) -> None:
        if _LOG_LEVEL == "DEBUG":
            self._write("DEBUG", event, **kwargs)

    # ------------------------------------------------------------------
    # Lifecycle helpers — called by runner.py at each stage
    # ------------------------------------------------------------------

    def parse_done(self, event_count: int, source: str, fmt: str) -> None:
        self.info("parse.done", event_count=event_count, source=source, format=fmt)

    def detect_done(self, findings: dict[str, Any]) -> None:
        self.info(
            "detect.done",
            spikes=len(findings.get("spikes", [])),
            cert_anomalies=len(findings.get("cert_anomalies", [])),
            patterns=len(findings.get("patterns", [])),
            correlations=len(findings.get("correlations", [])),
            event_count=findings.get("event_count", 0),
        )

    def agent_done(self, model: str, iterations: int, report_captured: bool) -> None:
        self.info(
            "agent.done",
            model=model,
            iterations=iterations,
            report_captured=report_captured,
        )

    def report_written(self, path: str, size_bytes: int) -> None:
        self.info("report.written", path=path, size_bytes=size_bytes)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write(self, level: str, event: str, **kwargs: Any) -> None:
        entry = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "level": level,
            "run_id": self.run_id,
            "event": event,
            **kwargs,
        }
        self._fh.write(json.dumps(entry, default=str) + "\n")
        self._fh.flush()
        getattr(_console, level.lower(), _console.info)(
            "[%s] %s %s", self.run_id[:8], event,
            " ".join(f"{k}={v}" for k, v in kwargs.items()),
        )

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
