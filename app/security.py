"""Password hashing, CSRF, customer-link signing and a tiny in-memory rate limiter."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings


def _secret() -> str:
    if not settings.secret_key:
        raise RuntimeError("SECRET_KEY is not set. Generate one with: python -m app.cli generate-secret")
    return settings.secret_key


# ------------------------------------------------------------------ passwords (scrypt)

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=2**15, r=8, p=1,
                            maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ customer session token

def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt=salt)


def issue_customer_token(customer_id: str, flow: str, device: str) -> str:
    return _serializer("kyc-customer").dumps({"cid": customer_id, "flow": flow, "dev": device_hash(device)})


def read_customer_token(token: str, max_age: int = 6 * 3600) -> dict | None:
    try:
        return _serializer("kyc-customer").loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def device_hash(device: str) -> str:
    return hmac.new(_secret().encode(), f"device:{device}".encode(), hashlib.sha256).hexdigest()


def new_device_id() -> str:
    return secrets.token_urlsafe(32)


# ------------------------------------------------------------------ signed customer links

def link_signature(flow: str, customer_id: str) -> str:
    msg = f"{flow}:{customer_id}".encode()
    return hmac.new(settings.link_signing_secret.encode(), msg, hashlib.sha256).hexdigest()


def link_is_valid(flow: str, customer_id: str, sig: str | None) -> bool:
    if not settings.link_signing_secret:
        return True
    return bool(sig) and hmac.compare_digest(link_signature(flow, customer_id), sig or "")


# ------------------------------------------------------------------ CSRF (admin forms)

def csrf_token(session: dict) -> str:
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["csrf"] = tok
    return tok


def csrf_ok(session: dict, submitted: str | None) -> bool:
    tok = session.get("csrf")
    return bool(tok) and bool(submitted) and hmac.compare_digest(tok, submitted or "")


# ------------------------------------------------------------------ rate limiting

class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 50_000:  # crude memory guard
                self._hits.clear()
            return True


limiter = RateLimiter()
