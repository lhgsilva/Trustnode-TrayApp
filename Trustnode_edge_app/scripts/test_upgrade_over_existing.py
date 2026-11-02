# -*- coding: utf-8 -*-
"""A new build must install over an existing one and keep everything.

2026-10-30, asked for directly: "we need to make sure every new installer can
be installed in existing computer with installation with all fixes and
updates". The failure this guards against already happened once - a new exe
would not start at all on machines that had an older version, because the
legacy app store was copied to %ProgramData% at import time.

That one escaped because the boot probe set TRUSTNODE_DATA_DIR, so it never
exercised the DEFAULT path resolution - the only resolution a customer's
machine ever uses. This check therefore sets NO data-dir override. It
redirects USERPROFILE/HOME at a throwaway copy of a real workspace instead,
so `~/.trustnode_edge` resolves inside the copy and the real code path runs.

What it proves, against the PACKAGED binary rather than the source:

  * the new backend boots on a store written by the previous version;
  * it resolves to that workspace, not to a fresh empty one somewhere else
    (a silent reset looks exactly like a successful upgrade until the
    customer opens the app and their gateways are gone);
  * the configuration is still there afterwards - gateways, dashboards,
    users, report templates;
  * this release's own migration (LAN settings out of the shared unscoped
    table) runs, and runs ONCE - a second boot must not undo or redo it;
  * the LAN surface answers.

The source workspace is only ever READ, and copied before anything starts.
Nothing here touches the live install.

    python scripts/test_upgrade_over_existing.py
    python scripts/test_upgrade_over_existing.py --workspace <path to a copy>
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
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXE = os.path.join(ROOT, "desktop", "dist", "win-unpacked", "resources",
                   "backend", "trustnode-service.exe")
PORT = "8098"
BASE = "http://127.0.0.1:%s" % PORT

PASS, FAIL, SKIP = [], [], []


def check(name, ok, detail=""):
    # Detail explains a FAILURE. Printed beside PASS it made a healthy run
    # read like a list of problems - "this is the upgrade that bricks a
    # machine" next to the word PASS.
    (PASS if ok else FAIL).append(name)
    print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def skip(name, why):
    SKIP.append(name)
    print("  %-62s SKIP  - %s" % (name, why))


def get(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read())


def count_docs(db):
    """What the OLD store holds, straight from SQLite."""
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        con.row_factory = sqlite3.Row
        for tbl in ("config_documents", "config_documents_scoped"):
            try:
                rows = con.execute("SELECT domain, payload_json FROM %s" % tbl).fetchall()
            except Exception:
                continue
            for r in rows:
                dom = str(r["domain"])
                try:
                    payload = json.loads(r["payload_json"] or "null")
                except Exception:
                    continue
                if isinstance(payload, list):
                    out[dom] = max(out.get(dom, 0), len(payload))
        con.close()
    except Exception:
        pass
    return out


def boot(home, label):
    """Start the packaged backend with HOME redirected. Returns the process."""
    env = dict(os.environ)
    # Deliberately NO TRUSTNODE_DATA_DIR / TRUSTNODE_APP_STORE_PATH: the
    # point is to exercise the resolution a customer's machine uses.
    for k in ("TRUSTNODE_DATA_DIR", "TRUSTNODE_APP_STORE_PATH"):
        env.pop(k, None)
    env.update({
        "USERPROFILE": home,
        "HOME": home,
        "HOMEDRIVE": os.path.splitdrive(home)[0] or "C:",
        "HOMEPATH": os.path.splitdrive(home)[1] or home,
        "TRUSTNODE_PORT": PORT,
        "TRUSTNODE_HOST": "127.0.0.1",
        "TRUSTNODE_SKIP_DOTENV": "1",
    })
    print("  [%s] starting the packaged backend..." % label)
    return subprocess.Popen([EXE], env=env, cwd=os.path.dirname(EXE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_up(seconds=180):
    for _ in range(seconds):
        time.sleep(1)
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            continue
    return False


def port_free(seconds=30):
    """Wait for the port to actually be released.

    2026-10-30: the second boot was started the instant the first was told
    to stop, and uvicorn could not bind a port Windows had not finished
    releasing - so "it boots again after the migration" failed while the
    same two boots passed with a pause between them. A test that reports a
    migration fault when the real answer is a socket is worse than no test:
    it sent me looking for a boot regression that did not exist.
    """
    import socket
    for _ in range(seconds):
        s_ = socket.socket()
        try:
            s_.bind(("127.0.0.1", int(PORT)))
            return True
        except OSError:
            time.sleep(1)
        finally:
            s_.close()
    return False


def stop(proc):
    try:
        proc.terminate()
        proc.wait(timeout=30)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default="",
                    help="an existing .trustnode_edge to copy (default: this user's)")
    args = ap.parse_args()

    print("upgrade over an existing installation")

    if not os.path.isfile(EXE):
        skip("the packaged backend exists", "no build at %s" % EXE)
        print("\n  %d passed, %d failed, %d skipped" % (len(PASS), len(FAIL), len(SKIP)))
        return 0
    check("the packaged backend exists", True, EXE)

    src = args.workspace or os.path.expanduser(os.path.join("~", ".trustnode_edge"))
    if not os.path.isdir(src):
        skip("an existing workspace to upgrade from", "none at %s" % src)
        print("\n  %d passed, %d failed, %d skipped" % (len(PASS), len(FAIL), len(SKIP)))
        return 0

    home = tempfile.mkdtemp(prefix="tn_upgrade_home_")
    dst = os.path.join(home, ".trustnode_edge")
    print("  copying %s -> %s (the source is only read)" % (src, dst))
    # -wal/-shm come and go while the live app checkpoints; one that vanished
    # mid-copy aborted the whole test (2026-11-02). SQLite needs neither.
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.log", "logs", "*-wal", "*-shm"))

    store = os.path.join(dst, "data", "trustnode_app_store.db")
    before = count_docs(store)
    check("the copy carries a real store to upgrade", bool(before),
          "%d domain(s): %s" % (len(before), ", ".join(sorted(before))[:70]))

    proc = None
    try:
        proc = boot(home, "first boot")
        up = wait_up()
        check("the new build boots on the old workspace", up,
              "it never answered - this is the upgrade that bricks a machine")
        if not up:
            return 1

        h = get("/api/health")
        ws = (h.get("workspace") or {}).get("data_dir") or ""
        inside = ws.replace("/", "\\").lower().startswith(dst.replace("/", "\\").lower())
        check("it opened the EXISTING workspace, not a fresh one", inside, ws)
        check("  and did not silently fall back elsewhere",
              not (h.get("workspace") or {}).get("hiding_real_data"),
              json.dumps(h.get("workspace") or {})[:120])

        s = get("/api/lan-sharing/status")
        check("the LAN surface answers after the upgrade", bool(s.get("ok")))
        fw = s.get("firewall") or {}
        check("  the firewall probe reports a real answer",
              fw.get("checked") is True and fw.get("present") in (True, False),
              json.dumps(fw)[:100])

        # Stop BEFORE reading the store. 2026-10-30: these assertions first
        # ran against the file while the backend still had it open, and the
        # store is in WAL mode - the committed rows were in the -wal and a
        # read-only connection did not see them. The check failed twice and
        # passed once, on timing alone, for a migration the boot log proved
        # had happened. A clean shutdown checkpoints the WAL; reading after
        # it is reading what a customer's next launch would read.
        stop(proc)
        proc = None

        after = count_docs(store)
        lost = {d: (before[d], after.get(d, 0)) for d in before
                if after.get(d, 0) < before[d]}
        check("no configuration was lost by the upgrade", not lost,
              "shrunk: %s" % json.dumps(lost))

        # This release's own migration.
        con = sqlite3.connect("file:%s?mode=ro" % store.replace("\\", "/"), uri=True)
        lan_rows = con.execute(
            "SELECT scope_key FROM config_documents_scoped "
            "WHERE domain='lan_sharing'").fetchall()
        con.close()
        check("the LAN settings moved into this edge's own scope",
              len(lan_rows) >= 1,
              "none migrated - Remote Access would still read the shared table")
        if lan_rows:
            check("  under exactly one scope, not several",
                  len(set(r[0] for r in lan_rows)) == 1,
                  str([r[0] for r in lan_rows]))
            check("  and under a scope that names the customer",
                  all(str(r[0]).split("|")[1] not in ("", "-") for r in lan_rows),
                  "a '-' scope means the identity was not resolved: %s"
                  % str([r[0] for r in lan_rows]))

        # A migration that runs twice is a migration that can undo itself.
        port_free()
        proc = boot(home, "second boot")
        up2 = wait_up()
        check("it boots again after the migration", up2)
        if up2:
            stop(proc)
            proc = None
            con = sqlite3.connect("file:%s?mode=ro" % store.replace("\\", "/"), uri=True)
            again = con.execute(
                "SELECT COUNT(DISTINCT scope_key) AS n FROM config_documents_scoped "
                "WHERE domain='lan_sharing'").fetchone()
            con.close()
            check("  the migration did not run a second time",
                  int(again[0] or 0) == len(set(r[0] for r in lan_rows)) if lan_rows else True,
                  "scopes now: %s" % again[0])
            final = count_docs(store)
            lost2 = {d: (before[d], final.get(d, 0)) for d in before
                     if final.get(d, 0) < before[d]}
            check("  and still nothing is lost", not lost2, json.dumps(lost2))
    finally:
        if proc is not None:
            stop(proc)
        shutil.rmtree(home, ignore_errors=True)
        print("  cleaned up the throwaway copy")

    print()
    print("  %d passed, %d failed, %d skipped" % (len(PASS), len(FAIL), len(SKIP)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
