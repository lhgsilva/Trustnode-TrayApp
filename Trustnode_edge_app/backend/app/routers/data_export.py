# -*- coding: utf-8 -*-
"""The Data Export assistant's API.

A read-only query surface over the historian: preview what a filter set
returns, then stream the whole thing to a file. Separate from the app-store
router on purpose - the historian read path serves every chart in the app and
must not grow grouping, pivots and arbitrary conditions to serve an export
screen.

Nothing here writes. The only lock it can take is SQLite's read lock, and it
uses the read-only connection so it cannot upgrade to a write.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services import data_export as _q
from app.state import app_store

router = APIRouter(prefix="/api/data-export", tags=["data-export"])


class ExportSpec(BaseModel):
    gateways: List[str] = Field(default_factory=list)
    devices: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    tag_contains: str = ""
    from_utc: str = ""
    to_utc: str = ""
    quality: str = "all"
    columns: List[str] = Field(default_factory=list)
    conditions: List[Dict[str, Any]] = Field(default_factory=list)
    bucket: str = ""
    aggregate: str = ""
    pivot: bool = False
    order: str = "asc"
    include_header: bool = True
    limit: int = 0


@router.get("/options")
def export_options() -> dict:
    """What this edge can be asked for: columns, aggregates, buckets, operators.

    The assistant renders its pickers from this rather than hardcoding lists
    that drift from what the backend actually accepts.
    """
    return {
        "ok": True,
        "columns": list(_q.COLUMNS.keys()),
        "default_columns": list(_q.DEFAULT_COLUMNS),
        "aggregates": list(_q.AGGREGATES.keys()),
        "buckets": list(_q.BUCKETS.keys()),
        "operators": list(_q.OPERATORS.keys()),
        "max_preview_rows": _q.MAX_PREVIEW_ROWS,
        "max_export_rows": _q.MAX_EXPORT_ROWS,
    }


#: How far back the pickers look, and how many rows they may read doing it.
#: Both caps exist because this runs while an operator waits: the cost must
#: depend on these numbers, never on the size of the historian.
SOURCES_WINDOW_DAYS = 30
SOURCES_ROW_CAP = 60000


@router.get("/sources")
def export_sources(window_days: int = SOURCES_WINDOW_DAYS) -> dict:
    """Gateways, devices and tags seen in the recent historian.

    Taken from the DATA, not from the gateway configuration: an operator
    exporting history usually wants something a deleted gateway recorded, and
    a picker built only from live config cannot offer it.

    Read as ONE bounded slice through the (tenant_id, ts_utc DESC) index. The
    previous version ran a DISTINCT per column, and device_name has no index -
    a full scan that outlived the client's timeout and surfaced as "Failed to
    fetch" on a page that was simply waiting.
    """
    import datetime as _dt

    tenant = app_store._current_tenant_id()
    days = max(1, min(int(window_days or SOURCES_WINDOW_DAYS), 3650))
    since = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    out: Dict[str, Any] = {
        "ok": True, "gateways": [], "devices": [], "tags": [],
        "gateway_names": {}, "window_days": days, "row_cap": SOURCES_ROW_CAP,
    }
    try:
        gateways: Dict[str, str] = {}
        devices: List[str] = []
        tags: List[str] = []
        seen_dev = set()
        seen_tag = set()
        with app_store._connect_readonly() as conn:
            rows = conn.execute(
                "SELECT gateway_id, gateway_name, device_name, tag_name "
                "FROM historian_readings "
                "WHERE tenant_id = ? AND ts_utc >= ? "
                "ORDER BY ts_utc DESC LIMIT ?",
                (tenant, since, SOURCES_ROW_CAP)).fetchall()
        for r in rows:
            gid = str(r["gateway_id"] or "").strip()
            if gid and gid not in gateways:
                gateways[gid] = str(r["gateway_name"] or gid).strip() or gid
            dev = str(r["device_name"] or "").strip()
            if dev and dev not in seen_dev:
                seen_dev.add(dev)
                devices.append(dev)
            tag = str(r["tag_name"] or "").strip()
            if tag and tag not in seen_tag:
                seen_tag.add(tag)
                tags.append(tag)
        out["gateways"] = sorted(gateways.keys())
        out["gateway_names"] = gateways
        out["devices"] = sorted(devices)
        out["tags"] = sorted(tags)
        out["scanned_rows"] = len(rows)
        # Say so when the cap was hit: the lists are then "what is recent",
        # not "everything", and an operator deserves to know which.
        out["capped"] = len(rows) >= SOURCES_ROW_CAP
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        out["ok"] = False
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


@router.post("/preview")
def export_preview(spec: ExportSpec) -> dict:
    """A capped sample plus the row count the full export would produce.

    The count is a real COUNT(*) over the same predicates - an operator about
    to export needs to know whether that is four thousand rows or four
    million BEFORE they wait for it.
    """
    tenant = app_store._current_tenant_id()
    payload = spec.model_dump()
    sql, params, columns = _q.build_query(payload, tenant)
    limit = max(1, min(int(spec.limit or _q.MAX_PREVIEW_ROWS), _q.MAX_PREVIEW_ROWS))
    try:
        with app_store._connect_readonly() as conn:
            rows = conn.execute(sql + " LIMIT ?", (*params, limit)).fetchall()
            dicts = [{c: r[i] for i, c in enumerate(columns)} for r in rows]
            count_sql, count_params, _ = _q.build_query(payload, tenant)
            inner = count_sql.split(" ORDER BY ")[0]
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM (%s)" % inner, count_params).fetchone()
            total_rows = int(total["n"] or 0) if total else 0
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                "columns": columns, "rows": [], "total_rows": 0}

    if spec.pivot:
        columns, dicts = _q.pivot_rows(dicts)
    return {"ok": True, "columns": columns, "rows": dicts,
            "total_rows": total_rows, "preview_rows": len(dicts),
            "truncated": total_rows > len(dicts)}


@router.post("/run")
def export_run(spec: ExportSpec) -> StreamingResponse:
    """Stream the whole result set as CSV.

    Streaming, not assembling: a day of this historian is ~9 million rows and
    building that in memory - on either side of the wire - is how an export
    becomes an outage. The client saves the stream straight to disk.
    """
    tenant = app_store._current_tenant_id()
    payload = spec.model_dump()
    sql, params, columns = _q.build_query(payload, tenant)
    cap = max(1, min(int(spec.limit or _q.MAX_EXPORT_ROWS), _q.MAX_EXPORT_ROWS))

    def rows():
        # NOT closed here: _connect_readonly hands back the connection this
        # THREAD owns and reuses. Closing it would pull the handle out from
        # under everything else running on the same thread - and opening a
        # fresh one per request is what made queries cost seconds.
        conn = app_store._connect_readonly()
        cur = conn.execute(sql + " LIMIT ?", (*params, cap))
        while True:
            chunk = cur.fetchmany(2000)
            if not chunk:
                break
            for row in chunk:
                yield row

    stream = _q.iter_csv(rows(), columns, include_header=bool(spec.include_header))
    return StreamingResponse(
        stream,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="trustnode_export.csv"',
                 "Cache-Control": "no-store"},
    )
