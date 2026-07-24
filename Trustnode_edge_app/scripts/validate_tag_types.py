"""
TrustNode — end-to-end validation of TAG TYPE handling (numeric vs STRING).

Validates, from scratch, that every layer stores and surfaces tag values with the
correct type semantics:

    value       REAL/DOUBLE NULL  -> numeric tags. NULL means "not a number".
    value_text  TEXT NULL         -> the original string, ALWAYS preserved.

Rules under test
----------------
R1  A numeric tag (REAL/DINT/BOOL) stores a numeric `value`.
R2  A non-numeric STRING tag stores value=NULL and the text in `value_text`
    (it must NEVER fabricate 0.0 — that made status strings chart as a flat
    zero line and dragged AVG/MIN/MAX to meaningless zeros).
R3  A numeric-looking STRING ('77') keeps BOTH: value=77.0 and value_text='77'
    (so the text is never discarded and '77'->'77A' cannot fake a numeric
    discontinuity).
R4  A FAILED read stores value=NULL + quality BAD + the driver error in
    value_text — never 0.0.
R5  Program-scoped AB tags ("Program:Prog.Tag") read successfully.
R6  Every historian/sink schema carries BOTH columns, and every INSERT writes
    value_text (local, store-and-forward outbox, external SQLite, Postgres).
R7  The frontend never coerces NULL to 0 (Number(null) === 0 is a trap).

Usage
-----
    python scripts/validate_tag_types.py                # offline checks
    python scripts/validate_tag_types.py --plc 192.168.10.240   # + live PLC

Exit code 0 = all passed, 1 = at least one failure.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

PASS, FAIL, SKIP = [], [], []


def ck(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"\n          {detail}" if detail else ""))
    return cond


def skip(name: str, why: str) -> None:
    SKIP.append(name)
    print(f"  SKIP  {name}\n          {why}")


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def db_path(*parts: str) -> str:
    return os.path.expanduser(os.path.join("~", ".trustnode_edge", "data", *parts))


# ---------------------------------------------------------------- 1. coercion
def validate_coercion() -> None:
    section("1. COERCION — driver turns raw PLC values into (value, value_text)")
    try:
        from app.services.plc_manager import GatewayWorker
    except Exception as exc:  # pragma: no cover
        skip("import GatewayWorker", f"{exc}")
        return

    coerce = GatewayWorker._coerce_value

    class _Probe:
        _opt_float = staticmethod(GatewayWorker._opt_float)

    probe = _Probe()

    cases = [
        # (raw,               value,  text,                rule, note)
        (12.5,                12.5,   None,                "R1", "REAL"),
        (7,                   7.0,    None,                "R1", "DINT"),
        (True,                1.0,    None,                "R1", "BOOL true"),
        (False,               0.0,    None,                "R1", "BOOL false (a REAL zero is legitimate)"),
        ("BT-RC2026-002",     None,   "BT-RC2026-002",     "R2", "STRING -> NULL + text (never 0.0)"),
        ("BATCH READY",       None,   "BATCH READY",       "R2", "status string"),
        ("",                  None,   "",                  "R2", "empty string"),
        (b"ABC",              None,   "ABC",               "R2", "bytes non-numeric"),
        ("77",                77.0,   "77",                "R3", "numeric STRING keeps BOTH"),
        ("  3.5 ",            3.5,    "3.5",               "R3", "whitespace-padded numeric string"),
        (b"42",               42.0,   "42",                "R3", "bytes numeric keeps BOTH"),
        ("77A",               None,   "77A",               "R3", "'77'->'77A' must not fake a zero"),
        ("nan",               None,   "nan",               "R3", "NaN text must not become a NaN float"),
        ("inf",               None,   "inf",               "R3", "inf text must not become an inf float"),
    ]
    for raw, want_v, want_t, rule, note in cases:
        got_v, got_t = coerce(probe, raw, "T")
        ok = (got_v == want_v or (got_v is None and want_v is None)) and got_t == want_t
        ck(f"[{rule}] {note}", ok, f"raw={raw!r} -> value={got_v!r} text={got_t!r} (want {want_v!r}/{want_t!r})")

    # R2 hard guarantee: no non-numeric string may ever yield 0.0
    zeros = [r for r, *_ in cases if isinstance(r, (str, bytes))
             and coerce(probe, r, "T")[0] == 0.0]
    ck("[R2] no non-numeric string ever coerces to 0.0", not zeros, f"offenders={zeros}")


# ------------------------------------------------------------------ 2. schema
def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    except Exception:
        return []


def validate_schema() -> None:
    section("2. SCHEMA — both columns exist on the local historian")
    p = db_path("trustnode_app_store.db")
    if not os.path.exists(p):
        skip("local historian schema", f"not found: {p}")
        return
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        cols = _cols(conn, "historian_readings")
        ck("[R6] historian_readings has `value`", "value" in cols, f"cols={cols}")
        ck("[R6] historian_readings has `value_text`", "value_text" in cols)
        info = {r[1]: r for r in conn.execute('PRAGMA table_info("historian_readings")')}
        if "value" in info:
            ck("[R6] `value` is NULLable (NULL = not a number)", info["value"][3] == 0,
               f"notnull={info['value'][3]}")
    finally:
        conn.close()


def validate_sink_sql() -> None:
    section("3. SINK SQL — every DDL/INSERT carries value_text (source audit)")
    src_path = os.path.join(BACKEND, "app", "services", "plc_manager.py")
    if not os.path.exists(src_path):
        skip("sink SQL audit", "plc_manager.py not found")
        return
    src = open(src_path, encoding="utf-8").read()

    inserts = re.findall(r"INSERT INTO[^\"]*\"?\{?[a-z_]*\}?\"?[^(]*\(([^)]*)\)", src)
    reading_inserts = [c for c in inserts if "tag_name" in c and "ts_utc" in c]
    missing = [c.strip()[:70] for c in reading_inserts if "value_text" not in c]
    ck("[R6] every reading INSERT lists value_text",
       not missing, f"{len(reading_inserts)} reading INSERTs; missing={missing}")

    n_ph = src.count(":value_text")
    n_keys = len(re.findall(r'"value_text":\s*r\.value_text|"value_text":\s*r\.get\("value_text"\)', src))
    ck("[R6] every :value_text placeholder has a row-dict key",
       n_keys >= n_ph, f"placeholders={n_ph} dict_keys={n_keys}")

    ck("[R6] Postgres migration adds value_text to existing tables",
       'ADD COLUMN IF NOT EXISTS value_text' in src)
    ck("[R6] SQLite migration adds value_text to existing tables",
       'ADD COLUMN value_text TEXT NULL' in src)

    # R4: no fabricated zeros left on error paths
    ck("[R4] no `value=0.0` fabricated on any driver error path",
       "value=0.0," not in src,
       "a failed read must store NULL, not a real-looking zero")


# ------------------------------------------------------------------- 4. data
def validate_stored_data() -> None:
    section("4. STORED DATA — what the running gateway actually wrote")
    p = db_path("trustnode_app_store.db")
    if not os.path.exists(p):
        skip("stored data", f"not found: {p}")
        return
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cut = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        rows = list(conn.execute(
            """SELECT tag_name,
                      COUNT(*) n,
                      SUM(CASE WHEN value IS NULL THEN 1 ELSE 0 END) nulls,
                      SUM(CASE WHEN value = 0.0 THEN 1 ELSE 0 END) zeros,
                      SUM(CASE WHEN value_text IS NOT NULL AND TRIM(value_text) <> '' THEN 1 ELSE 0 END) texts,
                      MAX(quality_label) ql,
                      MAX(value_text) sample
               FROM historian_readings WHERE ts_utc >= ?
               GROUP BY tag_name ORDER BY tag_name""", (cut,)))
        if not rows:
            skip("stored data", "no readings in the last 10 minutes — is a gateway running?")
            return
        print(f"  ({len(rows)} tags collected in the last 10 min)\n")

        text_tags = [r for r in rows if r["texts"] > 0]
        num_tags = [r for r in rows if r["texts"] == 0]

        ck("[R1] numeric tags present", bool(num_tags), f"{len(num_tags)} numeric tags")
        if not text_tags:
            skip("[R2/R3] STRING tag checks", "no STRING-typed tags are being collected")
        for r in text_tags:
            tag, n, nulls, zeros, texts = r["tag_name"], r["n"], r["nulls"], r["zeros"], r["texts"]
            ck(f"[R2] '{tag}' preserves text on every row", texts == n,
               f"rows={n} with_text={texts} sample={str(r['sample'])[:40]!r}")
            # every row is either NULL (non-numeric) or a real parsed number.
            # What must NEVER happen: value=0.0 while the text is non-numeric.
            bad = list(conn.execute(
                """SELECT COUNT(*) c FROM historian_readings
                   WHERE tag_name = ? AND ts_utc >= ? AND value = 0.0
                     AND value_text IS NOT NULL
                     AND CAST(value_text AS REAL) = 0.0 AND TRIM(value_text) NOT IN ('0','0.0','0.00')""",
                (tag, cut)))[0]["c"]
            ck(f"[R2] '{tag}' never fabricates 0.0 for non-numeric text", bad == 0,
               f"offending rows={bad}")

        # R4: failed reads must be NULL, never 0.0
        badq = list(conn.execute(
            """SELECT COUNT(*) c, SUM(CASE WHEN value = 0.0 THEN 1 ELSE 0 END) z
               FROM historian_readings WHERE ts_utc >= ? AND quality_label <> 'GOOD'""", (cut,)))[0]
        if (badq["c"] or 0) == 0:
            print("  note: no BAD-quality rows in window (all reads succeeded) — R4 not exercised")
        else:
            ck("[R4] BAD-quality rows store NULL, not 0.0", (badq["z"] or 0) == 0,
               f"bad_rows={badq['c']} of which value=0.0 -> {badq['z']}")

        # R5: program-scoped tags read successfully
        prog = list(conn.execute(
            """SELECT COUNT(DISTINCT tag_name) n,
                      SUM(CASE WHEN quality_label = 'GOOD' THEN 1 ELSE 0 END) good,
                      COUNT(*) total
               FROM historian_readings
               WHERE ts_utc >= ? AND tag_name LIKE 'Program:%'""", (cut,)))[0]
        if (prog["n"] or 0) == 0:
            skip("[R5] program-scoped tags", "none configured on this gateway")
        else:
            ck("[R5] program-scoped tags read GOOD", prog["good"] == prog["total"],
               f"{prog['n']} program tags, {prog['good']}/{prog['total']} GOOD")
    finally:
        conn.close()


# -------------------------------------------------------------- 5. front-end
def validate_frontend() -> None:
    section("5. FRONTEND — NULL must not render as 0")
    fe = os.path.join(REPO, "frontend", "src", "components", "Dashboard", "dashboardAnalytics.js")
    if not os.path.exists(fe):
        skip("frontend guards", "dashboardAnalytics.js not found")
        return
    src = open(fe, encoding="utf-8").read()
    m = re.search(r"function toNum\(v\)\s*\{(.*?)\n\}", src, re.S)
    body = m.group(1) if m else ""
    ck("[R7] toNum() treats null/undefined/'' as absent, not 0",
       ("v === null" in body and "undefined" in body),
       "Number(null) === 0 passes isFinite — without this guard a NULL "
       "would plot as a real zero")
    ck("[R7] getLatestTagRow recovers value_text for STRING tags",
       "value_text" in src and "last_value_text" in src)

    app = os.path.join(REPO, "frontend", "src", "App.jsx")
    if os.path.exists(app):
        a = open(app, encoding="utf-8").read()
        ck("[R7] tag monitor exposes text for STRING tags",
           "last_value_text" in a and "lastTextValue" in a)
        ck("[R7] CSV export includes value_text",
           "value_text" in a and "csv_header" in a)

    # R11 — a text tag must never render as a number (or "-") anywhere the
    # operator looks. These are the exact spots that showed "0.000" / "-".
    dd = os.path.join(REPO, "frontend", "src", "components", "Dashboard", "DashboardDesigner.jsx")
    if os.path.exists(dd):
        d = open(dd, encoding="utf-8").read()
        m = re.search(r"function formatHeaderValue\(value, decimals = 3\)\s*\{(.*?)\n\}", d, re.S)
        ck("[R11] formatHeaderValue returns '-' for null (not 0.000)",
           bool(m) and "value === null" in m.group(1),
           "Number(null) === 0 passes isFinite -> a text tag rendered as 0.000")
        ck("[R11] widget header prefers last_value_text",
           d.count("last_value_text != null") >= 2,
           "primary series AND extra series must both prefer text")
    if os.path.exists(app):
        a = open(app, encoding="utf-8").read()
        ck("[R11] tag table renders value_text when numeric is NULL",
           "latest?.value_text != null" in a,
           'previously `latest?.value ?? live?.value ?? "-"` showed "-" for text tags')
        ck("[R11] live tag value map carries value_text",
           a.count("value_text: r.value_text ?? null") >= 1
           and a.count("value_text: row?.value_text ?? null") >= 1,
           "both live-reading builders must carry text")


# ------------------------------------------------------------------- 6. PLC
def validate_live_plc(ip: str) -> None:
    section(f"6. LIVE PLC ({ip}) — program tags + declared data types")
    try:
        from pycomm3 import LogixDriver
    except Exception as exc:
        skip("live PLC", f"pycomm3 unavailable: {exc}")
        return
    try:
        with LogixDriver(ip, init_tags=True, init_program_tags=True) as plc:
            meta = plc.tags
            prog = [k for k in meta if str(k).startswith("Program:")]
            ck("[R5] controller returns program-scoped tags", bool(prog),
               f"{len(prog)} program tags cached (0 means init_program_tags is off)")

            by_type: dict[str, list[str]] = {}
            for name, m in meta.items():
                dt = m.get("data_type_name") or m.get("data_type") or "?"
                if isinstance(dt, dict):
                    dt = dt.get("name", "STRUCT")
                by_type.setdefault(str(dt), []).append(name)
            print("  declared types: " + ", ".join(
                f"{k}={len(v)}" for k, v in sorted(by_type.items(), key=lambda x: -len(x[1]))[:8]))

            strings = by_type.get("STRING", [])[:3]
            if not strings:
                skip("[R2/R3] live STRING read", "controller has no STRING tags")
            else:
                for res in plc.read(*strings):
                    is_str = isinstance(res.value, str)
                    ck(f"[R2] STRING '{res.tag}' reads as text", is_str and not res.error,
                       f"value={res.value!r} error={res.error!r}")
            if prog:
                for res in plc.read(*prog[:3]):
                    ck(f"[R5] program tag '{res.tag}' reads without error",
                       not res.error, f"value={res.value!r} error={res.error!r}")
    except Exception as exc:
        skip("live PLC", f"connect failed: {exc}")


# -------------------------------------------------------------- 7. interlock
def validate_interlock() -> None:
    """The dashboard interlock must block a TEXT tag on a numeric-only widget,
    allow it on text-capable widgets, and FAIL OPEN on anything unknown."""
    section("7. WIDGET INTERLOCK — text tags vs numeric-only widgets")
    tt = os.path.join(REPO, "frontend", "src", "components", "Dashboard", "tagTypes.js")
    if not os.path.exists(tt):
        skip("interlock", "tagTypes.js not found")
        return
    src = open(tt, encoding="utf-8").read()

    # R8: widget keys must exist in the real registry, or the interlock is inert.
    reg = os.path.join(REPO, "frontend", "src", "components", "Dashboard", "widgetRegistry.js")
    if os.path.exists(reg):
        real = set(re.findall(r'key:\s*"([a-z_0-9]+)"', open(reg, encoding="utf-8").read()))
        block = re.search(r"NUMERIC_ONLY_WIDGETS = new Set\(\[(.*?)\]\)", src, re.S)
        listed = set(re.findall(r'"([a-z_0-9]+)"', block.group(1) if block else ""))
        bogus = sorted(listed - real)
        ck("[R8] every numeric-only widget key exists in widgetRegistry",
           not bogus and bool(listed),
           f"listed={sorted(listed)} unknown={bogus}")
        # the real chart widgets must all be covered
        charts = {k for k in real if k.endswith("_chart")}
        missing = sorted(charts - listed)
        ck("[R8] all *_chart widgets are treated as numeric-only",
           not missing, f"uncovered={missing}")

    # R9: fail-open is mandatory — an unknown tag must never be blocked.
    ck("[R9] unknown tags fail OPEN (never blocked)",
       "UNKNOWN" in src and "fail open" in src.lower(),
       "blocking on a guess would refuse a tag that is merely not collected yet")
    ck("[R9] only a confident text tag yields severity 'block'",
       'severity: "block"' in src and "TAG_KIND.TEXT" in src)
    ck("[R9] numeric-looking strings warn but are allowed",
       'severity: "warn"' in src and "NUMERIC_TEXT" in src)

    # R10: declared type is authoritative and wired end-to-end.
    plc = os.path.join(BACKEND, "app", "routers", "plc.py")
    if os.path.exists(plc):
        p = open(plc, encoding="utf-8").read()
        ck("[R10] discover-tags exposes declared `types`",
           "types: dict[str, str]" in p and "types=types_map" in p)
    app = os.path.join(REPO, "frontend", "src", "App.jsx")
    if os.path.exists(app):
        a = open(app, encoding="utf-8").read()
        ck("[R10] frontend registers declared types from discovery",
           "registerDeclaredTagTypes" in a)
    dd = os.path.join(REPO, "frontend", "src", "components", "Dashboard", "DashboardDesigner.jsx")
    if os.path.exists(dd):
        d = open(dd, encoding="utf-8").read()
        ck("[R10] widget editor guards save with the interlock",
           "widgetIsNumericOnly(form.type)" in d and "checkTagForWidget" in d)
        ck("[R10] tag dropdown marks TEXT tags", "· TEXT" in d)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plc", default="", help="PLC IP for live checks (e.g. 192.168.10.240)")
    args = ap.parse_args()

    os.environ.setdefault("TRUSTNODE_APP_STORE_PATH", db_path("trustnode_app_store.db"))
    os.environ.setdefault("TRUSTNODE_TENANT_ID", "tenant-cust-e5916328")

    print("TrustNode — tag type handling validation")
    print(f"repo: {REPO}")

    validate_coercion()
    validate_schema()
    validate_sink_sql()
    validate_stored_data()
    validate_frontend()
    validate_interlock()
    if args.plc:
        validate_live_plc(args.plc)
    else:
        section("6. LIVE PLC — skipped")
        print("  (re-run with --plc <ip> to validate against the controller)")

    section("SUMMARY")
    print(f"  passed : {len(PASS)}")
    print(f"  failed : {len(FAIL)}")
    print(f"  skipped: {len(SKIP)}")
    if FAIL:
        print("\n  FAILURES:")
        for f in FAIL:
            print(f"    - {f}")
    print()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
