"""Gateway + tag catalog tools — list_gateways, list_tags.

Uses the cross-scope reader so we surface every configured gateway
regardless of which tenant scope it lives in.
"""
from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from ._scope import all_gateways, resolve_tag


# Operator 2026-07-03 (LIVE FILTER): when the user asks what is "live" /
# "running" / "active" / "now", we must not dump the whole configured catalog
# — we must prove recency by looking at the historian. This returns the set of
# (tag_name) and (gateway_id) that have at least one reading in the last
# `window_s` seconds, across all workspace DBs. Cheap: one indexed MAX/EXISTS
# scan per DB using (tenant_id, ts_utc) — bounded by the recent-time predicate.
def _recently_active(window_s: int = 300) -> Tuple[Set[str], Set[str], str]:
    """Return (live_tag_names, live_gateway_ids, cutoff_text)."""
    from .tag_summary import hist_connect, _historian_tenants
    from ._scope import all_db_paths
    import datetime as _dt
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=window_s)
    # Historian ts_utc is sqlite text 'YYYY-MM-DD HH:MM:SS.fff' (UTC, no tz).
    cutoff_txt = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    live_tags: Set[str] = set()
    live_gws: Set[str] = set()
    for db_path in all_db_paths():
        tenants = _historian_tenants(db_path)
        if not tenants:
            continue
        try:
            con = hist_connect(db_path)
        except Exception:
            continue
        try:
            for tid in tenants:
                try:
                    for (tag_name, gid) in con.execute(
                        "SELECT DISTINCT tag_name, gateway_id FROM historian_readings "
                        "WHERE tenant_id = ? AND ts_utc >= ?",
                        (tid, cutoff_txt),
                    ):
                        if tag_name:
                            live_tags.add(str(tag_name))
                        if gid:
                            live_gws.add(str(gid))
                except Exception:
                    continue
        finally:
            try: con.close()
            except Exception: pass
    return live_tags, live_gws, cutoff_txt


def run_find_tags(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Fuzzy-resolve a (possibly misspelled/partial) tag name to real tags.
    Returns an exact match if found, else a ranked list of suggestions."""
    query = str(args.get("query") or args.get("tag") or "").strip()
    if not query:
        return {"error": "Missing 'query' (the tag name to look up)."}
    res = resolve_tag(query, limit=int(args.get("limit") or 5))
    return {"query": query, **res}


def run_list_gateways(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    # live_only: only gateways that are ACTUALLY collecting right now — proven
    # by a recent historian reading (not just a configured/enabled flag).
    live_only = bool(args.get("live_only"))
    try:
        window_s = max(30, int(args.get("live_window_s") or 300))
    except Exception:
        window_s = 300
    live_gws: set = set()
    cutoff_txt = ""
    if live_only:
        _, live_gws, cutoff_txt = _recently_active(window_s)
    try:
        from app.state import plc_manager  # type: ignore
        statuses = plc_manager.list_gateway_statuses() or []
    except Exception:
        statuses = []
    rows: List[Dict[str, Any]] = []
    for g in all_gateways():
        gid = str(g.get("id") or "")
        # A gateway counts as "live" if it wrote recently OR the manager marks
        # it running — either is proof of activity.
        st = next((s for s in statuses if str(s.get("gateway_id") or "") == gid), {})
        is_live = (gid in live_gws) or bool(st.get("running"))
        if live_only and not is_live:
            continue
        rows.append({
            "id": gid,
            "name": g.get("name") or gid,
            "type": g.get("gateway_type"),
            "plc_ip": g.get("plc_ip"),
            "interval_ms": g.get("interval_ms"),
            "running": bool(st.get("running")) or (gid in live_gws),
            "last_check_utc": st.get("last_check_utc"),
            "db_write_count": st.get("db_write_count"),
        })
    out = {"count": len(rows), "gateways": rows}
    if live_only:
        out["live_only"] = True
        out["since"] = cutoff_txt
    return out


def run_list_tags(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    # live_only: only tags that have a reading in the recent window — i.e. tags
    # actually being collected right now, not every configured tag.
    live_only = bool(args.get("live_only"))
    try:
        window_s = max(30, int(args.get("live_window_s") or 300))
    except Exception:
        window_s = 300
    live_tags: set = set()
    cutoff_txt = ""
    if live_only:
        live_tags, _, cutoff_txt = _recently_active(window_s)
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
            if live_only and tag_name not in live_tags:
                continue
            out.append({"tag": tag_name, "gateway_id": gid, "gateway_name": gname})
    # If live_only and the config catalog didn't match the historian tag names
    # (naming can differ), fall back to the raw live tag names from the DB so we
    # still prove what's live rather than returning an empty list.
    if live_only and not out and live_tags:
        out = [{"tag": t, "gateway_id": "", "gateway_name": ""} for t in sorted(live_tags)]
    result = {"count": len(out), "tags": out}
    if live_only:
        result["live_only"] = True
        result["since"] = cutoff_txt
    return result


def run_get_live_values(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Return the LATEST value + timestamp for each tag currently collecting
    (or a specific set of tags). This is the tool for "show me the latest
    reading for every live tag", "current values", "what are all the tags
    reading now". Reads the most-recent historian row per (tag, gateway).

    Args:
      tags: optional list of tag names to limit to (else all recently-active).
      window_s: recency window in seconds (default 600). A tag with no reading
                inside the window is considered not live and omitted.
    """
    from .tag_summary import hist_connect, _historian_tenants
    from ._scope import all_db_paths, gateway_name_for
    import datetime as _dt

    try:
        window_s = max(30, int(args.get("window_s") or 600))
    except Exception:
        window_s = 600
    want = args.get("tags") or []
    if isinstance(want, str):
        want = [t.strip() for t in want.split(",") if t.strip()]
    want_set = {str(t).strip() for t in want if str(t).strip()}

    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=window_s)
    cutoff_txt = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    # latest row per (tag, gateway): {(tag,gid): {value, ts, gid}}
    latest: Dict[tuple, Dict[str, Any]] = {}
    for db_path in all_db_paths():
        tenants = _historian_tenants(db_path)
        if not tenants:
            continue
        try:
            con = hist_connect(db_path)
        except Exception:
            continue
        try:
            for tid in tenants:
                # Most-recent reading per (tag_name, gateway_id) within the window.
                sql = (
                    "SELECT tag_name, gateway_id, value, value_text, MAX(ts_utc) AS ts, quality_label "
                    "FROM historian_readings WHERE tenant_id = ? AND ts_utc >= ? "
                    "GROUP BY tag_name, gateway_id"
                )
                try:
                    for r in con.execute(sql, (tid, cutoff_txt)):
                        tag = str(r[0] or "")
                        gid = str(r[1] or "")
                        if not tag:
                            continue
                        if want_set and tag not in want_set:
                            continue
                        ts = str(r[4] or "")
                        key = (tag, gid)
                        prev = latest.get(key)
                        if prev is None or ts > prev.get("ts", ""):
                            val = r[2]
                            vt = r[3]
                            latest[key] = {
                                "tag": tag, "gateway_id": gid,
                                "value": (val if val is not None else vt),
                                "ts": ts,
                                "quality": str(r[5] or ""),
                            }
                except Exception:
                    continue
        finally:
            try: con.close()
            except Exception: pass

    rows = []
    for (tag, gid), v in latest.items():
        rows.append({
            "tag": tag,
            "gateway_id": gid,
            "gateway_name": gateway_name_for(gid) if gid else "",
            "value": v.get("value"),
            "ts_utc": v.get("ts"),
            "quality": v.get("quality"),
        })
    rows.sort(key=lambda x: (x["gateway_name"], x["tag"]))
    return {"count": len(rows), "since": cutoff_txt, "values": rows}
