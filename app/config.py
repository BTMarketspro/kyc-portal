"""Runtime configuration, read once from the environment (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "Identity Verification")
    env: str = os.getenv("APP_ENV", "development")
    base_url: str = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")

    # Secret used to sign session cookies and upload tokens. MUST be set in production.
    secret_key: str = os.getenv("SECRET_KEY", "")
    # Base64 encoded 32-byte AES key. Older keys (comma separated) are kept for decryption only.
    encryption_key: str = os.getenv("KYC_ENCRYPTION_KEY", "")
    old_encryption_keys: tuple[str, ...] = tuple(
        k.strip() for k in os.getenv("KYC_ENCRYPTION_OLD_KEYS", "").split(",") if k.strip()
    )

    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'storage' / 'kyc_portal.db'}")
    storage_dir: Path = Path(os.getenv("STORAGE_DIR", str(BASE_DIR / "storage")))

    # Yativo KYC API
    yativo_base_url: str = os.getenv("YATIVO_KYC_BASE_URL", "https://kyc-sandbox.yativo.com").rstrip("/")
    yativo_api_token: str = os.getenv("YATIVO_API_TOKEN", "")  # Not required today; sent as Bearer when set.
    yativo_timeout: int = _int("YATIVO_TIMEOUT_SECONDS", 180)
    lookup_cache_ttl: int = _int("LOOKUP_CACHE_TTL_SECONDS", 6 * 3600)

    # Address autocomplete. With a key, Google Places (New) is used through a server-side
    # proxy (the key never reaches the browser). Without one, the free OpenStreetMap/Photon
    # geocoder is used.
    google_maps_api_key: str = os.getenv("GOOGLE_MAPS_API_KEY", "")
    osm_autocomplete_url: str = os.getenv("OSM_AUTOCOMPLETE_URL", "https://photon.komoot.io/api/")

    # File handling. Every stored file is normalised into [min, max].
    file_min_bytes: int = _int("FILE_MIN_KB", 120) * 1024
    file_max_bytes: int = _int("FILE_MAX_KB", 3 * 1024) * 1024
    max_raw_upload_bytes: int = _int("MAX_RAW_UPLOAD_MB", 100) * 1024 * 1024

    # When set, customer links must carry ?sig=HMAC_SHA256(secret, "<type>:<customer_id>").
    link_signing_secret: str = os.getenv("LINK_SIGNING_SECRET", "")

    # Outgoing email (one-time codes that unlock a customer's previously saved details).
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = _int("SMTP_PORT", 587)
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", "")
    smtp_starttls: bool = _bool("SMTP_STARTTLS", True)

    session_https_only: bool = _bool("SESSION_HTTPS_ONLY", False)
    admin_session_minutes: int = _int("ADMIN_SESSION_MINUTES", 60)

    # Bootstrap admin account (created on first start when no admin exists).
    bootstrap_admin_email: str = os.getenv("ADMIN_EMAIL", "")
    bootstrap_admin_password: str = os.getenv("ADMIN_PASSWORD", "")

    allowed_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(h.strip() for h in os.getenv("ALLOWED_HOSTS", "*").split(",") if h.strip())
    )

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    @property
    def files_dir(self) -> Path:
        return self.storage_dir / "files"

    @property
    def cache_dir(self) -> Path:
        return self.storage_dir / "cache"


settings = Settings()