# -*- coding: utf-8 -*-
"""The SHIPPED binary must not let a config save invalidate a password.

2026-10-30. test_password_not_clobbered.py proves the rule against the
source. This proves it against the artifact the customer installs, because
"the source is right" has never been evidence of what shipped - the cloud
bundle, the exe branding and the dashboard-layout test all taught that.

It boots the packaged backend on a THROWAWAY copy of a workspace, signs in
on loopback, saves a users_access document carrying a different password
hash for an existing user, and checks that the stored credential survives.

    python scripts/test_packaged_password_safety.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXE = os.path.join(ROOT, "desktop", "dist", "win-unpacked", "resources",
                   "backend", "trustnode-service.exe")
PORT = "8094"
BASE = "http://127.0.0.1:%s" % PORT

PASS, FAIL, SKIP = [], [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def skip(name, why):
    SKIP.append(name)
    print("  %-62s SKIP  - %s" % (name, why))


def post(path, body, token="", method="POST"):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method=method,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def admin_hash(db):
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        row = con.execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()
        return str(row[0]) if row else ""
    finally:
        con.close()


def main() -> int:
    print("packaged binary: a save cannot invalidate a password")
    if not os.path.isfile(EXE):
        skip("the packaged backend exists", "no build at %s" % EXE)
        print("\n  %d passed, %d failed, %d skipped" % (len(PASS), len(FAIL), len(SKIP)))
        return 0

    src = os.path.expanduser(os.path.join("~", ".trustnode_edge"))
    if not os.path.isdir(src):
        skip("a workspace to copy", "none at %s" % src)
        print("\n  %d passed, %d failed, %d skipped" % (len(PASS), len(FAIL), len(SKIP)))
        return 0

    home = tempfile.mkdtemp(prefix="tn_pwsafe_")
    dst = os.path.join(home, ".trustnode_edge")
    # -wal/-shm come and go while a live backend checkpoints; copying one
    # that vanished mid-copy aborted the whole test (2026-11-02).
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.log", "logs", "*-wal", "*-shm"))
    auth_db = os.path.join(dst, "data", "trustnode_auth.db")

    env = dict(os.environ)
    for k in ("TRUSTNODE_DATA_DIR", "TRUSTNODE_APP_STORE_PATH"):
        env.pop(k, None)
    env.update({"USERPROFILE": home, "HOME": home, "TRUSTNODE_PORT": PORT,
                "TRUSTNODE_HOST": "127.0.0.1", "TRUSTNODE_SKIP_DOTENV": "1",
                # 2026-11-02: this used to sign in as admin/admin and PASSED -
                # not through break-glass (the copied workspace has a real
                # `admin`, which rightly shadows it) but through the seeded
                # cp_users (default, admin) row that opened every customer
                # edge. That hole is closed, so the test owns its own
                # break-glass name: no account of that name exists, which is
                # the one case break-glass is for.
                "TRUSTNODE_MASTER_ADMIN_USER": "zz-gate-master",
                "TRUSTNODE_MASTER_ADMIN_PASSWORD": "gate-" + os.urandom(6).hex()})

    proc = subprocess.Popen([EXE], env=env, cwd=os.path.dirname(EXE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        up = False
        for _ in range(180):
            time.sleep(1)
            try:
                if urllib.request.urlopen(BASE + "/api/health", timeout=3).status == 200:
                    up = True
                    break
            except Exception:
                pass
        check("the packaged backend boots", up)
        if not up:
            return 1

        before = admin_hash(auth_db)
        check("the copy has a stored admin credential", bool(before))

        # Break-glass on loopback, under the test's own name (see env above).
        st, body = post("/api/auth/login", {"username": env["TRUSTNODE_MASTER_ADMIN_USER"],
                                            "password": env["TRUSTNODE_MASTER_ADMIN_PASSWORD"]})
        token = str((body or {}).get("token") or "")
        check("an admin session can be obtained locally", st == 200 and bool(token),
              "status=%s" % st)
        if not token:
            return 1

        # THE TEST: save users_access carrying a DIFFERENT hash for admin.
        foreign = ("pbkdf2_sha256$120000$ffffffffffffffffffffffffffffffff$"
                   + "e" * 64)
        st, body = post("/api/app-store/domain", {
            "domain": "users_access",
            "actor": "packaged-safety-test",
            "payload": {"users": [{"username": "admin", "password": foreign,
                                   "role": "admin", "status": "active"}]},
            "allow_destructive": True,
        }, token, method="PUT")
        # The save must really happen, or "the password survived" passes for
        # the wrong reason. 2026-10-30: the first run of this file POSTed to a
        # PUT route, got 405, and reported the credential safe - a test that
        # could not fail, which is worse than no test at all.
        check("the save actually reached the endpoint", st == 200,
              "status=%s %s" % (st, json.dumps(body)[:110]))

        time.sleep(2)
    finally:
        try:
            proc.terminate(); proc.wait(timeout=30)
        except Exception:
            proc.kill()

    after = admin_hash(auth_db)
    check("the stored password survived the save", after == before,
          "the shipped build still lets a config save lock the operator out")
    check("  and it is not the hash the document carried",
          after != ("pbkdf2_sha256$120000$ffffffffffffffffffffffffffffffff$" + "e" * 64))

    shutil.rmtree(home, ignore_errors=True)

    print()
    print("  %d passed, %d failed, %d skipped" % (len(PASS), len(FAIL), len(SKIP)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
