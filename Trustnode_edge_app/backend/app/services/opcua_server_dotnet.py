"""OPC UA server (OPC Foundation .NET Standard sidecar) — operator 2026-06-17.

Spawns the bundled `TrustNodeOpcUa.exe` (built from
``backend/sidecars/opcua-cs/``) as a child process. Tag updates are
pushed via a small HTTP control channel bound to 127.0.0.1.

Sidecar resolution mirrors the Mosquitto manager.
"""
from __future__ import annotations

import json
import logging
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)

_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_last_error: str = ""
_current_port: int = 0
_control_port: int = 0
_publishable_devices: Dict[str, str] = {}  # "gid::tag" -> device name


def _candidate_sidecar_paths() -> list[Path]:
    here = Path(__file__).resolve()
    out: list[Path] = []
    out.append(here.parents[2] / "sidecars" / "opcua" / "TrustNodeOpcUa.exe")
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(Path(meipass) / "sidecars" / "opcua" / "TrustNodeOpcUa.exe")
    try:
        exe_dir = Path(sys.executable).resolve().parent
        out.append(exe_dir / "_internal" / "sidecars" / "opcua" / "TrustNodeOpcUa.exe")
        out.append(exe_dir / "sidecars" / "opcua" / "TrustNodeOpcUa.exe")
    except Exception:
        pass
    return out


def _resolve_sidecar() -> Optional[Path]:
    for p in _candidate_sidecar_paths():
        try:
            if p.exists() and p.is_file():
                return p
        except Exception:
            continue
    return None


def _sidecar_available() -> bool:
    return _resolve_sidecar() is not None


def _find_free_loopback_port() -> int:
    """Pick a control port on 127.0.0.1. Tries the deterministic
    14840+ range so debugging across restarts is easier; falls back to
    OS-assigned if everything in that range is taken.
    """
    for cand in range(14840, 14860):
        sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sk.bind(("127.0.0.1", cand))
            return cand
        except OSError:
            continue
        finally:
            try: sk.close()
            except Exception: pass
    # Fallback
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sk.bind(("127.0.0.1", 0))
    p = sk.getsockname()[1]
    sk.close()
    return int(p)


def is_running() -> bool:
    with _lock:
        return _proc is not None and _proc.poll() is None


def last_error() -> str:
    return _last_error


def current_port() -> int:
    with _lock:
        return _current_port if (_proc is not None and _proc.poll() is None) else 0


def _build_publishable(bootstrap: Dict[str, Any]) -> Dict[str, str]:
    """Returns {"<gid>::<tag>": device_name} for each tag flagged for OPC.
    Reads both inline `publish_opcua` per-tag dicts and the keyed
    `app_settings.tag_publish_flags` map.
    """
    out: Dict[str, str] = {}
    s = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
    flags = s.get("tag_publish_flags") if isinstance(s.get("tag_publish_flags"), dict) else {}
    gws = bootstrap.get("gateway_configurations") or {}
    if not isinstance(gws, dict):
        return out
    for gid, gw in gws.items():
        if not isinstance(gw, dict):
            continue
        tag_list = gw.get("tags") or []
        devices = gw.get("devices") if isinstance(gw.get("devices"), list) else []
        if not devices:
            devices = [{"name": "device", "tags": tag_list}]
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            dname = str(dev.get("name") or dev.get("device_name") or "device")
            for tag in (dev.get("tags") or tag_list or []):
                if isinstance(tag, dict):
                    tname = str(tag.get("name") or tag.get("tag_name") or "tag")
                    inline = bool(tag.get("publish_opcua"))
                else:
                    tname = str(tag)
                    inline = False
                key = f"{gid}::{tname}"
                fe = flags.get(key) or {}
                if not (inline or bool(fe.get("opcua"))):
                    continue
                out[key] = dname
    return out


def start(port: int = 4840, endpoint: Optional[str] = None,
          server_name: str = "TrustNode Edge OPC UA",
          anonymous: bool = True, username: str = "", password: str = "",
          bootstrap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    global _proc, _last_error, _current_port, _control_port, _publishable_devices
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return {"ok": True, "running": True, "port": _current_port, "note": "already running"}
    _last_error = ""
    sidecar = _resolve_sidecar()
    if sidecar is None:
        _last_error = "TrustNodeOpcUa.exe not found in backend/sidecars/opcua/"
        return {"ok": False, "running": False, "port": int(port), "note": _last_error}
    if bootstrap is None:
        try:
            from app.state import app_store
            bootstrap = app_store.get_bootstrap() or {}
        except Exception:
            bootstrap = {}
    _publishable_devices = _build_publishable(bootstrap or {})
    ctl = _find_free_loopback_port()
    args = [
        str(sidecar),
        "--opc-port", str(int(port)),
        "--control-port", str(int(ctl)),
        "--server-name", str(server_name or "TrustNode Edge OPC UA"),
    ]
    if anonymous:
        args.append("--anonymous")
    else:
        args.append("--no-anonymous")
        if username:
            args.extend(["--username", str(username)])
        if password:
            args.extend(["--password", str(password)])
    creationflags = 0
    if sys.platform == "win32":
        creationflags = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            cwd=str(sidecar.parent),
        )
    except Exception as exc:
        _last_error = f"spawn failed: {type(exc).__name__}: {exc}"
        return {"ok": False, "running": False, "port": int(port), "note": _last_error}
    with _lock:
        _proc = proc
        _current_port = int(port)
        _control_port = int(ctl)
    # Wait for "ready" line on stderr (sidecar prints "[opcua] ready") or early exit.
    deadline = time.time() + 8.0
    ready = False
    err_lines: list[str] = []
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            # Process died — pull stderr.
            try:
                _, err = proc.communicate(timeout=0.5)
                err_text = err.decode("utf-8", errors="replace") if err else ""
            except Exception:
                err_text = ""
            _last_error = f"sidecar exited code={rc}: {err_text[:400].strip()}"
            with _lock:
                _proc = None
                _current_port = 0
                _control_port = 0
            return {"ok": False, "running": False, "port": int(port), "note": _last_error}
        line = proc.stderr.readline()
        if not line:
            time.sleep(0.05)
            continue
        text = line.decode("utf-8", errors="replace").strip()
        err_lines.append(text)
        if "ready" in text.lower():
            ready = True
            break
    if not ready:
        _last_error = f"sidecar did not signal ready within 8s: {' | '.join(err_lines)[:400]}"
        try:
            proc.terminate()
        except Exception:
            pass
        with _lock:
            _proc = None
            _current_port = 0
            _control_port = 0
        return {"ok": False, "running": False, "port": int(port), "note": _last_error}
    # Drain stderr in background so the pipe doesn't fill and block.
    threading.Thread(target=_drain_stderr, args=(proc,), daemon=True, name="tn-opcua-stderr").start()
    return {"ok": True, "running": True, "port": int(port), "control_port": int(ctl)}


def _drain_stderr(proc: subprocess.Popen) -> None:
    try:
        for line in proc.stderr:
            try:
                logger.info("opcua sidecar: %s", line.decode("utf-8", errors="replace").rstrip())
            except Exception:
                pass
    except Exception:
        pass


def stop() -> Dict[str, Any]:
    global _proc, _current_port, _control_port
    with _lock:
        proc = _proc
        ctl = _control_port
        _proc = None
        _current_port = 0
        _control_port = 0
    if proc is not None and proc.poll() is None:
        # Ask politely first via /shutdown, then SIGTERM, then SIGKILL.
        if ctl:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{ctl}/shutdown", data=b"{}",
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                urllib.request.urlopen(req, timeout=2.0).read()
            except Exception:
                pass
        try:
            proc.wait(timeout=3.0)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    return {"ok": True, "running": False}


def publish_tag(gateway_id: str, device_name: str, tag_name: str,
                value: Any, ts_utc: Optional[str] = None,
                quality: Optional[str] = None) -> None:
    """Push one tag value to the sidecar via the control channel.
    The sidecar materialises the folder/variable nodes on first contact.
    No-op when the sidecar isn't running or the tag isn't flagged.
    """
    if _proc is None or _proc.poll() is not None or _control_port == 0:
        return
    key = f"{str(gateway_id)}::{str(tag_name)}"
    if key not in _publishable_devices:
        return
    try:
        numeric = float(value) if value is not None and value != "" else 0.0
    except Exception:
        return
    body = json.dumps({
        "gateway": str(gateway_id),
        "device": device_name or _publishable_devices.get(key) or "device",
        "tag": str(tag_name),
        "value": numeric,
        "ts": ts_utc or "",
        "quality": quality or "good",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{_control_port}/update", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=0.4).read()
    except Exception:
        # Drop silently — never block the gateway tick.
        pass
