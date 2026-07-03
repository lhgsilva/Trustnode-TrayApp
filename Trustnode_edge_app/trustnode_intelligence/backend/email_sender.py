"""Email delivery for scheduled insights.

Reuses the SAME SMTP config the reports module already uses, read from
app_store.get_bootstrap()['email_settings'] when present. If not
configured, log + skip — we never block the scheduler on email.
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional


_log = logging.getLogger("trustnode.intelligence.email")


def _read_smtp_config() -> Optional[dict]:
    try:
        from app.state import app_store as _store  # type: ignore
        bs = _store.get_bootstrap(prefer_cloud_reads=False) or {}
        settings = bs.get("email_settings") or bs.get("notifications") or {}
        if not isinstance(settings, dict):
            return None
        host = str(settings.get("smtp_host") or "").strip()
        if not host:
            return None
        return {
            "host": host,
            "port": int(settings.get("smtp_port") or 587),
            "username": str(settings.get("smtp_username") or ""),
            "password": str(settings.get("smtp_password") or ""),
            "from_addr": str(settings.get("smtp_from") or settings.get("smtp_username") or ""),
            "use_tls": bool(settings.get("smtp_use_tls", True)),
        }
    except Exception:
        return None


def send_insight_email(to_addrs: str, title: str, body: str) -> Optional[str]:
    """Returns None on success, error string on failure (non-fatal)."""
    cfg = _read_smtp_config()
    if not cfg:
        return "SMTP not configured"
    recipients = [a.strip() for a in (to_addrs or "").split(",") if a.strip()]
    if not recipients:
        return "No recipients"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"TrustNode Insight — {title}"
    msg["From"] = cfg["from_addr"]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as srv:
            srv.ehlo()
            if cfg["use_tls"]:
                srv.starttls()
                srv.ehlo()
            if cfg["username"]:
                srv.login(cfg["username"], cfg["password"])
            srv.sendmail(cfg["from_addr"], recipients, msg.as_string())
        return None
    except Exception as exc:
        _log.warning("Insight email send failed: %s", exc)
        return f"{type(exc).__name__}: {exc}"
