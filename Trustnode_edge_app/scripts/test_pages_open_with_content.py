# -*- coding: utf-8 -*-
"""Other pages take what the Reports pages got: content at mount, no page
starving the store.

2026-11-03, the survey after the Reports work:
  * pageCache.js: a page's last lists survive navigation and a restart;
  * Data Export opens with its last options and sources; OEE Operator with
    its last machine list;
  * a live batch's 20,000-row matrix is read no more than every 2 s;
  * the Retention page's row census is an id-range estimate, not a full
    COUNT(*) over the historian (which held WAL checkpoints back).

    python scripts/test_pages_open_with_content.py
"""
from __future__ import annotations

import os
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "frontend", "src")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def node(script: str) -> str:
    res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True,
                         timeout=60, cwd=SRC)
    return (res.stdout.strip() + ("\nERR " + res.stderr.strip() if res.returncode else "")).strip()


def main() -> int:
    print("other pages open with content")
    cache = os.path.join(SRC, "pageCache.js").replace("\\", "/")
    out = node(f"""
const store = new Map();
globalThis.localStorage = {{ getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) }};
const c = await import("file:///{cache}");
const a = c.readPageCache("x");
c.writePageCache("x", [1, 2, 3], 5000);
const b = c.readPageCache("x");
store.set("tn.page.cache.v1:y", JSON.stringify({{ value: {{ k: 1 }}, updatedAt: 7 }}));
const d = c.readPageCache("y");
c.clearPageCache("x");
console.log(JSON.stringify([a, b, d, c.readPageCache("x"), store.has("tn.page.cache.v1:x")]));
""")
    check("the page cache round-trips through memory and localStorage",
          out == '[null,{"value":[1,2,3],"updatedAt":5000},{"value":{"k":1},"updatedAt":7},null,false]', out[:200])

    de = open(os.path.join(SRC, "components", "DataExport", "DataExportPage.jsx"), encoding="utf-8").read()
    check("Data Export mounts with its cached options and sources",
          'readPageCache("data_export.options")' in de and 'readPageCache("data_export.sources")' in de
          and 'writePageCache("data_export.sources", src)' in de)
    oo = open(os.path.join(SRC, "components", "OEE", "OeeOperator.jsx"), encoding="utf-8").read()
    check("OEE Operator mounts with its cached machine list",
          'readPageCache("oee.operator.machines")' in oo and 'writePageCache("oee.operator.machines", rows)' in oo)
    bm = open(os.path.join(SRC, "components", "BatchManagement", "BatchManagementV2.jsx"), encoding="utf-8").read()
    check("a live batch's matrix is read no more than every 2 s",
          "Math.max(2000, iv) : 2000" in bm and "Math.max(1000, iv) : 1000" not in bm)

    import sys
    import tempfile
    import time
    _TMP = tempfile.mkdtemp(prefix="tn_census_")
    os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
    os.environ["TRUSTNODE_DATA_DIR"] = _TMP
    os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
    os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
    os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-census"
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    import inspect
    from app.state import app_store, retention_engine
    src = inspect.getsource(retention_engine._refresh_db_stats)
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    check("the retention census never runs COUNT(*) over the historian",
          "COUNT(*)" not in code and "MIN(id), MAX(id)" in code)
    app_store.append_historian_rows([{
        "ts_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - i)) + ".000",
        "gateway_id": "gw1", "gateway_name": "PLC", "device_name": "d", "tag_name": "T",
        "value": float(i), "quality": 192, "quality_label": "GOOD", "source": "test"} for i in range(250)])
    retention_engine._refresh_db_stats()
    snap = retention_engine._db_stats_cached()
    check("  and reports the row count (exact on an unpruned store)",
          int(snap.get("raw_rows") or 0) == 250 and snap.get("raw_rows_estimated") is True, repr(snap)[:160])

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
