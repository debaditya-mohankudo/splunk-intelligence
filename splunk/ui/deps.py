"""Shared dependencies for Splunk UI routes."""
from __future__ import annotations

from pathlib import Path

import jinja2
from fastapi.responses import HTMLResponse

_SPLUNK_DIR = Path(__file__).parent.parent
TEMPLATES_DIR = _SPLUNK_DIR / "templates"
STATIC_DIR = _SPLUNK_DIR / "static"

JINJA_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=True,
    auto_reload=True,
)

JINJA_ENV.globals["urls"] = {
    "runs":    "/ui/runs/",
    "cockpit": "/ui/cockpit",
    "stream":  "/ui/runs/{run_id}/stream",
}


def render(template_name: str, **ctx) -> HTMLResponse:
    t = JINJA_ENV.get_template(template_name)
    return HTMLResponse(t.render(**ctx))
