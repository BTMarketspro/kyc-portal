"""Encrypted storage for files and submissions, and delivery to Yativo."""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import yativo_client
from .config import settings
from .crypto import cipher
from .db import StoredFile, Submission, utcnow
from .files import NormalizedFile

log = logging.getLogger("kyc.services")

LOCKED_STATUSES = {"submitting", "submitted", "duplicate", "blocked", "not_provisioned", "delivery_failed"}
STATUS_LABELS = {
    "draft": "Draft",
    "submitting": "Sending",
    "submitted": "Submitted",
    "needs_correction": "Needs correction",
    "duplicate": "Duplicate",
    "blocked": "Blocked",
    "not_provisioned": "Account not provisioned",
    "delivery_failed": "Delivery failed",
}


# --------------------------------------------------------------------------- files

def _file_path(file_id: str):
    return settings.files_dir / f"{file_id}.bin"


def store_file(s: Session, nf: NormalizedFile, *, customer_id: str, flow: str, device_hash: str,
               field_hint: str, original_name: str, original_size: int) -> StoredFile:
    fid = str(uuid.uuid4())
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    _file_path(fid).write_bytes(cipher().encrypt(nf.data, f"file:{fid}"))
    meta = {"original_name": original_name[:255], "notes": nf.notes, "width": nf.width, "height": nf.height,
            "pages": nf.pages}
    row = StoredFile(
        id=fid, customer_id=customer_id, flow=flow, device_hash=device_hash, field_hint=field_hint[:255],
        meta_enc=cipher().encrypt_json(meta, f"filemeta:{fid}"), mime=nf.mime, ext=nf.ext, size=nf.size,
        original_size=original_size, sha256=hashlib.sha256(nf.data).hexdigest(),
    )
    s.add(row)
    return row


def read_file(f: StoredFile) -> bytes:
    return cipher().decrypt(_file_path(f.id).read_bytes(), f"file:{f.id}")


def file_meta(f: StoredFile) -> dict[str, Any]:
    try:
        meta = cipher().decrypt_json(f.meta_enc, f"filemeta:{f.id}")
    except Exception:  # noqa: BLE001
        meta = {}
    return {"id": f.id, "ref": f"file:{f.id}", "mime": f.mime, "ext": f.ext, "size": f.size,
            "original_size": f.original_size, "name": meta.get("original_name") or f"document.{f.ext}",
            "notes": meta.get("notes", []), "pages": meta.get("pages")}


def delete_file(s: Session, f: StoredFile) -> None:
    _file_path(f.id).unlink(missing_ok=True)
    s.delete(f)


def file_refs(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, dict):
        for v in value.values():
            out |= file_refs(v)
    elif isinstance(value, list):
        for v in value:
            out |= file_refs(v)
    elif isinstance(value, str) and value.startswith("file:"):
        out.add(value[5:])
    return out


def owned_files(s: Session, ids: set[str], customer_id: str, flow: str) -> dict[str, StoredFile]:
    if not ids:
        return {}
    rows = s.scalars(select(StoredFile).where(StoredFile.id.in_(ids))).all()
    return {r.id: r for r in rows if r.customer_id == customer_id and r.flow == flow}


def strip_foreign_files(value: Any, allowed: set[str]) -> Any:
    """Drop file references the customer doesn't own (defence in depth)."""
    if isinstance(value, dict):
        return {k: strip_foreign_files(v, allowed) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_foreign_files(v, allowed) for v in value]
    if isinstance(value, str) and value.startswith("file:") and value[5:] not in allowed:
        return ""
    return value


# --------------------------------------------------------------------------- submissions

def summary_of(flow: str, d: dict) -> dict[str, str]:
    if flow == "individual":
        name = " ".join(x for x in (d.get("first_name"), d.get("middle_name"), d.get("last_name")) if x)
        country = (d.get("residential_address") or {}).get("country") or d.get("nationality") or ""
    else:
        name = d.get("business_legal_name") or d.get("business_trade_name") or ""
        country = d.get("incorporation_country") or (d.get("registered_address") or {}).get("country") or ""
    return {"name": name or "—", "email": d.get("email") or "", "country": country or ""}


def write_payload(sub: Submission, data: dict) -> None:
    c = cipher()
    sub.payload_enc = c.encrypt_json(data, f"submission:{sub.id}:payload")
    summary = summary_of(sub.flow, data)
    sub.summary_enc = c.encrypt_json(summary, f"submission:{sub.id}:summary")
    sub.email_bidx = c.blind_index(summary["email"])
    sub.name_bidx = c.blind_index(re.sub(r"\s+", " ", summary["name"])) if summary["name"] != "—" else None


def read_payload(sub: Submission) -> dict:
    if not sub.payload_enc:
        return {}
    return cipher().decrypt_json(sub.payload_enc, f"submission:{sub.id}:payload")


def read_summary(sub: Submission) -> dict:
    if not sub.summary_enc:
        return {"name": "—", "email": "", "country": ""}
    try:
        return cipher().decrypt_json(sub.summary_enc, f"submission:{sub.id}:summary")
    except Exception:  # noqa: BLE001
        return {"name": "(unreadable)", "email": "", "country": ""}


def read_response(sub: Submission) -> Any:
    if not sub.yativo_response_enc:
        return None
    return cipher().decrypt_json(sub.yativo_response_enc, f"submission:{sub.id}:response")


def get_or_create_draft(s: Session, customer_id: str, flow: str, device_hash: str,
                        ip: str | None, ua: str | None) -> Submission:
    sub = s.scalars(
        select(Submission).where(Submission.customer_id == customer_id, Submission.flow == flow,
                                 Submission.locked.is_(False))
        .order_by(Submission.id.desc()).limit(1)
    ).first()
    if sub is None:
        sub = Submission(customer_id=customer_id, flow=flow, status="draft", device_hash=device_hash,
                         client_ip=ip, user_agent=(ua or "")[:500])
        s.add(sub)
        s.flush()
    return sub


def deliver(s: Session, sub: Submission) -> yativo_client.YativoResult:
    """Send the stored submission to Yativo and record the outcome. Caller commits."""
    data = read_payload(sub)
    files = owned_files(s, file_refs(data), sub.customer_id, sub.flow)

    def resolve(ref: str) -> tuple[str, bytes, str]:
        f = files.get(ref[5:])
        if f is None:
            raise ValueError(f"Missing file {ref}")
        return f"{f.field_hint or 'document'}.{f.ext}".replace("/", "_"), read_file(f), f.mime

    outbound = yativo_client.build_outbound(sub.flow, data, sub.customer_id)
    sub.status = "submitting"
    sub.attempts = (sub.attempts or 0) + 1
    s.flush()
    try:
        result = yativo_client.submit(sub.flow, outbound, resolve)
    except Exception as exc:  # noqa: BLE001
        log.exception("Delivery of submission %s failed", sub.id)
        result = yativo_client.YativoResult(None, {"error": str(exc)}, "delivery_failed")

    for f in files.values():
        f.submission_id = sub.id
    sub.status = result.outcome
    sub.yativo_status_code = result.status_code
    sub.yativo_response_enc = cipher().encrypt_json(
        {"status_code": result.status_code, "body": result.body, "outbound": outbound, "at": utcnow().isoformat()},
        f"submission:{sub.id}:response",
    )
    sub.warnings_count = sum(len(v) if isinstance(v, list) else 1 for v in result.warnings.values())
    sub.locked = result.outcome in LOCKED_STATUSES
    if result.ok:
        sub.delivered_at = utcnow()
    return result
