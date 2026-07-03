"""Inject a test trustnode_intelligence license into the local edge.

For Phase A smoke testing on the dev machine. NEVER run this against a
production tenant — it overwrites edge_license_snapshot.

Usage:
    python inject_license.py [--endpoint URL] [--model NAME] [--token TOKEN]

Defaults aim the Edge at the local Ollama install:
    endpoint = http://127.0.0.1:11434
    model    = qwen2.5:7b-instruct
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time


DB_PATHS = [
    # User-data fallback (older builds, dev mode)
    r"C:\Users\User\.trustnode_edge\data\trustnode_app_store.db",
    # Workspace detector default (current packaged build)
    r"C:\ProgramData\TrustNode\edge\trustnode_app_store.db",
]
TARGET_SCOPES = [
    "tenant-cust-e5916328|cust-e5916328|edge-74d903ffcd|admin-mari",
    "tenant-cust-e5916328|-|edge-74d903ffcd|admin-mari",
    "tenant-cust-e5916328|-|edge-01|admin-mari",
]


def _now_utc() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def build_license(endpoint: str, model: str, token: str) -> dict:
    """Shape matches license_inspect._evaluate()'s expectations:
        snapshot['license']['modules']            -> ["trustnode_intelligence", ...]
        snapshot['license']['module_configs']     -> {"trustnode_intelligence": {...}}
        snapshot['license']['package_key']        -> "operations_plus_ai"
    """
    return {
        "license_id": "test-license-mari-smoke",
        "customer_id": "cust-e5916328",
        "issued_utc": _now_utc(),
        "license": {
            "package_key": "operations_plus_ai",
            "modules": [
                "batch_management",
                "reporting",
                "trustnode_intelligence",
            ],
            "limits": {
                "max_tags": 0,           # 0 = unlimited
                "max_gateways_per_edge": 0,
                "max_admin_users": 0,
            },
            "module_configs": {
                "trustnode_intelligence": {
                    "endpoint_url": endpoint,
                    "model": model,
                    "auth_token": token,
                    "rate_limits": {
                        "queries_per_day": 500,
                        "max_tokens_per_query": 2048,
                    },
                    "features": {
                        "insights": True,
                        "email_schedule": True,
                    },
                    "allowed_tools": ["read_only"],
                },
            },
        },
    }


def inject(endpoint: str, model: str, token: str) -> None:
    snapshot = build_license(endpoint, model, token)
    # license_inspect._evaluate() reads `s.get("license")` flat off the
    # app_settings doc, NOT from `edge_license_snapshot.license`. We
    # write to BOTH shapes so whichever the live code uses, it works.
    snapshot["_flat_license_marker"] = True
    for db_path in DB_PATHS:
        import os
        if not os.path.exists(db_path):
            print(f"  SKIP (no DB at): {db_path}")
            continue
        print(f"  WRITING to: {db_path}")
        _inject_one(db_path, snapshot)
    print()
    print(f"License injected with endpoint={endpoint!r}, model={model!r}")
    print("Restart the Edge for the license_inspect cache to refresh (~30s TTL otherwise).")


def _inject_one(DB_PATH: str, snapshot: dict) -> None:
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        for scope in TARGET_SCOPES:
            row = con.execute(
                "SELECT payload_json, version FROM config_documents_scoped "
                "WHERE scope_key=? AND domain='app_settings'",
                (scope,),
            ).fetchone()
            if row:
                try:
                    data = json.loads(row[0])
                except Exception:
                    data = {}
                version = (row[1] or 0) + 1
                # Write license under BOTH shapes — see comment in inject().
                data["edge_license_snapshot"] = snapshot
                data["license"] = snapshot["license"]
                con.execute(
                    "UPDATE config_documents_scoped SET payload_json=?, version=?, updated_utc=? "
                    "WHERE scope_key=? AND domain='app_settings'",
                    (json.dumps(data, separators=(",", ":")), version, _now_utc(), scope),
                )
                print(f"  UPDATED scope={scope} (v={version})")
            else:
                data = {
                    "edge_license_snapshot": snapshot,
                    "license": snapshot["license"],
                }
                con.execute(
                    "INSERT INTO config_documents_scoped(scope_key, domain, payload_json, version, updated_utc) "
                    "VALUES (?,?,?,?,?)",
                    (scope, "app_settings", json.dumps(data, separators=(",", ":")), 1, _now_utc()),
                )
                print(f"  INSERTED scope={scope}")
        # Also write to the unscoped doc so license_inspect's fallback
        # path finds it regardless of which scope it resolves.
        row = con.execute(
            "SELECT payload_json FROM config_documents WHERE domain='app_settings'"
        ).fetchone()
        if row:
            try:
                data = json.loads(row[0])
            except Exception:
                data = {}
            data["edge_license_snapshot"] = snapshot
            data["license"] = snapshot["license"]
            con.execute(
                "UPDATE config_documents SET payload_json=? WHERE domain='app_settings'",
                (json.dumps(data, separators=(",", ":")),),
            )
            print("  UPDATED unscoped app_settings")
        else:
            con.execute(
                "INSERT INTO config_documents(domain, payload_json) VALUES (?,?)",
                ("app_settings", json.dumps({
                    "edge_license_snapshot": snapshot,
                    "license": snapshot["license"],
                }, separators=(",", ":"))),
            )
            print("  INSERTED unscoped app_settings")
        con.commit()
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://127.0.0.1:11434")
    ap.add_argument("--model", default="qwen2.5:7b-instruct")
    ap.add_argument("--token", default="dev-token-mari-smoke")
    args = ap.parse_args()
    inject(args.endpoint, args.model, args.token)


if __name__ == "__main__":
    main()
