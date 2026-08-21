"""Per-install TLS material for the LAN listener (operator 2026-08-21, Phase 2).

Owner decision: remote access must work on ANY company network with zero
friction, so HTTPS is OFFERED (recommended), never forced. This module makes
that offer cheap: a self-signed certificate + key generated once per install
(data dir `lan_tls/`), CN = hostname, SANs = hostname, <hostname>.local,
localhost and every non-loopback IPv4 seen at generation time, valid 10 years.
The Remote Access page exposes the certificate for download (trust guide) and
accepts an enterprise cert/key pair dropped in the same folder (`custom.crt`
/ `custom.key`) which then wins.

Uses `cryptography` (already bundled with the backend). Never raises to the
caller: every function returns None/False on failure and logs once.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import logging
import os
import socket
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("trustnode.lan-tls")


def _data_dir() -> Path:
    env = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
    if env:
        return Path(env)
    try:
        from app.state import app_store
        p = str(getattr(app_store, "_db_path", "") or "")
        if p:
            return Path(p).parent
    except Exception:
        pass
    return Path(os.path.expanduser("~")) / ".trustnode_edge" / "data"


def tls_dir() -> Path:
    d = _data_dir() / "lan_tls"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def cert_paths() -> Dict[str, Path]:
    d = tls_dir()
    custom_crt, custom_key = d / "custom.crt", d / "custom.key"
    if custom_crt.exists() and custom_key.exists():
        return {"cert": custom_crt, "key": custom_key, "kind": "custom"}  # type: ignore[dict-item]
    return {"cert": d / "cert.pem", "key": d / "key.pem", "kind": "self-signed"}  # type: ignore[dict-item]


def _lan_ips() -> list[str]:
    out: list[str] = []
    try:
        for fam, _t, _p, _c, sockaddr in socket.getaddrinfo(socket.gethostname(), None):
            if fam == socket.AF_INET:
                ip = str(sockaddr[0])
                if not ip.startswith("127.") and ip not in out:
                    out.append(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = str(s.getsockname()[0])
        s.close()
        if not ip.startswith("127.") and ip not in out:
            out.append(ip)
    except Exception:
        pass
    return out


def ensure_certificate(regenerate: bool = False) -> Optional[Dict[str, Any]]:
    """Create the self-signed pair if missing (or `regenerate`). Returns
    {cert, key, kind, fingerprint_sha256, hostname, sans} or None."""
    paths = cert_paths()
    cert_p, key_p = paths["cert"], paths["key"]
    if paths["kind"] == "custom":
        return describe()
    if cert_p.exists() and key_p.exists() and not regenerate:
        return describe()
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        hostname = socket.gethostname() or "trustnode-edge"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TrustNode Edge"),
        ])
        sans: list[Any] = [x509.DNSName(hostname), x509.DNSName(f"{hostname}.local"), x509.DNSName("localhost")]
        for ip in _lan_ips() + ["127.0.0.1"]:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
            except Exception:
                pass
        now = _dt.datetime.now(_dt.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256())
        )
        key_p.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        try:
            os.chmod(key_p, 0o600)
        except Exception:
            pass
        logger.info("LAN TLS: generated self-signed certificate for %s (%d SANs)", hostname, len(sans))
        return describe()
    except Exception as exc:
        logger.warning("LAN TLS: certificate generation failed: %r", exc)
        return None


def describe() -> Optional[Dict[str, Any]]:
    paths = cert_paths()
    cert_p, key_p = paths["cert"], paths["key"]
    if not (cert_p.exists() and key_p.exists()):
        return None
    try:
        from cryptography import x509
        data = cert_p.read_bytes()
        cert = x509.load_pem_x509_certificate(data)
        fp = hashlib.sha256(cert.public_bytes(__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.DER)).hexdigest()
        sans: list[str] = []
        try:
            ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            sans = [str(v) for v in ext.get_values_for_type(x509.DNSName)] + [str(v) for v in ext.get_values_for_type(x509.IPAddress)]
        except Exception:
            pass
        cn = ""
        try:
            cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
        except Exception:
            pass
        return {
            "cert": str(cert_p), "key": str(key_p), "kind": paths["kind"],
            "fingerprint_sha256": ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper(),
            "hostname": cn, "sans": sans,
            "not_after_utc": cert.not_valid_after_utc.isoformat()[:19] + "Z" if hasattr(cert, "not_valid_after_utc") else "",
        }
    except Exception as exc:
        logger.warning("LAN TLS: cannot describe certificate: %r", exc)
        return {"cert": str(cert_p), "key": str(key_p), "kind": paths["kind"], "fingerprint_sha256": "", "hostname": "", "sans": []}


def certificate_pem() -> Optional[bytes]:
    p = cert_paths()["cert"]
    try:
        return p.read_bytes() if p.exists() else None
    except Exception:
        return None
