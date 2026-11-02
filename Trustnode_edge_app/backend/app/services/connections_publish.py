"""Connections publish helper (operator 2026-06-17).

Selects the active OPC UA / MQTT runtime based on saved app_settings
and dispatches a tag publish to it. Used by plc_manager / power_manager
so they don't need to know which runtime is currently running.

Both runtimes are no-ops when their corresponding service isn't running,
so calling either unconditionally is safe — but reading settings once
per batch keeps us from importing both modules into every gateway tick.
"""
from __future__ import annotations

from typing import Any, Optional


# Operator 2026-08-21 (CPU HOT-LOOP FIX): _runtime_for used to call
# app_store.get_bootstrap() — a full read + JSON parse of EVERY config document
# under app_store._lock — for EVERY tag on EVERY channel, every cycle: with 48
# tags that was ~96 full config parses per second. py-spy showed the V2
# distribution thread spending ~60% of its time there, the backlog climbing
# past 300 cycles, CPU at 140-300%, and every other _lock user (health,
# deferred boot init, config loops) queuing behind it. The runtime choice
# changes only when the operator saves Connections settings, so a 10 s TTL
# cache is more than fresh enough.
import time as _time

_RUNTIME_CACHE: dict = {"at": 0.0, "vals": {}}
_RUNTIME_TTL_S = 10.0


def _runtime_for(channel: str) -> str:
    """Returns 'python' or 'native' (cached per channel for _RUNTIME_TTL_S)."""
    now = _time.monotonic()
    vals = _RUNTIME_CACHE["vals"]
    if vals and (now - _RUNTIME_CACHE["at"]) < _RUNTIME_TTL_S:
        return vals.get(channel, "python")
    try:
        from app.state import app_store
        bootstrap = app_store.get_bootstrap() or {}
    except Exception:
        return vals.get(channel, "python") if vals else "python"
    s = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
    conn = s.get("connections") if isinstance(s, dict) and isinstance(s.get("connections"), dict) else {}
    fresh = {}
    for ch in ("opcua", "mqtt"):
        cfg = conn.get(ch) if isinstance(conn.get(ch), dict) else {}
        rt = str(cfg.get("runtime") or "python").lower()
        fresh[ch] = "native" if rt == "native" else "python"
    _RUNTIME_CACHE["vals"] = fresh
    _RUNTIME_CACHE["at"] = now
    return fresh.get(channel, "python")


_ACTIVE_CACHE: dict = {"val": False, "at": 0.0}


def any_outbound_active() -> bool:
    """Is ANY outbound service actually running right now?

    2026-10-30. Collection calls this before touching the publish path, so a
    service that is stopped, unlicensed or broken costs the gateway nothing.
    Both checks are flag reads behind a reentrant lock; neither does I/O.

    Cached for a second: the collection loop asks once per cycle per gateway
    and the answer cannot change meaningfully faster than that.
    """
    import time as _t
    now = _t.monotonic()
    hit = _ACTIVE_CACHE.get("at", 0.0)
    if hit and (now - hit) < 1.0:
        return bool(_ACTIVE_CACHE.get("val"))
    active = False
    for ch in ("opcua", "mqtt"):
        try:
            if _runtime_for(ch) == "native":
                if ch == "opcua":
                    from app.services import opcua_server_dotnet as svc
                else:
                    from app.services import mqtt_broker_mosquitto as svc
            else:
                if ch == "opcua":
                    from app.services import opcua_server as svc
                else:
                    from app.services import mqtt_broker as svc
            if svc.is_running():
                active = True
                break
        except Exception:
            continue
    _ACTIVE_CACHE["val"] = active
    _ACTIVE_CACHE["at"] = now
    return active


def publish_opcua(gateway_id: str, device_name: str, tag_name: str,
                  value: Any, ts_utc: Optional[str] = None,
                  quality: Optional[str] = None) -> None:
    try:
        if _runtime_for("opcua") == "native":
            from app.services import opcua_server_dotnet as svc
        else:
            from app.services import opcua_server as svc
        svc.publish_tag(
            gateway_id=gateway_id, device_name=device_name, tag_name=tag_name,
            value=value, ts_utc=ts_utc, quality=quality,
        )
    except Exception:
        pass


def publish_mqtt(gateway_id: str, gateway_name: str, device_name: str,
                 tag_name: str, value: Any, ts_utc: Optional[str] = None,
                 quality: Optional[str] = None) -> None:
    try:
        if _runtime_for("mqtt") == "native":
            from app.services import mqtt_broker_mosquitto as svc
        else:
            from app.services import mqtt_broker as svc
        svc.publish_tag(
            gateway_id=gateway_id, gateway_name=gateway_name,
            device_name=device_name, tag_name=tag_name,
            value=value, ts_utc=ts_utc, quality=quality,
        )
    except Exception:
        pass
