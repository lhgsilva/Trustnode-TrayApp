from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import base64
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from app.models import GatewayConfig, GatewayReading


class TelemetryService:
    """Edge-side telemetry recorder with authoritative outbox semantics.

    This service keeps immutable local raw history and a strict sync outbox.
    It can optionally push to VPS ingest API using device-scoped auth.
    """

    def __init__(self) -> None:
        data_dir = Path(os.environ.get("TRUSTNODE_DATA_DIR", str(Path.home() / ".trustnode_edge" / "data"))).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "trustnode_telemetry.db"
        self.instance_path = data_dir / "collector_instance_id.txt"
        self.collector_instance_id = self._load_or_create_instance_id()
        self.tenant_id = str(os.environ.get("TRUSTNODE_TENANT_ID", "default") or "default")
        self.customer_id = str(os.environ.get("TRUSTNODE_CUSTOMER_ID", "default") or "default")
        self.vps_ingest_url = str(os.environ.get("TRUSTNODE_VPS_INGEST_URL", "")).strip().rstrip("/")
        ingest_env = str(os.environ.get("TRUSTNODE_EDGE_INGEST_ENABLED", "") or "").strip().lower()
        self.ingest_enabled = ingest_env in {"1", "true", "yes", "on"} or bool(self.vps_ingest_url)
        self.device_token = str(os.environ.get("TRUSTNODE_DEVICE_TOKEN", "")).strip()
        self.cloud_bootstrap_user = str(os.environ.get("TRUSTNODE_CLOUD_BOOTSTRAP_USER", "admin") or "admin").strip()
        self.cloud_bootstrap_password = str(os.environ.get("TRUSTNODE_CLOUD_BOOTSTRAP_PASSWORD", "admin") or "admin").strip()
        self._cloud_user_token = ""
        self._cloud_user_token_exp = 0.0
        self.max_batch = max(50, min(2000, int(os.environ.get("TRUSTNODE_OUTBOX_BATCH_SIZE", "500") or "500")))
        self.max_request_bytes = max(256_000, int(os.environ.get("TRUSTNODE_OUTBOX_MAX_REQUEST_BYTES", "2000000") or "2000000"))
        self.base_backoff_seconds = max(0.25, float(os.environ.get("TRUSTNODE_OUTBOX_BACKOFF_BASE_SECONDS", "0.5") or "0.5"))
        self.max_backoff_seconds = max(1.0, float(os.environ.get("TRUSTNODE_OUTBOX_BACKOFF_MAX_SECONDS", "30") or "30"))
        self.clock_drift_warn_ms = max(1000, int(os.environ.get("TRUSTNODE_CLOCK_DRIFT_WARN_MS", "5000") or "5000"))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True, name="tn-outbox-sync-v1")
        self._ensure_schema()
        if self.ingest_enabled:
            self._sync_thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self.ingest_enabled and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=2)

    def _load_or_create_instance_id(self) -> str:
        if self.instance_path.exists():
            value = self.instance_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = str(uuid.uuid4())
        self.instance_path.write_text(value, encoding="utf-8")
        return value

    @staticmethod
    def _utc_now_text() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _canonical_json(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

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
                  received_at_vps_ts_utc TEXT,
                  ingested_at_cloud_ts_utc TEXT,
                  payload_hash_sha256 TEXT NOT NULL,
                  time_status TEXT NOT NULL DEFAULT 'ok',
                  created_utc TEXT NOT NULL DEFAULT (datetime('now')),
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

                CREATE TABLE IF NOT EXISTS sync_outbox_v1 (
                  edge_record_id TEXT PRIMARY KEY,
                  gateway_id TEXT NOT NULL,
                  sample_ts_utc TEXT NOT NULL,
                  edge_monotonic_seq INTEGER NOT NULL,
                  payload_json TEXT NOT NULL,
                  payload_hash_sha256 TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  retries INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT,
                  next_retry_utc TEXT,
                  created_utc TEXT NOT NULL DEFAULT (datetime('now')),
                  updated_utc TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS ingest_audit_log_local (
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

                CREATE TABLE IF NOT EXISTS gateway_runtime_state (
                  gateway_id TEXT PRIMARY KEY,
                  last_seq INTEGER NOT NULL DEFAULT 0,
                  gateway_config_version TEXT NOT NULL DEFAULT '',
                  updated_utc TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS collection_config_versions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  gateway_id TEXT NOT NULL,
                  gateway_config_version TEXT NOT NULL,
                  config_json TEXT NOT NULL,
                  created_utc TEXT NOT NULL DEFAULT (datetime('now')),
                  UNIQUE(gateway_id, gateway_config_version)
                );

                CREATE TABLE IF NOT EXISTS telemetry_metrics (
                  key TEXT PRIMARY KEY,
                  value_real REAL,
                  value_text TEXT,
                  updated_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS gateway_device_tokens (
                  gateway_id TEXT PRIMARY KEY,
                  token TEXT NOT NULL,
                  expires_utc TEXT NOT NULL,
                  updated_utc TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ix_raw_gateway_ts ON telemetry_samples_raw(gateway_id, sample_ts_utc DESC);
                CREATE INDEX IF NOT EXISTS ix_raw_tenant_plant_machine_ts ON telemetry_samples_raw(tenant_id, plant_id, machine_id, sample_ts_utc DESC);
                CREATE INDEX IF NOT EXISTS ix_raw_gateway_seq ON telemetry_samples_raw(gateway_id, edge_monotonic_seq DESC);
                CREATE INDEX IF NOT EXISTS ix_outbox_v1_status_seq ON sync_outbox_v1(status, sample_ts_utc, edge_monotonic_seq);
                """
            )

    def configure_from_bootstrap(self, bootstrap: Dict[str, Any]) -> None:
        data: Dict[str, Any] = {}
        if isinstance(bootstrap, dict):
            wrapped = bootstrap.get("data")
            if isinstance(wrapped, dict):
                data = wrapped
            else:
                data = bootstrap
        app_settings = data.get("app_settings") if isinstance(data.get("app_settings"), dict) else {}
        endpoint_mode = str(app_settings.get("endpoint_mode") or "").strip().lower()
        cloud_url = (
            str(
                app_settings.get("cloud_url")
                or app_settings.get("cloud_api_url")
                or app_settings.get("vps_ingest_url")
                or ""
            )
            .strip()
            .rstrip("/")
        )
        cloud_auto_sync_enabled = bool(app_settings.get("cloud_auto_sync_enabled", True))
        if not cloud_url:
            # Fallback for legacy settings where cloud URL is stored outside
            # app_settings payload.
            cloud_url = str(data.get("cloud_url") or data.get("cloud_api_url") or "").strip().rstrip("/")
        if endpoint_mode == "cloud" and cloud_url:
            self.vps_ingest_url = cloud_url
        # Keep ingest enabled for mirror mode even when UI endpoint_mode remains local.
        if cloud_auto_sync_enabled and cloud_url:
            self.vps_ingest_url = cloud_url
        realm = str(
            app_settings.get("tenant_login_realm")
            or app_settings.get("tenant_id")
            or self.tenant_id
        ).strip()
        if realm:
            self.tenant_id = realm

    @staticmethod
    def _decode_jwt_exp(token: str) -> float:
        try:
            parts = str(token or "").split(".")
            if len(parts) < 2:
                return 0.0
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
            return float(payload.get("exp") or 0.0)
        except Exception:
            return 0.0

    def _ensure_cloud_user_token(self) -> Tuple[bool, str]:
        now = time.time()
        if self._cloud_user_token and self._cloud_user_token_exp > now + 60:
            return True, self._cloud_user_token
        if not self.vps_ingest_url:
            return False, "missing_vps_ingest_url"
        try:
            res = requests.post(
                f"{self.vps_ingest_url}/api/auth/login",
                json={"username": self.cloud_bootstrap_user, "password": self.cloud_bootstrap_password},
                timeout=10,
            )
            if res.status_code // 100 != 2:
                return False, f"cloud_login_http_{res.status_code}"
            payload = res.json()
            token = str(payload.get("token") or "").strip()
            if not token:
                return False, "cloud_login_missing_token"
            self._cloud_user_token = token
            self._cloud_user_token_exp = self._decode_jwt_exp(token)
            return True, token
        except Exception as exc:
            return False, str(exc)

    def _gateway_token_from_db(self, conn: sqlite3.Connection, gateway_id: str) -> str:
        row = conn.execute(
            "SELECT token, expires_utc FROM gateway_device_tokens WHERE gateway_id = ?",
            (gateway_id,),
        ).fetchone()
        if not row:
            return ""
        expires_text = str(row["expires_utc"] or "")
        try:
            exp = datetime.fromisoformat(expires_text.replace("Z", "+00:00")).timestamp()
            if exp <= time.time() + 60:
                return ""
        except Exception:
            return ""
        return str(row["token"] or "")

    def _issue_gateway_device_token(self, conn: sqlite3.Connection, gateway_id: str) -> Tuple[bool, str]:
        if self.device_token:
            return True, self.device_token
        ok, token_or_err = self._ensure_cloud_user_token()
        if not ok:
            return False, token_or_err
        try:
            res = requests.post(
                f"{self.vps_ingest_url}/api/v1/devices/token",
                json={
                    "tenant_id": self.tenant_id,
                    "gateway_id": gateway_id,
                    "expires_seconds": 3600,
                },
                headers={"Authorization": f"Bearer {token_or_err}"},
                timeout=10,
            )
            if res.status_code // 100 != 2:
                return False, f"issue_device_token_http_{res.status_code}"
            payload = res.json()
            device_token = str(payload.get("token") or "").strip()
            if not device_token:
                return False, "issue_device_token_missing_token"
            exp_epoch = self._decode_jwt_exp(device_token)
            if exp_epoch > 0:
                expires_utc = datetime.fromtimestamp(exp_epoch, tz=timezone.utc).isoformat()
            else:
                expires_utc = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc).isoformat()
            now_utc = self._utc_now_text()
            conn.execute(
                """
                INSERT INTO gateway_device_tokens(gateway_id, token, expires_utc, updated_utc)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(gateway_id) DO UPDATE SET
                  token=excluded.token,
                  expires_utc=excluded.expires_utc,
                  updated_utc=excluded.updated_utc
                """,
                (gateway_id, device_token, expires_utc, now_utc),
            )
            return True, device_token
        except Exception as exc:
            return False, str(exc)

    def _token_for_gateway(self, conn: sqlite3.Connection, gateway_id: str) -> Tuple[bool, str]:
        if self.device_token:
            return True, self.device_token
        cached = self._gateway_token_from_db(conn, gateway_id)
        if cached:
            return True, cached
        return self._issue_gateway_device_token(conn, gateway_id)

    def _gateway_next_seq(self, conn: sqlite3.Connection, gateway_id: str) -> int:
        row = conn.execute("SELECT last_seq FROM gateway_runtime_state WHERE gateway_id = ?", (gateway_id,)).fetchone()
        next_seq = int(row["last_seq"]) + 1 if row else 1
        conn.execute(
            """
            INSERT INTO gateway_runtime_state(gateway_id, last_seq, updated_utc)
            VALUES (?, ?, ?)
            ON CONFLICT(gateway_id) DO UPDATE SET last_seq=excluded.last_seq, updated_utc=excluded.updated_utc
            """,
            (gateway_id, next_seq, self._utc_now_text()),
        )
        return next_seq

    def _config_version(self, conn: sqlite3.Connection, gateway_id: str, config: GatewayConfig) -> str:
        cfg_payload = {
            "gateway_type": config.gateway_type,
            "plc_ip": config.plc_ip,
            "opc_url": config.opc_url,
            "tags": list(config.tags or []),
            "interval_ms": int(config.interval_ms or 1000),
            "site": config.site,
            "area": config.area,
            "equipment": config.equipment,
            "collection_triggers": config.collection_triggers,
            "collection_trigger_mode": config.collection_trigger_mode,
        }
        cfg_hash = hashlib.sha256(self._canonical_json(cfg_payload).encode("utf-8")).hexdigest()[:16]
        version = f"cfg-{cfg_hash}"
        conn.execute(
            """
            INSERT OR IGNORE INTO collection_config_versions(gateway_id, gateway_config_version, config_json, created_utc)
            VALUES (?, ?, ?, ?)
            """,
            (gateway_id, version, self._canonical_json(cfg_payload), self._utc_now_text()),
        )
        return version

    def _quality_code(self, readings: List[GatewayReading]) -> int:
        if not readings:
            return 0
        return min(int(r.quality) for r in readings)

    def _time_status(self, sample_dt: datetime) -> str:
        now_dt = datetime.now(timezone.utc)
        drift_ms = abs((sample_dt - now_dt).total_seconds() * 1000.0)
        return "drift_warn" if drift_ms > self.clock_drift_warn_ms else "ok"

    def _audit(self, conn: sqlite3.Connection, *, actor_type: str, actor_id: str, tenant_id: str, action: str, outcome: str, correlation_id: str, details: Dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO ingest_audit_log_local(ts_utc, actor_type, actor_id, tenant_id, action, outcome, correlation_id, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._utc_now_text(),
                actor_type,
                actor_id,
                tenant_id,
                action,
                outcome,
                correlation_id,
                self._canonical_json(details),
            ),
        )

    def record_collection_cycle(self, *, gateway_id: str, config: GatewayConfig, readings: List[GatewayReading], collection_status: str = "ok") -> Tuple[bool, Optional[str], Optional[str]]:
        if not readings:
            return False, "empty_readings", None

        correlation_id = str(uuid.uuid4())
        sample_ts = str(readings[0].ts_utc)
        try:
            sample_dt = datetime.fromisoformat(sample_ts.replace(" ", "T").replace("Z", "+00:00"))
            if sample_dt.tzinfo is None:
                sample_dt = sample_dt.replace(tzinfo=timezone.utc)
            sample_ts_norm = sample_dt.astimezone(timezone.utc).isoformat()
        except Exception:
            return False, "invalid_sample_ts_utc", None

        tags = [
            {
                "tag_name": str(r.tag_name),
                "value": float(r.value) if r.value is not None else None,
                "quality_code": int(r.quality),
                "quality_label": str(r.quality_label),
            }
            for r in readings
        ]

        with self._lock:
            try:
                with self._connect() as conn:
                    conn.isolation_level = None
                    conn.execute("BEGIN")
                    seq = self._gateway_next_seq(conn, gateway_id)
                    cfg_version = self._config_version(conn, gateway_id, config)

                    record_core = {
                        "tenant_id": self.tenant_id,
                        "customer_id": self.customer_id,
                        "plant_id": str(config.site or "unknown-plant"),
                        "machine_id": str(config.equipment or "unknown-machine"),
                        "gateway_id": gateway_id,
                        "collector_instance_id": self.collector_instance_id,
                        "gateway_config_version": cfg_version,
                        "plc_driver_type": str(config.gateway_type or "unknown"),
                        "plc_endpoint_id": str(config.opc_url or config.plc_ip or "unknown-endpoint"),
                        "sample_ts_utc": sample_ts_norm,
                        "edge_monotonic_seq": seq,
                        "interval_ms": int(config.interval_ms or 1000),
                        "tags_json": tags,
                        "quality_code": self._quality_code(readings),
                        "collection_status": str(collection_status or "ok"),
                        "collected_at_edge_ts_utc": self._utc_now_text(),
                        "time_status": self._time_status(sample_dt),
                    }
                    payload_hash = hashlib.sha256(self._canonical_json(record_core).encode("utf-8")).hexdigest()
                    edge_record_id = str(uuid.uuid4())

                    conn.execute(
                        """
                        INSERT INTO telemetry_samples_raw(
                            edge_record_id, tenant_id, customer_id, plant_id, machine_id,
                            gateway_id, collector_instance_id, gateway_config_version,
                            plc_driver_type, plc_endpoint_id, sample_ts_utc, edge_monotonic_seq,
                            interval_ms, tags_json, quality_code, collection_status,
                            collected_at_edge_ts_utc, payload_hash_sha256, time_status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            edge_record_id,
                            record_core["tenant_id"],
                            record_core["customer_id"],
                            record_core["plant_id"],
                            record_core["machine_id"],
                            record_core["gateway_id"],
                            record_core["collector_instance_id"],
                            record_core["gateway_config_version"],
                            record_core["plc_driver_type"],
                            record_core["plc_endpoint_id"],
                            record_core["sample_ts_utc"],
                            record_core["edge_monotonic_seq"],
                            record_core["interval_ms"],
                            self._canonical_json(record_core["tags_json"]),
                            record_core["quality_code"],
                            record_core["collection_status"],
                            record_core["collected_at_edge_ts_utc"],
                            payload_hash,
                            record_core["time_status"],
                        ),
                    )

                    outbox_payload = dict(record_core)
                    outbox_payload["edge_record_id"] = edge_record_id
                    outbox_payload["payload_hash_sha256"] = payload_hash
                    if self.ingest_enabled:
                        conn.execute(
                            """
                            INSERT INTO sync_outbox_v1(edge_record_id, gateway_id, sample_ts_utc, edge_monotonic_seq, payload_json, payload_hash_sha256, status, retries, created_utc, updated_utc)
                            VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                            """,
                            (
                                edge_record_id,
                                gateway_id,
                                record_core["sample_ts_utc"],
                                record_core["edge_monotonic_seq"],
                                self._canonical_json(outbox_payload),
                                payload_hash,
                                self._utc_now_text(),
                                self._utc_now_text(),
                            ),
                        )

                    machine_key = f"{self.tenant_id}:{record_core['plant_id']}:{record_core['machine_id']}"
                    current = conn.execute(
                        "SELECT sample_ts_utc, edge_monotonic_seq FROM latest_machine_state WHERE machine_key = ?",
                        (machine_key,),
                    ).fetchone()
                    should_update = True
                    if current:
                        cur_ts = str(current["sample_ts_utc"])
                        cur_seq = int(current["edge_monotonic_seq"])
                        should_update = (sample_ts_norm > cur_ts) or (sample_ts_norm == cur_ts and seq > cur_seq)
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
                                self.tenant_id,
                                self.customer_id,
                                record_core["plant_id"],
                                record_core["machine_id"],
                                gateway_id,
                                sample_ts_norm,
                                seq,
                                self._canonical_json(tags),
                                record_core["quality_code"],
                                cfg_version,
                                self._utc_now_text(),
                            ),
                        )

                    self._audit(
                        conn,
                        actor_type="device",
                        actor_id=gateway_id,
                        tenant_id=self.tenant_id,
                        action="collection_cycle_commit",
                        outcome="success",
                        correlation_id=correlation_id,
                        details={"edge_record_id": edge_record_id, "seq": seq, "tag_count": len(tags)},
                    )

                    conn.execute("COMMIT")
                    self._metric_upsert(conn, "outbox_depth", self._outbox_depth(conn), None)
                    return True, None, edge_record_id
            except Exception as exc:
                try:
                    with self._connect() as conn2:
                        self._audit(
                            conn2,
                            actor_type="device",
                            actor_id=gateway_id,
                            tenant_id=self.tenant_id,
                            action="collection_cycle_commit",
                            outcome="failure",
                            correlation_id=correlation_id,
                            details={"error": str(exc)},
                        )
                except Exception:
                    pass
                return False, str(exc), None

    def _metric_upsert(self, conn: sqlite3.Connection, key: str, value_real: Optional[float], value_text: Optional[str]) -> None:
        conn.execute(
            """
            INSERT INTO telemetry_metrics(key, value_real, value_text, updated_utc)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_real=excluded.value_real, value_text=excluded.value_text, updated_utc=excluded.updated_utc
            """,
            (key, value_real, value_text, self._utc_now_text()),
        )

    def _outbox_depth(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) AS c FROM sync_outbox_v1 WHERE status IN ('pending','retry')").fetchone()
        return int(row["c"] if row else 0)

    def local_history(self, limit: int = 1000) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rs = conn.execute(
                """
                SELECT * FROM telemetry_samples_raw
                ORDER BY sample_ts_utc DESC, edge_monotonic_seq DESC
                LIMIT ?
                """,
                (max(1, min(10000, int(limit))),),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rs:
                d = dict(r)
                try:
                    d["tags_json"] = json.loads(d.get("tags_json") or "[]")
                except Exception:
                    pass
                out.append(d)
            return out

    def local_latest(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rs = conn.execute(
                "SELECT * FROM latest_machine_state ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(5000, int(limit))),),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rs:
                d = dict(r)
                try:
                    d["tags_json"] = json.loads(d.get("tags_json") or "[]")
                except Exception:
                    pass
                out.append(d)
            return out

    def diagnostics(self) -> Dict[str, Any]:
        with self._connect() as conn:
            outbox_depth = self._outbox_depth(conn)
            oldest = conn.execute(
                "SELECT sample_ts_utc FROM sync_outbox_v1 WHERE status IN ('pending','retry') ORDER BY sample_ts_utc ASC LIMIT 1"
            ).fetchone()
            by_gateway_rows = conn.execute(
                """
                SELECT gateway_id, status, COUNT(*) AS c
                FROM sync_outbox_v1
                GROUP BY gateway_id, status
                """
            ).fetchall()
            by_gateway: Dict[str, Dict[str, int]] = {}
            for row in by_gateway_rows:
                gid = str(row["gateway_id"] or "")
                st = str(row["status"] or "")
                if gid not in by_gateway:
                    by_gateway[gid] = {"pending": 0, "retry": 0, "acked": 0, "rejected": 0}
                if st in by_gateway[gid]:
                    by_gateway[gid][st] = int(row["c"] or 0)
            token_rows = conn.execute(
                "SELECT gateway_id, expires_utc, updated_utc FROM gateway_device_tokens ORDER BY updated_utc DESC"
            ).fetchall()
            gateway_tokens = [
                {
                    "gateway_id": str(r["gateway_id"] or ""),
                    "expires_utc": str(r["expires_utc"] or ""),
                    "updated_utc": str(r["updated_utc"] or ""),
                }
                for r in token_rows
            ]
            last_error = conn.execute(
                """
                SELECT edge_record_id, gateway_id, last_error, updated_utc
                FROM sync_outbox_v1
                WHERE status IN ('retry','rejected') AND COALESCE(last_error, '') <> ''
                ORDER BY updated_utc DESC
                LIMIT 1
                """
            ).fetchone()
            return {
                "collector_instance_id": self.collector_instance_id,
                "db_path": str(self.db_path),
                "tenant_id": self.tenant_id,
                "customer_id": self.customer_id,
                "ingest_enabled": self.ingest_enabled,
                "vps_ingest_url": self.vps_ingest_url,
                "cloud_bootstrap_user": self.cloud_bootstrap_user,
                "device_token_mode": "static_env" if bool(self.device_token) else "gateway_auto_issue",
                "cloud_user_token_cached": bool(self._cloud_user_token and self._cloud_user_token_exp > time.time() + 60),
                "outbox_depth": outbox_depth,
                "oldest_unsynced_sample_ts_utc": str(oldest["sample_ts_utc"]) if oldest else None,
                "outbox_by_gateway": by_gateway,
                "gateway_tokens": gateway_tokens,
                "last_outbox_error": {
                    "edge_record_id": str(last_error["edge_record_id"] or ""),
                    "gateway_id": str(last_error["gateway_id"] or ""),
                    "error": str(last_error["last_error"] or ""),
                    "updated_utc": str(last_error["updated_utc"] or ""),
                } if last_error else None,
            }

    def clear_outbox(self, *, include_acked: bool = False, actor: str = "admin") -> Dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                before = int(self._outbox_depth(conn))
                if include_acked:
                    deleted = int(conn.execute("DELETE FROM sync_outbox_v1").rowcount or 0)
                else:
                    deleted = int(
                        conn.execute(
                            "DELETE FROM sync_outbox_v1 WHERE status IN ('pending','retry','rejected')"
                        ).rowcount
                        or 0
                    )
                self._metric_upsert(conn, "outbox_depth", self._outbox_depth(conn), None)
                self._audit(
                    conn,
                    actor_type="user",
                    actor_id=str(actor or "admin"),
                    tenant_id=self.tenant_id,
                    action="edge_ingest_queue_clear",
                    outcome="success",
                    correlation_id=str(uuid.uuid4()),
                    details={"include_acked": bool(include_acked), "deleted": deleted, "before_depth": before},
                )
                after = int(self._outbox_depth(conn))
                return {
                    "ok": True,
                    "deleted_rows": deleted,
                    "before_depth": before,
                    "after_depth": after,
                    "message": f"Edge ingest outbox cleared: deleted {deleted} row(s).",
                }

    def _pull_upload_batch(self, conn: sqlite3.Connection) -> List[sqlite3.Row]:
        now = self._utc_now_text()
        rows = conn.execute(
            """
            SELECT * FROM sync_outbox_v1
            WHERE status IN ('pending','retry')
              AND (next_retry_utc IS NULL OR next_retry_utc <= ?)
            ORDER BY sample_ts_utc ASC, edge_monotonic_seq ASC
            LIMIT ?
            """,
            (now, max(self.max_batch * 4, self.max_batch)),
        ).fetchall()
        if not rows:
            return []
        first_gateway = str(rows[0]["gateway_id"] or "")
        selected: List[sqlite3.Row] = []
        for row in rows:
            if str(row["gateway_id"] or "") != first_gateway:
                continue
            selected.append(row)
            if len(selected) >= self.max_batch:
                break
        return selected

    def _next_backoff(self, retries: int) -> float:
        base = self.base_backoff_seconds * (2 ** max(0, retries - 1))
        jitter = random.uniform(0.0, 0.25 * base)
        return min(self.max_backoff_seconds, base + jitter)

    def _post_ingest_batch(self, records: List[Dict[str, Any]], *, gateway_id: str, bearer_token: str) -> Tuple[bool, Dict[str, Any]]:
        if not self.vps_ingest_url or not bearer_token:
            return False, {"error": "VPS ingest URL/device token not configured"}
        payload = {
            "tenant_id": self.tenant_id,
            "gateway_id": str(gateway_id or ""),
            "records": records,
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        compressed = gzip.compress(body)
        if len(compressed) > self.max_request_bytes:
            return False, {"error": f"compressed batch too large: {len(compressed)} bytes"}

        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }
        try:
            t0 = time.perf_counter()
            res = requests.post(
                f"{self.vps_ingest_url}/api/v1/ingest/batch",
                data=compressed,
                headers=headers,
                timeout=10,
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            if res.status_code // 100 != 2:
                return False, {"error": f"http_{res.status_code}", "body": res.text[:2000], "latency_ms": latency_ms}
            try:
                parsed = res.json()
            except Exception:
                parsed = {"ok": True}
            parsed["latency_ms"] = latency_ms
            return True, parsed
        except Exception as exc:
            return False, {"error": str(exc)}

    def _sync_loop(self) -> None:
        while not self._stop.is_set():
            any_work = False
            try:
                rows: List[Dict[str, Any]] = []
                gateway_id = ""
                # Keep lock scope minimal: do not hold it while performing network I/O.
                with self._lock:
                    with self._connect() as conn:
                        batch = self._pull_upload_batch(conn)
                        if batch:
                            rows = [dict(r) for r in batch]
                            gateway_id = str(rows[0].get("gateway_id") or "") if rows else ""
                            any_work = True
                        self._metric_upsert(conn, "outbox_depth", self._outbox_depth(conn), None)

                if rows:
                    ok_token = False
                    token_or_err = ""
                    # Token fetch may perform cloud login / token issue: keep outside lock.
                    with self._connect() as conn_token:
                        ok_token, token_or_err = self._token_for_gateway(conn_token, gateway_id)

                    if not ok_token:
                        now = self._utc_now_text()
                        err = f"device_token_unavailable:{token_or_err}"
                        with self._lock:
                            with self._connect() as conn:
                                for row in rows:
                                    retries = int(row.get("retries") or 0) + 1
                                    delay = self._next_backoff(retries)
                                    next_retry = datetime.fromtimestamp(time.time() + delay, tz=timezone.utc).isoformat()
                                    conn.execute(
                                        "UPDATE sync_outbox_v1 SET status='retry', retries=?, updated_utc=?, next_retry_utc=?, last_error=? WHERE edge_record_id=?",
                                        (retries, now, next_retry, err[:1000], str(row["edge_record_id"])),
                                    )
                                self._audit(
                                    conn,
                                    actor_type="system",
                                    actor_id="outbox_sync",
                                    tenant_id=self.tenant_id,
                                    action="device_token_issue",
                                    outcome="failure",
                                    correlation_id=str(uuid.uuid4()),
                                    details={"gateway_id": gateway_id, "batch_size": len(rows), "error": str(token_or_err)},
                                )
                                self._metric_upsert(conn, "outbox_depth", self._outbox_depth(conn), None)
                        continue

                    records: List[Dict[str, Any]] = []
                    for row in rows:
                        rec = json.loads(row["payload_json"])
                        records.append(rec)

                    ok, result = self._post_ingest_batch(records, gateway_id=gateway_id, bearer_token=token_or_err)
                    corr = str(result.get("correlation_id") or str(uuid.uuid4()))

                    with self._lock:
                        with self._connect() as conn:
                            if ok:
                                acked = set(result.get("acknowledged_ids") or [])
                                dups = set(result.get("duplicate_ids") or [])
                                rejected = {str((r or {}).get("edge_record_id") or ""): str((r or {}).get("reason") or "rejected") for r in (result.get("rejected") or [])}
                                now = self._utc_now_text()
                                for row in rows:
                                    rid = str(row["edge_record_id"])
                                    if rid in acked or rid in dups:
                                        conn.execute(
                                            "UPDATE sync_outbox_v1 SET status='acked', updated_utc=?, last_error=NULL WHERE edge_record_id=?",
                                            (now, rid),
                                        )
                                    elif rid in rejected:
                                        conn.execute(
                                            "UPDATE sync_outbox_v1 SET status='rejected', updated_utc=?, last_error=? WHERE edge_record_id=?",
                                            (now, rejected[rid][:1000], rid),
                                        )
                                    else:
                                        retries = int(row.get("retries") or 0) + 1
                                        delay = self._next_backoff(retries)
                                        next_retry = datetime.fromtimestamp(time.time() + delay, tz=timezone.utc).isoformat()
                                        conn.execute(
                                            "UPDATE sync_outbox_v1 SET status='retry', retries=?, updated_utc=?, next_retry_utc=?, last_error=? WHERE edge_record_id=?",
                                            (retries, now, next_retry, "missing_ack", rid),
                                        )
                                self._audit(
                                    conn,
                                    actor_type="system",
                                    actor_id="outbox_sync",
                                    tenant_id=self.tenant_id,
                                    action="batch_acknowledged",
                                    outcome="success",
                                    correlation_id=corr,
                                    details={
                                        "batch_size": len(rows),
                                        "acked": len(acked),
                                        "duplicates": len(dups),
                                        "rejected": len(rejected),
                                        "latency_ms": result.get("latency_ms"),
                                    },
                                )
                            else:
                                now = self._utc_now_text()
                                err = str(result.get("error") or "upload_failed")
                                for row in rows:
                                    retries = int(row.get("retries") or 0) + 1
                                    delay = self._next_backoff(retries)
                                    next_retry = datetime.fromtimestamp(time.time() + delay, tz=timezone.utc).isoformat()
                                    conn.execute(
                                        "UPDATE sync_outbox_v1 SET status='retry', retries=?, updated_utc=?, next_retry_utc=?, last_error=? WHERE edge_record_id=?",
                                        (retries, now, next_retry, err[:1000], str(row["edge_record_id"])),
                                    )
                                self._audit(
                                    conn,
                                    actor_type="system",
                                    actor_id="outbox_sync",
                                    tenant_id=self.tenant_id,
                                    action="batch_sent",
                                    outcome="failure",
                                    correlation_id=str(uuid.uuid4()),
                                    details={"batch_size": len(rows), "error": err},
                                )
                            self._metric_upsert(conn, "outbox_depth", self._outbox_depth(conn), None)
            except Exception:
                pass

            if any_work:
                time.sleep(0.15)
            else:
                time.sleep(0.5)
