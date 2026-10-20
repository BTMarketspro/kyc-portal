"""Minimal SMTP mailer. Without SMTP settings, codes are logged in development only."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from html import escape

from .config import settings

log = logging.getLogger("kyc.mail")


class MailUnavailable(RuntimeError):
    pass


def send_code(to: str, code: str, brand: str, primary: str = "#1F5EFF") -> None:
    subject = f"{code} is your {brand} verification code"
    text = (f"Your verification code is {code}.\n\nIt expires in 10 minutes. "
            "If you didn't request it, you can ignore this email.")
    html = f"""<div style="font-family:Inter,Arial,sans-serif;max-width:460px;margin:auto;padding:24px;color:#0F172A">
  <h2 style="margin:0 0 8px">{escape(brand)}</h2>
  <p style="color:#64748B;margin:0 0 20px">Use this code to load the details you saved earlier.</p>
  <div style="font-size:32px;font-weight:700;letter-spacing:8px;background:#F3F6FB;border-radius:12px;padding:16px;text-align:center;color:{escape(primary)}">{escape(code)}</div>
  <p style="color:#64748B;font-size:13px;margin-top:20px">The code expires in 10 minutes. If you didn't request it, you can ignore this email.</p>
</div>"""

    if not settings.smtp_host:
        if settings.is_production:
            raise MailUnavailable("Email is not configured.")
        log.warning("SMTP not configured — development code for %s: %s", to, code)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    try:
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ssl.create_default_context(), timeout=20)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
            if settings.smtp_starttls:
                server.starttls(context=ssl.create_default_context())
        with server:
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        log.error("Sending code email failed: %s", exc)
        raise MailUnavailable("We couldn't send the email right now.") from exc
