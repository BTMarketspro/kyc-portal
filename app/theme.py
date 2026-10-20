"""Admin-editable branding: colour scheme, logo, copy and which account options customers see."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from .db import get_setting, put_setting

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

COLOR_FIELDS = {
    "primary": "Primary (buttons, links, progress)",
    "primary_contrast": "Text on primary",
    "accent": "Success / completed steps",
    "background": "Page background",
    "surface": "Card background",
    "text": "Main text",
    "muted": "Secondary text",
    "border": "Borders & inputs",
    "danger": "Errors",
}

PRESETS: dict[str, dict[str, str]] = {
    "Ocean (default)": {
        "primary": "#1F5EFF", "primary_contrast": "#FFFFFF", "accent": "#12B76A", "background": "#F3F6FB",
        "surface": "#FFFFFF", "text": "#0F172A", "muted": "#64748B", "border": "#E2E8F0", "danger": "#E5484D",
    },
    "Emerald": {
        "primary": "#0E9F6E", "primary_contrast": "#FFFFFF", "accent": "#0E9F6E", "background": "#F1F7F4",
        "surface": "#FFFFFF", "text": "#0B1F17", "muted": "#5B6B63", "border": "#DCE7E1", "danger": "#DC3545",
    },
    "Violet": {
        "primary": "#6D4AFF", "primary_contrast": "#FFFFFF", "accent": "#16A34A", "background": "#F5F3FF",
        "surface": "#FFFFFF", "text": "#1E1B4B", "muted": "#6B7280", "border": "#E5E1F8", "danger": "#E11D48",
    },
    "Sunset": {
        "primary": "#F25C2A", "primary_contrast": "#FFFFFF", "accent": "#15A35A", "background": "#FFF7F2",
        "surface": "#FFFFFF", "text": "#2B1A12", "muted": "#7C6A60", "border": "#F1E1D7", "danger": "#D92D20",
    },
    "Graphite": {
        "primary": "#111827", "primary_contrast": "#FFFFFF", "accent": "#16A34A", "background": "#F4F4F5",
        "surface": "#FFFFFF", "text": "#111827", "muted": "#6B7280", "border": "#E4E4E7", "danger": "#DC2626",
    },
    "Midnight (dark)": {
        "primary": "#4C8DFF", "primary_contrast": "#FFFFFF", "accent": "#34D399", "background": "#0B1220",
        "surface": "#121A2B", "text": "#E6EAF2", "muted": "#94A3B8", "border": "#24304A", "danger": "#F87171",
    },
}

DEFAULT_BRANDING: dict[str, Any] = {
    "brand_name": "Identity Verification",
    "logo_url": "",
    "support_email": "",
    "radius": 12,
    "font": "Inter",
    "welcome_title": "Let's verify your identity",
    "welcome_text": "It only takes a few minutes. Have your ID document and a recent bank statement ready.",
    "success_title": "Verification submitted",
    "success_text": "Thank you. Your details were submitted successfully and are now under review. "
                    "We'll notify you once the review is complete.",
    "return_url": "",
}

FONTS = ["Inter", "Manrope", "DM Sans", "Plus Jakarta Sans", "Poppins", "IBM Plex Sans", "Nunito Sans"]

# Which virtual accounts customers may request, and the default selection.
# EUR (DE) is closed to customers created after 2026-09-02, so it is off by default.
DEFAULT_ACCOUNTS: dict[str, dict[str, bool]] = {
    "usd": {"enabled": True, "default": True},
    "eur": {"enabled": True, "default": False},
    "gbp": {"enabled": True, "default": False},
    "eurde": {"enabled": False, "default": False},
}
ACCOUNT_LABELS = {
    "usd": ("USD account", "Receive US dollars via ACH and wire."),
    "eur": ("EUR account", "Receive euros via SEPA."),
    "gbp": ("GBP account", "Receive pounds via Faster Payments. An additional verification fee may apply."),
    "eurde": ("EUR (DE) account", "German IBAN. Cannot be combined with USD or EUR."),
}


def get_theme(s: Session) -> dict[str, str]:
    stored = get_setting(s, "theme", {}) or {}
    base = dict(PRESETS["Ocean (default)"])
    base.update({k: v for k, v in stored.items() if k in COLOR_FIELDS and isinstance(v, str) and HEX.match(v)})
    return base


def save_theme(s: Session, values: dict[str, str]) -> dict[str, str]:
    clean = {k: v for k, v in values.items() if k in COLOR_FIELDS and HEX.match(v or "")}
    put_setting(s, "theme", clean)
    return clean


def get_branding(s: Session) -> dict[str, Any]:
    out = dict(DEFAULT_BRANDING)
    out.update(get_setting(s, "branding", {}) or {})
    return out


def save_branding(s: Session, values: dict[str, Any]) -> None:
    clean: dict[str, Any] = {}
    for k in DEFAULT_BRANDING:
        if k not in values:
            continue
        v = values[k]
        if k == "radius":
            try:
                v = max(0, min(24, int(v)))
            except (TypeError, ValueError):
                v = DEFAULT_BRANDING["radius"]
        elif k == "font":
            v = v if v in FONTS else "Inter"
        elif k in {"logo_url", "return_url"}:
            v = str(v).strip()
            if v and not re.match(r"^https://", v):
                v = ""
        else:
            v = str(v).strip()[:600]
        clean[k] = v
    put_setting(s, "branding", clean)


def get_accounts(s: Session) -> dict[str, dict[str, bool]]:
    stored = get_setting(s, "accounts", {}) or {}
    out = {k: dict(v) for k, v in DEFAULT_ACCOUNTS.items()}
    for k, v in stored.items():
        if k in out and isinstance(v, dict):
            out[k].update({kk: bool(vv) for kk, vv in v.items() if kk in ("enabled", "default")})
    return out


def allowed_accounts(s: Session) -> dict[str, bool]:
    return {k: v["enabled"] for k, v in get_accounts(s).items()}


def css_vars(theme: dict[str, str], branding: dict[str, Any]) -> str:
    lines = [f"--c-{k.replace('_', '-')}: {v};" for k, v in theme.items()]
    lines.append(f"--radius: {int(branding.get('radius', 12))}px;")
    lines.append(f"--font: '{branding.get('font', 'Inter')}', system-ui, -apple-system, 'Segoe UI', sans-serif;")
    return ":root{" + "".join(lines) + "}"


def is_dark(theme: dict[str, str]) -> bool:
    h = theme.get("background", "#FFFFFF").lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128
