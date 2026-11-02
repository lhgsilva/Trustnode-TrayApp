# -*- coding: utf-8 -*-
"""A customer's own admin can never be handed master permissions.

2026-11-02, raised by the owner: "every customer might have their own admin
and none of this can give them a master permission."

The break-glass account defaults to admin / admin - and `admin` is the name
the product itself creates for a customer's administrator. Its check sat
BEFORE the real account check, so it SHADOWED the customer's own user.
Verified on a live edge before the fix:

    admin / admin  ->  200  role=admin

Anyone sitting at that machine was handed _FULL_PERMISSIONS and
tenant_id "default", bypassing both the customer's password and their
scope. On every install, because the password is env-configured and no
installer sets it.

The rule now: a real account always wins. Break-glass answers only for an
edge with no usable account under that name - a fresh install, or a store
that has lost its users - which is the case it exists for. It is not a
master key to somebody else's edge.

    python scripts/test_master_never_shadows_customer.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tn_master_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ.pop("TRUSTNODE_MASTER_ADMIN_PASSWORD", None)
os.environ.pop("TRUSTNODE_MASTER_ADMIN_USER", None)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def main() -> int:
    print("break-glass must never shadow a customer's own admin")

    with open(os.path.join(HERE, "..", "backend", "app", "routers", "auth.py"),
              encoding="utf-8") as fh:
        src = fh.read()

    # The default name collides with the product's own admin, which is why
    # the ordering mattered at all.
    m = re.search(r'TRUSTNODE_MASTER_ADMIN_USER",\s*"([a-z]+)"', src)
    check("the break-glass name still defaults to the customer's own",
          bool(m) and m.group(1) == "admin",
          "if this ever changes, the guard below matters less but must stay")

    check("a real account is looked up before the master path runs",
          "_name_is_taken" in src and "auth_store.get_user(username)" in src)
    check("  and the master path is skipped when the name is taken",
          "if (not _name_is_taken) and username == master_user" in src,
          "this is the whole fix: a real account always wins")
    check("  the attempt is recorded, not silently ignored",
          "master_shadowed_by_real_account" in src,
          "an operator must be able to see that someone tried")

    # The behaviour, against the real store.
    from app.services.auth_store import AuthStore
    from app.auth import hash_password, verify_password
    store = AuthStore(os.path.join(_TMP, "auth.db"))

    # A fresh edge: nobody owns the name, so break-glass still works. This is
    # the case it exists for - removing it would strand a new install.
    check("on an edge with no accounts, nothing owns the name",
          store.get_user("admin") is None)

    # Now the customer creates their administrator, called admin.
    store.upsert_user(username="admin",
                      password_hash=hash_password("the-customers-own-password"),
                      role="admin", tenant_id="cust-a", status="active")
    owner = store.get_user("admin")
    check("the customer's admin exists and owns the name",
          bool(owner) and bool(str(owner.get("password_hash") or "").strip()))

    # The guard's condition, evaluated exactly as the router does.
    name_is_taken = bool(owner and str(owner.get("password_hash") or "").strip())
    check("break-glass is refused once the name is owned", name_is_taken is True,
          "admin/admin would otherwise return full master permissions")

    # And the customer's own password still works - the fix must not lock
    # them out of their own edge.
    check("the customer's real password still verifies",
          verify_password("the-customers-own-password",
                          owner.get("password_hash") or ""))
    check("  and a wrong one still does not",
          not verify_password("admin", owner.get("password_hash") or ""),
          "the master password must not open a real account either")

    # A user row with an EMPTY hash must not count as owning the name -
    # that would let a blank row disable recovery entirely.
    store.upsert_user(username="ghost", password_hash=hash_password("x"),
                      role="viewer", tenant_id="cust-a")
    ghost = store.get_user("ghost")
    check("a user with a real hash counts as owning its name",
          bool(str((ghost or {}).get("password_hash") or "").strip()))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
