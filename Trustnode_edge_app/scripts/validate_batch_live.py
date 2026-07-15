"""Create REALISTIC batches over LIVE historian data and validate computed results.

Unlike the fast smoke test, this leaves batches whose data windows cover real
collection, so KPIs / charts / excursions / pass-fail actually populate — ideal for
UI validation. It creates:

  1) "Live Cure - In Spec"      : BT_PVA_Level (spec 10-60) -> ~23 -> WITHIN_SPEC, PASS
  2) "Live Cure - Excursion"    : BT_PVB_Level (spec 10-50) -> ~55 -> OUT_OF_SPEC, FAIL + excursion
  3) A Batch Group with 2 children over live data -> group KPIs

It reads the batch's data window straight from the running app's DB and widens it to
cover the last N minutes of real data (exactly what a batch that ran during that
window would have), then recompute -> asserts the expected quality/excursions.
"""
import json, urllib.request, urllib.error, sqlite3, os, glob, time

BASE = "http://127.0.0.1:8000"
GATEWAY = "gw-1781903248499"
_tok = None

def login():
    global _tok
    for u, p in [("admin", "admin"), ("admin-mari", "admin")]:
        try:
            r = urllib.request.Request(BASE + "/api/auth/login", data=json.dumps({"username": u, "password": p}).encode(),
                                       method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=15) as resp:
                b = json.loads(resp.read().decode() or "{}")
                _tok = b.get("access_token") or b.get("token")
                if _tok: return u
        except Exception:
            pass

def req(path, method="GET", body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json", "Authorization": f"Bearer {_tok}"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode(); return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, {"_raw": raw[:200]}

def find_db():
    # the running portable uses its own data dir; find the DB with the FRESHEST historian row
    cands = []
    cands += glob.glob(os.path.expanduser("~/.trustnode_edge/data/trustnode_app_store.db"))
    for tmp in glob.glob(os.path.join(os.environ.get("TEMP", ""), "*")):
        cands += glob.glob(os.path.join(tmp, "**", "trustnode_app_store.db"), recursive=True)
    best, best_ts = None, ""
    for c in cands:
        try:
            con = sqlite3.connect(f"file:{c}?mode=ro", uri=True, timeout=3)
            ts = con.execute("SELECT MAX(ts_utc) FROM historian_readings").fetchone()[0]
            con.close()
            if ts and ts > best_ts: best, best_ts = c, ts
        except Exception:
            pass
    return best, best_ts

def data_window(db, tag, minutes=15):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    row = con.execute(f"""SELECT MIN(ts_utc), MAX(ts_utc), COUNT(*), AVG(value), MIN(value), MAX(value)
        FROM historian_readings WHERE tag_name=? AND ts_utc >= datetime((SELECT MAX(ts_utc) FROM historian_readings),'-{minutes} minutes')""", (tag,)).fetchone()
    con.close()
    return row  # (lo, hi, n, avg, min, max)

def make_batch(name, definition_id, window_lo, window_hi, db):
    st, b = req("/api/batch-management/v2/batches", "POST",
                {"definition_id": definition_id, "equipment_id": GATEWAY, "reference": name, "product": name})
    bid = (b.get("row") or {}).get("id")
    req(f"/api/batch-management/v2/batches/{bid}/start", "POST", {})
    req(f"/api/batch-management/v2/batches/{bid}/stop", "POST", {})
    # widen its window to cover the real live data (what a batch that ran then would have)
    con = sqlite3.connect(db, timeout=10)
    con.execute("UPDATE batch_data_window SET window_start=?, window_end=? WHERE batch_id=?", (window_lo, window_hi, bid))
    con.commit(); con.close()
    req(f"/api/batch-management/v2/batches/{bid}/recompute", "POST", {}, timeout=60)
    return bid

def main():
    u = login()
    print(f"login: {u}\n")
    db, freshest = find_db()
    print(f"live DB: {db}\n  freshest historian row: {freshest}")
    if not db:
        print("  no DB found"); return 1

    passed = failed = 0
    def check(n, c, d=""):
        nonlocal passed, failed
        if c: passed += 1; print(f"  PASS  {n}  ({d})" if d else f"  PASS  {n}")
        else: failed += 1; print(f"  FAIL  {n}  -> {d}")

    # ---- Definition 1: PVA in-spec (spec 10-60) ----
    st, r = req("/api/batch-management/v2/definitions", "POST", {
        "name": "Live Cure - In Spec", "code": "LIVE-INSPEC", "equipment_id": GATEWAY, "plant": "Plant A", "area": "Line 1",
        "config": {"batch_mode": "individual", "start_config": {"method": "manual"}, "stop_config": {"method": "manual"},
                   "tags": [{"tag_name": "BT_PVA_Level", "gateway_id": GATEWAY, "tag_category": "process_value", "required": True, "trend_enabled": True,
                             "limits": [{"limit_type": "spec_lower", "limit_value": 10, "severity": "error"},
                                        {"limit_type": "spec_upper", "limit_value": 60, "severity": "error"}]}],
                   "kpis": [{"code": "avg", "name": "Avg Level", "scope": "batch"}, {"code": "max", "name": "Max Level", "scope": "batch"}]}})
    d1 = (r.get("row") or {}).get("id"); req(f"/api/batch-management/v2/definitions/{d1}/publish", "POST", {})

    # ---- Definition 2: PVB out-of-spec (spec 10-50, PVB~55 -> excursion) ----
    st, r = req("/api/batch-management/v2/definitions", "POST", {
        "name": "Live Cure - Excursion", "code": "LIVE-EXC", "equipment_id": GATEWAY, "plant": "Plant A", "area": "Line 2",
        "config": {"batch_mode": "individual", "start_config": {"method": "manual"}, "stop_config": {"method": "manual"},
                   "tags": [{"tag_name": "BT_PVB_Level", "gateway_id": GATEWAY, "tag_category": "process_value", "required": True, "trend_enabled": True,
                             "limits": [{"limit_type": "spec_lower", "limit_value": 10, "severity": "error"},
                                        {"limit_type": "spec_upper", "limit_value": 50, "severity": "error"},
                                        {"limit_type": "warning_upper", "limit_value": 45, "severity": "warning"}]}],
                   "kpis": [{"code": "avg", "name": "Avg Level", "scope": "batch"}, {"code": "max", "name": "Max Level", "scope": "batch"}]}})
    d2 = (r.get("row") or {}).get("id"); req(f"/api/batch-management/v2/definitions/{d2}/publish", "POST", {})

    print("\n[Validation over LIVE data]")
    # in-spec batch
    lo, hi, n, avg, mn, mx = data_window(db, "BT_PVA_Level")
    b1 = make_batch("LIVE-INSPEC-1", d1, lo, hi, db)
    st, bd = req(f"/api/batch-management/v2/batches/{b1}")
    row = bd.get("row") or {}
    st, k = req(f"/api/batch-management/v2/batches/{b1}/kpis")
    kavg = next((x["numeric_value"] for x in (k.get("rows") or []) if x.get("kpi_code") == "avg"), None)
    check("in-spec batch WITHIN_SPECIFICATION", row.get("quality_status") == "within_specification",
          f"quality={row.get('quality_status')} dq={row.get('data_quality_status')} avg={kavg} (data avg~{avg:.1f})")

    # excursion batch
    lo, hi, n, avg, mn, mx = data_window(db, "BT_PVB_Level")
    b2 = make_batch("LIVE-EXCURSION-1", d2, lo, hi, db)
    st, bd = req(f"/api/batch-management/v2/batches/{b2}")
    row = bd.get("row") or {}
    st, x = req(f"/api/batch-management/v2/batches/{b2}/excursions")
    exc = x.get("rows") or []
    check("excursion batch OUT_OF_SPECIFICATION", row.get("quality_status") == "out_of_specification",
          f"quality={row.get('quality_status')} (PVB data max~{mx})")
    check("excursion recorded vs spec_upper", any(e.get("limit_type") == "spec_upper" for e in exc),
          f"excursions={[(e.get('tag_name'), e.get('limit_type'), e.get('actual_maximum')) for e in exc]}")

    # report on the excursion batch (real content)
    st, g = req(f"/api/batch-management/v2/batches/{b2}/reports", "POST", {}, timeout=60)
    check("report generated for excursion batch", g.get("ok") and (g.get("reference") or {}).get("report_status") == "generated",
          f"status={(g.get('reference') or {}).get('report_status')}")

    # group over live data
    st, gr = req("/api/batch-management/v2/groups", "POST",
                 {"definition_id": d1, "equipment_id": GATEWAY, "reference": "LIVE-GROUP-1", "expected_child_count": 2})
    gid = (gr.get("row") or {}).get("id")
    lo, hi, *_ = data_window(db, "BT_PVA_Level")
    for i in range(2):
        st, cb = req("/api/batch-management/v2/batches", "POST",
                     {"definition_id": d1, "batch_group_id": gid, "equipment_id": GATEWAY, "reference": f"LIVE-CHILD-{i}"})
        cid = (cb.get("row") or {}).get("id")
        req(f"/api/batch-management/v2/batches/{cid}/start", "POST", {}); req(f"/api/batch-management/v2/batches/{cid}/stop", "POST", {})
        con = sqlite3.connect(db, timeout=10); con.execute("UPDATE batch_data_window SET window_start=?, window_end=? WHERE batch_id=?", (lo, hi, cid)); con.commit(); con.close()
        req(f"/api/batch-management/v2/batches/{cid}/recompute", "POST", {}, timeout=60)
    req(f"/api/batch-management/v2/groups/{gid}/recompute", "POST", {}, timeout=60)
    st, gk = req(f"/api/batch-management/v2/groups/{gid}/kpis")
    check("group KPIs over live data", len(gk.get("rows") or []) >= 5, f"kpis={len(gk.get('rows') or [])}")

    print(f"\n=== VALIDATION: {passed} passed, {failed} failed ===")
    print("Batches created for UI validation (open Batch Management -> Overview):")
    print(f"  LIVE-INSPEC-1    (in-spec, PASS)     batch {b1}")
    print(f"  LIVE-EXCURSION-1 (out-of-spec, FAIL) batch {b2}  <- has excursions + report")
    print(f"  LIVE-GROUP-1     (group + 2 children) group {gid}")
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())
