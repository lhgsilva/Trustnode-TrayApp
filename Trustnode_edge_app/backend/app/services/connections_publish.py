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


def _runtime_for(channel: str) -> str:
    """Returns 'python' or 'native'."""
    try:
        from app.state import app_store
        bootstrap = app_store.get_bootstrap() or {}
    except Exception:
        return "python"
    s = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
    conn = s.get("connections") if isinstance(s, dict) and isinstance(s.get("connections"), dict) else {}
    cfg = conn.get(channel) if isinstance(conn.get(channel), dict) else {}
    rt = str(cfg.get("runtime") or "python").lower()
    return "native" if rt == "native" else "python"


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
