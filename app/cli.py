"""Management commands.

    python -m app.cli generate-key            # AES-256 key for KYC_ENCRYPTION_KEY
    python -m app.cli generate-secret         # SECRET_KEY / LINK_SIGNING_SECRET
    python -m app.cli create-admin EMAIL      # prompts for a password
    python -m app.cli sign-link TYPE CUSTOMER_ID
    python -m app.cli retry-failed            # resend submissions that failed to reach Yativo
"""
from __future__ import annotations

import getpass
import secrets
import sys
from urllib.parse import urlencode


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]

    if cmd == "generate-key":
        from .crypto import generate_key

        print(generate_key())
        return 0
    if cmd == "generate-secret":
        print(secrets.token_urlsafe(48))
        return 0

    if cmd == "create-admin":
        from sqlalchemy import select

        from .db import AdminUser, init_db, session_scope
        from .security import hash_password

        if not args:
            print("usage: create-admin EMAIL")
            return 1
        email = args[0].strip().lower()
        pw = getpass.getpass("Password (min 12 chars): ")
        if len(pw) < 12 or pw != getpass.getpass("Repeat: "):
            print("Passwords must match and be at least 12 characters.")
            return 1
        init_db()
        with session_scope() as s:
            user = s.scalars(select(AdminUser).where(AdminUser.email == email)).first()
            if user:
                user.password_hash = hash_password(pw)
                print(f"Updated password for {email}")
            else:
                s.add(AdminUser(email=email, password_hash=hash_password(pw)))
                print(f"Created admin {email}")
        return 0

    if cmd == "sign-link":
        from .config import settings
        from .security import link_signature

        if len(args) != 2 or args[0] not in ("individual", "business"):
            print("usage: sign-link individual|business CUSTOMER_ID")
            return 1
        params = {"customer_type": args[0], "customer_id": args[1]}
        if settings.link_signing_secret:
            params["sig"] = link_signature(args[0], args[1])
        print(f"{settings.base_url}/verify?{urlencode(params)}")
        return 0

    if cmd == "retry-failed":
        from .db import init_db
        from .main import retry_failed_deliveries

        init_db()
        print(f"Retried {retry_failed_deliveries()} submission(s)")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
