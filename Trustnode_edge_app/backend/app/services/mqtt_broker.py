"""MQTT broker + publisher (operator 2026-06-17, Phase 3).

Edge-side MQTT broker that LAN clients can subscribe to. Tags must
opt-in via the per-tag `publish_mqtt` flag.

Library: `amqtt` (MIT-licensed) for the broker. Pure Python, no native
deps, bundles cleanly with PyInstaller.

Topic convention:
  trustnode/<tenant_id>/<edge_id>/<gateway_name>/<device_name>/<tag_name>

Payload (JSON):
  {"value": <number|string>, "ts_utc": "<iso>", "quality": "<good|bad>"}
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_broker = None
_last_error: str = ""
_current_port: int = 0
_publishable: set[str] = set()   # "gid::device::tag" keys
_tenant_id: str = "default"
_edge_id: str = "edge-01"


def is_running() -> bool:
    with _lock:
        return _broker is not None and _thread is not None and _thread.is_alive()


def last_error() -> str:
    return _last_error


def current_port() -> int:
    with _lock:
        return _current_port if is_running() else 0


def _build_publishable_set(bootstrap: Dict[str, Any]) -> set[str]:
    """Returns set of "<gid>::<device>::<tag>" keys. Reads both the
    keyed flat map app_settings.tag_publish_flags AND any inline
    publish_mqtt flag on a per-tag dict.
    """
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


def _serve(port: int, anonymous: bool, username: str, password: str) -> None:
    global _broker, _loop, _last_error, _current_port
    try:
        from amqtt.broker import Broker
    except Exception as exc:
        _last_error = f"amqtt import failed: {type(exc).__name__}: {exc}"
        logger.warning("mqtt_broker: %s", _last_error)
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with _lock:
        _loop = loop

    cfg: Dict[str, Any] = {
        "listeners": {
            "default": {
                "type": "tcp",
                "bind": f"0.0.0.0:{int(port)}",
                "max_connections": 200,
            }
        },
        "sys_interval": 0,
        "auth": {
            "allow-anonymous": bool(anonymous),
        },
        "topic-check": {"enabled": False},
    }
    if not anonymous and username:
        # amqtt's password file auth is overkill for a single LAN user;
        # use the anonymous-allow flag + an in-memory check on connect
        # only if we ever wire up a custom plugin. Today: anonymous true
        # by default for LAN ease; toggle off and provide username only
        # to lock anonymous out (effective deny-all without plugin).
        cfg["auth"]["allow-anonymous"] = False

    async def _run() -> None:
        global _broker, _last_error, _current_port
        try:
            broker = Broker(cfg)
            await broker.start()
            with _lock:
                _broker = broker
                _current_port = int(port)
            while True:
                await asyncio.sleep(1.0)
        except OSError as exc:
            _last_error = f"bind failed on port {port}: {exc.errno} {exc.strerror or exc}"
            logger.warning("mqtt_broker: %s", _last_error)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _last_error = f"crashed: {type(exc).__name__}: {exc}"
            logger.warning("mqtt_broker: %s", _last_error)
        finally:
            try:
                if _broker is not None:
                    await _broker.shutdown()
            except Exception:
                pass
            with _lock:
                _broker = None
                _current_port = 0

    try:
        loop.run_until_complete(_run())
    finally:
        try:
            loop.close()
        except Exception:
            pass
        with _lock:
            _loop = None


def start(port: int = 1883, anonymous: bool = True,
          username: str = "", password: str = "",
          tenant_id: str = "default", edge_id: str = "edge-01",
          bootstrap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    global _thread, _last_error, _publishable, _tenant_id, _edge_id
    with _lock:
        if _broker is not None and _thread is not None and _thread.is_alive():
            return {"ok": True, "running": True, "port": _current_port, "note": "already running"}
    _last_error = ""
    if bootstrap is None:
        try:
            from app.state import app_store
            bootstrap = app_store.get_bootstrap() or {}
        except Exception:
            bootstrap = {}
    _publishable = _build_publishable_set(bootstrap or {})
    _tenant_id = str(tenant_id or "default")
    _edge_id = str(edge_id or "edge-01")
    with _lock:
        _thread = threading.Thread(
            target=_serve,
            args=(int(port), bool(anonymous), str(username or ""), str(password or "")),
            daemon=True,
            name="tn-mqtt-broker",
        )
        _thread.start()
    import time as _t
    for _ in range(40):
        _t.sleep(0.1)
        if is_running():
            return {"ok": True, "running": True, "port": int(port)}
        if _last_error:
            return {"ok": False, "running": False, "port": int(port), "note": _last_error}
    return {"ok": False, "running": False, "port": int(port), "note": _last_error or "failed to start (timeout)"}


def stop() -> Dict[str, Any]:
    global _thread, _broker, _loop
    with _lock:
        loop = _loop
        thread = _thread
    if loop is not None:
        try:
            def _cancel_all():
                for task in asyncio.all_tasks(loop=loop):
                    task.cancel()
            loop.call_soon_threadsafe(_cancel_all)
        except Exception:
            pass
    if thread is not None:
        thread.join(timeout=3.0)
    with _lock:
        _thread = None
        _broker = None
        _loop = None
        _publishable.clear()
    return {"ok": True, "running": False}


def publish_tag(gateway_id: str, gateway_name: str, device_name: str,
                tag_name: str, value: Any, ts_utc: Optional[str] = None,
                quality: Optional[str] = None) -> None:
    """Publish a tag value to the broker so subscribed clients get it.
    No-op if the broker isn't running or the tag isn't flagged
    `publish_mqtt`.
    """
    key = f"{str(gateway_id)}::{str(device_name)}::{str(tag_name)}"
    with _lock:
        if not (_broker is not None and key in _publishable):
            return
        loop = _loop
    if loop is None or loop.is_closed():
        return
    topic = f"trustnode/{_tenant_id}/{_edge_id}/{gateway_name}/{device_name}/{tag_name}"
    payload = json.dumps({
        "value": value,
        "ts_utc": ts_utc or "",
        "quality": quality or "good",
    }).encode()

    async def _pub():
        try:
            from amqtt.mqtt.constants import QOS_1
            # The broker can publish via its session manager API.
            await _broker._broadcast_message(  # noqa: SLF001 — only public path
                source_session=None,
                topic=topic,
                data=payload,
                force_qos=QOS_1,
            )
        except Exception:
            pass

    try:
        asyncio.run_coroutine_threadsafe(_pub(), loop)
    except Exception:
        pass
