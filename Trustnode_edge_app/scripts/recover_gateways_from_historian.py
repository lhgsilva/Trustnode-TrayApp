"""Recover gateway_configurations from Supabase historian metadata.

Reality the script faces: at some point a `cloud_pull` reset the local
`gateway_configurations` config to a single placeholder gateway. The
real gateway configs (PLC, SIEMENS GW, etc.) only survive as the
gateway_id / gateway_name / tag_name combinations recorded against the
samples they wrote to Supabase historian.

What this script does:
  1. Reads the running edge's local SQLite to find the per-edge scope
     key (`tenant|customer|edge`) and the current gateway list.
  2. Queries Supabase historian for distinct (gateway_id, gateway_name,
     tag_name) tuples that have written in the last N days.
  3. Reconstructs a gateway_configurations entry for every gateway that
     isn't already in the local scope. Tag list is the union of tag
     names seen for that gateway. Type is inferred (OPC UA tags look
     like `ns=...;s=...`, everything else assumed allen_bradley).
  4. Writes the merged list into the per-edge scope's
     `gateway_configurations`. Existing gateway entries are preserved
     untouched — the script only ADDS gateways it found in historian
     that aren't already configured.
  5. Skips power meters (handled by power_management_config separately)
     and synthetic test gateways (gw-smoke-synth, gw-latency-probe).

Safe to re-run; idempotent.

Run from Trustnode_edge_app/:
    python scripts/recover_gateways_from_historian.py
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


SKIP_PREFIXES = ("gw-smoke", "gw-latency", "gw-test", "power_meter")
SKIP_EXACT = {"default"}  # historian sometimes records this as a placeholder
LOOKBACK_DAYS = 30


def load_env(p: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def resolve_local_db() -> Path:
    base = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
    if base:
        return Path(base) / "trustnode_app_store.db"
    return Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"


def derive_edge_scope(conn: sqlite3.Connection) -> str | None:
    """Read app_settings.edge_profile to learn the current edge id and
    build the `tenant|customer|edge` scope key the running backend uses
    for shared domains.

    Prefers the **global** `config_documents` app_settings row — that's
    what the running backend treats as the canonical edge identity.
    Multiple per-user scopes can carry stale or test edge_ids that don't
    reflect the running edge."""
    row = conn.execute(
        "SELECT payload_json FROM config_documents WHERE domain='app_settings'"
    ).fetchone()
    if not row:
        # Last resort — any scoped admin row that has an edge_id.
        row = conn.execute(
            "SELECT payload_json FROM config_documents_scoped "
            "WHERE domain='app_settings' ORDER BY updated_utc DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        cfg = json.loads(row[0])
    except Exception:
        return None
    ep = cfg.get("edge_profile") if isinstance(cfg, dict) else {}
    ep = ep if isinstance(ep, dict) else {}
    edge_id = str(ep.get("edge_id") or "").strip().lower()
    if not edge_id:
        return None
    tenant_id = "default"
    customer_id = str(ep.get("linked_customer_id") or "").strip().lower() or "-"
    return f"{tenant_id}|{customer_id}|{edge_id}"


def infer_gateway_type(tag_names: list[str]) -> str:
    """OPC UA tags look like `ns=3;s="something"`. Modbus addresses are
    numeric. Everything else defaults to allen_bradley which matches
    the existing template's gateway_type."""
    for t in tag_names:
        if "ns=" in str(t) and "s=" in str(t):
            return "siemens_opcua"
    return "allen_bradley"


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    env = load_env(here / ".env")
    if not env.get("TRUSTNODE_CLOUD_DB_HOST"):
        print("TRUSTNODE_CLOUD_DB_HOST not set in .env", file=sys.stderr)
        return 2

    db_path = resolve_local_db()
    if not db_path.is_file():
        print(f"local DB not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row

    edge_scope = derive_edge_scope(conn)
    if not edge_scope:
        print("could not derive edge scope from app_settings", file=sys.stderr)
        return 2
    print(f"-- target shared-edge scope: {edge_scope!r} --")

    # Read current gateway list
    row = conn.execute(
        "SELECT payload_json, version FROM config_documents_scoped "
        "WHERE scope_key=? AND domain='gateway_configurations'",
        (edge_scope,),
    ).fetchone()
    existing = json.loads(row["payload_json"]) if row else []
    if not isinstance(existing, list):
        existing = []
    existing_ids = {str(g.get("id") or "") for g in existing if isinstance(g, dict)}
    print(f"   already configured: {sorted(existing_ids) or '∅'}")

    # Read database list for a fallback database_id
    dbrow = conn.execute(
        "SELECT payload_json FROM config_documents_scoped "
        "WHERE scope_key=? AND domain='database_configurations'",
        (edge_scope,),
    ).fetchone()
    db_list = json.loads(dbrow["payload_json"]) if dbrow else []
    db_list = db_list if isinstance(db_list, list) else []
    db_default = next((str(d.get("id") or "") for d in db_list
                       if isinstance(d, dict) and str(d.get("engine") or "") == "sqlite"),
                      "local-sqlite-default")

    # Query Supabase historian
    print(f"\n-- querying Supabase historian (last {LOOKBACK_DAYS}d) --")
    import psycopg
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    with psycopg.connect(
        host=env["TRUSTNODE_CLOUD_DB_HOST"],
        port=int(env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
        dbname=env["TRUSTNODE_CLOUD_DB_NAME"],
        user=env["TRUSTNODE_CLOUD_DB_USER"],
        password=env["TRUSTNODE_CLOUD_DB_PASSWORD"],
        sslmode="require",
        connect_timeout=15,
    ) as pg_conn:
        cur = pg_conn.cursor()
        cur.execute(
            """
            SELECT gateway_id, max(gateway_name), array_agg(DISTINCT tag_name)
              FROM public.historian_readings
             WHERE ts_utc > %s
               AND coalesce(gateway_id,'') <> ''
             GROUP BY gateway_id
             ORDER BY gateway_id
            """,
            (since,),
        )
        rows = cur.fetchall()

    discovered: list[dict] = []
    for gid, gname, tags in rows:
        gid_s = str(gid or "").strip()
        if not gid_s:
            continue
        if gid_s in SKIP_EXACT or any(gid_s.startswith(p) for p in SKIP_PREFIXES):
            continue
        if gid_s in existing_ids:
            continue
        tag_list = [str(t) for t in (tags or []) if str(t).strip()]
        gtype = infer_gateway_type(tag_list)
        # Reasonable defaults — the operator can edit IP / OPC URL on the
        # edge after recovery; the tags and id stay correct.
        entry = {
            "id": gid_s,
            "name": str(gname or gid_s),
            "gateway_type": gtype,
            "plc_ip": "" if gtype == "siemens_opcua" else "192.168.10.240",
            "opc_url": "opc.tcp://192.168.10.242:4840" if gtype == "siemens_opcua" else "",
            "interval_ms": 1000,
            "database_id": db_default,
            "device_id": "",
            "tags": sorted(tag_list),
        }
        discovered.append(entry)
        print(f"   + {gid_s!r:25s} type={gtype:18s} tags={len(tag_list):>2}  -> reconstruct")

    if not discovered:
        print("\nNothing to add. All historian gateways are already configured.")
        conn.close()
        return 0

    new_list = list(existing) + discovered
    payload = json.dumps(new_list, separators=(",", ":"))
    new_version = int(row["version"] if row else 0) + 1

    with conn:
        if row:
            conn.execute(
                "UPDATE config_documents_scoped SET payload_json=?, version=?, "
                "updated_utc=strftime('%Y-%m-%d %H:%M:%f','now') "
                "WHERE scope_key=? AND domain='gateway_configurations'",
                (payload, new_version, edge_scope),
            )
        else:
            conn.execute(
                "INSERT INTO config_documents_scoped(scope_key, domain, payload_json, version, updated_utc) "
                "VALUES (?, 'gateway_configurations', ?, ?, strftime('%Y-%m-%d %H:%M:%f','now'))",
                (edge_scope, payload, new_version),
            )

    print(f"\nWrote {len(new_list)} gateway(s) into {edge_scope!r} (v={new_version}).")
    print("Restart the edge backend so the new configs are loaded into plc_manager.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
