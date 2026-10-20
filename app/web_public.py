"""Customer-facing pages and JSON API used by the verification wizard."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from sqlalchemy import select

from . import address, lookups as L, mailer, services, theme, validation
from .config import settings
from .db import EmailCode, StoredFile, Submission, latest_submission, session_scope, utcnow
from .files import FileRejected, make_thumbnail, normalize_upload
from .security import device_hash, issue_customer_token, limiter, link_is_valid, new_device_id, read_customer_token
from .templating import templates

log = logging.getLogger("kyc.public")
router = APIRouter()

CUSTOMER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-:.]{0,127}$")
FLOW_ALIASES = {
    "individual": "individual", "personal": "individual", "person": "individual", "kyc": "individual",
    "business": "business", "company": "business", "corporate": "business", "kyb": "business",
}
DEVICE_COOKIE = "kyc_dev"

CUSTOMER_MESSAGES = {
    "submitted": ("success", None, None),
    "submitting": ("pending", "We're processing your submission",
                   "Your details have been received and are being processed. You can close this page."),
    "delivery_failed": ("pending", "We've received your details",
                        "Your information is safely stored and will be processed shortly. There's nothing else you need to do."),
    "duplicate": ("error", "We couldn't complete your verification",
                  "These details are already linked to another account. Please contact support so we can help."),
    "blocked": ("error", "We couldn't accept this application",
                "Please contact support for assistance."),
    "not_provisioned": ("pending", "Your account isn't ready yet",
                        "Your details have been saved, but your business account is still being set up. "
                        "We'll complete the verification as soon as it's ready. Please contact support if this persists."),
}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or "unknown"


def _branding_ctx(s) -> dict[str, Any]:
    t = theme.get_theme(s)
    b = theme.get_branding(s)
    return {"theme": t, "branding": b, "css_vars": theme.css_vars(t, b), "dark": theme.is_dark(t)}


def _resolve_params(request: Request) -> tuple[str | None, str | None]:
    q = request.query_params
    raw_flow = (q.get("customer_type") or q.get("type") or q.get("customerType") or q.get("account_type") or "").strip().lower()
    cid = (q.get("customer_id") or q.get("customerId") or q.get("id") or "").strip()
    return FLOW_ALIASES.get(raw_flow), cid


def _error_page(request: Request, s, title: str, message: str, code: int = 400) -> HTMLResponse:
    ctx = {"request": request, **_branding_ctx(s), "kind": "error", "title": title, "message": message}
    return templates.TemplateResponse(request, "status.html", ctx, status_code=code)


@router.get("/", include_in_schema=False)
def root(request: Request):
    if request.query_params:
        return RedirectResponse(f"/verify?{request.url.query}")
    with session_scope() as s:
        return _error_page(request, s, "Verification link required",
                           "Please open the verification link you received to get started.", 404)


@router.get("/verify", response_class=HTMLResponse)
def verify_page(request: Request, preview: str | None = None):
    flow, cid = _resolve_params(request)
    with session_scope() as s:
        if preview == "1" and not cid:
            flow, cid = flow or "individual", "preview"
        if not flow:
            return _error_page(request, s, "Invalid verification link",
                               "The link is missing the customer type. Please use the link you were sent.")
        if not cid or not CUSTOMER_ID_RE.match(cid):
            return _error_page(request, s, "Invalid verification link",
                               "The link is missing a valid customer reference. Please use the link you were sent.")
        if cid != "preview" and not link_is_valid(flow, cid, request.query_params.get("sig")):
            return _error_page(request, s, "Invalid verification link",
                               "This link is invalid or has been altered. Please use the link you were sent.", 403)

        device = request.cookies.get(DEVICE_COOKIE) or new_device_id()
        dev_hash = device_hash(device)
        ctx_brand = _branding_ctx(s)

        sub = latest_submission(s, cid, flow) if cid != "preview" else None
        if sub is not None and sub.locked:
            kind, title, message = CUSTOMER_MESSAGES.get(sub.status, ("pending", None, None))
            b = ctx_brand["branding"]
            ctx = {"request": request, **ctx_brand, "kind": kind,
                   "title": title or b["success_title"], "message": message or b["success_text"]}
            resp = templates.TemplateResponse(request, "status.html", ctx)
            return _with_device(resp, device)

        draft, server_errors = {}, {}
        if sub is not None and sub.device_hash == dev_hash and sub.payload_enc:
            try:
                draft = services.read_payload(sub)
            except Exception:  # noqa: BLE001
                draft = {}
            if sub.status == "needs_correction":
                resp_data = services.read_response(sub) or {}
                body = resp_data.get("body") if isinstance(resp_data, dict) else {}
                server_errors = (body or {}).get("validation_errors") or {}

        # A returning customer on a new browser (or after their old draft was submitted) can load
        # the details they saved before — but only after proving they own the email on file.
        prefill = None
        if not draft and cid != "preview":
            source = _prefill_source(s, cid, flow)
            if source is not None:
                prefill = {"email": _mask_email(services.read_summary(source)["email"])}

        try:
            boot = _bootstrap(s, flow, cid, device, draft, server_errors)
            boot["prefill"] = prefill
        except L.LookupUnavailable:
            return _error_page(request, s, "Service temporarily unavailable",
                               "We can't load the verification form right now. Please try again in a few minutes.", 503)
        ctx = {"request": request, **ctx_brand, "boot": boot, "flow": flow}
        resp = templates.TemplateResponse(request, "kyc.html", ctx)
        return _with_device(resp, device)


def _prefill_source(s, cid: str, flow: str):
    """Most recent saved application for this customer that has an email we can verify."""
    rows = s.scalars(select(Submission).where(Submission.customer_id == cid, Submission.flow == flow,
                                              Submission.payload_enc.is_not(None))
                     .order_by(Submission.id.desc()).limit(5)).all()
    for row in rows:
        if services.read_summary(row).get("email"):
            return row
    return None


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    name, _, tld = domain.rpartition(".")
    mask = lambda x, keep: x[:keep] + "•" * min(5, max(2, len(x) - keep))  # noqa: E731
    return f"{mask(local, 2)}@{mask(name, 1)}.{tld}" if name else mask(email, 2)


def _code_hash(code_id_seed: str, code: str) -> str:
    return hmac.new(settings.secret_key.encode(), f"{code_id_seed}:{code}".encode(), hashlib.sha256).hexdigest()


def _with_device(resp: Response, device: str) -> Response:
    resp.set_cookie(DEVICE_COOKIE, device, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax",
                    secure=settings.session_https_only)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _bootstrap(s, flow: str, cid: str, device: str, draft: dict, server_errors: dict) -> dict[str, Any]:
    accounts = theme.get_accounts(s)
    enums: dict[str, Any] = {
        "genders": L.GENDERS,
        "employment_status": L.EMPLOYMENT_STATUS,
        "immigration_status": L.IMMIGRATION_STATUS,
        "business_types": L.BUSINESS_TYPES,
        "document_purposes": L.DOCUMENT_PURPOSES,
        "high_risk_activities": L.HIGH_RISK_ACTIVITIES,
        "uploaded_document_types": L.UPLOADED_DOCUMENT_TYPES,
        "extra_document_types": L.EXTRA_DOCUMENT_TYPES,
        "sender_geographies": L.SENDER_GEOGRAPHIES,
        "sender_industries": L.SENDER_INDUSTRIES,
        "sender_types": L.SENDER_TYPES,
        "beneficiary_types": L.BENEFICIARY_TYPES,
        "payout_methods": L.PAYOUT_METHODS,
        "meeting_modes": L.MEETING_MODES,
        "no_income_tax_countries": sorted(L.NO_INCOME_TAX_COUNTRIES),
        "non_photo_id_types": sorted(L.NON_PHOTO_ID_TYPES),
        "banned_name_words": list(L.BANNED_NAME_WORDS),
        "banned_email_words": list(L.BANNED_EMAIL_WORDS),
    }
    if flow == "individual":
        enums["account_purposes"] = L.individual_account_purposes()
        enums["source_of_funds"] = L.individual_source_of_funds()
        enums["expected_monthly"] = L.expected_monthly_payments()
    else:
        enums["account_purposes"] = L.business_account_purposes()
        enums["source_of_funds"] = L.business_source_of_funds()
        enums["currencies"] = L.transaction_currencies()
        enums["transaction_countries"] = L.transaction_countries()
    return {
        "flow": flow,
        "customer_id": cid,
        "token": issue_customer_token(cid, flow, device),
        "preview": cid == "preview",
        "countries": L.countries(),
        "calling_codes": L.calling_codes(),
        "enums": enums,
        "accounts": {k: {**v, "label": theme.ACCOUNT_LABELS[k][0], "hint": theme.ACCOUNT_LABELS[k][1]}
                     for k, v in accounts.items()},
        "address_provider": address.provider(),
        "file_limits": {"min": settings.file_min_bytes, "max": settings.file_max_bytes,
                        "raw_max": settings.max_raw_upload_bytes},
        "draft": draft,
        "server_errors": server_errors,
    }


# ------------------------------------------------------------------------- API helpers

def _auth(request: Request, token: str | None) -> dict:
    data = read_customer_token(token or "")
    if not data:
        raise HTTPException(401, "Your session has expired. Please reload the page.")
    device = request.cookies.get(DEVICE_COOKIE) or ""
    if not device or device_hash(device) != data.get("dev"):
        raise HTTPException(401, "Your session has expired. Please reload the page.")
    if data.get("cid") == "preview":
        raise HTTPException(403, "This is a preview. Submissions are disabled.")
    return data


def _rate(key: str, limit: int, window: int) -> None:
    if not limiter.allow(key, limit, window):
        raise HTTPException(429, "Too many requests. Please wait a moment and try again.")


def _locked_or_draft(s, auth: dict, request: Request):
    latest = latest_submission(s, auth["cid"], auth["flow"])
    if latest is not None and latest.locked:
        raise HTTPException(409, "This verification has already been submitted. Please reload the page.")
    sub = services.get_or_create_draft(s, auth["cid"], auth["flow"], auth["dev"], _client_ip(request),
                                       request.headers.get("user-agent"))
    if sub.device_hash != auth["dev"]:
        # Another browser started this customer's draft earlier; this one takes over.
        sub.device_hash = auth["dev"]
    return sub


# ------------------------------------------------------------------------- lookups

@router.get("/api/lookups/{name}")
def api_lookup(name: str, request: Request, country: str | None = None):
    _rate(f"lookup:{_client_ip(request)}", 300, 60)
    try:
        if name == "subdivisions":
            data: Any = L.subdivisions(country or "")
        elif name == "id-types":
            data = L.photo_id_types(country or "")
        elif name == "postal":
            data = L.postal_format(country or "")
        elif name == "occupations":
            data = L.occupations()
        elif name == "industries":
            data = L.business_industries()
        else:
            raise HTTPException(404, "Unknown lookup")
    except ValueError:
        raise HTTPException(422, "Invalid country") from None
    except L.LookupUnavailable:
        raise HTTPException(503, "Lookup temporarily unavailable") from None
    return JSONResponse(data, headers={"Cache-Control": "public, max-age=3600"})


# ------------------------------------------------------------------------- address autocomplete

@router.get("/api/address/autocomplete")
def api_address_autocomplete(request: Request, q: str = "", country: str = "", session: str = "",
                             x_kyc_token: str | None = Header(None)):
    auth = _auth(request, x_kyc_token)
    _rate(f"addr:{auth['dev']}", 240, 600)
    try:
        return {"provider": address.provider(), "results": address.autocomplete(q[:200], country, session[:64])}
    except Exception as exc:  # noqa: BLE001
        log.warning("Address autocomplete failed: %s", exc)
        return {"provider": address.provider(), "results": [], "error": "Address search is unavailable. Please type your address."}


@router.get("/api/address/details")
def api_address_details(request: Request, id: str, country: str = "", session: str = "",
                        x_kyc_token: str | None = Header(None)):
    auth = _auth(request, x_kyc_token)
    _rate(f"addr:{auth['dev']}", 240, 600)
    try:
        return {"address": address.details(id[:300], country, session[:64])}
    except Exception as exc:  # noqa: BLE001
        log.warning("Address details failed: %s", exc)
        return {"address": None}


# ------------------------------------------------------------------------- uploads

@router.post("/api/upload")
async def api_upload(request: Request, file: UploadFile = File(...), field: str = Form(""),
                     x_kyc_token: str | None = Header(None)):
    auth = _auth(request, x_kyc_token)
    _rate(f"upload:{auth['dev']}", 80, 600)
    chunks, total = [], 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.max_raw_upload_bytes:
            raise HTTPException(413, f"This file is larger than {settings.max_raw_upload_bytes // (1024 * 1024)} MB.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        nf = await run_in_threadpool(normalize_upload, raw, file.filename or "", file.content_type or "")
    except FileRejected as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception:  # noqa: BLE001
        log.exception("Upload processing failed")
        raise HTTPException(422, "We couldn't process this file. Please try a different photo or PDF.") from None
    hint = re.sub(r"[^a-zA-Z0-9_.\-]", "_", field)[:120] or "document"
    with session_scope() as s:
        row = services.store_file(s, nf, customer_id=auth["cid"], flow=auth["flow"], device_hash=auth["dev"],
                                  field_hint=hint, original_name=file.filename or "upload", original_size=total)
        s.flush()
        return services.file_meta(row)


@router.get("/api/files/{file_id}/thumb")
def api_file_thumb(file_id: str, request: Request, t: str = ""):
    auth = _auth(request, t)
    with session_scope() as s:
        f = s.get(StoredFile, file_id)
        if f is None or f.customer_id != auth["cid"] or f.flow != auth["flow"] or f.device_hash != auth["dev"]:
            raise HTTPException(404)
        thumb = make_thumbnail(services.read_file(f), f.mime, 480)
    if thumb is None:
        raise HTTPException(404)
    return Response(thumb, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@router.get("/api/files/{file_id}/meta")
def api_file_meta(file_id: str, request: Request, x_kyc_token: str | None = Header(None)):
    auth = _auth(request, x_kyc_token)
    with session_scope() as s:
        f = s.get(StoredFile, file_id)
        if f is None or f.customer_id != auth["cid"] or f.flow != auth["flow"] or f.device_hash != auth["dev"]:
            raise HTTPException(404)
        return services.file_meta(f)


# ------------------------------------------------------------------------- drafts, validation, submit

def _clean_input(s, auth: dict, data: Any) -> dict:
    if not isinstance(data, dict):
        raise HTTPException(422, "Invalid form data.")
    refs = services.file_refs(data)
    owned = services.owned_files(s, refs, auth["cid"], auth["flow"])
    return services.strip_foreign_files(data, set(owned))


@router.post("/api/draft")
async def api_draft(request: Request, x_kyc_token: str | None = Header(None)):
    auth = _auth(request, x_kyc_token)
    _rate(f"draft:{auth['dev']}", 120, 600)
    body = await request.json()
    with session_scope() as s:
        sub = _locked_or_draft(s, auth, request)
        data = _clean_input(s, auth, body.get("data"))
        services.write_payload(sub, data)
        return {"ok": True}


@router.post("/api/validate")
async def api_validate(request: Request, x_kyc_token: str | None = Header(None)):
    auth = _auth(request, x_kyc_token)
    _rate(f"validate:{auth['dev']}", 240, 600)
    body = await request.json()
    steps = [x for x in (body.get("steps") or []) if isinstance(x, str)]
    with session_scope() as s:
        data = _clean_input(s, auth, body.get("data"))
        accounts = theme.allowed_accounts(s)
        sub = _locked_or_draft(s, auth, request)
        services.write_payload(sub, data)  # validating a step also saves the draft
    try:
        _, errors = await run_in_threadpool(validation.validate, auth["flow"], data, accounts)
    except L.LookupUnavailable:
        raise HTTPException(503, "Verification service is temporarily unavailable. Please try again shortly.") from None
    if steps:
        errors = validation.filter_errors(auth["flow"], errors, steps)
    return {"ok": not errors, "errors": errors}


@router.post("/api/submit")
async def api_submit(request: Request, x_kyc_token: str | None = Header(None)):
    auth = _auth(request, x_kyc_token)
    _rate(f"submit:{auth['dev']}", 10, 600)
    body = await request.json()
    with session_scope() as s:
        data = _clean_input(s, auth, body.get("data"))
        accounts = theme.allowed_accounts(s)
        prior = latest_submission(s, auth["cid"], auth["flow"])
        if prior is not None and prior.locked:
            return JSONResponse({"ok": False, "status": prior.status,
                                 "message": "This verification has already been submitted."}, status_code=409)
    try:
        norm, errors = await run_in_threadpool(validation.validate, auth["flow"], data, accounts)
    except L.LookupUnavailable:
        raise HTTPException(503, "Verification service is temporarily unavailable. Please try again shortly.") from None
    if errors:
        return JSONResponse({"ok": False, "status": "invalid", "errors": errors,
                             "step": _first_step(auth["flow"], errors)}, status_code=422)

    def _persist_and_deliver() -> dict[str, Any]:
        with session_scope() as s:
            sub = _locked_or_draft(s, auth, request)
            services.write_payload(sub, norm)
            sub.submitted_at = utcnow()
            sub.client_ip = _client_ip(request)
            s.flush()
            result = services.deliver(s, sub)
            b = theme.get_branding(s)
            if result.outcome == "needs_correction":
                errs = result.validation_errors or {"_": [result.body.get("message", "Please review your details.")
                                                          if isinstance(result.body, dict) else "Please review your details."]}
                return {"http": 422, "ok": False, "status": "invalid", "errors": errs,
                        "step": _first_step(auth["flow"], errs), "source": "review"}
            kind, title, message = CUSTOMER_MESSAGES.get(result.outcome, ("pending", None, None))
            return {"http": 200, "ok": result.outcome in ("submitted", "delivery_failed", "not_provisioned"),
                    "status": result.outcome, "kind": kind,
                    "title": title or b["success_title"], "message": message or b["success_text"],
                    "return_url": b.get("return_url") or ""}

    out = await run_in_threadpool(_persist_and_deliver)
    return JSONResponse({k: v for k, v in out.items() if k != "http"}, status_code=out["http"])


def _first_step(flow: str, errors: dict) -> str | None:
    order = list(validation.STEPS[flow].keys())
    found = [validation.step_of(flow, p) for p in errors]
    found = [f for f in found if f]
    return min(found, key=order.index) if found else None


# ------------------------------------------------------------------------- returning customers

CODE_TTL = timedelta(minutes=10)
CODE_MAX_ATTEMPTS = 5


@router.post("/api/prefill/send")
async def api_prefill_send(request: Request, x_kyc_token: str | None = Header(None)):
    auth = _auth(request, x_kyc_token)
    _rate(f"otp-send:{auth['cid']}", 3, 900)
    _rate(f"otp-send:{_client_ip(request)}", 10, 900)

    def work() -> tuple[str, str, str]:
        with session_scope() as s:
            source = _prefill_source(s, auth["cid"], auth["flow"])
            if source is None:
                raise HTTPException(404, "No saved details were found.")
            email = services.read_summary(source)["email"]
            for old in s.scalars(select(EmailCode).where(EmailCode.customer_id == auth["cid"],
                                                        EmailCode.flow == auth["flow"], EmailCode.used.is_(False))):
                old.used = True
            code = f"{secrets.randbelow(10**6):06d}"
            seed = secrets.token_hex(8)
            s.add(EmailCode(customer_id=auth["cid"], flow=auth["flow"], device_hash=auth["dev"],
                            source_submission_id=source.id, code_hash=f"{seed}${_code_hash(seed, code)}",
                            expires_at=utcnow() + CODE_TTL))
            b = theme.get_branding(s)
            return email, code, b["brand_name"]

    email, code, brand = await run_in_threadpool(work)
    try:
        await run_in_threadpool(mailer.send_code, email, code, brand)
    except mailer.MailUnavailable as exc:
        raise HTTPException(503, f"{exc} Please try again later or start a new application.") from None
    return {"ok": True, "email": _mask_email(email)}


@router.post("/api/prefill/verify")
async def api_prefill_verify(request: Request, x_kyc_token: str | None = Header(None)):
    auth = _auth(request, x_kyc_token)
    _rate(f"otp-verify:{auth['dev']}", 15, 900)
    body = await request.json()
    code = re.sub(r"\D", "", str(body.get("code", "")))[:6]
    with session_scope() as s:
        row = s.scalars(select(EmailCode).where(EmailCode.customer_id == auth["cid"], EmailCode.flow == auth["flow"],
                                                EmailCode.device_hash == auth["dev"], EmailCode.used.is_(False))
                        .order_by(EmailCode.id.desc()).limit(1)).first()
        expires = row.expires_at if row and row.expires_at.tzinfo else (row.expires_at.replace(tzinfo=utcnow().tzinfo) if row else None)
        if row is None or expires < utcnow() or row.attempts >= CODE_MAX_ATTEMPTS:
            raise HTTPException(410, "This code has expired. Please request a new one.")
        seed, _, digest = row.code_hash.partition("$")
        if len(code) != 6 or not hmac.compare_digest(digest, _code_hash(seed, code)):
            row.attempts += 1
            left = CODE_MAX_ATTEMPTS - row.attempts
            s.commit()
            raise HTTPException(422, f"That code isn't right. {left} attempt{'s' if left != 1 else ''} left." if left
                                else "Too many attempts. Please request a new code.")
        row.used = True
        source = s.get(Submission, row.source_submission_id)
        data = services.read_payload(source) if source else {}
        data.pop("consent", None)  # the declaration must be confirmed again
        draft = _locked_or_draft(s, auth, request)
        services.write_payload(draft, data)
        draft.device_hash = auth["dev"]
        # Re-bind earlier uploads to this browser so their previews load.
        for f in services.owned_files(s, services.file_refs(data), auth["cid"], auth["flow"]).values():
            f.device_hash = auth["dev"]
    return {"ok": True, "data": data}


@router.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}
