"""One-time backfill: add `plc_endpoint` to every widget in cloud
`dashboard_configurations` by looking up the gateway's PLC endpoint
(opc_url for Siemens, plc_ip otherwise) on the VPS edge.

The Lite app uses `plc_endpoint` to fall back from gateway_id matching
to PLC-endpoint matching, so charts keep animating when a different
gateway (same PLC) is started. New widgets get the snapshot at save
time (saveDashboardWidget); this script does the back-fill for widgets
saved before that change.

The gateway directory is fetched ONCE from the VPS edge SQLite at
/opt/trustnode-edge/data/trustnode_app_store.db (the only place where
gateway configs are authoritative; cloud config_documents has a stub).

Run from Trustnode_edge_app/:
    python scripts/backfill_widget_plc_endpoint.py        # dry-run
    python scripts/backfill_widget_plc_endpoint.py --apply  # write cloud
    python scripts/backfill_widget_plc_endpoint.py --apply --local  # also write local SQLite
"""
from __future__ import annotations
import argparse, io, json, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko, psycopg

HERE = Path(__file__).resolve().parent.parent
env = {}
for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s: continue
    k, v = s.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")


def gateway_endpoint(gateway: dict) -> str:
    """Replicate the App.jsx rule: opc_url for Siemens, plc_ip for everything else."""
    if not isinstance(gateway, dict):
        return ""
    if str(gateway.get("gateway_type") or "") == "siemens_opcua":
        return str(gateway.get("opc_url") or "")
    return str(gateway.get("plc_ip") or "")


def fetch_vps_gateways() -> dict[str, str]:
    """SSH to the VPS, read every gateway from the edge SQLite, return
    a dict of {gateway_id: plc_endpoint}.

    Also picks up scoped variants if config_documents_scoped exists on the
    VPS — older builds only had the unscoped table."""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
              username=env["VPS_USER"], password=env["VPS_PASSWORD"],
              timeout=15, allow_agent=False, look_for_keys=False)
    remote = r'''
import sqlite3, json, sys
db = "/opt/trustnode-edge/data/trustnode_app_store.db"
con = sqlite3.connect(db); con.row_factory = sqlite3.Row
out = {}   # gateway_id -> plc_endpoint
# Unscoped (legacy / default tenant)
for r in con.execute("SELECT payload_json FROM config_documents WHERE domain='gateway_configurations'"):
    raw = r[0]
    try: p = json.loads(raw) if isinstance(raw, str) else raw
    except Exception: continue
    if isinstance(p, list):
        for g in p:
            gid = str(g.get("id") or "")
            if not gid: continue
            ep = ((g.get("opc_url") or "") if g.get("gateway_type") == "siemens_opcua" else (g.get("plc_ip") or ""))
            ep = str(ep or "")
            if ep: out[gid] = ep
# Scoped (multi-tenant)
try:
    for r in con.execute("SELECT scope_key, payload_json FROM config_documents_scoped WHERE domain='gateway_configurations'"):
        try: p = json.loads(r[1]) if isinstance(r[1], str) else r[1]
        except Exception: continue
        if isinstance(p, list):
            for g in p:
                gid = str(g.get("id") or "")
                if not gid: continue
                ep = ((g.get("opc_url") or "") if g.get("gateway_type") == "siemens_opcua" else (g.get("plc_ip") or ""))
                ep = str(ep or "")
                if ep and gid not in out: out[gid] = ep
except sqlite3.OperationalError:
    pass
con.close()
print(json.dumps(out))
'''
    sftp = c.open_sftp()
    with sftp.file("/tmp/_fetch_gws.py", "w") as f: f.write(remote)
    sftp.close()
    _, out_io, err_io = c.exec_command("python3 /tmp/_fetch_gws.py", timeout=60)
    sout = out_io.read().decode("utf-8", "replace").strip()
    serr = err_io.read().decode("utf-8", "replace").strip()
    c.exec_command("rm -f /tmp/_fetch_gws.py")
    c.close()
    if serr:
        print(f"  vps stderr: {serr}")
    try:
        return json.loads(sout) if sout else {}
    except Exception as exc:
        print(f"  vps payload parse error: {exc} -- raw: {sout[:200]}")
        return {}


def cloud_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=env["TRUSTNODE_CLOUD_DB_HOST"],
        port=int(env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
        dbname=env.get("TRUSTNODE_CLOUD_DB_NAME") or "postgres",
        user=env["TRUSTNODE_CLOUD_DB_USER"],
        password=env["TRUSTNODE_CLOUD_DB_PASSWORD"],
        sslmode=env.get("TRUSTNODE_CLOUD_DB_SSLMODE") or "require",
        connect_timeout=15,
    )


def patch_widget(w: dict, gw_to_ep: dict[str, str]) -> int:
    """Add `plc_endpoint` to the widget (and any series_extra entries)
    when missing. Returns the number of fields populated."""
    changes = 0
    cfg = w.get("config") if isinstance(w.get("config"), dict) else None
    if cfg is None: return 0
    gid = str(cfg.get("gateway_id") or "")
    if gid and not cfg.get("plc_endpoint"):
        ep = gw_to_ep.get(gid)
        if ep:
            cfg["plc_endpoint"] = ep
            changes += 1
    extras = cfg.get("series_extra")
    if isinstance(extras, list):
        for s in extras:
            if not isinstance(s, dict): continue
            sgid = str(s.get("gateway_id") or "")
            if sgid and not s.get("plc_endpoint"):
                ep = gw_to_ep.get(sgid)
                if ep:
                    s["plc_endpoint"] = ep
                    changes += 1
    return changes


def local_sqlite_path() -> Path:
    """Match the backend's _resolve_db_path() default: ~/.trustnode_edge/data/trustnode_app_store.db."""
    return Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"


def backfill_local_sqlite(gw_to_ep: dict[str, str], apply: bool) -> None:
    """Walk every dashboard_configurations row in the local edge SQLite
    (both unscoped `config_documents` and per-user `config_documents_scoped`)
    and add `plc_endpoint` to every widget. Same logic as the cloud walk
    just above, but against the local DB the desktop edge actually reads."""
    import sqlite3
    db = local_sqlite_path()
    if not db.is_file():
        print(f"\n[local] {db} not found — skipping local backfill")
        return
    print(f"\n== Walking LOCAL SQLite ({db}) ==")
    con = sqlite3.connect(str(db), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        updates: list[tuple[str, dict, str | None]] = []  # (table_id, payload, scope_key)
        widgets_total = widgets_patched = fields_filled = 0

        # Unscoped: config_documents(domain='dashboard_configurations')
        r = con.execute(
            "SELECT payload_json FROM config_documents WHERE domain='dashboard_configurations'"
        ).fetchone()
        if r:
            payload = json.loads(r[0]) if isinstance(r[0], str) else r[0]
            widgets = payload.get("widgets") if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
            changed = 0
            for w in widgets or []:
                if not isinstance(w, dict): continue
                widgets_total += 1
                n = patch_widget(w, gw_to_ep)
                if n:
                    widgets_patched += 1
                    changed += n
            if changed:
                fields_filled += changed
                updates.append(("unscoped", payload, None))
                print(f"  config_documents (unscoped): +{changed} field(s)")

        # Scoped: config_documents_scoped per user/edge
        for sr in con.execute(
            "SELECT scope_key, payload_json FROM config_documents_scoped "
            "WHERE domain='dashboard_configurations'"
        ).fetchall():
            scope_key = sr[0]
            payload = json.loads(sr[1]) if isinstance(sr[1], str) else sr[1]
            widgets = payload.get("widgets") if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
            changed = 0
            for w in widgets or []:
                if not isinstance(w, dict): continue
                widgets_total += 1
                n = patch_widget(w, gw_to_ep)
                if n:
                    widgets_patched += 1
                    changed += n
            if changed:
                fields_filled += changed
                updates.append(("scoped", payload, scope_key))
                print(f"  scoped[{scope_key}]: +{changed} field(s)")

        print(f"\nlocal widgets scanned: {widgets_total}")
        print(f"local widgets patched: {widgets_patched}")
        print(f"local fields filled:   {fields_filled}")
        if not updates:
            print("local: nothing to do")
            return
        if not apply:
            print("local: dry-run; pass --apply --local to write.")
            return

        # Write back. Use a tx so partial writes don't leave inconsistent state.
        with con:
            for kind, payload, scope in updates:
                blob = json.dumps(payload)
                if kind == "unscoped":
                    con.execute(
                        "UPDATE config_documents "
                        "SET payload_json = ?, version = COALESCE(version, 0) + 1, "
                        "    updated_utc = strftime('%Y-%m-%d %H:%M:%f', 'now') "
                        "WHERE domain = 'dashboard_configurations'",
                        (blob,),
                    )
                else:
                    con.execute(
                        "UPDATE config_documents_scoped "
                        "SET payload_json = ?, version = COALESCE(version, 0) + 1, "
                        "    updated_utc = strftime('%Y-%m-%d %H:%M:%f', 'now') "
                        "WHERE domain = 'dashboard_configurations' AND scope_key = ?",
                        (blob, scope),
                    )
        print(f"local: wrote {len(updates)} row(s)")
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write changes")
    ap.add_argument("--local", action="store_true", help="also patch the LOCAL edge SQLite")
    args = ap.parse_args()

    print("== Fetching VPS gateway directory ==")
    gw_to_ep = fetch_vps_gateways()
    print(f"  {len(gw_to_ep)} gateway endpoint(s) known to the VPS")

    # Local SQLite is the authoritative source for the user's current
    # gateways. The VPS only has the central tenant's gateways and the
    # cloud telemetry only covers gateways that have produced data.
    # Without this step, freshly-created gateways that haven't published
    # yet (e.g. just configured, never started) wouldn't get a
    # plc_endpoint mapping.
    import sqlite3
    local_db = local_sqlite_path()
    if local_db.is_file():
        print(f"\n== Merging LOCAL gateway directory ({local_db}) ==")
        added = 0
        con_local = sqlite3.connect(str(local_db), timeout=10)
        try:
            for r in con_local.execute(
                "SELECT payload_json FROM config_documents WHERE domain='gateway_configurations'"
            ):
                payload = json.loads(r[0]) if isinstance(r[0], str) else r[0]
                if isinstance(payload, list):
                    for g in payload:
                        gid = str(g.get("id") or ""); ep = gateway_endpoint(g)
                        if gid and ep and gid not in gw_to_ep:
                            gw_to_ep[gid] = ep; added += 1
            for r in con_local.execute(
                "SELECT payload_json FROM config_documents_scoped WHERE domain='gateway_configurations'"
            ):
                payload = json.loads(r[0]) if isinstance(r[0], str) else r[0]
                if isinstance(payload, list):
                    for g in payload:
                        gid = str(g.get("id") or ""); ep = gateway_endpoint(g)
                        if gid and ep and gid not in gw_to_ep:
                            gw_to_ep[gid] = ep; added += 1
        finally:
            con_local.close()
        print(f"  +{added} gateway endpoint(s) recovered from local SQLite")

    # Supplement from cloud telemetry: any gateway that ever published
    # a sample has its plc_ip recorded against live_latest /
    # historian_readings. That covers customer-site gateways whose
    # config never made it into the VPS edge SQLite.
    print("\n== Supplementing from cloud telemetry (live_latest + historian) ==")
    con_supp = cloud_conn()
    try:
        with con_supp.cursor() as cur:
            cur.execute(
                "SELECT gateway_id, plc_ip "
                "FROM ( "
                "  SELECT gateway_id, plc_ip FROM public.live_latest "
                "  WHERE plc_ip IS NOT NULL AND plc_ip <> '' "
                "  UNION "
                "  SELECT gateway_id, plc_ip FROM public.historian_readings "
                "  WHERE plc_ip IS NOT NULL AND plc_ip <> '' "
                "  GROUP BY gateway_id, plc_ip "
                ") s "
                "GROUP BY gateway_id, plc_ip"
            )
            added = 0
            for gid, ip in cur.fetchall():
                gid = str(gid or ""); ip = str(ip or "")
                if not gid or not ip: continue
                if gid not in gw_to_ep:
                    gw_to_ep[gid] = ip
                    added += 1
            print(f"  +{added} gateway endpoint(s) recovered from telemetry")
    finally:
        con_supp.close()
    print(f"  total: {len(gw_to_ep)} gateway endpoint(s)")
    for gid, ep in sorted(gw_to_ep.items()):
        print(f"    {gid:25s} -> {ep}")

    print("\n== Walking cloud dashboard_configurations ==")
    con = cloud_conn()
    try:
        rows_total = 0
        widgets_total = 0
        widgets_patched = 0
        fields_filled = 0
        updates: list[tuple[str, str, dict]] = []  # (tenant_id, scope_key, new_payload)
        with con.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, scope_key, payload_json, version "
                "FROM public.dashboard_configurations"
            )
            for tid, scope, payload, version in cur.fetchall():
                rows_total += 1
                # payload_json may be jsonb -> dict; coerce
                if isinstance(payload, str):
                    try: payload = json.loads(payload)
                    except Exception: continue
                if not isinstance(payload, dict):
                    continue
                widgets = payload.get("widgets") if isinstance(payload.get("widgets"), list) else []
                if not widgets:
                    continue
                changed_here = 0
                for w in widgets:
                    if not isinstance(w, dict): continue
                    widgets_total += 1
                    n = patch_widget(w, gw_to_ep)
                    if n:
                        widgets_patched += 1
                        changed_here += n
                if changed_here:
                    fields_filled += changed_here
                    updates.append((tid, scope, payload))
                    print(f"  {tid}|{scope}: +{changed_here} field(s)")
        print(f"\nrows scanned:    {rows_total}")
        print(f"widgets scanned: {widgets_total}")
        print(f"widgets patched: {widgets_patched}")
        print(f"fields filled:   {fields_filled}")

        if not updates:
            print("\nCloud: nothing to do.")
        elif not args.apply:
            print("\n[dry-run] pass --apply to write these changes.")
        else:
            print("\n== Writing cloud updates ==")
            with con.cursor() as cur:
                for tid, scope, payload in updates:
                    cur.execute(
                        "UPDATE public.dashboard_configurations "
                        "SET payload_json = %s::jsonb, "
                        "    version = COALESCE(version, 0) + 1, "
                        "    updated_utc = now() "
                        "WHERE tenant_id = %s AND scope_key = %s",
                        (json.dumps(payload), tid, scope),
                    )
            con.commit()
            print(f"  wrote {len(updates)} row(s).")
    finally:
        con.close()

    # Local SQLite is opt-in (the user's edge has to be reachable from
    # this workstation). Do it after the cloud writes succeed.
    if args.local:
        backfill_local_sqlite(gw_to_ep, args.apply)

    return 0


if __name__ == "__main__":
    sys.exit(main())
