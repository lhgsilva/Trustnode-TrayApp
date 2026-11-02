# -*- coding: utf-8 -*-
"""The LAN settings belong to THIS edge, and '-' is not an identity.

2026-10-30, from the operator's own store while a remote session died.
Two faults, measured, both of them CLAUDE.md rule 1b:

  (b) Remote Access read and wrote `config_documents` - the legacy UNSCOPED
      table. One SQLite file routinely holds several edges; that store held
      three (edge-01, edge-74d903ffcd, edge-7e86b73f66). So "the LAN
      settings" were whichever edge wrote them last. The live install's
      lan_sharing_enabled / lan_https_only / lan_http_enabled existed ONLY
      there - no scoped document carried them at all.

  (c) The same edge held personal documents under two keys:

        tenant-cust-11e164d2|cust-11e164d2|edge-7e86b73f66|admin   (23 keys)
        tenant-cust-11e164d2|-|edge-7e86b73f66|admin               (18 keys)

      The placeholder one was written LAST, at 19:46:14, during a LAN login -
      _build_scope_key degrades to `-` when the bootstrap identity read comes
      back without the customer. canonical_scope_key only adopted a sibling
      when its own key was EMPTY, so once both held rows the split was
      permanent and fed itself.

The rule asserted here: `-` means "we did not know yet", never "no customer".
A document filed under a known customer for this edge always wins. A key that
names a customer keeps the strict rule and never adopts another's.

Runs entirely in a temporary workspace - it never touches the live store.

    python scripts/test_lan_settings_scope.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tn_lanscope_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if detail else ""))


def main() -> int:
    print("LAN settings scope")

    # app.state is what the router imports; importing it migrates whatever
    # database the environment points at, which is why the env above is set
    # before anything else happens.
    from app.state import app_store
    from app.routers import lan_sharing as lan

    db = os.environ["TRUSTNODE_APP_STORE_PATH"]

    def put(scope_key, domain, payload, when):
        with sqlite3.connect(db) as c:
            c.execute(
                "INSERT OR REPLACE INTO config_documents_scoped "
                "(scope_key, domain, payload_json, version, updated_utc) "
                "VALUES (?,?,?,1,?)",
                (scope_key, domain, json.dumps(payload), when))

    def clear_cache():
        app_store._canonical_scope_cache = {}          # noqa: SLF001

    T = "tenant-a"

    # ---------------------------------------------------------------- (c)
    # The operator's exact shape: both keys hold documents, placeholder newer.
    put("%s|cust-a|edge-1|admin" % T, "app_settings", {"theme": "dark"},
        "2026-10-30 19:34:16")
    put("%s|-|edge-1|admin" % T, "app_settings", {"theme": "light"},
        "2026-10-30 19:46:14")
    clear_cache()

    got = app_store.canonical_scope_key("%s|-|edge-1|admin" % T)
    check("a '-' key defers to the same edge's named customer",
          got == "%s|cust-a|edge-1|admin" % T, got)

    clear_cache()
    got = app_store.canonical_scope_key("%s|cust-a|edge-1|admin" % T)
    check("  a named key keeps its own document", got == "%s|cust-a|edge-1|admin" % T, got)

    # The strict rule survives: a known customer never adopts another's.
    put("%s|cust-b|edge-2|admin" % T, "app_settings", {"theme": "x"},
        "2026-10-30 10:00:00")
    clear_cache()
    got = app_store.canonical_scope_key("%s|cust-c|edge-2|admin" % T)
    check("  a named key never adopts a different customer's document",
          got != "%s|cust-b|edge-2|admin" % T, got)

    # An edge that genuinely has no customer anywhere is untouched - this is
    # every pre-activation install, so breaking it would break commissioning.
    put("%s|-|edge-3" % T, "gateway_configurations", [{"id": "g1"}],
        "2026-10-30 11:00:00")
    clear_cache()
    got = app_store.canonical_scope_key("%s|-|edge-3" % T)
    check("  an edge with no customer at all is unaffected",
          got == "%s|-|edge-3" % T, got)

    # Shape is still respected: a site key must not adopt a per-user document.
    clear_cache()
    got = app_store.canonical_scope_key("%s|-|edge-1" % T)
    check("  a site-level key does not adopt a per-user document",
          got.count("|") == 2, got)

    # ---------------------------------------------------------------- (b)
    print()
    # Another edge's LAN settings sitting in the shared unscoped table.
    app_store.upsert_domain("app_settings", {
        "edge_id": "edge-1", "customer_id": "cust-a", "tenant_id": T,
        "edge_profile": {"edge_id": "edge-1", "linked_customer_id": "cust-a"},
        "lan_sharing_enabled": True, "lan_https_only": True,
        "lan_http_enabled": False, "lan_bind_host": "0.0.0.0",
        "theme": "dark",
    }, actor="test")
    clear_cache()

    s = lan._load_app_settings()                       # noqa: SLF001
    check("an existing install's LAN settings are carried across",
          s.get("lan_sharing_enabled") is True and s.get("lan_https_only") is True,
          json.dumps(s))
    check("  and nothing else travels with them",
          set(s) <= set(lan._LAN_KEYS), json.dumps(sorted(s)))  # noqa: SLF001

    # After the seed they must live in THIS edge's scope, not the shared table.
    with sqlite3.connect(db) as c:
        rows = c.execute("SELECT scope_key FROM config_documents_scoped "
                         "WHERE domain='lan_sharing'").fetchall()
    check("  they are stored against this edge",
          len(rows) == 1 and "edge-1" in rows[0][0], str(rows))

    # The save path writes scoped, and merges rather than replacing.
    s2 = dict(s)
    s2["lan_sharing_enabled"] = False
    lan._save_app_settings(s2)                         # noqa: SLF001
    again = lan._load_app_settings()                   # noqa: SLF001
    check("turning sharing off keeps the other settings",
          again.get("lan_sharing_enabled") is False
          and again.get("lan_https_only") is True
          and again.get("lan_bind_host") == "0.0.0.0", json.dumps(again))

    # THE bug: a short load must not blank the rest. This is what the
    # callers do - load, set one key, save.
    lan._save_app_settings({"lan_sharing_enabled": True})   # noqa: SLF001
    after = lan._load_app_settings()                   # noqa: SLF001
    check("a save carrying one key does not drop the other three",
          after.get("lan_sharing_enabled") is True
          and after.get("lan_https_only") is True
          and after.get("lan_bind_host") == "0.0.0.0", json.dumps(after))

    # And a second edge in the same file must not see edge-1's answer.
    with sqlite3.connect(db) as c:
        c.execute("UPDATE config_documents SET payload_json = ? WHERE domain='app_settings'",
                  (json.dumps({
                      "edge_id": "edge-9", "customer_id": "cust-z", "tenant_id": T,
                      "edge_profile": {"edge_id": "edge-9",
                                       "linked_customer_id": "cust-z"},
                  }),))
    clear_cache()
    try:
        app_store._bootstrap_cache = None              # noqa: SLF001
    except Exception:
        pass
    other = lan._load_app_settings()                   # noqa: SLF001
    check("a different edge in the same file gets its OWN answer",
          other.get("lan_https_only") is not True or not other,
          "edge-9 saw edge-1's settings: %s" % json.dumps(other))

    # lan_socket must agree with what we store, or the page and the
    # listeners disagree - which is how this looked on screen.
    from app.services import lan_socket
    check("lan_socket reads the same four keys",
          set(lan._LAN_KEYS) == {                       # noqa: SLF001
              "lan_sharing_enabled", "lan_http_enabled",
              "lan_https_only", "lan_bind_host"},
          "if lan_socket learns a fifth, add it here too")
    check("  an absent flag still means OFF, not ON",
          lan_socket.sharing_enabled({}) is False)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
