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
    prefer_cloud_reads = bool(app_store._prefer_cloud_reads())  # 2026-08-21: explicit deployment flag, not the Host header
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
        # exclude_unset: every field here has a default (devices -> []), so a
        # plain model_dump() turns "save the tariffs" into "save a config with
        # no meters" and the merge in update_config would have nothing to keep.
        # Only what the client actually sent reaches the store (2026-08-26).
        cfg = power_manager.update_config(
            payload.model_dump(exclude_unset=True), actor="admin")
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


@router.get("/meter-models")
def list_meter_models() -> dict:
    """Pre-loaded supplier register maps, so a known meter works out of the box.

    Added 2026-08-27 after an EM122 was configured with the EM525 map: it
    connected, polled, and returned 0.0000 for every value. A meter that reads
    zeros is worse than one that errors, because nothing looks wrong.
    """
    from app.services.meter_registers import (
        METER_MODELS, REGISTER_LABELS, describe_address)
    models = []
    for m in METER_MODELS:
        regs = m.get("registers") or {}
        models.append({
            **{k: v for k, v in m.items() if k != "registers"},
            "registers": dict(regs),
            "register_count": len(regs),
            "labels": {k: REGISTER_LABELS.get(k, k) for k in regs},
            "preview": [
                {"key": k, "address": a, "reads_as": describe_address(a),
                 "label": REGISTER_LABELS.get(k, k)}
                for k, a in list(regs.items())[:6]
            ],
        })
    return {"ok": True, "models": models}


class RegisterTableImport(BaseModel):
    """A supplier register table, pasted or read from an uploaded file."""
    text: str = ""


@router.post("/parse-register-table")
def parse_register_table(payload: RegisterTableImport) -> dict:
    """Turn a supplier's register table into registers we can collect.

    Retyping 33 rows out of a datasheet is where wrong addresses come from, so
    the operator pastes the table instead. Tolerant of CSV, TSV and text copied
    out of a PDF, and it reports what it could NOT read rather than silently
    dropping rows.
    """
    from app.services.meter_registers import parse_supplier_table, describe_address
    result = parse_supplier_table(payload.text or "")
    for row in result.get("rows") or []:
        row["reads_as"] = describe_address(row.get("address"))
    return result


@router.get("/address-help")
def address_help(address: str = "") -> dict:
    """How one address will actually be read - shown beside the input so the
    datasheet-to-offset conversion is visible instead of surprising."""
    from app.services.meter_registers import describe_address, normalize_register_address
    try:
        offset, function = normalize_register_address(address)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "offset": offset, "function": function,
            "message": describe_address(address)}


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
    prefer_cloud_reads = bool(app_store._prefer_cloud_reads())  # 2026-08-21: explicit deployment flag, not the Host header
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
    prefer_cloud_reads = bool(app_store._prefer_cloud_reads())  # 2026-08-21: explicit deployment flag, not the Host header
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
    prefer_cloud_reads = bool(app_store._prefer_cloud_reads())  # 2026-08-21: explicit deployment flag, not the Host header
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


# Grain thresholds. Chosen so a chart never receives more than ~1500 points
# per tag: at 1 Hz that is 25 minutes of seconds, 25 hours of minutes, and so
# on. The operator picks a PERIOD; the server picks the resolution that fits.
_POWER_GRAINS = (
    #  window <= ,        bucket,    approx seconds per bucket
    (30 * 60,            "second",   1),
    (36 * 3600,          "minute",   60),
    (90 * 24 * 3600,     "hour",     3600),
    (float("inf"),       "day",      86400),
)


def _pick_power_bucket(span_s: float, max_points: int = 1500) -> str:
    """The finest grain that keeps a single tag under `max_points`."""
    for limit_s, bucket, secs in _POWER_GRAINS:
        if span_s <= limit_s and (span_s / secs) <= max_points:
            return bucket
    return "day"


# EXACTLY the tags powerMainChartData draws for each metric. It filters the
# response through this same priority list and discards everything else, so
# anything extra here is rows read, bucketed and serialised for nothing - that
# waste is what made the default 24-hour view take 38 seconds.
#
# Keep in step with `metricTagPriority` in App.jsx; the pairing is asserted by
# scripts/test_power_series_window.py.
_METRIC_TAGS = {
    # Total first, then each phase. A meter that writes only some of these
    # returns rows only for those; an absent tag is not an error.
    #
    # Note on power: the EM122 three-phase register map has
    # active_power_total_w and NO per-phase power, so W charts as one line on
    # that meter. The per-phase names are listed for meters that do publish
    # them rather than pretending every meter is the same.
    "power_kw":   ["active_power_total_w", "active_power_w",
                   "active_power_l1_w", "active_power_l2_w", "active_power_l3_w"],
    "voltage_v":  ["voltage_v", "voltage_l1_v", "voltage_l2_v", "voltage_l3_v"],
    "current_a":  ["current_a", "current_l1_a", "current_l2_a", "current_l3_a"],
    "energy_kwh": ["energy_total_wh", "energy_wh"],
}


def _tags_for_metric(metric: str) -> list[str]:
    """Tags for one metric, or for a comma-separated list of them.

    The page draws one metric but needs several: the totals and the tariff
    maths are computed from power_kw and energy_kwh whatever the chart happens
    to be showing. Returning the union keeps that a single query.
    """
    raw = str(metric or "").strip().lower()
    if not raw:
        return []
    out: list[str] = []
    for part in raw.split(","):
        for tag in _METRIC_TAGS.get(part.strip(), []):
            if tag not in out:
                out.append(tag)
    return out


@router.get("/series")
def get_power_series(request: Request, from_utc: str = "", to_utc: str = "",
                     minutes: float = 0.0, bucket: str = "auto",
                     device_id: str = "", tag: str = "", metric: str = "",
                     max_points: int = 1500) -> dict:
    """Bucketed power series for a TIME WINDOW.

    Replaces the row-limited `/history` for charting. `/history` returned the
    last N rows with no window at all; with 87 registers per sample that was
    about 90 seconds of data whatever period the operator selected.

    `bucket=auto` picks the grain from the window so the payload stays bounded
    - seconds for a live view, minutes for a day, hours for a quarter - which
    is what lets the same page move from seconds to days without falling over.
    """
    from datetime import datetime, timedelta, timezone as _tz

    def _stamp(dt) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"

    now = datetime.now(_tz.utc)
    to_txt = str(to_utc or "").strip() or _stamp(now)
    if str(from_utc or "").strip():
        from_txt = str(from_utc).strip()
    else:
        # Anchor the window on `to`, NOT on the wall clock. Asking for "the 2
        # minutes ending at 12:14:49" while the clock says 12:16 must return
        # 12:12:49-12:14:49, not 12:14:00-12:14:49.
        mins = float(minutes or 0.0) or 60.0
        try:
            anchor = datetime.strptime(
                str(to_txt).strip().replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=_tz.utc)
        except Exception:
            anchor = now
        from_txt = _stamp(anchor - timedelta(minutes=mins))

    # Span in seconds, from the two bounds as written.
    def _parse(txt: str) -> datetime:
        t = str(txt or "").strip().replace("T", " ")[:19]
        return datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz.utc)

    try:
        span_s = max(1.0, (_parse(to_txt) - _parse(from_txt)).total_seconds())
    except Exception:
        span_s = 3600.0

    grain = str(bucket or "auto").strip().lower()
    if grain in ("", "auto"):
        grain = _pick_power_bucket(span_s, max(50, min(int(max_points or 1500), 5000)))

    # The chart draws a handful of series; the meter writes 87 registers. A
    # 15-minute window at 1 s across all of them is 73 229 rows and ~6 s, to
    # render six lines. Narrow it in SQL - `max_points` bounds points PER TAG,
    # which does nothing when the row count is points x tags.
    tag_filter = str(tag or "").strip()
    tag_list: list[str] = []
    if not tag_filter:
        tag_list = _tags_for_metric(metric)

    rows = app_store.bucket_raw_historian_rows(
        bucket=grain,
        from_utc=from_txt,
        to_utc=to_txt,
        gateway=str(device_id or "").strip(),
        tag=tag_filter,
        tags=tag_list,
        # One tag's worth of points times a generous tag count. The SQL GROUP BY
        # has already collapsed the raw rows, so this bounds the RESULT, not the
        # scan.
        limit=min(100000, max(1000, int(max_points or 1500) * 120)),
        source="power_modbus,power_insight",
    )
    return {
        "ok": True,
        "rows": rows,
        "bucket": grain,
        "from_utc": from_txt,
        "to_utc": to_txt,
        "span_seconds": span_s,
        # The chart shows the grain, so nobody mistakes an hourly average for a
        # live reading.
        "auto_bucket": str(bucket or "auto").strip().lower() in ("", "auto"),
        "tag_filter": tag_filter,
        "tags": tag_list,
    }


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
