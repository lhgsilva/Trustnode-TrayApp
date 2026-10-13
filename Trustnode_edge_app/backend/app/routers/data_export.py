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


def _export_tenants() -> List[str]:
    """Every tenant id this edge legitimately reads, primary first.

    One rule, shared by the pickers and the export itself. They used to
    resolve tenants separately: _current_tenant_id() here, and the same in
    the sources endpoint - so when it disagreed with what the historian had
    actually been written under, the page offered nothing to filter by and
    an export returned nothing, with no error from either.
    """
    primary = app_store._current_tenant_id()
    try:
        aliases = app_store._tenant_alias_ids(primary) or []
    except Exception:                                   # noqa: BLE001
        aliases = []
    out = [primary, *[a for a in aliases if a and a != primary]]
    return [t for t in out if t]


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

    # The SAME tenant rule every working historian read uses, aliases and
    # all. 2026-10-08: this endpoint hand-rolled `tenant_id = ?` from
    # _current_tenant_id(), a second copy of a rule that already exists -
    # and when the two disagreed the pickers came back empty with no error.
    # On the reporting machine every historian row is written under
    # tenant-cust-e5916328 while this resolved "default", so the page
    # offered nothing to filter by and looked broken. _tenant_filter_clause
    # also covers a bridged prior tenant, which the hand-rolled version
    # silently excluded.
    tenant_clause, tenant_params = app_store._tenant_filter_clause("tenant_id")
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
        SELECT = ("SELECT gateway_id, gateway_name, device_name, tag_name "
                  "FROM historian_readings WHERE " + tenant_clause + " ")
        with app_store._connect_readonly() as conn:
            rows = conn.execute(
                SELECT + "AND ts_utc >= :since ORDER BY ts_utc DESC LIMIT :cap",
                {**tenant_params, "since": since,
                 "cap": SOURCES_ROW_CAP}).fetchall()
            # An empty recent window must not present as an empty page. A
            # historian that was last written weeks ago is still exportable,
            # and a picker with nothing in it reads as "the filters are
            # broken" - which is exactly how it was reported. Fall back to
            # the newest rows regardless of age, under the same row cap, and
            # say that is what happened.
            if not rows:
                rows = conn.execute(
                    SELECT + "ORDER BY ts_utc DESC LIMIT :cap",
                    {**tenant_params, "cap": SOURCES_ROW_CAP}).fetchall()
                out["widened"] = bool(rows)
            # Still nothing. Either this historian is genuinely empty or the
            # rows are under a tenant this request does not resolve to - and
            # those two look identical on screen. Say which, WITHOUT naming
            # another customer's tenant: the count is enough for the page to
            # explain itself and for a support call to go straight to the
            # cause instead of "the export page is broken".
            if not rows:
                try:
                    other = conn.execute(
                        "SELECT COUNT(*) FROM historian_readings "
                        "WHERE NOT (" + tenant_clause + ")",
                        tenant_params).fetchone()[0]
                except Exception:                       # noqa: BLE001
                    other = 0
                out["tenant_id"] = tenant
                out["rows_outside_tenant"] = int(other or 0)
                if other:
                    out["note"] = (
                        "No readings are stored under this edge's tenant "
                        "(%s), but the historian holds %d row(s) under a "
                        "different one. The pickers are empty for that "
                        "reason, not because the export is broken."
                        % (tenant, int(other)))
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
    tenant = _export_tenants()
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
    tenant = _export_tenants()
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
