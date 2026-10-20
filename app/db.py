"""Database models. Everything personal lives in encrypted columns / encrypted files.

Plain-text columns are limited to what the admin list needs to sort and filter on
(customer id, flow type, status, timestamps) plus keyed blind indexes for exact search.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(128), index=True)
    flow: Mapped[str] = mapped_column(String(16), index=True)  # individual | business
    # draft -> submitting -> submitted | needs_correction | rejected | duplicate | blocked | delivery_failed
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft")
    payload_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # the validated form data (file refs, not bytes)
    summary_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # display name / email for the list view
    email_bidx: Mapped[str | None] = mapped_column(String(64), index=True)
    name_bidx: Mapped[str | None] = mapped_column(String(64), index=True)
    yativo_status_code: Mapped[int | None] = mapped_column(Integer)
    yativo_response_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    warnings_count: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)  # customer can no longer edit
    # Keyed hash of the browser's device cookie: a saved draft is only ever restored to the
    # browser that created it, so knowing a customer id alone never reveals their data.
    device_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    client_ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StoredFile(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4
    customer_id: Mapped[str] = mapped_column(String(128), index=True)
    flow: Mapped[str] = mapped_column(String(16))
    submission_id: Mapped[int | None] = mapped_column(ForeignKey("submissions.id"), index=True)
    field_hint: Mapped[str | None] = mapped_column(String(255))
    device_hash: Mapped[str | None] = mapped_column(String(64))
    meta_enc: Mapped[bytes] = mapped_column(LargeBinary)  # original name, notes
    mime: Mapped[str] = mapped_column(String(64))
    ext: Mapped[str] = mapped_column(String(8))
    size: Mapped[int] = mapped_column(Integer)
    original_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailCode(Base):
    """One-time code emailed to a returning customer before their saved details are shown."""

    __tablename__ = "email_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(128), index=True)
    flow: Mapped[str] = mapped_column(String(16))
    device_hash: Mapped[str] = mapped_column(String(64))
    source_submission_id: Mapped[int] = mapped_column(Integer)
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_setting(s: Session, key: str, default: Any = None) -> Any:
    row = s.get(Setting, key)
    return row.value if row is not None else default


def put_setting(s: Session, key: str, value: Any) -> None:
    row = s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=value))
    else:
        row.value = json.loads(json.dumps(value))


def audit(s: Session, actor: str, action: str, target: str | None = None, detail: str | None = None,
          ip: str | None = None) -> None:
    s.add(AuditLog(actor=actor, action=action, target=target, detail=detail, ip=ip))


def latest_submission(s: Session, customer_id: str, flow: str) -> Submission | None:
    return s.scalars(
        select(Submission)
        .where(Submission.customer_id == customer_id, Submission.flow == flow)
        .order_by(Submission.id.desc())
        .limit(1)
    ).first()
