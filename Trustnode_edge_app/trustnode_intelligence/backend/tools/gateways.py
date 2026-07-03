"""Gateway + tag catalog tools — list_gateways, list_tags.

Uses the cross-scope reader so we surface every configured gateway
regardless of which tenant scope it lives in.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ._scope import all_gateways, resolve_tag


def run_find_tags(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Fuzzy-resolve a (possibly misspelled/partial) tag name to real tags.
    Returns an exact match if found, else a ranked list of suggestions."""
    query = str(args.get("query") or args.get("tag") or "").strip()
    if not query:
        return {"error": "Missing 'query' (the tag name to look up)."}
    res = resolve_tag(query, limit=int(args.get("limit") or 5))
    return {"query": query, **res}


def run_list_gateways(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from app.state import plc_manager  # type: ignore
        statuses = plc_manager.list_gateway_statuses() or []
    except Exception:
        statuses = []
    rows: List[Dict[str, Any]] = []
    for g in all_gateways():
        gid = str(g.get("id") or "")
        st = next((s for s in statuses if str(s.get("gateway_id") or "") == gid), {})
        rows.append({
            "id": gid,
            "name": g.get("name") or gid,
            "type": g.get("gateway_type"),
            "plc_ip": g.get("plc_ip"),
            "interval_ms": g.get("interval_ms"),
            "running": bool(st.get("running")),
            "last_check_utc": st.get("last_check_utc"),
            "db_write_count": st.get("db_write_count"),
        })
    return {"count": len(rows), "gateways": rows}


def run_list_tags(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    out: List[Dict[str, Any]] = []
    for g in all_gateways():
        gid = str(g.get("id") or "")
        gname = str(g.get("name") or gid)
        for t in (g.get("tags") or []):
            tag_name = ""
            if isinstance(t, str):
                tag_name = t
            elif isinstance(t, dict):
                tag_name = str(t.get("tag_name") or t.get("name") or "")
            if not tag_name:
                continue
            out.append({"tag": tag_name, "gateway_id": gid, "gateway_name": gname})
    return {"count": len(out), "tags": out}
