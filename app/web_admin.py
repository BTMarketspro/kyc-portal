"""Admin console: review submissions (decrypted on the fly), resend to Yativo, branding settings."""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select

from . import lookups as L, present, services, theme
from .config import settings
from .crypto import cipher
from .db import AdminUser, AuditLog, StoredFile, Submission, audit, put_setting, session_scope, utcnow
from .files import make_thumbnail
from .security import csrf_ok, csrf_token, limiter, link_signature, verify_password
from .templating import templates

router = APIRouter(prefix="/admin")
PAGE_SIZE = 25


class NeedsLogin(Exception):
    pass


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or "unknown"


def current_admin(request: Request) -> str:
    sess = request.session
    email, last = sess.get("admin_email"), sess.get("admin_seen", 0)
    if not email or time.time() - last > settings.admin_session_minutes * 60:
        sess.pop("admin_email", None)
        raise NeedsLogin()
    sess["admin_seen"] = int(time.time())
    return email


async def _form(request: Request) -> dict[str, Any]:
    form = await request.form()
    if not csrf_ok(request.session, form.get("csrf")):
        raise HTTPException(400, "Your form expired. Please go back and try again.")
    return form


def _ctx(request: Request, s, admin: str | None, **extra) -> dict[str, Any]:
    t, b = theme.get_theme(s), theme.get_branding(s)
    return {"request": request, "admin": admin, "csrf": csrf_token(request.session), "theme": t, "branding": b,
            "css_vars": theme.css_vars(t, b), "status_labels": services.STATUS_LABELS, **extra}


# ------------------------------------------------------------------------- auth

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    with session_scope() as s:
        return templates.TemplateResponse(request, "admin/login.html", _ctx(request, s, None, error=None))


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request):
    form = await _form(request)
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    ip = _ip(request)
    with session_scope() as s:
        if not limiter.allow(f"login:{ip}", 8, 900) or not limiter.allow(f"login:{email}", 8, 900):
            return templates.TemplateResponse(request, "admin/login.html",
                                              _ctx(request, s, None, error="Too many attempts. Try again in 15 minutes."),
                                              status_code=429)
        user = s.scalars(select(AdminUser).where(AdminUser.email == email)).first()
        ok = await run_in_threadpool(verify_password, password, user.password_hash if user else "scrypt$AAAA$AAAA")
        if not user or not user.is_active or not ok:
            audit(s, email or "?", "login_failed", ip=ip)
            return templates.TemplateResponse(request, "admin/login.html",
                                              _ctx(request, s, None, error="Incorrect email or password."),
                                              status_code=401)
        user.last_login_at = utcnow()
        audit(s, email, "login", ip=ip)
    request.session.clear()
    request.session.update({"admin_email": email, "admin_seen": int(time.time())})
    return RedirectResponse("/admin", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    await _form(request)
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


# ------------------------------------------------------------------------- list

@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "", flow: str = "", q: str = "", page: int = 1):
    admin = current_admin(request)
    page = max(1, page)
    with session_scope() as s:
        stmt = select(Submission).where(Submission.payload_enc.is_not(None))
        if status:
            stmt = stmt.where(Submission.status == status)
        else:
            stmt = stmt.where(Submission.status != "draft")
        if flow in ("individual", "business"):
            stmt = stmt.where(Submission.flow == flow)
        if q.strip():
            term = q.strip()
            bidx = cipher().blind_index(term)
            stmt = stmt.where(or_(Submission.customer_id == term, Submission.email_bidx == bidx,
                                  Submission.name_bidx == bidx))
        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = s.scalars(stmt.order_by(Submission.updated_at.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
        items = [{"sub": r, "summary": services.read_summary(r)} for r in rows]
        counts = dict(s.execute(select(Submission.status, func.count()).group_by(Submission.status)).all())
        ctx = _ctx(request, s, admin, items=items, counts=counts, total=total, page=page,
                   pages=max(1, -(-total // PAGE_SIZE)), f_status=status, f_flow=flow, q=q,
                   qs=lambda **kw: urlencode({k: v for k, v in {"status": status, "flow": flow, "q": q, **kw}.items() if v}))
        return templates.TemplateResponse(request, "admin/list.html", ctx)


# ------------------------------------------------------------------------- detail

@router.get("/submissions/{sub_id}", response_class=HTMLResponse)
def detail(request: Request, sub_id: int):
    admin = current_admin(request)
    with session_scope() as s:
        sub = s.get(Submission, sub_id)
        if sub is None:
            raise HTTPException(404)
        data = services.read_payload(sub)
        response = services.read_response(sub)
        refs = services.file_refs(data)
        files = {f.id: services.file_meta(f) for f in services.owned_files(s, refs, sub.customer_id, sub.flow).values()}
        audit(s, admin, "view_submission", str(sub.id), ip=_ip(request))
        history = s.scalars(select(AuditLog).where(AuditLog.target == str(sub.id))
                            .order_by(AuditLog.id.desc()).limit(30)).all()
        body = (response or {}).get("body") if isinstance(response, dict) else None
        ctx = _ctx(request, s, admin, sub=sub, summary=services.read_summary(sub),
                   sections=present.sections(sub.flow, data), files=files, response=response, body=body or {},
                   raw_payload=json.dumps(data, indent=2, ensure_ascii=False),
                   raw_outbound=json.dumps((response or {}).get("outbound"), indent=2, ensure_ascii=False) if response else "",
                   raw_response=json.dumps(body, indent=2, ensure_ascii=False) if body is not None else "",
                   history=history)
        return templates.TemplateResponse(request, "admin/detail.html", ctx)


@router.get("/files/{file_id}")
def file_download(request: Request, file_id: str, thumb: int = 0, download: int = 0):
    admin = current_admin(request)
    with session_scope() as s:
        f = s.get(StoredFile, file_id)
        if f is None:
            raise HTTPException(404)
        data = services.read_file(f)
        meta = services.file_meta(f)
        if not thumb:
            audit(s, admin, "download_file" if download else "view_file", str(f.submission_id or ""), f.id, _ip(request))
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if thumb:
        t = make_thumbnail(data, f.mime, 640)
        if t is None:
            raise HTTPException(404)
        return Response(t, media_type="image/jpeg", headers=headers)
    disp = "attachment" if download else "inline"
    safe = "".join(ch for ch in meta["name"] if ch.isalnum() or ch in "._- ")[:100] or f"document.{f.ext}"
    if not safe.lower().endswith("." + f.ext):
        safe = f"{safe.rsplit('.', 1)[0]}.{f.ext}"
    headers["Content-Disposition"] = f'{disp}; filename="{safe}"'
    return Response(data, media_type=f.mime, headers=headers)


@router.post("/submissions/{sub_id}/resend")
async def resend(request: Request, sub_id: int):
    admin = current_admin(request)
    await _form(request)

    def work() -> str:
        with session_scope() as s:
            sub = s.get(Submission, sub_id)
            if sub is None:
                raise HTTPException(404)
            result = services.deliver(s, sub)
            audit(s, admin, "resend", str(sub.id), f"outcome={result.outcome} http={result.status_code}", _ip(request))
            return result.outcome

    outcome = await run_in_threadpool(work)
    return RedirectResponse(f"/admin/submissions/{sub_id}?flash={outcome}", status_code=303)


@router.post("/submissions/{sub_id}/reopen")
async def reopen(request: Request, sub_id: int):
    """Let the customer edit and resubmit through their original link."""
    admin = current_admin(request)
    await _form(request)
    with session_scope() as s:
        sub = s.get(Submission, sub_id)
        if sub is None:
            raise HTTPException(404)
        sub.locked = False
        sub.status = "needs_correction" if sub.status != "draft" else "draft"
        audit(s, admin, "reopen", str(sub.id), ip=_ip(request))
    return RedirectResponse(f"/admin/submissions/{sub_id}?flash=reopened", status_code=303)


@router.post("/submissions/{sub_id}/delete")
async def delete(request: Request, sub_id: int):
    admin = current_admin(request)
    form = await _form(request)
    with session_scope() as s:
        sub = s.get(Submission, sub_id)
        if sub is None:
            raise HTTPException(404)
        if str(form.get("confirm", "")).strip() != sub.customer_id:
            return RedirectResponse(f"/admin/submissions/{sub_id}?flash=confirm_mismatch", status_code=303)
        for f in s.scalars(select(StoredFile).where(StoredFile.customer_id == sub.customer_id,
                                                    StoredFile.flow == sub.flow)).all():
            services.delete_file(s, f)
        audit(s, admin, "delete_submission", str(sub.id), f"customer_id={sub.customer_id}", _ip(request))
        s.delete(sub)
    return RedirectResponse("/admin?flash=deleted", status_code=303)


# ------------------------------------------------------------------------- settings

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0):
    admin = current_admin(request)
    with session_scope() as s:
        ctx = _ctx(request, s, admin, presets=theme.PRESETS, color_fields=theme.COLOR_FIELDS, fonts=theme.FONTS,
                   accounts=theme.get_accounts(s), account_labels=theme.ACCOUNT_LABELS, saved=saved,
                   yativo_base=settings.yativo_base_url, address_provider="Google Places" if settings.google_maps_api_key
                   else "OpenStreetMap (free, no key)")
        return templates.TemplateResponse(request, "admin/settings.html", ctx)


@router.post("/settings")
async def save_settings(request: Request):
    admin = current_admin(request)
    form = await _form(request)
    with session_scope() as s:
        theme.save_theme(s, {k: str(form.get(f"color_{k}", "")).strip() for k in theme.COLOR_FIELDS})
        theme.save_branding(s, {k: form.get(k, "") for k in theme.DEFAULT_BRANDING})
        accounts = {k: {"enabled": form.get(f"acc_{k}_enabled") == "on", "default": form.get(f"acc_{k}_default") == "on"}
                    for k in theme.DEFAULT_ACCOUNTS}
        put_setting(s, "accounts", accounts)
        audit(s, admin, "update_settings", ip=_ip(request))
    return RedirectResponse("/admin/settings?saved=1", status_code=303)


@router.post("/settings/refresh-lookups")
async def refresh_lookups(request: Request):
    admin = current_admin(request)
    await _form(request)
    L.clear_cache()
    with session_scope() as s:
        audit(s, admin, "refresh_lookups", ip=_ip(request))
    return RedirectResponse("/admin/settings?saved=2", status_code=303)


# ------------------------------------------------------------------------- links & audit

@router.get("/links", response_class=HTMLResponse)
def links(request: Request, customer_id: str = "", customer_type: str = "individual"):
    admin = current_admin(request)
    link = ""
    cid = customer_id.strip()
    flow = customer_type if customer_type in ("individual", "business") else "individual"
    if cid:
        params = {"customer_type": flow, "customer_id": cid}
        if settings.link_signing_secret:
            params["sig"] = link_signature(flow, cid)
        link = f"{settings.base_url}/verify?{urlencode(params)}"
    with session_scope() as s:
        ctx = _ctx(request, s, admin, link=link, customer_id=cid, customer_type=flow,
                   signed=bool(settings.link_signing_secret))
        return templates.TemplateResponse(request, "admin/links.html", ctx)


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, page: int = 1):
    admin = current_admin(request)
    page = max(1, page)
    with session_scope() as s:
        rows = s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).offset((page - 1) * 50).limit(50)).all()
        return templates.TemplateResponse(request, "admin/audit.html", _ctx(request, s, admin, rows=rows, page=page))
