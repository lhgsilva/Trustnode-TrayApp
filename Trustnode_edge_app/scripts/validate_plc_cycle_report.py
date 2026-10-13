# -*- coding: utf-8 -*-
"""Validate cycle triggers, inner triggers and the report against REAL PLC data.

2026-10-01: a PLC is running ladder that drives

    start_cycle  -> cycle_started       end_cycle  -> cycle_ended      (main)
    start_cycle1 -> cycle_started1      end_cycle1 -> cycle_ended1     (inner)
    start_cycle2 -> cycle_started2      end_cycle2 -> cycle_ended2     (inner)

plus a couple of simulated REAL values to chart. This checks the whole chain
against what the PLC actually recorded, not against seeded data:

  * the bits are being collected at all, and recently;
  * the main start/end pair yields ONE window per cycle;
  * each inner pair yields its own window INSIDE the main one;
  * resolve_trigger_window - what a report section uses - finds the same
    windows from the historian rows;
  * a report renders with sections bound to the main window and to each
    inner trigger.

Safety: the live store is only ever opened READ-ONLY, and the matched rows are
copied into a scratch workspace before anything imports app.state. Importing
it migrates whatever database the environment points at, so the renderer must
never see the live one.

Usage:
  python scripts/validate_plc_cycle_report.py                  # auto-discover
  python scripts/validate_plc_cycle_report.py --hours 24 --gateway gw-1779098315351
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:160]) if detail else ""))
    if not ok:
        FAILS.append(name)


def live_store_path() -> str:
    explicit = os.environ.get("TRUSTNODE_APP_STORE_PATH", "").strip()
    if explicit:
        return explicit
    base = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
    if base:
        return os.path.join(base, "trustnode_app_store.db")
    return os.path.join(os.path.expanduser("~"), ".trustnode_edge", "data",
                        "trustnode_app_store.db")


#: The coil names the ladder drives. The input contacts (start_cycle, ...) are
#: accepted too - whichever the gateway actually collects.
MAIN = [("cycle_started", "start_cycle"), ("cycle_ended", "end_cycle")]
INNER = [[("cycle_started1", "start_cycle1"), ("cycle_ended1", "end_cycle1")],
         [("cycle_started2", "start_cycle2"), ("cycle_ended2", "end_cycle2")]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0,
                    help="how far back to look for the cycle bits")
    ap.add_argument("--gateway", default="", help="restrict to one gateway id")
    args = ap.parse_args()

    print("TrustNode - cycle + inner triggers against real PLC readings")
    print()

    store = live_store_path()
    print("[the live historian, opened read-only]")
    print("  %s" % store)
    if not os.path.exists(store):
        check("the store exists", False, store)
        return 2
    conn = sqlite3.connect("file:%s?mode=ro" % store.replace(os.sep, "/"),
                           uri=True, timeout=20)
    conn.row_factory = sqlite3.Row

    wanted = {name for pair in MAIN for name in pair}
    for grp in INNER:
        for pair in grp:
            wanted.update(pair)

    params: list = [args.hours]
    gw_sql = ""
    if args.gateway:
        gw_sql = " AND COALESCE(gateway_id,'') = ?"
        params.append(args.gateway)
    rows = conn.execute(
        "SELECT ts_utc, gateway_id, tag_name, value, value_text "
        "FROM historian_readings "
        "WHERE ts_utc >= datetime('now', '-' || ? || ' hours')" + gw_sql +
        " ORDER BY ts_utc ASC", params).fetchall()
    present = {}
    for r in rows:
        present.setdefault(str(r["tag_name"]), 0)
        present[str(r["tag_name"])] += 1

    found = sorted(t for t in present if t in wanted)
    check("the cycle bits are being collected",
          bool(found),
          "looked for %s in the last %gh - found %s"
          % (sorted(wanted), args.hours,
             (", ".join("%s(%d)" % (t, present[t]) for t in found) or "NOTHING")))
    if not found:
        print()
        print("  Nothing to validate. Either the gateway is not collecting these")
        print("  tags yet, or they are named differently. Re-run with --hours")
        print("  or point --gateway at the PLC once it is collecting; the tags")
        print("  actually seen in that window were:")
        for t, n in sorted(present.items(), key=lambda kv: -kv[1])[:15]:
            print("     %-34s %d" % (t, n))
        conn.close()
        print()
        print("RESULT: FAIL - no PLC cycle readings to validate against")
        return 2

    def pick(pair):
        for name in pair:
            if name in present:
                return name
        return ""

    start_tag, end_tag = pick(MAIN[0]), pick(MAIN[1])
    check("  a main START and END bit are both present",
          bool(start_tag and end_tag), "%s / %s" % (start_tag or "-", end_tag or "-"))

    gateways = sorted({str(r["gateway_id"] or "") for r in rows
                       if str(r["tag_name"]) in wanted})
    gw = args.gateway or (gateways[0] if gateways else "")
    print("  gateway: %s" % (gw or "(none)"))

    def transitions(tag, report_initial_high=True):
        """Rising edges of a boolean tag, as timestamps.

        A bit that is ALREADY high on the first reading has no rising edge in
        this window - the cycle began before it. That is reported rather than
        silently counted, because treating it as a start would put a made-up
        timestamp on a real measurement.
        """
        out, prev = [], None
        for r in rows:
            if str(r["tag_name"]) != tag:
                continue
            raw = r["value"]
            if raw is None:
                txt = str(r["value_text"] or "").strip().lower()
                cur = txt in {"1", "true", "on", "yes"}
            else:
                cur = float(raw) != 0.0
            if prev is None and cur and report_initial_high:
                print("     note: %s was already ON at %s - that cycle started "
                      "before this window" % (tag, str(r["ts_utc"])))
            if cur and prev is False:
                out.append(str(r["ts_utc"]))
            prev = cur
        return out

    print()
    print("[what the PLC actually did]")
    starts = transitions(start_tag) if start_tag else []
    ends = transitions(end_tag) if end_tag else []
    check("the main cycle started at least once", bool(starts),
          "%d rising edge(s) on %s" % (len(starts), start_tag))
    check("  and ended", bool(ends), "%d on %s" % (len(ends), end_tag))

    cycles = []
    for s in starts:
        nxt = [e for e in ends if e > s]
        if nxt:
            cycles.append((s, nxt[0]))
    check("  every start is matched by an end", len(cycles) == len(starts),
          "%d complete cycle(s) of %d start(s)" % (len(cycles), len(starts)))
    for i, (a, b) in enumerate(cycles[:3], 1):
        print("     cycle %d: %s -> %s" % (i, a, b))

    inner_found = []
    for n, grp in enumerate(INNER, 1):
        st, en = pick(grp[0]), pick(grp[1])
        if not (st and en):
            continue
        s_edges, e_edges = transitions(st), transitions(en)
        if s_edges:
            inner_found.append((n, st, en, s_edges, e_edges))
        print("     inner %d: %s=%d edge(s), %s=%d" % (n, st, len(s_edges), en, len(e_edges)))
    check("at least one INNER trigger fired", bool(inner_found),
          "the inner pairs are what a per-section trigger uses")

    if cycles and inner_found:
        main_a, main_b = cycles[0]
        inside = [(n, s) for (n, _st, _en, s_edges, _e) in inner_found
                  for s in s_edges if main_a <= s <= main_b]
        check("  an inner trigger fired INSIDE the main cycle", bool(inside),
              "%d inner start(s) within %s..%s" % (len(inside), main_a, main_b))

    # ---- copy the matched rows into a scratch workspace -------------------- #
    tmp = tempfile.mkdtemp(prefix="tn-plcval-")
    scratch = os.path.join(tmp, "s.db")
    out = sqlite3.connect(scratch)
    out.execute(
        "CREATE TABLE historian_readings ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT DEFAULT 'default',"
        " ts_utc TEXT, gateway_id TEXT, gateway_name TEXT, device_name TEXT,"
        " plc_ip TEXT, database_name TEXT, tag_name TEXT, value REAL,"
        " value_text TEXT, data_type TEXT, quality INTEGER, quality_label TEXT,"
        " source TEXT, created_utc TEXT)")
    out.executemany(
        "INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id, tag_name,"
        " value, value_text, quality, quality_label, source, created_utc)"
        " VALUES ('default',?,?,?,?,?,192,'GOOD','plc-validation',?)",
        [(str(r["ts_utc"]), str(r["gateway_id"] or ""), str(r["tag_name"]),
          r["value"], r["value_text"], str(r["ts_utc"])) for r in rows])
    out.commit()
    out.close()
    conn.close()
    print()
    print("[copied %d live reading(s) into a scratch workspace]" % len(rows))
    print("  %s" % scratch)

    # Numeric tags worth charting - the simulated REALs, whatever they are named.
    numeric = sorted({str(r["tag_name"]) for r in rows
                      if r["value"] is not None and str(r["tag_name"]) not in wanted})
    print("  numeric tags available to chart: %s"
          % (", ".join(numeric[:8]) + (" ..." if len(numeric) > 8 else "") or "(none)"))

    spec = {
        "scratch": scratch,
        "gateway": gw,
        "start_tag": start_tag,
        "end_tag": end_tag,
        "cycle": list(cycles[0]) if cycles else [],
        "inner": [{"n": n, "start": st, "end": en} for (n, st, en, _s, _e) in inner_found],
        "numeric": numeric[:4],
        # The whole span actually collected, so a report can still be produced
        # before any cycle has been run.
        "span": [str(rows[0]["ts_utc"]), str(rows[-1]["ts_utc"])] if rows else [],
    }
    spec_path = os.path.join(tmp, "spec.json")
    with open(spec_path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh)

    # ---- validate the report pipeline in that scratch workspace ------------ #
    print()
    print("[the report pipeline, against those readings]")
    env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
               TRUSTNODE_APP_STORE_PATH=scratch,
               TRUSTNODE_BOOT_INTEGRITY_CHECK="never")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "_plc_cycle_render.py"), spec_path],
        cwd=os.path.join(ROOT, "backend"), env=env, capture_output=True,
        text=True, timeout=600)
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CHECK "):
            _, ok, rest = line.split(" ", 2)
            name, _, detail = rest.partition("|")
            check(name.strip(), ok == "PASS", detail.strip())
        elif line.startswith("PDF "):
            pdf = line[4:].strip()
            dest = os.path.join(ROOT, "scripts", "validation_out",
                                "PLC_Cycle_Validation.pdf")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                shutil.copyfile(pdf, dest)
                print()
                print("REPORT: %s" % dest)
            except OSError:
                pass
    if proc.returncode != 0 and not FAILS:
        check("the render step completed", False, (proc.stderr or "")[-200:])

    print()
    print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
    return 0 if not FAILS else 2


if __name__ == "__main__":
    sys.exit(main())
