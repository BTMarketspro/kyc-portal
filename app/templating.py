from __future__ import annotations

import json
from datetime import datetime

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .config import BASE_DIR

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _tojson_attr(value) -> Markup:
    # Safe for embedding inside <script type="application/json">.
    raw = json.dumps(value, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return Markup(raw)


def _dt(value: datetime | None) -> str:
    if not value:
        return "—"
    return value.strftime("%d %b %Y, %H:%M UTC")


def _filesize(n: int | None) -> str:
    if not n:
        return "—"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


templates.env.filters["jsonscript"] = _tojson_attr
templates.env.filters["pairs"] = lambda values: [(v, v) for v in values]
templates.env.filters["dt"] = _dt
templates.env.filters["filesize"] = _filesize
