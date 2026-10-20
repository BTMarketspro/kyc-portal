"""FastAPI application: customer KYC/KYB wizard + admin console."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import services
from .config import BASE_DIR, settings
from .crypto import cipher
from .db import AdminUser, Submission, init_db, session_scope, utcnow
from .security import hash_password
from .web_admin import NeedsLogin, router as admin_router
from .web_public import router as public_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kyc")

RETRY_EVERY_SECONDS = 600
MAX_AUTO_ATTEMPTS = 6


def _bootstrap_admin() -> None:
    if not (settings.bootstrap_admin_email and settings.bootstrap_admin_password):
        return
    with session_scope() as s:
        if s.scalars(select(AdminUser).limit(1)).first() is None:
            s.add(AdminUser(email=settings.bootstrap_admin_email.strip().lower(),
                            password_hash=hash_password(settings.bootstrap_admin_password)))
            log.info("Created bootstrap admin %s", settings.bootstrap_admin_email)


def retry_failed_deliveries() -> int:
    """Re-send submissions that couldn't reach Yativo (network errors / 5xx)."""
    done = 0
    with session_scope() as s:
        cutoff = utcnow() - timedelta(seconds=RETRY_EVERY_SECONDS - 30)
        rows = s.scalars(select(Submission).where(Submission.status == "delivery_failed",
                                                  Submission.attempts < MAX_AUTO_ATTEMPTS)).all()
        for sub in rows:
            updated = sub.updated_at if sub.updated_at.tzinfo else sub.updated_at.replace(tzinfo=utcnow().tzinfo)
            if updated > cutoff:
                continue
            result = services.deliver(s, sub)
            log.info("Auto-retry submission %s -> %s", sub.id, result.outcome)
            s.commit()
            done += 1
    return done


async def _retry_loop() -> None:
    while True:
        await asyncio.sleep(RETRY_EVERY_SECONDS)
        try:
            await run_in_threadpool(retry_failed_deliveries)
        except Exception:  # noqa: BLE001
            log.exception("Retry loop failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.secret_key:
        raise RuntimeError("SECRET_KEY is not set. Run: python -m app.cli generate-secret")
    cipher()  # fail fast if the encryption key is missing or malformed
    init_db()
    _bootstrap_admin()
    task = asyncio.create_task(_retry_loop())
    yield
    task.cancel()


app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key or "unset",
    session_cookie="kyc_admin",
    max_age=12 * 3600,
    same_site="strict",
    https_only=settings.session_https_only,
)
if settings.allowed_hosts and settings.allowed_hosts != ("*",):
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: blob: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'self'; "
    "base-uri 'none'; form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if settings.session_https_only:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.exception_handler(NeedsLogin)
async def _needs_login(request: Request, _: NeedsLogin):
    return RedirectResponse(f"/admin/login?next={request.url.path}", status_code=303)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    # The wizard reads "message" from every API error.
    key = "message" if request.url.path.startswith("/api/") else "detail"
    return JSONResponse({"ok": False, key: exc.detail}, status_code=exc.status_code)


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(public_router)
app.include_router(admin_router)
