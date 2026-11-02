# -*- coding: utf-8 -*-
"""Put back the password hash this edge used to accept.

2026-10-30. A users_access save mirrored a foreign hash over the AuthStore
row, so the password that signed in at 19:47:02 stopped being accepted at
19:53:44. See scripts/test_password_not_clobbered.py for the mechanism and
the fix that stops it happening again.

This does NOT set a new password and never asks for one. It copies back the
hash this machine already holds in its own app-store documents - the one the
operator's existing password matches. The current hash is printed and backed
up first, so the step is reversible.

    python scripts/restore_admin_password.py --user admin            # show
    python scripts/restore_admin_password.py --user admin --apply    # do it

Nothing is written without --apply.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from collections import Counter

DATA = os.path.expanduser(os.path.join("~", ".trustnode_edge", "data"))


def fp(h):
    if not h:
        return "<none>"
    return "%s… sha=%s" % (str(h)[:24], hashlib.sha256(str(h).encode()).hexdigest()[:10])


def users_of(payload):
    try:
        d = json.loads(payload) if isinstance(payload, str) else payload
    except Exception:
        return {}
    users = d.get("users") if isinstance(d, dict) else d
    out = {}
    for u in (users or []):
        if isinstance(u, dict) and str(u.get("username") or "").strip():
            out[str(u["username"]).strip()] = u
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="admin")
    ap.add_argument("--data-dir", default=DATA)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    app_db = os.path.join(args.data_dir, "trustnode_app_store.db")
    auth_db = os.path.join(args.data_dir, "trustnode_auth.db")
    for f in (app_db, auth_db):
        if not os.path.isfile(f):
            print("not found: %s" % f)
            return 1

    ac = sqlite3.connect(auth_db)
    ac.row_factory = sqlite3.Row
    row = ac.execute("SELECT password_hash, updated_utc FROM users WHERE username = ?",
                     (args.user,)).fetchone()
    if not row:
        print("no such user in the AuthStore: %s" % args.user)
        return 1
    current = str(row["password_hash"] or "")
    print("AuthStore (what the login checks)")
    print("    %s   updated %s" % (fp(current), row["updated_utc"]))

    pc = sqlite3.connect("file:%s?mode=ro" % app_db.replace("\\", "/"), uri=True)
    pc.row_factory = sqlite3.Row
    found = []
    for r in pc.execute("SELECT 'unscoped' AS k, payload_json, updated_utc "
                        "FROM config_documents WHERE domain='users_access'"):
        u = users_of(r["payload_json"]).get(args.user)
        if u and u.get("password"):
            found.append((r["k"], str(u["password"]), r["updated_utc"]))
    for r in pc.execute("SELECT scope_key AS k, payload_json, updated_utc "
                        "FROM config_documents_scoped WHERE domain='users_access'"):
        u = users_of(r["payload_json"]).get(args.user)
        if u and u.get("password"):
            found.append((r["k"], str(u["password"]), r["updated_utc"]))
    pc.close()

    print("\nhashes this machine holds for %r" % args.user)
    for k, h, when in found:
        mark = "  <- same as AuthStore" if h == current else ""
        print("    %-50s %s   %s%s" % (k[:50], fp(h), when, mark))

    others = [h for _, h, _ in found if h != current]
    if not others:
        print("\nEvery document already agrees with the AuthStore. Nothing to "
              "restore - the password in use is the only one this machine has.")
        return 0

    # The hash the most documents agree on is the one this edge used before a
    # single document was rewritten. Majority, not recency: the newest write
    # is exactly the one that caused the problem.
    best, votes = Counter(others).most_common(1)[0]
    print("\ncandidate: %s" % fp(best))
    print("    agreed on by %d of %d document(s)" % (votes, len(found)))

    if not args.apply:
        print("\nNothing written. Re-run with --apply to restore it.")
        return 0

    backup = auth_db + ".before-password-restore"
    shutil.copy2(auth_db, backup)
    ac.execute("UPDATE users SET password_hash = ? WHERE username = ?", (best, args.user))
    ac.commit()
    # A stale lockout would make a correct password look wrong.
    try:
        ac.execute("DELETE FROM login_lockouts WHERE username = ?", (args.user,))
        ac.commit()
    except Exception:
        pass
    ac.close()
    print("\nrestored. backup of the auth database: %s" % backup)
    print("Sign in with the password you used before. If it is still refused, "
          "the old hash is in that backup and nothing else was touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
