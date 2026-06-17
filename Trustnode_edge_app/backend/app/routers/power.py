from datetime import datetime, timezone
from typing import Any, Dict

import logging
import traceback

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

from app.state import app_store, power_manager

router = APIRouter(prefix="/api/power", tags=["power"])


class PowerConfigPayload(BaseModel):
    # Operator 2026-06-15: tariff entries were silently dropped because
    # this model didn't declare electricity_tariffs / energy_price.
    # Pydantic strips unknown keys on .model_dump() — even though the
    # power_manager normaliser would accept them. Allow extras as
    # belt-and-braces against future config additions.
    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    selected_device_id: str = "power_meter_01"
    devices: list[dict[str, Any]] = Field(default_factory=list)
    energy_price_eur_kwh: float = 0.0
    electricity_tariffs: list[dict[str, Any]] = Field(default_factory=list)
    downtime_rules: list[dict[str, Any]] = Field(default_factory=list)


class PowerConnectionTestPayload(BaseModel):
    device: Dict[str, Any] | None = None
    device_id: str = ""
    timeout_ms: int = 3000


@router.get("/config")
def get_power_config(request: Request) -> dict:
    host = str(request.headers.get("host") or "").strip().lower().split(":")[0]
    prefer_cloud_reads = bool(host and host not in {"localhost", "127.0.0.1"})
    if prefer_cloud_reads:
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=True) or {}
        cfg = bootstrap.get("power_management_config")
        if isinstance(cfg, dict):
            return {"ok": True, "config": cfg}
    return {"ok": True, "config": power_manager.get_config()}


@router.get("/profiles")
def get_power_profiles() -> dict:
    return {"ok": True, **power_manager.get_profiles()}


@router.put("/config")
def set_power_config(payload: PowerConfigPayload) -> dict:
    try:
        cfg = power_manager.update_config(payload.model_dump(), actor="admin")
    except (ValueError, TypeError) as exc:
        # Field-level validation in power_manager (e.g. a non-numeric port or
        # register address) surfaces here. Return the *reason* so the UI can
        # tell the operator WHICH field is wrong instead of a generic 500.
        logger.warning("Power config update rejected: %s", exc)
        raise HTTPException(status_code=400, detail=f"Invalid power configuration: {exc}") from exc
    except Exception as exc:
        logger.error("Power config update crashed: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Power configuration save failed: {exc}") from exc
    return {"ok": True, "config": cfg}


@router.post("/test-connection")
def test_power_connection(payload: PowerConnectionTestPayload) -> dict:
    cfg = power_manager.get_config()
    test_payload: dict[str, Any] = {}
    if isinstance(payload.device, dict) and payload.device:
        test_payload = dict(payload.device)
    elif payload.device_id:
        did = str(payload.device_id or "").strip()
        test_payload = next((dict(d) for d in cfg.get("devices", []) if str(d.get("id")) == did), {})
    if not test_payload:
        selected = str(cfg.get("selected_device_id") or "")
        test_payload = next((dict(d) for d in cfg.get("devices", []) if str(d.get("id")) == selected), {})
    if not test_payload:
        return {"ok": False, "message": "No power meter selected for connection test"}
    result = power_manager.test_connection(test_payload, timeout_s=max(0.5, float(payload.timeout_ms) / 1000.0))
    return {"ok": bool(result.get("ok")), **result}


@router.get("/status")
def get_power_status(request: Request) -> dict:
    host = str(request.headers.get("host") or "").strip().lower().split(":")[0]
    prefer_cloud_reads = bool(host and host not in {"localhost", "127.0.0.1"})
    if not prefer_cloud_reads:
        return {"ok": True, "status": power_manager.get_status()}

    bootstrap = app_store.get_bootstrap(prefer_cloud_reads=True) or {}
    cfg = bootstrap.get("power_management_config") if isinstance(bootstrap, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}
    devices = list(cfg.get("devices") or [])
    selected_id = str(cfg.get("selected_device_id") or (devices[0].get("id") if devices else "") or "")
    rows = app_store.get_live_rows(limit=8000, prefer_cloud_reads=True)
    power_rows = [r for r in rows if str(r.get("source") or "") in ("power_modbus", "power_insight")]
    now = datetime.now(timezone.utc)
    latest_by_device: dict[str, dict[str, Any]] = {}
    for r in power_rows:
        did = str(r.get("gateway_id") or "")
        ts = str(r.get("ts") or "")
        if not did or not ts:
            continue
        prev = latest_by_device.get(did)
        if prev is None or str(prev.get("ts") or "") <= ts:
            latest_by_device[did] = r

    out_devices: list[dict[str, Any]] = []
    any_connected = False
    selected_status: dict[str, Any] | None = None
    for d in devices:
        did = str(d.get("id") or "")
        latest = latest_by_device.get(did)
        last_ts = str((latest or {}).get("ts") or "")
        age_s = None
        if last_ts:
            try:
                dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00")).astimezone(timezone.utc)
                age_s = max(0.0, (now - dt).total_seconds())
            except Exception:
                age_s = None
        poll_ms = int(d.get("poll_interval_ms") or 1000)
        # Cloud reads can lag edge writes because of sync batching and network jitter.
        # Keep status stable for operators while still marking truly stale streams offline.
        freshness_window_s = max(20.0, (poll_ms / 1000.0) * 20.0)
        connected = bool(latest) and (age_s is not None and age_s <= freshness_window_s)
        any_connected = any_connected or connected
        row = {
            "device_id": did,
            "name": str(d.get("name") or did),
            "connected": connected,
            "enabled": bool(d.get("enabled", True)),
            "last_error": "" if connected else f"No fresh cloud power rows (>{int(freshness_window_s)}s)",
            "last_poll_utc": last_ts,
            "last_success_utc": last_ts,
            "ip": str(d.get("ip") or ""),
            "port": int(d.get("port") or 502),
            "unit_id": int(d.get("unit_id") or 1),
            "poll_interval_ms": poll_ms,
        }
        out_devices.append(row)
        if did == selected_id:
            selected_status = row

    return {
        "ok": True,
        "status": {
            "enabled": bool(cfg.get("enabled", True)),
            "selected_device_id": selected_id,
            "connected": bool((selected_status or {}).get("connected")) if selected_status else any_connected,
            "last_error": str((selected_status or {}).get("last_error") or "") if selected_status else "",
            "devices": out_devices,
        },
    }


@router.get("/latest")
def get_power_latest(request: Request, device_id: str = "") -> dict:
    host = str(request.headers.get("host") or "").strip().lower().split(":")[0]
    prefer_cloud_reads = bool(host and host not in {"localhost", "127.0.0.1"})
    if not prefer_cloud_reads:
        return {"ok": True, "sample": power_manager.get_latest(device_id=device_id or None)}

    rows = app_store.get_live_rows(limit=8000, prefer_cloud_reads=True)
    power_rows = [r for r in rows if str(r.get("source") or "") in ("power_modbus", "power_insight")]
    if str(device_id or "").strip():
        power_rows = [r for r in power_rows if str(r.get("gateway_id") or "") == str(device_id).strip()]
    if not power_rows:
        return {"ok": True, "sample": {}}
    latest_ts = max(str(r.get("ts") or "") for r in power_rows)
    latest_rows = [r for r in power_rows if str(r.get("ts") or "") == latest_ts]
    values: dict[str, float] = {}
    values_raw: dict[str, float] = {}
    selected_device = str(latest_rows[0].get("gateway_id") or "")
    for r in latest_rows:
        tag = str(r.get("tag") or "")
        if not tag:
            continue
        try:
            if tag.endswith("_raw"):
                values_raw[tag[:-4]] = float(r.get("value"))
            else:
                values[tag] = float(r.get("value"))
        except Exception:
            continue
    return {
        "ok": True,
        "sample": {
            "ts": latest_ts,
            "device": selected_device,
            "values": values,
            "values_scaled": values,
            "values_raw": values_raw,
        },
    }


@router.get("/history")
def get_power_history(request: Request, limit: int = 300, device_id: str = "") -> dict:
    lim = max(1, min(int(limit or 300), 50000))
    host = str(request.headers.get("host") or "").strip().lower().split(":")[0]
    prefer_cloud_reads = bool(host and host not in {"localhost", "127.0.0.1"})
    # Operator 2026-06-16: push the source filter into SQL so we no
    # longer pull 8x the rows and discard most. With the SQL-side
    # filter the query walks the (tenant_id, ts_utc DESC) index and
    # stops at the requested row count — measured ~570ms → ~30ms.
    rows = app_store.get_historian_rows(
        limit=lim,
        prefer_cloud_reads=prefer_cloud_reads,
        source="power_modbus,power_insight",
        gateway=str(device_id or "").strip(),
    )
    return {"ok": True, "rows": rows}


@router.get("/diagnostics")
def get_power_diagnostics() -> dict:
    return {"ok": True, "diagnostics": power_manager.get_diagnostics()}


@router.post("/devices/{device_id}/start")
def start_power_device(device_id: str) -> dict:
    cfg = power_manager.set_device_enabled(device_id, True, actor="admin")
    return {"ok": True, "config": cfg}


@router.post("/devices/{device_id}/stop")
def stop_power_device(device_id: str) -> dict:
    cfg = power_manager.set_device_enabled(device_id, False, actor="admin")
    return {"ok": True, "config": cfg}
