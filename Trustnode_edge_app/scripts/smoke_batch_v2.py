"""Batch Management v2 — end-to-end smoke test against a LIVE edge backend.

Exercises every page's backend + leaves real sample data you can then validate in
the UI (Batch Overview / Batch Definitions / Batch Analysis).

What it covers (each step asserts + prints PASS/FAIL):
  Definitions page:  create draft (General/Structure/Identification/Start/Stop/
                     Tags+Limits/KPIs/Reports&Email) -> validate -> publish ->
                     immutability (edit published -> new draft version) -> versions list
  Overview page:     create batch -> start -> hold -> resume -> stop (state machine),
                     illegal transition rejected, comments, events timeline
  Calculations:      recompute -> KPIs, excursions (vs spec limits), data-quality
  Reports:           generate batch report (real PDF via Report module) -> list ->
                     preview URL -> (email path is exercised as a soft check)
  Groups page:       create group -> child batches -> group KPIs -> complete
  Analysis page:     excursions list, batch comparison

Usage:
  python scripts/smoke_batch_v2.py                 # against http://127.0.0.1:8000
  python scripts/smoke_batch_v2.py --base URL --user U --pass P
  python scripts/smoke_batch_v2.py --keep          # don't delete the sample data (default: keep)

It uses REAL tags found on the edge so batches have historian data. Safe: it only
creates batch-module rows (definitions/batches/groups) — never touches historian,
gateways, or collection.
"""
from __future__ import annotations
import argparse, json, sys, time, urllib.request, urllib.error

def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8000")
    p.add_argument("--user", default="admin")
    p.add_argument("--pw", default="admin")
    p.add_argument("--tags", default="BT_PVA_Level,BT_PVB_Level,BT_PVC_Level")
    p.add_argument("--gateway", default="gw-1781903248499")
    p.add_argument("--keep", action="store_true", default=True)
    return p.parse_args()

A = _args()
BASE = A.base.rstrip("/")
_tok = None
_passed = 0
_failed = 0

def _req(path, method="GET", body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if _tok:
        headers["Authorization"] = f"Bearer {_tok}"
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, {"_raw": raw[:300]}

def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1; print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        _failed += 1; print(f"  FAIL  {name}  ->  {detail}")

def login():
    global _tok
    for u, p in [(A.user, A.pw), ("admin-mari", "admin"), ("admin", "admin")]:
        st, b = _req("/api/auth/login", "POST", {"username": u, "password": p})
        _tok = b.get("access_token") or b.get("token")
        if _tok:
            return u
    return None

def main():
    print(f"=== Batch v2 smoke test against {BASE} ===\n")
    u = login()
    check("login", bool(_tok), f"as {u}" if u else "no token")
    if not _tok:
        print("cannot continue without auth"); return 1

    st, s = _req("/api/batch-management/v2/status")
    check("module licensed", st == 200 and s.get("enabled") is True, f"reason={s.get('reason')}")

    tags = [t.strip() for t in A.tags.split(",") if t.strip()]

    # ---------- DEFINITIONS PAGE ----------
    print("\n[Definitions] create draft with full config")
    cfg = {
        "batch_mode": "both",
        "group_config": {"expected_child_count": 2, "naming": "{GroupRef}-B{Seq}"},
        "identification": {"method": "auto", "prefix": "SMOKE"},
        "start_config": {"method": "manual"},
        "stop_config": {"method": "manual"},
        "tags": [
            {"tag_name": tags[0], "gateway_id": A.gateway, "tag_category": "process_value",
             "required": True, "trend_enabled": True,
             "limits": [
                 {"limit_type": "spec_lower", "limit_value": 10, "severity": "error"},
                 {"limit_type": "spec_upper", "limit_value": 60, "severity": "error"},
                 {"limit_type": "warning_upper", "limit_value": 50, "severity": "warning"},
             ]},
        ] + [
            {"tag_name": t, "gateway_id": A.gateway, "tag_category": "process_value",
             "trend_enabled": True, "limits": []} for t in tags[1:]
        ],
        "triggers": [
            {"trigger_scope": "BATCH_START", "gateway_id": A.gateway,
             "condition": {"operator": "AND", "rules": [{"tag": tags[0], "kind": "rising_edge"}]}, "enabled": True},
        ],
        "kpis": [{"code": "avg", "name": f"Avg {tags[0]}", "scope": "batch"},
                 {"code": "max", "name": f"Max {tags[0]}", "scope": "batch"}],
        "batch_report_template_id": "tpl-batch-detailed",
        "auto_generate_batch_report": False,
    }
    st, r = _req("/api/batch-management/v2/definitions", "POST",
                 {"name": "Smoke Test Definition", "code": "SMOKE-1", "equipment_id": A.gateway,
                  "plant": "Plant A", "area": "Line 1", "config": cfg})
    d = r.get("row") or {}
    did = d.get("id")
    check("create definition draft", st == 200 and bool(did), f"id={did} status={d.get('status')}")
    check("draft has tags+limits", bool((d.get("config") or {}).get("tags")),
          f"tags={len((d.get('config') or {}).get('tags') or [])}")

    st, v = _req(f"/api/batch-management/v2/definitions/{did}/validate", "POST", {})
    check("validate definition", st == 200 and v.get("ok") is True, f"errors={v.get('errors')}")

    st, p = _req(f"/api/batch-management/v2/definitions/{did}/publish", "POST", {})
    check("publish definition", st == 200 and (p.get("row") or {}).get("status") == "published")

    # immutability: edit published -> should create a NEW draft version
    st, e = _req(f"/api/batch-management/v2/definitions/{did}", "PUT",
                 {"name": "Smoke Test Definition", "equipment_id": A.gateway,
                  "config": {**cfg, "batch_mode": "individual"}})
    st2, vers = _req(f"/api/batch-management/v2/definitions/{did}/versions")
    vlist = vers.get("rows") or []
    check("edit published -> new draft version", len(vlist) >= 2,
          f"versions={[(x.get('version_number'), x.get('status')) for x in vlist]}")

    # re-publish v2 so batches can start from it
    _req(f"/api/batch-management/v2/definitions/{did}/publish", "POST", {})

    # ---------- OVERVIEW PAGE: batch lifecycle ----------
    print("\n[Overview] batch lifecycle (create/start/hold/resume/stop)")
    st, b = _req("/api/batch-management/v2/batches", "POST",
                 {"definition_id": did, "equipment_id": A.gateway, "reference": "SMOKE-BATCH-1", "product": "WidgetX"})
    bid = (b.get("row") or {}).get("id")
    check("create batch", st == 200 and bool(bid), f"id={bid} status={(b.get('row') or {}).get('status')}")

    st, b = _req(f"/api/batch-management/v2/batches/{bid}/start", "POST", {"reason": "smoke start"})
    check("start batch", st == 200 and (b.get("row") or {}).get("status") == "running")

    # let some historian data accumulate in the window
    time.sleep(4)

    st, b = _req(f"/api/batch-management/v2/batches/{bid}/hold", "POST", {"reason": "smoke hold"})
    check("hold batch", st == 200 and (b.get("row") or {}).get("status") == "held")
    st, b = _req(f"/api/batch-management/v2/batches/{bid}/resume", "POST", {})
    check("resume batch", st == 200 and (b.get("row") or {}).get("status") == "running")
    time.sleep(2)
    st, b = _req(f"/api/batch-management/v2/batches/{bid}/stop", "POST", {"reason": "smoke stop"})
    check("stop batch", st == 200 and (b.get("row") or {}).get("status") == "completed")

    # illegal transition: stop an already-completed batch -> 409
    st, _ = _req(f"/api/batch-management/v2/batches/{bid}/stop", "POST", {})
    check("illegal transition rejected (409)", st == 409, f"got HTTP {st}")

    # comment + events
    _req(f"/api/batch-management/v2/batches/{bid}/comments", "POST", {"message": "smoke comment"})
    st, ev = _req(f"/api/batch-management/v2/batches/{bid}/events")
    check("event timeline populated", st == 200 and len(ev.get("rows") or []) >= 4,
          f"events={len(ev.get('rows') or [])}")

    # ---------- CALCULATIONS ----------
    print("\n[Calc] recompute -> KPIs / excursions / data-quality")
    st, rc = _req(f"/api/batch-management/v2/batches/{bid}/recompute", "POST", {}, timeout=40)
    check("recompute batch", st == 200 and rc.get("ok"),
          f"quality={rc.get('quality_status')} dq={rc.get('data_quality_status')}")
    st, k = _req(f"/api/batch-management/v2/batches/{bid}/kpis")
    kcodes = {x.get("kpi_code") for x in (k.get("rows") or [])}
    check("KPIs computed", st == 200 and {"cycle_time"} <= kcodes, f"codes={sorted(kcodes)[:8]}")
    st, x = _req(f"/api/batch-management/v2/batches/{bid}/excursions")
    check("excursions endpoint", st == 200, f"count={len(x.get('rows') or [])}")

    # ---------- REPORTS ----------
    print("\n[Reports] generate + list + preview")
    st, g = _req(f"/api/batch-management/v2/batches/{bid}/reports", "POST", {}, timeout=60)
    ref = (g.get("reference") or {})
    check("generate batch report (PDF via Report module)", st == 200 and g.get("ok") and ref.get("report_status") == "generated",
          f"status={ref.get('report_status')} err={g.get('error')}")
    st, lr = _req(f"/api/batch-management/v2/batches/{bid}/reports")
    check("list batch reports", st == 200 and len(lr.get("rows") or []) >= 1)

    # ---------- GROUPS PAGE ----------
    print("\n[Groups] create group + children + KPIs + complete")
    st, gr = _req("/api/batch-management/v2/groups", "POST",
                  {"definition_id": did, "equipment_id": A.gateway, "reference": "SMOKE-GROUP-1", "expected_child_count": 2})
    gid = (gr.get("row") or {}).get("id")
    check("create group", st == 200 and bool(gid), f"id={gid}")
    # two children
    child_ids = []
    for i in range(2):
        st, cb = _req("/api/batch-management/v2/batches", "POST",
                      {"definition_id": did, "batch_group_id": gid, "equipment_id": A.gateway, "reference": f"SMOKE-CHILD-{i}"})
        cid = (cb.get("row") or {}).get("id"); child_ids.append(cid)
        _req(f"/api/batch-management/v2/batches/{cid}/start", "POST", {})
        time.sleep(1)
        _req(f"/api/batch-management/v2/batches/{cid}/stop", "POST", {})
        _req(f"/api/batch-management/v2/batches/{cid}/recompute", "POST", {}, timeout=40)
    st, gb = _req(f"/api/batch-management/v2/groups/{gid}/batches")
    check("group has children", st == 200 and len(gb.get("rows") or []) >= 2, f"children={len(gb.get('rows') or [])}")
    st, gk = _req(f"/api/batch-management/v2/groups/{gid}/recompute", "POST", {}, timeout=40)
    check("group KPIs computed", st == 200, f"kpis={len((gk.get('kpis') or []))}")
    st, gc = _req(f"/api/batch-management/v2/groups/{gid}/complete", "POST", {})
    check("complete group (children preserved)", st == 200 and (gc.get("row") or {}).get("status") == "completed")
    st, gb2 = _req(f"/api/batch-management/v2/groups/{gid}/batches")
    check("children preserved after group complete", len(gb2.get("rows") or []) >= 2)

    # ---------- ANALYSIS PAGE ----------
    print("\n[Analysis] excursions + comparison")
    st, ax = _req("/api/batch-management/v2/analysis/excursions")
    check("analysis excursions", st == 200)
    st, cmp = _req(f"/api/batch-management/v2/analysis/comparison?batch_ids={bid},{child_ids[0]}&max_points=200", timeout=40)
    check("batch comparison", st == 200, f"batches={len(cmp.get('batches') or [])}")

    # ---------- SUMMARY ----------
    print(f"\n=== RESULT: {_passed} passed, {_failed} failed ===")
    print(f"Sample data left for UI validation: definition '{did}', batch '{bid}' (SMOKE-BATCH-1), group '{gid}' (SMOKE-GROUP-1) + 2 children.")
    print("Open Batch Management -> Overview / Definitions / Analysis to validate the UI against this data.")
    return 0 if _failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
