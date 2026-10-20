# -*- coding: utf-8 -*-
"""Sending a recovery code to the address a user registered.

2026-10-20. Two things drove this file.

The first was a hole. `POST /api/control-plane/password-reset/public/issue`
took a username, took no credentials at all, and returned the reset token in
its own response body:

    return {"ok": True, "tenant_id": tid, "row": row}
    #        row carries "reset_token": secrets.token_urlsafe(28)

`password-reset/public/apply` then accepted that token with a new password.
Anyone who could reach the portal could take over any account, `master`
included, in two requests. The route was confirmed live on the public
internet. A reset code is only a secret if it travels to the ACCOUNT OWNER,
which means out of band - by email - and never back to the caller.

The second was the operator's own request: send a code rather than a link,
and let Supabase do the sending, because the addresses already live there
(9 of 9 `auth.users` rows carry one).

Design, deliberately small:

  * Supabase Auth owns generation, expiry and delivery. We do not mint a
    second code, store it, or expire it - there is no new token table here,
    and nothing to leak.
  * The username -> email mapping comes from `cp_users.email`, because a
    username is what the operator types and an address is what Supabase
    needs.
  * Every outcome returns the SAME answer to the caller. A different reply
    for "no such user" is a free account-enumeration oracle.

The edge keeps its own SMTP link flow and its offline file-based recovery
untouched: a machine on a factory LAN with no internet cannot be rescued by
an email service, and taking that path away would lock people out.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

#: What Supabase calls this flow. "recovery" emails the password-reset
#: template; "magiclink" emails the sign-in template.
OTP_RECOVERY = "recovery"
OTP_MAGIC_LINK = "magiclink"

#: Users the portal created land in Supabase Auth under this domain when
#: they have no real address of their own (see _verify_against_supabase_auth).
_SYNTHETIC_DOMAIN = "@trustnode.local"


def supabase_config() -> Tuple[str, str]:
    """(url, anon_key) for the Auth API, or ("", "") when not configured.

    The ANON key is correct here: /auth/v1 is the public auth surface, and
    service_role bypasses Auth rather than using it.
    """
    url = (os.environ.get("TRUSTNODE_SUPABASE_URL") or "").strip().rstrip("/")
    anon = (os.environ.get("TRUSTNODE_SUPABASE_ANON_KEY") or "").strip()
    return (url, anon) if url and anon else ("", "")


def is_deliverable(email: str) -> bool:
    """Can a real person actually receive mail at this address?

    `<username>@trustnode.local` is a placeholder the portal mints so a user
    exists in Supabase Auth; nobody reads mail there. Treating it as
    deliverable would mean telling an operator a code is on its way when it
    is not.
    """
    addr = str(email or "").strip().lower()
    if not addr or "@" not in addr:
        return False
    return not addr.endswith(_SYNTHETIC_DOMAIN)


def send_code(email: str, *, otp_type: str = OTP_RECOVERY,
              timeout_s: float = 8.0) -> Tuple[bool, str]:
    """Ask Supabase to email a code to `email`.

    Returns (sent, reason). The caller must NOT vary its response to the
    user based on this - it is for the audit log.

    Whether the message shows a 6-digit code or a link is decided by the
    Supabase email template: it must include {{ .Token }} for the code.
    Both verify through the same endpoint, so a template that still sends a
    link keeps working.
    """
    url, anon = supabase_config()
    if not url:
        return False, "supabase_not_configured"
    if not is_deliverable(email):
        return False, "no_deliverable_email"
    try:
        import requests
        r = requests.post(
            f"{url}/auth/v1/otp",
            headers={"apikey": anon, "Content-Type": "application/json"},
            json={
                "email": str(email).strip(),
                "type": otp_type,
                # Never create an account as a side effect of someone typing
                # a username into a recovery box.
                "create_user": False,
            },
            timeout=timeout_s,
        )
        if r.status_code in (200, 204):
            return True, "sent"
        return False, f"supabase_http_{r.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}"


def verify_code(email: str, code: str, *, otp_type: str = OTP_RECOVERY,
                timeout_s: float = 8.0) -> Tuple[bool, Optional[str], str]:
    """Check a code with Supabase.

    Returns (ok, access_token, reason). The access token is what lets us set
    a new password as that user, so the password change is authorised by the
    code itself rather than by anything we hold.
    """
    url, anon = supabase_config()
    if not url:
        return False, None, "supabase_not_configured"
    if not str(code or "").strip():
        return False, None, "code_required"
    try:
        import requests
        r = requests.post(
            f"{url}/auth/v1/verify",
            headers={"apikey": anon, "Content-Type": "application/json"},
            json={"email": str(email).strip(), "token": str(code).strip(),
                  "type": otp_type},
            timeout=timeout_s,
        )
        if r.status_code == 200:
            body = r.json() if r.content else {}
            return True, str(body.get("access_token") or "") or None, "ok"
        return False, None, f"supabase_http_{r.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, None, f"{type(exc).__name__}"


def set_password_with_token(access_token: str, new_password: str,
                            *, timeout_s: float = 8.0) -> Tuple[bool, str]:
    """Set the password of whoever that access token belongs to."""
    url, anon = supabase_config()
    if not url:
        return False, "supabase_not_configured"
    if not access_token or len(str(new_password or "")) < 8:
        return False, "token_and_password_required"
    try:
        import requests
        r = requests.put(
            f"{url}/auth/v1/user",
            headers={
                "apikey": anon,
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"password": str(new_password)},
            timeout=timeout_s,
        )
        if r.status_code == 200:
            return True, "ok"
        return False, f"supabase_http_{r.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}"
