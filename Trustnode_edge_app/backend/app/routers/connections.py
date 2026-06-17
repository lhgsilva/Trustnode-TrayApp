"""Connections router (operator 2026-06-17, Phase 3).

Endpoints under /api/connections/* let the operator turn outward
OPC UA / MQTT services on or off from the edge UI. Settings persist in
``app_settings.connections.{opcua,mqtt}`` so a restart re-applies them.

Like LAN sharing, both services run in-process on dedicated daemon
threads — no backend restart needed.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

from app.state import app_store
from app.services import opcua_server, mqtt_broker
from app.services import opcua_server_dotnet, mqtt_broker_mosquitto

router = APIRouter(prefix="/api/connections", tags=["connections"])


def _opcua_runtime(cfg: dict | None = None):
    """Return the OPC UA runtime module (python asyncua or .NET sidecar)
    based on the saved config. Defaults to python if missing/invalid.
    """
    rt = "python"
    if isinstance(cfg, dict):
        rt = str(cfg.get("runtime") or "python").lower()
    return opcua_server_dotnet if rt == "native" else opcua_server


def _mqtt_runtime(cfg: dict | None = None):
    rt = "python"
    if isinstance(cfg, dict):
        rt = str(cfg.get("runtime") or "python").lower()
    return mqtt_broker_mosquitto if rt == "native" else mqtt_broker


def _load_settings() -> Dict[str, Any]:
    try:
        bootstrap = app_store.get_bootstrap() or {}
    except Exception:
        bootstrap = {}
    s = bootstrap.get("app_settings") or {}
    if not isinstance(s, dict):
        s = {}
    conn = s.get("connections") if isinstance(s.get("connections"), dict) else {}
    return dict(conn)


def _save_settings(conn: Dict[str, Any]) -> None:
    try:
        bootstrap = app_store.get_bootstrap() or {}
    except Exception:
        bootstrap = {}
    s = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
    s = dict(s or {})
    s["connections"] = conn
    app_store.upsert_domain("app_settings", s, actor="connections_router")


def _edge_identity() -> tuple[str, str]:
    try:
        bootstrap = app_store.get_bootstrap() or {}
    except Exception:
        return ("default", "edge-01")
    s = bootstrap.get("app_settings") or {}
    tenant = str(s.get("tenant_id") or "default") if isinstance(s, dict) else "default"
    edge = str(s.get("edge_id") or "edge-01") if isinstance(s, dict) else "edge-01"
    return (tenant, edge)


# ─── OPC UA ──────────────────────────────────────────────────────────

class OpcuaConfig(BaseModel):
    enabled: bool = False
    port: int = 4840
    server_name: str = "TrustNode Edge OPC UA"
    anonymous: bool = True
    username: str = ""
    password: str = ""
    # Runtime selector (operator 2026-06-17). 'python' = bundled
    # asyncua (default, no extra binary). 'native' = OPC Foundation
    # .NET sidecar (installable separately; smaller spec gap, used
    # by industrial-grade SCADA stacks).
    runtime: str = "python"


@router.get("/opcua/status")
def opcua_status() -> dict:
    conn = _load_settings()
    cfg = conn.get("opcua") if isinstance(conn.get("opcua"), dict) else {}
    rt = _opcua_runtime(cfg)
    return {
        "ok": True,
        "running": rt.is_running(),
        "port": rt.current_port(),
        "endpoint": f"opc.tcp://0.0.0.0:{rt.current_port()}/trustnode/edge" if rt.is_running() else "",
        "last_error": rt.last_error(),
        "runtime": (cfg or {}).get("runtime") or "python",
        "config": cfg,
    }


@router.post("/opcua/enable")
def opcua_enable(body: OpcuaConfig) -> dict:
    conn = _load_settings()
    conn["opcua"] = body.model_dump()
    _save_settings(conn)
    rt = _opcua_runtime(conn["opcua"])
    res = rt.start(
        port=int(body.port or 4840),
        server_name=str(body.server_name or "TrustNode Edge OPC UA"),
        anonymous=bool(body.anonymous),
        username=str(body.username or ""),
        password=str(body.password or ""),
    )
    return {"ok": bool(res.get("ok")), "runtime": body.runtime, **res}


@router.post("/opcua/disable")
def opcua_disable() -> dict:
    conn = _load_settings()
    op = conn.get("opcua") if isinstance(conn.get("opcua"), dict) else {}
    op = dict(op)
    op["enabled"] = False
    conn["opcua"] = op
    _save_settings(conn)
    rt = _opcua_runtime(op)
    return rt.stop()


# ─── MQTT ────────────────────────────────────────────────────────────

class MqttConfig(BaseModel):
    enabled: bool = False
    port: int = 1883
    anonymous: bool = True
    username: str = ""
    password: str = ""
    # 'python' = bundled amqtt broker. 'native' = Eclipse Mosquitto
    # sidecar (installable separately; the de-facto industrial broker).
    runtime: str = "python"


@router.get("/mqtt/status")
def mqtt_status() -> dict:
    conn = _load_settings()
    cfg = conn.get("mqtt") if isinstance(conn.get("mqtt"), dict) else {}
    rt = _mqtt_runtime(cfg)
    return {
        "ok": True,
        "running": rt.is_running(),
        "port": rt.current_port(),
        "last_error": rt.last_error(),
        "runtime": (cfg or {}).get("runtime") or "python",
        "config": cfg,
    }


@router.post("/mqtt/enable")
def mqtt_enable(body: MqttConfig) -> dict:
    conn = _load_settings()
    conn["mqtt"] = body.model_dump()
    _save_settings(conn)
    tenant, edge = _edge_identity()
    rt = _mqtt_runtime(conn["mqtt"])
    res = rt.start(
        port=int(body.port or 1883),
        anonymous=bool(body.anonymous),
        username=str(body.username or ""),
        password=str(body.password or ""),
        tenant_id=tenant,
        edge_id=edge,
    )
    return {"ok": bool(res.get("ok")), "runtime": body.runtime, **res}


@router.post("/mqtt/disable")
def mqtt_disable() -> dict:
    conn = _load_settings()
    mq = conn.get("mqtt") if isinstance(conn.get("mqtt"), dict) else {}
    mq = dict(mq)
    mq["enabled"] = False
    conn["mqtt"] = mq
    _save_settings(conn)
    rt = _mqtt_runtime(mq)
    return rt.stop()
