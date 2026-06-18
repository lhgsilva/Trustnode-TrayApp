"""License signature verification (operator 2026-06-18, Phase 2a).

The dev portal (VPS) signs every license payload with an ED25519 private
key kept in TRUSTNODE_LICENSE_SIGNING_PRIVATE_PEM. The tray app bundles
the matching PUBLIC key (license_signing_public.pem) and verifies the
signature on every license check.

If the signature is INVALID, the license enters "tampered" state — all
premium modules lock at the UI layer. If the local app is offline AND
the last successful verification was > grace period ago, the license
also locks. Grace period default: 30 days.

A valid signature payload looks like:
    {
        "license_id": "lic-...",
        "tenant_id": "...",
        "customer_id": "...",
        "edge_id": "...",
        "modules": ["dashboard", "historian", ...],
        "issued_utc": "2026-06-18T12:00:00Z",
        "expires_utc": "2027-06-18T12:00:00Z",
        "signature": "<base64 ED25519 signature over the rest of the fields>"
    }

The signature covers everything EXCEPT the `signature` field itself,
serialized as JSON with sorted keys + no whitespace.
"""
from __future__ import annotations

import base64
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Grace period: how long offline the tray can run on a previously
# verified license before locking. 30 days = ~one license cycle.
GRACE_PERIOD_DAYS = 30


def _public_key_candidates() -> list[Path]:
    """Where the public key might live, in priority order."""
    here = Path(__file__).resolve()
    out: list[Path] = []
    # Dev tree
    out.append(here.parents[2] / "keys" / "license_signing_public.pem")
    # PyInstaller _MEIPASS (onefile or onedir)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(Path(meipass) / "keys" / "license_signing_public.pem")
    # Next to the EXE (Electron resources)
    try:
        exe_dir = Path(sys.executable).resolve().parent
        out.append(exe_dir / "keys" / "license_signing_public.pem")
        out.append(exe_dir / "_internal" / "keys" / "license_signing_public.pem")
    except Exception:
        pass
    return out


_PUBLIC_KEY = None


def _load_public_key():
    """Load the ED25519 public key, cached after first call."""
    global _PUBLIC_KEY
    if _PUBLIC_KEY is not None:
        return _PUBLIC_KEY
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception as exc:
        logger.warning("license_signature: cryptography unavailable (%s) — signature verification disabled", exc)
        return None
    for p in _public_key_candidates():
        try:
            if not p.exists():
                continue
            pem = p.read_bytes()
            key = serialization.load_pem_public_key(pem)
            if isinstance(key, Ed25519PublicKey):
                _PUBLIC_KEY = key
                return key
        except Exception:
            continue
    logger.warning("license_signature: no public key found in any candidate path")
    return None


def _canonical_payload(license_payload: Dict[str, Any]) -> bytes:
    """Build the canonical bytes that the signature covers.
    Excludes `signature` itself; sorts keys; no whitespace.
    """
    body = {k: v for k, v in license_payload.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_license_signature(license_payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Verify the ED25519 signature on a license payload.

    Returns:
        {
            "verified": bool,
            "reason": str,         # human-readable when not verified
            "signature_present": bool,
            "public_key_available": bool,
        }

    A FALSE `verified` with `signature_present=False` means the license
    came from a pre-signature build (legacy customer) — UI should treat
    as grandfathered, not tampered. A FALSE with `signature_present=True`
    means the signature was provided but failed — that's a real tamper
    signal.
    """
    if not isinstance(license_payload, dict):
        return {"verified": False, "reason": "no payload", "signature_present": False, "public_key_available": False}
    pk = _load_public_key()
    pk_available = pk is not None
    signature_raw = license_payload.get("signature")
    if not signature_raw:
        return {"verified": False, "reason": "no signature field", "signature_present": False, "public_key_available": pk_available}
    if not pk_available:
        # Public key missing from the install — we can't verify. Don't
        # crash; mark as legacy-tolerant so the customer doesn't lose
        # access on a misbuilt EXE. The dev should rebuild with the key.
        return {"verified": False, "reason": "public key missing", "signature_present": True, "public_key_available": False}
    try:
        sig = base64.b64decode(str(signature_raw))
    except Exception as exc:
        return {"verified": False, "reason": f"signature decode failed: {exc}", "signature_present": True, "public_key_available": True}
    try:
        body = _canonical_payload(license_payload)
        pk.verify(sig, body)
        return {"verified": True, "reason": "ok", "signature_present": True, "public_key_available": True}
    except Exception as exc:
        return {"verified": False, "reason": f"signature invalid: {type(exc).__name__}", "signature_present": True, "public_key_available": True}


def sign_license_payload(license_payload: Dict[str, Any], private_pem: bytes) -> Dict[str, Any]:
    """Dev-portal side: sign a license payload, returning the same dict
    with a `signature` field added. Raises if cryptography lib unavailable
    or private key invalid.

    The dev portal VPS loads its private key from
    TRUSTNODE_LICENSE_SIGNING_PRIVATE_PEM (an env var) and calls this
    when creating / updating a license.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("private key is not ED25519")
    body = {k: v for k, v in license_payload.items() if k != "signature"}
    body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = key.sign(body_bytes)
    out = dict(body)
    out["signature"] = base64.b64encode(sig).decode("ascii")
    return out
