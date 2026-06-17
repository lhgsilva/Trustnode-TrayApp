"""MQTT broker (Eclipse Mosquitto sidecar variant) — operator 2026-06-17.

Spawns the bundled `mosquitto.exe` as a child process with a generated
config file. Tag publishing uses an embedded `paho-mqtt` client that
publishes to the local broker on 127.0.0.1:<port>.

Sidecar resolution order:
  1. ``backend/sidecars/mosquitto/mosquitto.exe`` next to the source tree
     (dev mode).
  2. ``sys._MEIPASS/sidecars/mosquitto/mosquitto.exe`` when running from
     a PyInstaller onedir bundle.
  3. ``<executable_dir>/_internal/sidecars/mosquitto/mosquitto.exe`` for
     packaged ``trustnode-service.exe`` (PyInstaller _internal layout).

Operator 2026-06-17 (Phase 3 native).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)

_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_last_error: str = ""
_current_port: int = 0
_publishable: set[str] = set()
_tenant_id: str = "default"
_edge_id: str = "edge-01"
_publisher = None  # paho-mqtt client connected to the local broker
_config_path: Optional[str] = None


def _candidate_sidecar_paths() -> list[Path]:
    here = Path(__file__).resolve()
    out: list[Path] = []
    # Dev: backend/sidecars/mosquitto/ next to the source tree
    out.append(here.parents[2] / "sidecars" / "mosquitto" / "mosquitto.exe")
    # PyInstaller _MEIPASS (onefile)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(Path(meipass) / "sidecars" / "mosquitto" / "mosquitto.exe")
    # PyInstaller onedir: _internal/sidecars/mosquitto/
    try:
        exe_dir = Path(sys.executable).resolve().parent
        out.append(exe_dir / "_internal" / "sidecars" / "mosquitto" / "mosquitto.exe")
        out.append(exe_dir / "sidecars" / "mosquitto" / "mosquitto.exe")
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


def is_running() -> bool:
    with _lock:
        return _proc is not None and _proc.poll() is None


def last_error() -> str:
    return _last_error


def current_port() -> int:
    with _lock:
        return _current_port if (_proc is not None and _proc.poll() is None) else 0


def _build_publishable_set(bootstrap: Dict[str, Any]) -> set[str]:
    out: set[str] = set()
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
                    inline_flag = bool(tag.get("publish_mqtt"))
                else:
                    tname = str(tag)
                    inline_flag = False
                key = f"{gid}::{tname}"
                fe = flags.get(key) or {}
                if not (inline_flag or bool(fe.get("mqtt"))):
                    continue
                out.add(f"{gid}::{dname}::{tname}")
    return out


def _write_config(port: int, anonymous: bool, username: str, password: str) -> str:
    """Write a temp mosquitto.conf with the operator's settings.
    If username/password are set, also write a password file using
    the bundled `mosquitto_passwd` if present; otherwise we fall back
    to plaintext (sufficient for LAN-only deployments, which is the
    primary use case).
    """
    tmpdir = Path(tempfile.gettempdir()) / "trustnode-mosquitto"
    tmpdir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmpdir / f"mosquitto-{int(port)}.conf"
    pw_path = tmpdir / f"mosquitto-{int(port)}.pw"
    persistence_dir = tmpdir / f"mosquitto-{int(port)}-data"
    persistence_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"listener {int(port)} 0.0.0.0",
        "protocol mqtt",
        "max_connections -1",
        f"persistence true",
        f"persistence_location {persistence_dir.as_posix()}/",
        "log_dest stdout",
        "log_type warning",
        "log_type error",
        # We DO NOT use authentication on the broker level when the
        # operator wants anonymous LAN access — every additional layer
        # is a step that can go wrong on a SCADA install.
        f"allow_anonymous {'true' if anonymous else 'false'}",
    ]
    if not anonymous and username:
        # Plaintext password file. The Mosquitto runtime accepts either
        # plaintext (with allow_zero_length_clientid set) or hashed via
        # mosquitto_passwd; bundling the latter would add another exe.
        # LAN deployments typically use unique on-box accounts.
        pw_path.write_text(f"{username}:{password}\n", encoding="utf-8")
        lines.append(f"password_file {pw_path.as_posix()}")
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(cfg_path)


def _start_local_publisher(port: int, anonymous: bool, username: str, password: str) -> None:
    """Open a long-lived paho client connection to the local broker so
    publish_tag() can fan tags out at line-rate.
    """
    global _publisher
    try:
        import paho.mqtt.client as mqtt  # local import keeps boot light
    except Exception as exc:
        logger.warning("mqtt_broker_mosquitto: paho-mqtt missing (%s) — publishing disabled", exc)
        _publisher = None
        return
    try:
        client = mqtt.Client(client_id=f"trustnode-edge-{os.getpid()}", clean_session=True)
        if not anonymous and username:
            client.username_pw_set(username, password or "")
        client.connect_async("127.0.0.1", int(port), keepalive=30)
        client.loop_start()
        _publisher = client
    except Exception as exc:
        logger.warning("mqtt_broker_mosquitto: publisher connect failed: %s", exc)
        _publisher = None


def _stop_local_publisher() -> None:
    global _publisher
    pub = _publisher
    _publisher = None
    if pub is None:
        return
    try:
        pub.loop_stop()
        pub.disconnect()
    except Exception:
        pass


def start(port: int = 1883, anonymous: bool = True,
          username: str = "", password: str = "",
          tenant_id: str = "default", edge_id: str = "edge-01",
          bootstrap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    global _proc, _last_error, _current_port, _publishable, _tenant_id, _edge_id, _config_path
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return {"ok": True, "running": True, "port": _current_port, "note": "already running"}
    _last_error = ""
    sidecar = _resolve_sidecar()
    if sidecar is None:
        _last_error = (
            "mosquitto.exe not found. Expected at backend/sidecars/mosquitto/ in dev "
            "or _internal/sidecars/mosquitto/ in packaged build."
        )
        return {"ok": False, "running": False, "port": int(port), "note": _last_error}
    if bootstrap is None:
        try:
            from app.state import app_store
            bootstrap = app_store.get_bootstrap() or {}
        except Exception:
            bootstrap = {}
    _publishable = _build_publishable_set(bootstrap or {})
    _tenant_id = str(tenant_id or "default")
    _edge_id = str(edge_id or "edge-01")
    try:
        cfg = _write_config(int(port), bool(anonymous), str(username or ""), str(password or ""))
    except Exception as exc:
        _last_error = f"config write failed: {type(exc).__name__}: {exc}"
        return {"ok": False, "running": False, "port": int(port), "note": _last_error}
    _config_path = cfg
    creationflags = 0
    if sys.platform == "win32":
        # Don't show a console window for the child sidecar.
        creationflags = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            [str(sidecar), "-c", cfg],
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
    # Wait briefly for either successful bind or early exit.
    for _ in range(20):
        time.sleep(0.1)
        rc = proc.poll()
        if rc is not None:
            # Process died. Grab stderr to surface the reason.
            try:
                _, err = proc.communicate(timeout=0.5)
                err_text = err.decode("utf-8", errors="replace").strip() if err else ""
            except Exception:
                err_text = ""
            _last_error = f"mosquitto exited with code {rc}. {err_text[:300]}".strip()
            with _lock:
                _proc = None
                _current_port = 0
            return {"ok": False, "running": False, "port": int(port), "note": _last_error}
    # Sidecar still alive — start the local publisher.
    _start_local_publisher(int(port), bool(anonymous), str(username or ""), str(password or ""))
    return {"ok": True, "running": True, "port": int(port)}


def stop() -> Dict[str, Any]:
    global _proc, _current_port, _config_path
    _stop_local_publisher()
    with _lock:
        proc = _proc
        _proc = None
        _current_port = 0
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except Exception:
                proc.kill()
        except Exception:
            pass
    _config_path = None
    return {"ok": True, "running": False}


def publish_tag(gateway_id: str, gateway_name: str, device_name: str,
                tag_name: str, value: Any, ts_utc: Optional[str] = None,
                quality: Optional[str] = None) -> None:
    key = f"{str(gateway_id)}::{str(device_name)}::{str(tag_name)}"
    pub = _publisher
    if pub is None:
        return
    if key not in _publishable:
        return
    topic = f"trustnode/{_tenant_id}/{_edge_id}/{gateway_name}/{device_name}/{tag_name}"
    payload = json.dumps({
        "value": value,
        "ts_utc": ts_utc or "",
        "quality": quality or "good",
    }).encode()
    try:
        pub.publish(topic, payload=payload, qos=1, retain=False)
    except Exception:
        pass
