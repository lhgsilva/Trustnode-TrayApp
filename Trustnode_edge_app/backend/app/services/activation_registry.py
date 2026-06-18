"""Activation receipt mirror in the Windows registry (operator 2026-06-18).

Why this exists
---------------
The workspace export/import flow makes the customer *responsible* for
saving a JSON file before an update. The first-launch detector handles
the "you forgot, but the SQLite is still there" case. This module
handles the LAST gap: customer wipes ALL data folders manually (or AV
quarantines them), then re-installs. SQLite is gone, JSON export was
never taken. Today, the license activation is lost; customer has to
contact us for a new code.

By mirroring the activation receipt into the Windows registry, the
backend's control_plane_store auto-restores the license on first boot
when no cp_licenses / cp_edges rows exist. Customer sees an already-
activated edge even after a nuclear wipe.

Storage layout
--------------
We write to one of two hives, in order of preference:

  HKLM\\Software\\TrustNode\\Activation  (machine-wide, survives user delete)
  HKCU\\Software\\TrustNode\\Activation  (per-user, survives data wipe)

The HKLM write only succeeds when the backend runs with admin
privileges (Windows service install, or a tray launched with elevation).
On a non-admin tray we silently fall back to HKCU — still covers the
common "customer wiped the data folder" case.

Values written:
  receipt         REG_SZ   JSON blob (see export_activation_state)
  receipt_size    REG_DWORD bytes
  receipt_sha256  REG_SZ   integrity check
  written_utc     REG_SZ   ISO timestamp
  format_version  REG_DWORD 1

Reads return None on:
  - non-Windows platform
  - winreg import failure (shouldn't happen on standard Python)
  - both hives missing the key
  - JSON parse failure
  - sha256 mismatch (corruption)

This module is intentionally dependency-free — uses only stdlib winreg.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REGISTRY_PATH = r"Software\TrustNode\Activation"
_FORMAT_VERSION = 1


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _import_winreg():
    """Lazy import so non-Windows callers don't blow up at module load."""
    try:
        import winreg  # type: ignore
        return winreg
    except Exception as exc:
        logger.debug("winreg import failed: %s", exc)
        return None


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_to_hive(winreg, hive_const: int, receipt_json: str) -> tuple[bool, str]:
    """Try to write the receipt under hive_const\\_REGISTRY_PATH.

    Returns (ok, hive_name). On permission denied (typical for HKLM as
    non-admin), returns (False, hive_name) WITHOUT raising — caller
    decides whether to fall back to HKCU.
    """
    hive_name = "HKLM" if hive_const == winreg.HKEY_LOCAL_MACHINE else "HKCU"
    try:
        # KEY_WOW64_64KEY ensures 32/64-bit Python sees the same view.
        # CreateKeyEx returns the existing key if present, no-op on
        # repeated writes.
        access = winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY
        key = winreg.CreateKeyEx(hive_const, _REGISTRY_PATH, 0, access)
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            winreg.SetValueEx(key, "receipt", 0, winreg.REG_SZ, receipt_json)
            winreg.SetValueEx(key, "receipt_size", 0, winreg.REG_DWORD, len(receipt_json.encode("utf-8")))
            winreg.SetValueEx(key, "receipt_sha256", 0, winreg.REG_SZ, _sha256_hex(receipt_json))
            winreg.SetValueEx(key, "written_utc", 0, winreg.REG_SZ, now_iso)
            winreg.SetValueEx(key, "format_version", 0, winreg.REG_DWORD, _FORMAT_VERSION)
            return True, hive_name
        finally:
            winreg.CloseKey(key)
    except PermissionError:
        return False, hive_name
    except OSError as exc:
        # Some sandboxes refuse registry access entirely — treat as
        # graceful failure, no raise.
        logger.debug("registry write to %s failed: %s", hive_name, exc)
        return False, hive_name


def _read_from_hive(winreg, hive_const: int) -> Optional[dict[str, Any]]:
    hive_name = "HKLM" if hive_const == winreg.HKEY_LOCAL_MACHINE else "HKCU"
    try:
        access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        key = winreg.OpenKeyEx(hive_const, _REGISTRY_PATH, 0, access)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("registry open %s failed: %s", hive_name, exc)
        return None
    try:
        receipt_text, _ = winreg.QueryValueEx(key, "receipt")
        expected_sha = None
        try:
            expected_sha, _ = winreg.QueryValueEx(key, "receipt_sha256")
        except FileNotFoundError:
            pass
        if expected_sha:
            actual_sha = _sha256_hex(str(receipt_text))
            if actual_sha != str(expected_sha):
                logger.warning(
                    "activation registry receipt SHA mismatch in %s — ignoring (expected=%s actual=%s)",
                    hive_name, expected_sha, actual_sha,
                )
                return None
        try:
            payload = json.loads(str(receipt_text))
        except Exception as exc:
            logger.warning("activation registry receipt in %s is not valid JSON: %s", hive_name, exc)
            return None
        if not isinstance(payload, dict):
            return None
        payload["__source_hive"] = hive_name
        return payload
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("registry read from %s failed: %s", hive_name, exc)
        return None
    finally:
        try:
            winreg.CloseKey(key)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_activation_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    """Mirror the activation payload into the Windows registry.

    Tries HKLM first (machine-wide, requires admin). Falls back to HKCU
    if HKLM is denied. Returns a dict with hive, ok, reason. NEVER
    raises — the caller can ignore the result and continue.

    No-op on non-Windows.
    """
    result = {"ok": False, "hive": "", "reason": ""}
    if not _is_windows():
        result["reason"] = "non-windows"
        return result
    winreg = _import_winreg()
    if winreg is None:
        result["reason"] = "winreg unavailable"
        return result
    if not isinstance(payload, dict):
        result["reason"] = "payload not a dict"
        return result
    try:
        receipt_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except Exception as exc:
        result["reason"] = f"serialise failed: {exc}"
        return result
    # Try HKLM first.
    ok, hive = _write_to_hive(winreg, winreg.HKEY_LOCAL_MACHINE, receipt_json)
    if ok:
        result.update(ok=True, hive=hive, reason="written")
        return result
    # Fall back to HKCU.
    ok, hive = _write_to_hive(winreg, winreg.HKEY_CURRENT_USER, receipt_json)
    if ok:
        result.update(ok=True, hive=hive, reason="written (HKLM denied)")
        return result
    result["reason"] = "all hives denied"
    return result


def read_activation_receipt() -> Optional[dict[str, Any]]:
    """Read the activation payload back from the registry.

    Tries HKLM first (machine-wide), then HKCU. Returns the JSON dict
    on success (annotated with __source_hive for diagnostics), or None
    if neither hive has a valid receipt.

    No-op on non-Windows.
    """
    if not _is_windows():
        return None
    winreg = _import_winreg()
    if winreg is None:
        return None
    # Prefer the machine-wide receipt — it's the more durable one.
    out = _read_from_hive(winreg, winreg.HKEY_LOCAL_MACHINE)
    if out is not None:
        return out
    return _read_from_hive(winreg, winreg.HKEY_CURRENT_USER)


def clear_activation_receipt() -> dict[str, Any]:
    """Remove the receipt from both hives. Used by the Settings → Reset
    Workspace flow so a wiped workspace truly is a clean slate.

    Best-effort: missing keys are not an error. Returns a per-hive
    deletion summary. No-op on non-Windows.
    """
    result = {"hkcu_deleted": False, "hklm_deleted": False, "errors": []}
    if not _is_windows():
        return result
    winreg = _import_winreg()
    if winreg is None:
        return result
    for hive_const, name in (
        (winreg.HKEY_LOCAL_MACHINE, "hklm_deleted"),
        (winreg.HKEY_CURRENT_USER, "hkcu_deleted"),
    ):
        try:
            access = winreg.KEY_ALL_ACCESS | winreg.KEY_WOW64_64KEY
            key = winreg.OpenKeyEx(hive_const, _REGISTRY_PATH, 0, access)
            try:
                for val_name in ("receipt", "receipt_size", "receipt_sha256", "written_utc", "format_version"):
                    try:
                        winreg.DeleteValue(key, val_name)
                    except FileNotFoundError:
                        pass
            finally:
                winreg.CloseKey(key)
            result[name] = True
        except FileNotFoundError:
            pass
        except OSError as exc:
            result["errors"].append(f"{name}: {exc}")
    return result
