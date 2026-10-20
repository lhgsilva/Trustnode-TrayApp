# -*- coding: utf-8 -*-
"""Fold abandoned SQLite stores into the rollup tier, then let them go.

2026-10-20. The data directory held 7.8 GB in files nothing writes any more:

    trustnode_edge.db                      4.28 GB  10,556,079 readings
                                                    2026-07-16 -> 2026-08-21
    trustnode_app_store.db.preVACUUM_...   2.62 GB     521,482 readings
                                                    2026-06-15 -> 2026-06-19
    trustnode_store_forward.db.bak_...     0.90 GB   1,785,872 outbox rows

The obvious move is to delete them. That would have destroyed five weeks of
history, because the live store's earliest surviving data of ANY kind is
2026-08-23 (the 900 s tier) - both historian files end BEFORE that. Retention
pruned the raw rows they duplicated, so they stopped being copies and became
the only record.

The resolution is not "delete" or "keep 7.8 GB". It is to keep what the
operator's own policy says to keep. The active Lean policy retains 1-hour
buckets for a year, so folding trustnode_edge.db to 1-hour buckets costs
42,336 rows - about 15 MB for 4.28 GB, 290x smaller - and loses nothing the
policy would have kept anyway.

HOW IT STAYS HONEST

  * The aggregation is retention_engine._ROLLUP_FROM_RAW, imported, not
    re-written. One rule, one place: if the engine's bucket semantics
    change, this moves with them.
  * The legacy table has no tenant_id or gateway_id. Tags are mapped to the
    gateway that owns them in the live rollup, and ONLY when exactly one
    gateway owns that tag. Ambiguous or unknown tags go to a labelled
    gateway id instead of being silently attributed to the wrong series.
  * Nothing is written without --apply, and nothing is deleted without a
    manifest that verified first. Verification compares every (tag, hour)
    in the source against the bucket actually written, by count.

USAGE

    python scripts/archive_legacy_stores.py --list
    python scripts/archive_legacy_stores.py --archive            # dry run
    python scripts/archive_legacy_stores.py --archive --apply
    python scripts/archive_legacy_stores.py --verify
    python scripts/archive_legacy_stores.py --delete --apply

Point it somewhere safe first with --store PATH. The default is the live
workspace, and --apply against the live workspace asks for confirmation
unless --yes is given.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), ".trustnode_edge", "data")
STORE_NAME = "trustnode_app_store.db"
MANIFEST_NAME = "legacy_archive_manifest.json"

#: Where tags we cannot attribute with certainty land. Deliberately visible:
#: a reader seeing this knows the series was imported, not measured here.
UNMAPPED_GATEWAY = "legacy-unmapped"


def _rollup_sql() -> str:
    """The engine's own raw->bucket SQL, pointed at our attached source."""
    from app.services.retention_engine import _ROLLUP_FROM_RAW
    needle = "FROM historian_readings"
    if _ROLLUP_FROM_RAW.count(needle) != 1:
        raise SystemExit(
            "retention_engine._ROLLUP_FROM_RAW no longer reads exactly one "
            "'FROM historian_readings'. Re-check this script against it "
            "rather than letting the aggregation silently drift.")
    return _ROLLUP_FROM_RAW.replace(needle, "FROM legacy_src")


# --------------------------------------------------------------- candidates
def candidates(data_dir: str) -> List[Dict[str, Any]]:
    """Legacy files present, with what is actually inside them."""
    out: List[Dict[str, Any]] = []
    for fn in sorted(os.listdir(data_dir)):
        p = os.path.join(data_dir, fn)
        if not os.path.isfile(p):
            continue
        legacy = (fn == "trustnode_edge.db"
                  or ".preVACUUM" in fn
                  or fn.startswith("trustnode_store_forward.db.bak"))
        if not legacy:
            continue
        info: Dict[str, Any] = {"file": fn, "path": p,
                                "bytes": os.path.getsize(p), "readings": 0,
                                "span": None, "table": None, "archivable": False}
        try:
            con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"),
                                  uri=True, timeout=30)
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "historian_readings" in names:
                info["table"] = "historian_readings"
                info["readings"] = con.execute(
                    "SELECT COUNT(*) FROM historian_readings").fetchone()[0]
                lo, hi = con.execute(
                    "SELECT MIN(ts_utc), MAX(ts_utc) FROM historian_readings"
                ).fetchone()
                info["span"] = [str(lo or "")[:19], str(hi or "")[:19]]
                info["archivable"] = bool(info["readings"])
            elif "outbox_readings" in names:
                # Queued sink writes, not a historian. Carries no independent
                # history worth folding - it is what the sinks already took.
                info["table"] = "outbox_readings"
                info["readings"] = con.execute(
                    "SELECT COUNT(*) FROM outbox_readings").fetchone()[0]
            con.close()
        except Exception as exc:  # noqa: BLE001
            info["error"] = "%s: %s" % (type(exc).__name__, exc)
        out.append(info)
    return out


# ------------------------------------------------------------------ mapping
def tag_gateway_map(store: str) -> Tuple[Dict[str, str], List[str]]:
    """tag -> the gateway that owns it, when exactly one does."""
    con = sqlite3.connect("file:%s?mode=ro" % store.replace("\\", "/"),
                          uri=True, timeout=60)
    owners: Dict[str, set] = {}
    for tag, gw in con.execute(
            "SELECT DISTINCT tag_name, gateway_id FROM historian_rollup"):
        owners.setdefault(str(tag), set()).add(str(gw or ""))
    for tag, gw in con.execute(
            "SELECT DISTINCT tag_name, gateway_id FROM historian_readings"):
        owners.setdefault(str(tag), set()).add(str(gw or ""))
    con.close()
    mapped = {t: next(iter(g)) for t, g in owners.items() if len(g) == 1}
    ambiguous = sorted(t for t, g in owners.items() if len(g) > 1)
    return mapped, ambiguous


def resolve_tenant(store: str, override: str = "") -> str:
    if override:
        return override
    con = sqlite3.connect("file:%s?mode=ro" % store.replace("\\", "/"),
                          uri=True, timeout=30)
    row = con.execute(
        "SELECT tenant_id, COUNT(*) c FROM historian_rollup GROUP BY tenant_id "
        "ORDER BY c DESC LIMIT 1").fetchone()
    con.close()
    return str(row[0]) if row else ""


# ------------------------------------------------------------------ archive
def archive_file(store: str, src: Dict[str, Any], tenant: str, res_s: int,
                 apply: bool) -> Dict[str, Any]:
    """Fold one legacy historian into the rollup tier."""
    res: Dict[str, Any] = {"file": src["file"], "resolution_s": res_s,
                           "tenant_id": tenant, "source_readings": src["readings"],
                           "applied": bool(apply)}
    mapped, ambiguous = tag_gateway_map(store)
    con = sqlite3.connect(store, timeout=120.0)
    # Explicit transaction control: python sqlite3 opens one implicitly on the
    # first DML, and BEGIN IMMEDIATE then fails with "transaction within a
    # transaction". We want one deliberate write txn around the fold.
    con.isolation_level = None
    con.execute("PRAGMA busy_timeout=120000")
    try:
        con.execute("ATTACH DATABASE ? AS legacy", ("file:%s?mode=ro"
                                                    % src["path"].replace("\\", "/"),))
    except sqlite3.OperationalError:
        con.execute("ATTACH DATABASE ? AS legacy", (src["path"],))

    tags = [r[0] for r in con.execute(
        "SELECT DISTINCT tag_name FROM legacy.historian_readings")]
    pairs = [(t, mapped.get(t, UNMAPPED_GATEWAY)) for t in tags]
    res["tags"] = len(tags)
    res["unmapped_tags"] = sorted(t for t, g in pairs if g == UNMAPPED_GATEWAY)
    res["gateways"] = sorted({g for _, g in pairs})

    # A temp table beats a correlated CASE over 10M rows, and keeps the
    # engine's SQL untouched apart from its FROM.
    con.execute("CREATE TEMP TABLE _tagmap (tag_name TEXT PRIMARY KEY, "
                "gateway_id TEXT NOT NULL)")
    con.executemany("INSERT INTO _tagmap VALUES (?,?)", pairs)
    con.execute("""
        CREATE TEMP VIEW legacy_src AS
        SELECT r.ts_utc AS ts_utc,
               ? AS tenant_id,
               m.gateway_id AS gateway_id,
               r.tag_name AS tag_name,
               r.value AS value,
               r.value_text AS value_text,
               r.quality AS quality
        FROM legacy.historian_readings r
        JOIN _tagmap m ON m.tag_name = r.tag_name
    """.replace("?", "'" + tenant.replace("'", "''") + "'"))

    lo, hi = src["span"]
    sql = _rollup_sql()
    t0 = time.time()
    if apply:
        con.execute("BEGIN IMMEDIATE")
        con.execute(sql, {"res": res_s, "tenant": tenant,
                          "from_ts": lo, "to_ts": _bump(hi)})
        con.commit()
    res["seconds"] = round(time.time() - t0, 1)

    # ---- verification: every (tag, bucket) in the source must be present
    # with the same count. This is the check that licenses the delete.
    srcq = con.execute("""
        SELECT m.gateway_id, r.tag_name,
               (CAST(strftime('%s', r.ts_utc) AS INTEGER) / ?) * ? * 1000 AS b,
               COUNT(r.value)
        FROM legacy.historian_readings r
        JOIN _tagmap m ON m.tag_name = r.tag_name
        GROUP BY m.gateway_id, r.tag_name, b
    """, (res_s, res_s)).fetchall()
    missing, mismatched = 0, 0
    if apply:
        for gw, tag, b, n in srcq:
            got = con.execute(
                "SELECT n FROM historian_rollup WHERE resolution_s=? AND "
                "tenant_id=? AND gateway_id=? AND tag_name=? AND bucket_ms=?",
                (res_s, tenant, gw, tag, b)).fetchone()
            if got is None:
                missing += 1
            elif int(got[0] or 0) < int(n or 0):
                mismatched += 1
    res["source_buckets"] = len(srcq)
    res["missing_buckets"] = missing
    res["short_buckets"] = mismatched
    res["verified"] = bool(apply and missing == 0 and mismatched == 0)
    con.execute("DETACH DATABASE legacy")
    con.close()
    return res


def _bump(ts: str) -> str:
    """An exclusive upper bound just past the last reading."""
    return (str(ts)[:19] + "z") if ts else "9999"


# ------------------------------------------------------------------- manifest
def manifest_path(data_dir: str) -> str:
    return os.path.join(data_dir, MANIFEST_NAME)


def load_manifest(data_dir: str) -> Dict[str, Any]:
    try:
        with open(manifest_path(data_dir), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_manifest(data_dir: str, man: Dict[str, Any]) -> None:
    with open(manifest_path(data_dir), "w", encoding="utf-8") as fh:
        json.dump(man, fh, indent=2)


# ----------------------------------------------------------------------- cli
def human(n: float) -> str:
    return "%.2f GB" % (n / 1e9) if n >= 1e9 else "%.1f MB" % (n / 1e6)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--store", default="")
    ap.add_argument("--tenant", default="")
    ap.add_argument("--resolution", type=int, default=3600)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--archive", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()

    data_dir = os.path.abspath(a.data_dir)
    store = a.store or os.path.join(data_dir, STORE_NAME)
    if not os.path.isfile(store):
        print("no app store at %s" % store)
        return 2

    cands = candidates(data_dir)
    print("TrustNode - legacy store archive")
    print("  data dir : %s" % data_dir)
    print("  store    : %s" % store)
    print()
    if not cands:
        print("  no legacy files present - nothing to do.")
        return 0

    total = sum(c["bytes"] for c in cands)
    print("  %-44s %10s %13s  %s" % ("file", "size", "rows", "span"))
    for c in cands:
        span = (" -> ".join(c["span"])) if c.get("span") else "-"
        print("  %-44s %10s %13s  %s"
              % (c["file"][:44], human(c["bytes"]),
                 "{:,}".format(c["readings"]), span))
    print()
    print("  total on disk: %s" % human(total))

    if a.list or not (a.archive or a.verify or a.delete):
        print()
        print("  --archive to fold the historian files into %ss buckets"
              % a.resolution)
        return 0

    tenant = resolve_tenant(store, a.tenant)
    if not tenant:
        print("  cannot resolve a tenant - pass --tenant")
        return 2

    live = os.path.abspath(store) == os.path.abspath(
        os.path.join(DEFAULT_DATA_DIR, STORE_NAME))
    if a.apply and live and not a.yes:
        print()
        print("  REFUSING: --apply against the LIVE workspace without --yes.")
        print("  Run it against a copy first:")
        print("    --store C:\\path\\to\\copy.db")
        return 2

    man = load_manifest(data_dir)
    if a.archive:
        print()
        print("  tenant     : %s" % tenant)
        print("  resolution : %ss" % a.resolution)
        print("  mode       : %s" % ("APPLY" if a.apply else "dry run"))
        for c in cands:
            if not c.get("archivable"):
                print()
                print("  %s: no historian table - not archivable here." % c["file"])
                print("    (%s rows in %s; the sinks already consumed these)"
                      % ("{:,}".format(c["readings"]), c.get("table") or "?"))
                continue
            print()
            print("  archiving %s ..." % c["file"])
            r = archive_file(store, c, tenant, a.resolution, a.apply)
            man.setdefault("files", {})[c["file"]] = r
            print("    tags=%s gateways=%s buckets=%s  %ss"
                  % (r["tags"], len(r["gateways"]), "{:,}".format(r["source_buckets"]),
                     r["seconds"]))
            if r["unmapped_tags"]:
                print("    %d tag(s) could not be attributed to one gateway; "
                      "they are under '%s': %s"
                      % (len(r["unmapped_tags"]), UNMAPPED_GATEWAY,
                         ", ".join(r["unmapped_tags"][:6])))
            if a.apply:
                print("    verified=%s missing=%s short=%s"
                      % (r["verified"], r["missing_buckets"], r["short_buckets"]))
        if a.apply:
            man["archived_utc"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_manifest(data_dir, man)
            print()
            print("  manifest: %s" % manifest_path(data_dir))

    if a.verify:
        print()
        print("  === manifest ===")
        for fn, r in (man.get("files") or {}).items():
            print("  %-44s verified=%s missing=%s short=%s"
                  % (fn[:44], r.get("verified"), r.get("missing_buckets"),
                     r.get("short_buckets")))
        if not man.get("files"):
            print("  nothing archived yet")

    if a.delete:
        print()
        files = man.get("files") or {}
        freed = 0
        for c in cands:
            r = files.get(c["file"])
            if c.get("archivable") and not (r and r.get("verified")):
                print("  KEEPING %s - not verified as archived. Run "
                      "--archive --apply first." % c["file"])
                continue
            if not a.apply:
                print("  would delete %s (%s)" % (c["file"], human(c["bytes"])))
                freed += c["bytes"]
                continue
            shutil.move(c["path"], c["path"] + ".todelete")
            print("  renamed %s -> .todelete (%s)" % (c["file"], human(c["bytes"])))
            freed += c["bytes"]
        print()
        print("  %s %s" % ("freed" if a.apply else "would free", human(freed)))
        if a.apply:
            print("  Files are RENAMED, not removed. Delete the .todelete "
                  "files yourself once you are satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
