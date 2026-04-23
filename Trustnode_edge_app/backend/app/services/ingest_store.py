from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


class IngestStore:
    def __init__(self) -> None:
        data_dir = Path(os.environ.get("TRUSTNODE_DATA_DIR", str(Path.home() / ".trustnode_edge" / "data"))).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "trustnode_ingest.db"
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now_utc() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;

                CREATE TABLE IF NOT EXISTS telemetry_samples_raw (
                  edge_record_id TEXT PRIMARY KEY,
                  tenant_id TEXT NOT NULL,
                  customer_id TEXT NOT NULL,
                  plant_id TEXT NOT NULL,
                  machine_id TEXT NOT NULL,
                  gateway_id TEXT NOT NULL,
                  collector_instance_id TEXT NOT NULL,
                  gateway_config_version TEXT NOT NULL,
                  plc_driver_type TEXT NOT NULL,
                  plc_endpoint_id TEXT NOT NULL,
                  sample_ts_utc TEXT NOT NULL,
                  edge_monotonic_seq INTEGER NOT NULL,
                  interval_ms INTEGER NOT NULL,
                  tags_json TEXT NOT NULL,
                  quality_code INTEGER,
                  collection_status TEXT NOT NULL,
                  collected_at_edge_ts_utc TEXT NOT NULL,
                  received_at_vps_ts_utc TEXT NOT NULL,
                  ingested_at_cloud_ts_utc TEXT NOT NULL,
                  payload_hash_sha256 TEXT NOT NULL,
                  time_status TEXT NOT NULL DEFAULT 'ok',
                  CHECK(interval_ms > 0),
                  UNIQUE(gateway_id, edge_monotonic_seq)
                );

                CREATE TABLE IF NOT EXISTS latest_machine_state (
                  machine_key TEXT PRIMARY KEY,
                  tenant_id TEXT NOT NULL,
                  customer_id TEXT NOT NULL,
                  plant_id TEXT NOT NULL,
                  machine_id TEXT NOT NULL,
                  gateway_id TEXT NOT NULL,
                  sample_ts_utc TEXT NOT NULL,
                  edge_monotonic_seq INTEGER NOT NULL,
                  tags_json TEXT NOT NULL,
                  quality_code INTEGER,
                  gateway_config_version TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ingest_audit_log (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts_utc TEXT NOT NULL,
                  actor_type TEXT NOT NULL,
                  actor_id TEXT NOT NULL,
                  tenant_id TEXT NOT NULL,
                  action TEXT NOT NULL,
                  outcome TEXT NOT NULL,
                  correlation_id TEXT NOT NULL,
                  details_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS gateway_registry (
                  gateway_id TEXT PRIMARY KEY,
                  tenant_id TEXT NOT NULL,
                  customer_id TEXT,
                  plant_id TEXT,
                  machine_id TEXT,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  updated_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS gateway_credentials_metadata (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  gateway_id TEXT NOT NULL,
                  tenant_id TEXT NOT NULL,
                  credential_fingerprint TEXT NOT NULL,
                  active INTEGER NOT NULL DEFAULT 1,
                  rotated_at_utc TEXT NOT NULL,
                  UNIQUE(gateway_id, credential_fingerprint)
                );

                CREATE TABLE IF NOT EXISTS collection_config_versions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  gateway_id TEXT NOT NULL,
                  tenant_id TEXT NOT NULL,
                  gateway_config_version TEXT NOT NULL,
                  created_utc TEXT NOT NULL,
                  UNIQUE(gateway_id, tenant_id, gateway_config_version)
                );

                CREATE TABLE IF NOT EXISTS tenant_users (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id TEXT NOT NULL,
                  username TEXT NOT NULL,
                  role TEXT NOT NULL,
                  mfa_enabled INTEGER NOT NULL DEFAULT 0,
                  created_utc TEXT NOT NULL,
                  UNIQUE(tenant_id, username)
                );

                CREATE TABLE IF NOT EXISTS security_audit_log (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts_utc TEXT NOT NULL,
                  actor_type TEXT NOT NULL,
                  actor_id TEXT NOT NULL,
                  tenant_id TEXT NOT NULL,
                  action TEXT NOT NULL,
                  outcome TEXT NOT NULL,
                  correlation_id TEXT NOT NULL,
                  details_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ix_raw_tenant_plant_machine_ts ON telemetry_samples_raw(tenant_id, plant_id, machine_id, sample_ts_utc DESC);
                CREATE INDEX IF NOT EXISTS ix_raw_gateway_ts ON telemetry_samples_raw(gateway_id, sample_ts_utc DESC);
                CREATE INDEX IF NOT EXISTS ix_raw_gateway_seq ON telemetry_samples_raw(gateway_id, edge_monotonic_seq DESC);
                """
            )

    def audit(self, *, actor_type: str, actor_id: str, tenant_id: str, action: str, outcome: str, correlation_id: str, details: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ingest_audit_log(ts_utc, actor_type, actor_id, tenant_id, action, outcome, correlation_id, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (self._now_utc(), actor_type, actor_id, tenant_id, action, outcome, correlation_id, json.dumps(details, separators=(",", ":"), sort_keys=True)),
            )

    def upsert_record(self, record: Dict[str, Any], *, received_at_vps_ts_utc: str) -> str:
        """Returns one of: inserted, duplicate, rejected_seq_conflict"""
        machine_key = f"{record['tenant_id']}:{record['plant_id']}:{record['machine_id']}"
        with self._connect() as conn:
            now = self._now_utc()
            existing_id = conn.execute(
                "SELECT edge_record_id FROM telemetry_samples_raw WHERE edge_record_id = ?",
                (record["edge_record_id"],),
            ).fetchone()
            if existing_id:
                return "duplicate"
            try:
                conn.execute(
                    """
                    INSERT INTO telemetry_samples_raw(
                      edge_record_id, tenant_id, customer_id, plant_id, machine_id, gateway_id,
                      collector_instance_id, gateway_config_version, plc_driver_type, plc_endpoint_id,
                      sample_ts_utc, edge_monotonic_seq, interval_ms, tags_json, quality_code,
                      collection_status, collected_at_edge_ts_utc, received_at_vps_ts_utc,
                      ingested_at_cloud_ts_utc, payload_hash_sha256, time_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["edge_record_id"],
                        record["tenant_id"],
                        record["customer_id"],
                        record["plant_id"],
                        record["machine_id"],
                        record["gateway_id"],
                        record["collector_instance_id"],
                        record["gateway_config_version"],
                        record["plc_driver_type"],
                        record["plc_endpoint_id"],
                        record["sample_ts_utc"],
                        int(record["edge_monotonic_seq"]),
                        int(record["interval_ms"]),
                        json.dumps(record["tags_json"], separators=(",", ":"), sort_keys=True),
                        int(record.get("quality_code") or 0),
                        record["collection_status"],
                        record["collected_at_edge_ts_utc"],
                        received_at_vps_ts_utc,
                        now,
                        record["payload_hash_sha256"],
                        str(record.get("time_status") or "ok"),
                    ),
                )
                inserted = True
            except sqlite3.IntegrityError as exc:
                msg = str(exc).lower()
                if "edge_record_id" in msg:
                    inserted = False
                elif "gateway_id, edge_monotonic_seq" in msg or "unique constraint failed: telemetry_samples_raw.gateway_id, telemetry_samples_raw.edge_monotonic_seq" in msg:
                    return "rejected_seq_conflict"
                else:
                    inserted = False

            if inserted:
                existing = conn.execute(
                    "SELECT sample_ts_utc, edge_monotonic_seq FROM latest_machine_state WHERE machine_key = ?",
                    (machine_key,),
                ).fetchone()
                should_update = True
                if existing:
                    cur_ts = str(existing["sample_ts_utc"])
                    cur_seq = int(existing["edge_monotonic_seq"])
                    incoming_ts = str(record["sample_ts_utc"])
                    incoming_seq = int(record["edge_monotonic_seq"])
                    should_update = (incoming_ts > cur_ts) or (incoming_ts == cur_ts and incoming_seq > cur_seq)
                if should_update:
                    conn.execute(
                        """
                        INSERT INTO latest_machine_state(machine_key, tenant_id, customer_id, plant_id, machine_id, gateway_id, sample_ts_utc, edge_monotonic_seq, tags_json, quality_code, gateway_config_version, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(machine_key) DO UPDATE SET
                          tenant_id=excluded.tenant_id,
                          customer_id=excluded.customer_id,
                          plant_id=excluded.plant_id,
                          machine_id=excluded.machine_id,
                          gateway_id=excluded.gateway_id,
                          sample_ts_utc=excluded.sample_ts_utc,
                          edge_monotonic_seq=excluded.edge_monotonic_seq,
                          tags_json=excluded.tags_json,
                          quality_code=excluded.quality_code,
                          gateway_config_version=excluded.gateway_config_version,
                          updated_at=excluded.updated_at
                        """,
                        (
                            machine_key,
                            record["tenant_id"],
                            record["customer_id"],
                            record["plant_id"],
                            record["machine_id"],
                            record["gateway_id"],
                            record["sample_ts_utc"],
                            int(record["edge_monotonic_seq"]),
                            json.dumps(record["tags_json"], separators=(",", ":"), sort_keys=True),
                            int(record.get("quality_code") or 0),
                            record["gateway_config_version"],
                            now,
                        ),
                    )
                return "inserted"
            return "duplicate"

    def query_history(
        self,
        tenant_id: str,
        limit: int = 1000,
        *,
        customer_id: str = "",
        plant_id: str = "",
        machine_id: str = "",
        gateway_id: str = "",
    ) -> List[Dict[str, Any]]:
        where = ["tenant_id=?"]
        params: List[Any] = [tenant_id]
        if str(customer_id or "").strip():
            where.append("customer_id=?")
            params.append(str(customer_id).strip())
        if str(plant_id or "").strip():
            where.append("plant_id=?")
            params.append(str(plant_id).strip())
        if str(machine_id or "").strip():
            where.append("machine_id=?")
            params.append(str(machine_id).strip())
        if str(gateway_id or "").strip():
            where.append("gateway_id=?")
            params.append(str(gateway_id).strip())
        where_sql = " AND ".join(where)
        params.append(max(1, min(10000, int(limit))))
        with self._connect() as conn:
            rs = conn.execute(
                f"SELECT * FROM telemetry_samples_raw WHERE {where_sql} ORDER BY sample_ts_utc DESC, edge_monotonic_seq DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rs:
                d = dict(r)
                d["tags_json"] = json.loads(d.get("tags_json") or "[]")
                out.append(d)
            return out

    def query_latest(
        self,
        tenant_id: str,
        limit: int = 500,
        *,
        customer_id: str = "",
        plant_id: str = "",
        machine_id: str = "",
        gateway_id: str = "",
    ) -> List[Dict[str, Any]]:
        where = ["tenant_id=?"]
        params: List[Any] = [tenant_id]
        if str(customer_id or "").strip():
            where.append("customer_id=?")
            params.append(str(customer_id).strip())
        if str(plant_id or "").strip():
            where.append("plant_id=?")
            params.append(str(plant_id).strip())
        if str(machine_id or "").strip():
            where.append("machine_id=?")
            params.append(str(machine_id).strip())
        if str(gateway_id or "").strip():
            where.append("gateway_id=?")
            params.append(str(gateway_id).strip())
        where_sql = " AND ".join(where)
        params.append(max(1, min(5000, int(limit))))
        with self._connect() as conn:
            rs = conn.execute(
                f"SELECT * FROM latest_machine_state WHERE {where_sql} ORDER BY updated_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rs:
                d = dict(r)
                d["tags_json"] = json.loads(d.get("tags_json") or "[]")
                out.append(d)
            return out
