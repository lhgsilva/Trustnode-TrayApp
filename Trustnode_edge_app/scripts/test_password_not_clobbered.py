# -*- coding: utf-8 -*-
"""A config save must never invalidate a password that works.

2026-10-30, reported as "the login is not working anymore" on a LAN client.
The URL was fine, the page loaded, the endpoint answered - the password was
simply rejected. Measured on the operator's own machine:

    19:47:02  admin signs in over the LAN                       OK
    19:47:06  this edge's users_access document is saved
    19:53:44  every sign-in from then on     invalid_credentials

Four users_access documents existed in that one file, all with a user called
`admin`:

    unscoped                                     pbkdf2…bb1b2d96…
    tenant|-|edge-01                             pbkdf2…bb1b2d96…
    tenant|cust-e5916328|edge-74d903ffcd         pbkdf2…bb1b2d96…
    tenant|cust-11e164d2|edge-7e86b73f66         pbkdf2…803afc95…   <- saved 19:47:06

The AuthStore - the store the login actually checks - ended up holding
803afc95. Two faults combined:

  1. every users_access save called migrate_from_app_store_payload(), which
     wrote the document's hash straight over the AuthStore row. A document
     carrying a stale or foreign hash therefore silently replaced a working
     credential. Nothing was logged.

  2. the boot migration selected EVERY scoped users_access document in the
     file and fed them all in - other edges, other customers - so whichever
     was consumed last decided the password. That is CLAUDE.md rule 1b: a
     cross-scope read, and a data-protection failure, not a display bug.

The rule now: the mirror may create users and update role / permissions /
status, but it changes a password only when the save really changed one -
which the caller proves by passing what the document held beforehand.

    python scripts/test_password_not_clobbered.py
"""
from __future__ import annotations

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tn_pwd_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-64s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def main() -> int:
    print("a save must not invalidate a working password")

    from app.services.auth_store import AuthStore
    from app.auth import hash_password, verify_password

    store = AuthStore(os.path.join(_TMP, "auth.db"))

    WORKS = hash_password("the-password-the-operator-knows")
    FOREIGN = hash_password("some-other-edges-password")

    def doc(h, name="admin", role="admin"):
        return {"users": [{"username": name, "password": h, "role": role,
                           "tenant_id": "t1", "status": "active"}]}

    # Seed the user the way a first boot would, then confirm the password
    # works - otherwise the rest of this proves nothing.
    store.migrate_from_app_store_payload(doc(WORKS))
    u = store.get_user("admin")
    check("the user is seeded and the password verifies",
          bool(u) and verify_password("the-password-the-operator-knows",
                                      u.get("password_hash") or ""))

    # THE BUG: a later save whose document carries a different hash, with
    # nothing in that save having changed the password.
    store.migrate_from_app_store_payload(
        doc(FOREIGN), actor="ui_save:test", prior_hashes={"admin": FOREIGN})
    u = store.get_user("admin")
    check("a save that did not change the password leaves it alone",
          verify_password("the-password-the-operator-knows",
                          u.get("password_hash") or ""),
          "the operator is locked out of their own edge")

    # With no prior_hashes the caller cannot prove a change, so the stored
    # credential stands. This is the path every old caller took.
    store.migrate_from_app_store_payload(doc(FOREIGN), actor="legacy_caller")
    u = store.get_user("admin")
    check("  and a caller that cannot prove a change cannot force one",
          verify_password("the-password-the-operator-knows",
                          u.get("password_hash") or ""))

    # 2026-10-30: this first asserted that a document carrying a different
    # hash counted as a password change. That is EXACTLY the write that
    # locked the operator out - the Users screen builds this document from
    # the control-plane user list, so a different hash means "the cloud's
    # copy of this username", not "the operator typed a new password".
    # A config save never changes an existing account's password now;
    # passwords go through the password endpoints.
    NEW = hash_password("a-deliberately-new-password")
    store.migrate_from_app_store_payload(
        doc(NEW), actor="ui_save:test", prior_hashes={"admin": WORKS})
    u = store.get_user("admin")
    check("a config save cannot change an existing password at all",
          verify_password("the-password-the-operator-knows",
                          u.get("password_hash") or ""),
          "a mirrored cloud hash can still lock the operator out")

    # The supported path must still work, or we have taken something away.
    store.set_user_password("admin", NEW)
    u = store.get_user("admin")
    check("the password endpoints still set a password",
          verify_password("a-deliberately-new-password", u.get("password_hash") or ""))
    check("  and the old password stops working",
          not verify_password("the-password-the-operator-knows",
                              u.get("password_hash") or ""))

    # A brand-new user must still be created and able to log in at once -
    # that is what the mirror exists for.
    OTHER = hash_password("brand-new-user-password")
    store.migrate_from_app_store_payload(
        doc(OTHER, name="freshuser", role="viewer"),
        actor="ui_save:test", prior_hashes={})
    nu = store.get_user("freshuser")
    check("a brand-new user is still created by the mirror",
          bool(nu) and verify_password("brand-new-user-password",
                                       (nu or {}).get("password_hash") or ""))

    # Role and permission edits must still land on an existing user.
    store.migrate_from_app_store_payload(
        {"users": [{"username": "admin", "password": FOREIGN, "role": "operator",
                    "tenant_id": "t1", "status": "active"}]},
        actor="ui_save:test", prior_hashes={"admin": FOREIGN})
    u = store.get_user("admin")
    check("role changes still apply to an existing user",
          str(u.get("role")) == "operator", str(u.get("role")))
    check("  without disturbing the password",
          verify_password("a-deliberately-new-password", u.get("password_hash") or ""))

    # The operator's exact shape: several documents in one file, each with
    # its own `admin`, consumed in one call. The last one used to win.
    store2 = AuthStore(os.path.join(_TMP, "auth2.db"))
    store2.migrate_from_app_store_payload(doc(WORKS))
    store2.migrate_from_app_store_payload(
        doc(WORKS), scoped_payloads=[doc(FOREIGN), doc(FOREIGN)],
        actor="boot", prior_hashes={"admin": WORKS})
    u = store2.get_user("admin")
    check("another document's admin cannot take over this one's password",
          verify_password("the-password-the-operator-knows",
                          u.get("password_hash") or ""),
          "this is the operator's lockout, reproduced")

    # And it must be visible when it happens - a silent guard taught nobody
    # anything for six hours.
    res = store2.migrate_from_app_store_payload(
        doc(FOREIGN), actor="ui_save:test", prior_hashes={"admin": FOREIGN})
    check("the mirror reports when it kept a password",
          int(res.get("password_kept") or 0) >= 1, str(res))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
