"""One-shot diagnostic seeder for Lucas's cloud dashboard.

Writes a sample dashboard (4 widgets bound to his real PLC tags) into
the Supabase mirror tables so the Lite share-link displays something
while we trace why his local edge is silently skipping the config
mirror. Authorized by the user via AskUserQuestion on 2026-06-11.

Safe to re-run: ON CONFLICT (tenant_id, scope_key) DO UPDATE upserts in
place. Removable by deleting the rows where scope_key ends with
'edge-lucas'.
"""
from __future__ import annotations

import io
import json
import sys
import uuid
from pathlib import Path

import psycopg  # type: ignore

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent.parent
ENV_FILE = HERE / ".env"
TENANT = "tenant-cust-b47b1b83"
CUSTOMER = "cust-b47b1b83"
EDGE = "edge-lucas"
SCOPE = f"{TENANT}|{CUSTOMER}|{EDGE}"
GATEWAY = "gw-1781124704421"


def load_env(p: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main() -> int:
    env = load_env(ENV_FILE)
    ck = dict(
        host=env["TRUSTNODE_CLOUD_DB_HOST"],
        port=int(env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
        dbname=env["TRUSTNODE_CLOUD_DB_NAME"],
        user=env["TRUSTNODE_CLOUD_DB_USER"],
        password=env["TRUSTNODE_CLOUD_DB_PASSWORD"],
        sslmode="require",
        connect_timeout=15,
    )

    gw_payload = {
        "gateways": [
            {
                "id": GATEWAY,
                "name": "Primary Gateway",
                "gateway_type": "plc",
                "plc_ip": "127.0.0.1",
                "interval_ms": 1000,
                "enabled": True,
            }
        ]
    }

    # Grid is 20 columns wide on both edge and Lite. Two widgets per row
    # therefore want w=10 each (NOT w=6 which would only fill 60 % of the
    # row and leave a big dead band on the right). Heights are in 36 px
    # grid units — h=6 ≈ 220 px which feels right for a trend chart.
    widgets = [
        {
            "id": "w-" + uuid.uuid4().hex[:8],
            "title": "SimREAL[3] - Trend",
            "type": "line_chart",
            "x": 0, "y": 0, "w": 10, "h": 6,
            "config": {
                "gateway_id": GATEWAY,
                "tag_name": "SimREAL[3]",
                "readings_count": 60,
                "chart_show_legend": False,
            },
            "color": "#14a89a",
        },
        {
            "id": "w-" + uuid.uuid4().hex[:8],
            "title": "SimREAL[4] - Trend",
            "type": "line_chart",
            "x": 10, "y": 0, "w": 10, "h": 6,
            "config": {
                "gateway_id": GATEWAY,
                "tag_name": "SimREAL[4]",
                "readings_count": 60,
            },
            "color": "#3b82f6",
        },
        {
            "id": "w-" + uuid.uuid4().hex[:8],
            "title": "SimDINT[3]",
            "type": "value_kpi",
            "x": 0, "y": 6, "w": 5, "h": 4,
            "config": {"gateway_id": GATEWAY, "tag_name": "SimDINT[3]"},
        },
        {
            "id": "w-" + uuid.uuid4().hex[:8],
            "title": "SimDINT[4]",
            "type": "value_kpi",
            "x": 5, "y": 6, "w": 5, "h": 4,
            "config": {"gateway_id": GATEWAY, "tag_name": "SimDINT[4]"},
        },
    ]
    dash_payload = {
        "profiles": [
            {
                "key": "default",
                "label": "Production",
                "widgets": widgets,
                "mode": "chart",
                "per_row": 2,
                "tag_colors": {},
            }
        ],
        "widgets": widgets,
        "mode": "chart",
        "per_row": 2,
        "tag_colors": {},
    }

    upsert_sql = """
        INSERT INTO {table} (tenant_id, scope_key, payload_json, version, updated_utc)
        VALUES (%s, %s, %s::jsonb, %s, now())
        ON CONFLICT (tenant_id, scope_key) DO UPDATE SET
          payload_json = EXCLUDED.payload_json,
          version      = EXCLUDED.version,
          updated_utc  = now()
        RETURNING tenant_id, scope_key, version
    """

    with psycopg.connect(**ck) as c:
        cur = c.cursor()
        cur.execute(
            upsert_sql.format(table="gateway_configurations"),
            (TENANT, SCOPE, json.dumps(gw_payload), 1),
        )
        print("gateway_configurations:", cur.fetchone())
        cur.execute(
            upsert_sql.format(table="dashboard_configurations"),
            (TENANT, SCOPE, json.dumps(dash_payload), 1),
        )
        print("dashboard_configurations:", cur.fetchone())
        c.commit()
    print()
    print("Seed complete. Refresh the Lite share-link or /lite/ to see 4 widgets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
