import json
import os
import sqlite3
import threading
import shutil
import hashlib
import secrets
import time
import socket
import math
from urllib.parse import quote_plus
from datetime import timedelta
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from app.tenant import get_current_tenant, normalize_tenant_id


class AppStore:
    REQUIRED_CONFIG_DOMAINS: Dict[str, Any] = {
        "app_settings": {},
        "users_access": {"users": [], "current_user": ""},
        "devices": [],
        "gateway_configurations": [],
        "database_configurations": [],
        "triggers_limits": {"collection_triggers": [], "collection_trigger_mode": "any", "trigger_rules": []},
        "dashboard_configurations": {"widgets": [], "mode": "kpi", "per_row": 2},
        "alarms_setup": {"alarms": []},
        "reporting_setup": {"filters": {}, "documents": [], "templates": [], "schedules": []},
        "tags": {"alarm_prefs": {}},
        "email_notifications": {"settings": {}, "profiles": [], "active_profile_id": ""},
        "metadata": {},
    }
    DEFAULT_LOCAL_DB_ID = "local-sqlite-default"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cloud_schema_lock = threading.Lock()
        self._cloud_engine_lock = threading.Lock()
        self._cloud_schema_ready_keys: set[str] = set()
        self._cloud_engine_cache: dict[str, Any] = {}
        # 17 different code paths call _get_cloud_database_target on every
        # request; without a cache the lookup ran two SQL queries under the
        # global lock for every /api/* call and serialized the backend under
        # load (cloud client view spam-polling). Cache for 5 s.
        self._cloud_target_cache_lock = threading.Lock()
        self._cloud_target_cache_value: Dict[str, Any] | None = None
        self._cloud_target_cache_ts: float = 0.0
        self._cloud_target_cache_ttl: float = max(
            1.0,
            float(os.environ.get("TRUSTNODE_CLOUD_TARGET_CACHE_SECONDS", "5") or "5"),
        )
        self._db_path = self._resolve_db_path()
        self._stop_event = threading.Event()
        self._sync_wakeup_event = threading.Event()
        self._live_sync_wakeup_event = threading.Event()
        self._cloud_live_cache_lock = threading.Lock()
        # Fast default cadence for cloud/live products; tunable via env.
        self._sync_interval_seconds = max(
            0.2,
            float(os.environ.get("TRUSTNODE_CONFIG_SYNC_SECONDS", "0.2") or "0.2"),
        )
        self._config_pull_interval_seconds = max(
            self._sync_interval_seconds,
            float(os.environ.get("TRUSTNODE_CONFIG_PULL_SECONDS", "20") or "20"),
        )
        self._disable_config_push = str(os.environ.get("TRUSTNODE_DISABLE_CONFIG_PUSH", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._data_sync_batch_size = max(
            200,
            min(20000, int(os.environ.get("TRUSTNODE_DATA_SYNC_BATCH_SIZE", "2000") or "2000")),
        )
        self._data_sync_log_batch_size = max(
            50,
            min(5000, int(os.environ.get("TRUSTNODE_LOG_SYNC_BATCH_SIZE", "400") or "400")),
        )
        self._data_sync_log_every_n = max(
            1,
            min(20, int(os.environ.get("TRUSTNODE_LOG_SYNC_EVERY_N", "4") or "4")),
        )
        self._data_bulk_sync_interval_seconds = max(
            0.05,
            float(os.environ.get("TRUSTNODE_DATA_BULK_SYNC_SECONDS", "0.08") or "0.08"),
        )
        self._data_sync_burst_batches = max(
            1,
            min(24, int(os.environ.get("TRUSTNODE_DATA_SYNC_BURST_BATCHES", "12") or "12")),
        )
        self._data_sync_burst_seconds = max(
            0.1,
            float(os.environ.get("TRUSTNODE_DATA_SYNC_BURST_SECONDS", "1.8") or "1.8"),
        )
        self._live_fast_batch_size = max(
            500,
            min(20000, int(os.environ.get("TRUSTNODE_LIVE_FAST_BATCH_SIZE", "6000") or "6000")),
        )
        self._live_fast_initial_rows = max(
            self._live_fast_batch_size,
            min(40000, int(os.environ.get("TRUSTNODE_LIVE_FAST_INITIAL_ROWS", "12000") or "12000")),
        )
        self._live_sync_interval_seconds = max(
            0.08,
            float(os.environ.get("TRUSTNODE_LIVE_SYNC_SECONDS", "0.1") or "0.1"),
        )
        self._live_sync_burst_batches = max(
            1,
            min(24, int(os.environ.get("TRUSTNODE_LIVE_SYNC_BURST_BATCHES", "8") or "8")),
        )
        self._live_sync_burst_seconds = max(
            0.05,
            float(os.environ.get("TRUSTNODE_LIVE_SYNC_BURST_SECONDS", "0.25") or "0.25"),
        )
        self._live_source_switch_threshold_ms = max(
            500,
            int(os.environ.get("TRUSTNODE_LIVE_SOURCE_SWITCH_MS", "1500") or "1500"),
        )
        self._live_source_max_stale_ms = max(
            1200,
            int(os.environ.get("TRUSTNODE_LIVE_SOURCE_MAX_STALE_MS", "2000") or "2000"),
        )
        self._live_data_catchup_interval_seconds = max(
            0.1,
            float(os.environ.get("TRUSTNODE_LIVE_DATA_CATCHUP_SECONDS", "0.15") or "0.15"),
        )
        self._live_fast_last_local_id = 0
        self._live_fast_pending_latest: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._local_live_latest_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._data_sync_tick = 0
        # IMPORTANT: cache MUST be keyed by tenant_id. Previously it was a
        # single shared list, so the cloud cache loop populated it under
        # one tenant context and every other tenant's HTTP read returned
        # those rows (with their own tenant_id stamped on the response).
        self._cloud_live_cache_rows_by_tenant: dict[str, list[dict[str, Any]]] = {}
        self._cloud_live_cache_updated_utc_by_tenant: dict[str, str] = {}
        self._cloud_live_cache_limit = max(
            200,
            min(5000, int(os.environ.get("TRUSTNODE_CLOUD_LIVE_CACHE_LIMIT", "1200") or "1200")),
        )
        self._cloud_live_cache_interval_seconds = max(
            0.05,
            float(os.environ.get("TRUSTNODE_CLOUD_LIVE_CACHE_SECONDS", "0.1") or "0.1"),
        )
        # Strict mirror mode keeps cloud reads sourced only from canonical mirrored
        # tables filtered by tenant_id (no unscoped fallback, no mixed-table merge).
        self._strict_cloud_mirror = str(
            os.environ.get("TRUSTNODE_STRICT_CLOUD_MIRROR", "1")
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._edge_config_isolation_enabled = str(
            os.environ.get("TRUSTNODE_EDGE_CONFIG_ISOLATION", "1")
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._local_edge_id = self._derive_local_edge_id()
        self._ensure_schema()
        self._ensure_required_config_domains()
        self._ensure_local_edge_profile()
        self._ensure_default_database_configuration()
        if not self._disable_config_push:
            self._compact_sync_outbox_for_domains()
            self._backfill_outbox_for_existing_domains()
        # Operator diagnostics: when a portable EXE runs with a different
        # working directory than expected, two app_store DBs can exist
        # in parallel. Logging the absolute path on every startup makes
        # the active DB unambiguous in support sessions.
        try:
            print(f"[trustnode] app_store_db = {os.path.abspath(self._db_path)}", flush=True)
        except Exception:
            pass
        # Reconcile the sync_targets row against the actual cloud target
        # resolved from database_configurations once at startup, so a
        # stale "no enabled cloud target" error from a previous run gets
        # cleared as soon as a valid cloud config is present.
        try:
            self._reconcile_sync_targets_with_config()
        except Exception:
            pass
        # Best-effort one-shot push of the Lite-readable mirror tables on
        # startup. The periodic loop will do this every 5s anyway, but
        # kicking it once at boot means a freshly-started backend doesn't
        # leave the Lite app showing "no dashboards yet" for the first
        # five-second window after a restart. Errors are swallowed; the
        # periodic loop will retry.
        try:
            self._reconcile_lite_mirror_tables_once()
        except Exception:
            pass
        # Seed demo report templates + push the local templates list to the
        # cloud once on boot. Each demo is keyed by a stable id so seeding
        # is idempotent — re-running it on subsequent boots won't duplicate
        # rows or overwrite a customer-edited copy.
        try:
            self._seed_and_reconcile_report_templates_once()
        except Exception:
            pass
        # Boot-time mirror: republish every Lite-visible scoped doc so an
        # edge that just upgraded to a build with cloud-mirror support
        # backfills its history into Supabase WITHOUT requiring the
        # operator to open and save each dashboard / alarm / trigger one
        # by one. Idempotent (re-mirror is a versioned UPSERT) and runs in
        # a background thread so it never delays boot.
        try:
            threading.Thread(
                target=self._boot_remirror_scoped_docs_safe,
                name="tn-boot-remirror",
                daemon=True,
            ).start()
        except Exception:
            pass
        self._scheduler_thread = threading.Thread(target=self._retention_scheduler_loop, daemon=True)
        self._live_sync_thread = threading.Thread(target=self._live_sync_loop, daemon=True)
        self._cloud_live_cache_thread = threading.Thread(target=self._cloud_live_cache_loop, daemon=True)
        self._sync_thread = threading.Thread(target=self._config_sync_loop, daemon=True)
        self._scheduler_thread.start()
        self._live_sync_thread.start()
        self._cloud_live_cache_thread.start()
        self._sync_thread.start()

    def _cloud_target_schema_key(self, cloud: dict[str, Any], schema: str) -> str:
        return "|".join(
            [
                str(cloud.get("host") or "").strip().lower(),
                str(cloud.get("port") or "").strip(),
                str(cloud.get("database") or "").strip().lower(),
                str(cloud.get("username") or "").strip().lower(),
                str(schema or "public").strip().lower(),
            ]
        )

    def _canonical_json(self, payload: Any) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _strip_runtime_fields_for_config_sync(self, payload: Any) -> Any:
        transient_keys = {
            "saved_utc",
            "connection_ok",
            "ping_ok",
            "port_ok",
            "protocol_ok",
            "last_test",
            "last_check_utc",
            "db_write_count",
            "db_pending_count",
            "db_last_write_utc",
            "db_last_error",
            "last_error",
            "running",
            "transient",
            "collection_blocked",
            "collection_block_reason",
        }
        if isinstance(payload, dict):
            out: Dict[str, Any] = {}
            for k, v in payload.items():
                key = str(k)
                if key in transient_keys:
                    continue
                out[key] = self._strip_runtime_fields_for_config_sync(v)
            return out
        if isinstance(payload, list):
            return [self._strip_runtime_fields_for_config_sync(v) for v in payload]
        return payload

    def _load_previous_scoped_payload(self, scope_key: str, domain: str) -> Any:
        """Read the previous payload for (scope_key, domain) without mutating
        anything. Returns parsed JSON or None when there's no prior row.
        Used by stabilisers that need to compare new-vs-old shape before
        committing a write."""
        try:
            with self._lock:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT payload_json FROM config_documents_scoped "
                        "WHERE scope_key = ? AND domain = ?",
                        (str(scope_key or ""), str(domain or "")),
                    ).fetchone()
            if not row:
                return None
            return json.loads(str(row["payload_json"] or "null"))
        except Exception:
            return None

    def _stabilise_gateway_ids_by_plc_ip(
        self,
        *,
        new_payload: Any,
        prev_payload: Any,
    ) -> Any:
        """When the operator deletes a gateway and adds one back at the same
        plc_ip (+ gateway_type), reuse the previous id. Widgets, alarms and
        triggers reference gateways by id; without this, every re-create
        leaves dashboards pointing at a dead id.

        Conservative — only rewrites the id when there's exactly one
        unambiguous match on (plc_ip, gateway_type). Same-id passthrough is
        preserved, so editing an existing gateway never triggers a rewrite.
        """
        if not isinstance(new_payload, list) or not isinstance(prev_payload, list):
            return new_payload
        prev_by_id: Dict[str, Dict[str, Any]] = {}
        for g in prev_payload:
            if isinstance(g, dict):
                gid = str(g.get("id") or "").strip()
                if gid:
                    prev_by_id[gid] = g
        new_ids = {str(g.get("id") or "").strip() for g in new_payload if isinstance(g, dict)}
        # A "removed" gateway is one whose id is in prev but not in new.
        removed: list[Dict[str, Any]] = []
        for gid, g in prev_by_id.items():
            if gid not in new_ids:
                removed.append(g)
        if not removed:
            return new_payload

        def _norm_ip(x: Any) -> str:
            return str(x or "").strip()

        def _norm_type(x: Any) -> str:
            return str(x or "").strip().lower()

        # Group removed by (plc_ip, gateway_type) for ambiguity check.
        removed_by_key: Dict[Tuple[str, str], list[str]] = {}
        for g in removed:
            ip = _norm_ip(g.get("plc_ip"))
            gt = _norm_type(g.get("gateway_type"))
            if not ip:
                continue
            removed_by_key.setdefault((ip, gt), []).append(str(g.get("id") or "").strip())

        # Walk new entries; for each one whose id is NOT in prev (truly new),
        # check whether a single removed gateway matches the same key.
        result: list[Any] = []
        for g in new_payload:
            if not isinstance(g, dict):
                result.append(g)
                continue
            gid = str(g.get("id") or "").strip()
            if gid and gid in prev_by_id:
                result.append(g)
                continue
            ip = _norm_ip(g.get("plc_ip"))
            gt = _norm_type(g.get("gateway_type"))
            if not ip:
                result.append(g)
                continue
            candidates = removed_by_key.get((ip, gt)) or []
            if len(candidates) == 1:
                stable_id = candidates[0]
                rewritten = dict(g)
                rewritten["id"] = stable_id
                # Drop the consumed removed id so we don't re-use it twice.
                removed_by_key[(ip, gt)] = []
                result.append(rewritten)
            else:
                result.append(g)
        return result

    def _get_or_create_cloud_engine(self, cloud: dict[str, Any], schema: str) -> tuple[Any, str]:
        from sqlalchemy import create_engine  # type: ignore

        key = self._cloud_target_schema_key(cloud, schema)
        with self._cloud_engine_lock:
            cached = self._cloud_engine_cache.get(key)
            if cached is not None:
                return cached, key
            url = self._build_pg_sqlalchemy_url(
                str(cloud.get("host") or ""),
                int(cloud.get("port") or 5432),
                str(cloud.get("database") or "postgres"),
                str(cloud.get("username") or ""),
                str(cloud.get("password") or ""),
            )
            connect_args = {
                "sslmode": "require" if cloud.get("tls", True) else "disable",
                "connect_timeout": max(1, int(os.environ.get("TRUSTNODE_CLOUD_DB_CONNECT_TIMEOUT_SECONDS", "6") or "6")),
                "prepare_threshold": None,
                "options": os.environ.get(
                    "TRUSTNODE_CLOUD_DB_OPTIONS",
                    "-c lock_timeout=1200ms -c statement_timeout=4500ms",
                ),
            }
            # Pool sizing: the data-sync workers + control-plane reads
            # (when running in cloud-canonical mode) + the Lite mirror
            # all share this engine. Bumped from 4+4 to 8+12 (max 20)
            # to ride out portal-page-load bursts that fire ~10 cp_*
            # endpoints in parallel without exhausting the pool. The
            # Supabase Pooler can comfortably hold ~30 concurrent
            # connections per project; we're well under that.
            pool_size = int(os.environ.get("TRUSTNODE_CLOUD_DB_POOL_SIZE", "8") or "8")
            max_overflow = int(os.environ.get("TRUSTNODE_CLOUD_DB_MAX_OVERFLOW", "12") or "12")
            engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_recycle=300,
                pool_timeout=10,  # don't wait forever if pool is exhausted
                connect_args=connect_args,
            )
            stale_keys = [k for k in self._cloud_engine_cache.keys() if k != key]
            for stale_key in stale_keys:
                stale_engine = self._cloud_engine_cache.pop(stale_key, None)
                if stale_engine is None:
                    continue
                try:
                    stale_engine.dispose()
                except Exception:
                    pass
            self._cloud_engine_cache[key] = engine
            return engine, key

    def _configured_tenant_id(self) -> str:
        forced = normalize_tenant_id(str(os.environ.get("TRUSTNODE_TENANT_ID") or "").strip())
        if forced and forced != "default":
            return forced
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM config_documents WHERE domain = 'app_settings' LIMIT 1"
                ).fetchone()
            payload = json.loads(str(row["payload_json"] or "{}")) if row else {}
            if isinstance(payload, dict):
                realm = str(payload.get("tenant_login_realm") or payload.get("tenant_id") or "").strip()
                normalized = normalize_tenant_id(realm)
                if normalized and normalized != "default":
                    return normalized
        except Exception:
            pass
        return "default"

    def _current_tenant_id(self) -> str:
        request_tenant = normalize_tenant_id(get_current_tenant())
        if request_tenant and request_tenant != "default":
            return request_tenant
        configured_tenant = self._configured_tenant_id()
        if configured_tenant and configured_tenant != "default":
            return configured_tenant
        return request_tenant or "default"

    def _prefer_cloud_reads(self) -> bool:
        env_value = str(os.environ.get("TRUSTNODE_PREFER_CLOUD_READS", "")).strip().lower()
        if env_value in {"1", "true", "yes", "on"}:
            return True
        if env_value in {"0", "false", "no", "off"}:
            return False
        try:
            settings = self.get_config_domain("app_settings")
            endpoint_mode = str((settings or {}).get("endpoint_mode") or "").strip().lower()
            if endpoint_mode == "cloud":
                return True
        except Exception:
            pass
        return False

    def _derive_local_edge_id(self) -> str:
        raw_host = (
            str(os.environ.get("COMPUTERNAME") or "").strip()
            or str(os.environ.get("HOSTNAME") or "").strip()
            or str(socket.gethostname() or "").strip()
            or "local"
        )
        safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in raw_host).strip("-")
        safe = safe or "local"
        if not safe.startswith("edge-"):
            safe = f"edge-{safe}"
        return safe[:64]

    def _ensure_local_edge_profile(self) -> None:
        now = self._utc_now()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json, version FROM config_documents WHERE domain = ?",
                    ("app_settings",),
                ).fetchone()
                payload: Dict[str, Any] = {}
                version = 1
                if row:
                    version = int(row["version"] or 1)
                    try:
                        raw = json.loads(str(row["payload_json"] or "{}"))
                        if isinstance(raw, dict):
                            payload = raw
                    except Exception:
                        payload = {}
                edge_profile = payload.get("edge_profile")
                if not isinstance(edge_profile, dict):
                    edge_profile = {}
                current_edge_id = str(edge_profile.get("edge_id") or "").strip()
                if current_edge_id and current_edge_id != "edge-01":
                    return
                edge_profile["edge_id"] = self._local_edge_id
                edge_profile["edge_name"] = str(edge_profile.get("edge_name") or self._local_edge_id)
                edge_profile["description"] = str(edge_profile.get("description") or "")
                edge_profile["location"] = str(edge_profile.get("location") or "")
                edge_profile["machine_group"] = str(edge_profile.get("machine_group") or "")
                payload["edge_profile"] = edge_profile
                next_version = max(1, version + 1)
                conn.execute(
                    """
                    INSERT INTO config_documents(domain, payload_json, version, updated_utc)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(domain) DO UPDATE SET
                      payload_json = excluded.payload_json,
                      version = excluded.version,
                      updated_utc = excluded.updated_utc
                    """,
                    ("app_settings", json.dumps(payload), next_version, now),
                )

    def get_config_domain(self, domain: str, default: Any | None = None) -> Any:
        name = str(domain or "").strip()
        if not name:
            return {} if default is None else default
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM config_documents WHERE domain = ?",
                    (name,),
                ).fetchone()
        if not row:
            return {} if default is None else default
        try:
            payload = json.loads(str(row["payload_json"] or "null"))
        except Exception:
            return {} if default is None else default
        return payload if payload is not None else ({} if default is None else default)

    def _ensure_cloud_schema_once(self, engine: Any, schema: str, target_key: str) -> None:
        if target_key in self._cloud_schema_ready_keys:
            return
        with self._cloud_schema_lock:
            if target_key in self._cloud_schema_ready_keys:
                return
            from sqlalchemy import text  # type: ignore

            with engine.begin() as conn:
                if schema != "public":
                    conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."historian_readings" (
                          id BIGSERIAL PRIMARY KEY,
                          local_id BIGINT NOT NULL,
                          tenant_id TEXT NOT NULL DEFAULT 'default',
                          ts_utc TIMESTAMPTZ NOT NULL,
                          gateway_id TEXT NULL,
                          gateway_name TEXT NULL,
                          device_name TEXT NULL,
                          plc_ip TEXT NULL,
                          database_name TEXT NULL,
                          tag_name TEXT NOT NULL,
                          value DOUBLE PRECISION NULL,
                          quality INTEGER NULL,
                          quality_label TEXT NULL,
                          source TEXT NULL,
                          created_utc TIMESTAMPTZ NULL
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."plc_readings" (
                          id BIGSERIAL PRIMARY KEY,
                          local_id BIGINT NOT NULL,
                          tenant_id TEXT NOT NULL DEFAULT 'default',
                          ts_utc TIMESTAMPTZ NOT NULL,
                          gateway_id TEXT NULL,
                          gateway_name TEXT NULL,
                          device_name TEXT NULL,
                          plc_ip TEXT NULL,
                          database_name TEXT NULL,
                          tag_name TEXT NOT NULL,
                          value DOUBLE PRECISION NULL,
                          quality INTEGER NULL,
                          quality_label TEXT NULL,
                          source TEXT NULL,
                          created_utc TIMESTAMPTZ NULL
                        )
                        """
                    )
                )
                conn.execute(text(f'ALTER TABLE "{schema}"."plc_readings" ADD COLUMN IF NOT EXISTS local_id BIGINT'))
                conn.execute(text(f'ALTER TABLE "{schema}"."plc_readings" ADD COLUMN IF NOT EXISTS tenant_id TEXT'))
                conn.execute(text(f'ALTER TABLE "{schema}"."plc_readings" ADD COLUMN IF NOT EXISTS gateway_id TEXT'))
                conn.execute(text(f'ALTER TABLE "{schema}"."plc_readings" ADD COLUMN IF NOT EXISTS gateway_name TEXT'))
                conn.execute(text(f'ALTER TABLE "{schema}"."plc_readings" ADD COLUMN IF NOT EXISTS device_name TEXT'))
                conn.execute(text(f'ALTER TABLE "{schema}"."plc_readings" ADD COLUMN IF NOT EXISTS plc_ip TEXT'))
                conn.execute(text(f'ALTER TABLE "{schema}"."plc_readings" ADD COLUMN IF NOT EXISTS database_name TEXT'))
                conn.execute(text(f'ALTER TABLE "{schema}"."plc_readings" ADD COLUMN IF NOT EXISTS tag_name TEXT'))
                conn.execute(text(f'ALTER TABLE "{schema}"."plc_readings" ADD COLUMN IF NOT EXISTS quality INTEGER'))
                conn.execute(text(f'ALTER TABLE "{schema}"."plc_readings" ADD COLUMN IF NOT EXISTS quality_label TEXT'))
                conn.execute(text(f'ALTER TABLE "{schema}"."plc_readings" ADD COLUMN IF NOT EXISTS source TEXT'))
                conn.execute(text(f'ALTER TABLE "{schema}"."plc_readings" ADD COLUMN IF NOT EXISTS created_utc TIMESTAMPTZ'))
                conn.execute(text(f'ALTER TABLE "{schema}"."historian_readings" DROP CONSTRAINT IF EXISTS "historian_readings_local_id_key"'))
                conn.execute(text(f'ALTER TABLE "{schema}"."plc_readings" DROP CONSTRAINT IF EXISTS "plc_readings_local_id_key"'))
                conn.execute(text(f'ALTER TABLE "{schema}"."app_logs" DROP CONSTRAINT IF EXISTS "app_logs_local_id_key"'))
                conn.execute(text(f'DROP INDEX IF EXISTS "{schema}"."ux_plc_local_id"'))
                conn.execute(text(f'DROP INDEX IF EXISTS "{schema}"."ux_hist_local_id"'))
                conn.execute(text(f'DROP INDEX IF EXISTS "{schema}"."ux_logs_local_id"'))
                conn.execute(
                    text(
                        f'CREATE UNIQUE INDEX IF NOT EXISTS "ux_plc_tenant_gateway_local_id" ON "{schema}"."plc_readings"(tenant_id, gateway_id, local_id)'
                    )
                )
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_plc_tenant_ts" ON "{schema}"."plc_readings"(tenant_id, ts_utc DESC)'))

                # Compatibility migration for pre-local_id deployments.
                conn.execute(text(f'ALTER TABLE "{schema}"."historian_readings" ADD COLUMN IF NOT EXISTS tenant_id TEXT'))
                conn.execute(text(f'ALTER TABLE "{schema}"."historian_readings" ADD COLUMN IF NOT EXISTS local_id BIGINT'))
                conn.execute(
                    text(
                        f'CREATE UNIQUE INDEX IF NOT EXISTS "ux_hist_tenant_gateway_local_id" ON "{schema}"."historian_readings"(tenant_id, gateway_id, local_id)'
                    )
                )
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_hist_tenant_ts" ON "{schema}"."historian_readings"(tenant_id, ts_utc DESC)'))
                conn.execute(
                    text(
                        f'CREATE INDEX IF NOT EXISTS "ix_hist_tenant_gw_tag_localid" ON "{schema}"."historian_readings"(tenant_id, gateway_id, tag_name, local_id DESC, id DESC)'
                    )
                )

                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."app_logs" (
                          id BIGSERIAL PRIMARY KEY,
                          local_id BIGINT NOT NULL,
                          tenant_id TEXT NOT NULL DEFAULT 'default',
                          ts_utc TIMESTAMPTZ NOT NULL,
                          level TEXT NOT NULL,
                          category TEXT NOT NULL,
                          message TEXT NOT NULL,
                          gateway_id TEXT NULL,
                          gateway_name TEXT NULL,
                          device_name TEXT NULL,
                          database_name TEXT NULL,
                          created_utc TIMESTAMPTZ NULL
                        )
                        """
                    )
                )
                conn.execute(text(f'ALTER TABLE "{schema}"."app_logs" ADD COLUMN IF NOT EXISTS local_id BIGINT'))
                conn.execute(text(f'ALTER TABLE "{schema}"."app_logs" ADD COLUMN IF NOT EXISTS tenant_id TEXT'))
                conn.execute(
                    text(
                        f'CREATE UNIQUE INDEX IF NOT EXISTS "ux_logs_tenant_gateway_local_id" ON "{schema}"."app_logs"(tenant_id, gateway_id, local_id)'
                    )
                )
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_logs_tenant_ts" ON "{schema}"."app_logs"(tenant_id, ts_utc DESC)'))

                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."live_latest" (
                          tenant_id TEXT NOT NULL DEFAULT 'default',
                          gateway_id TEXT NOT NULL DEFAULT '',
                          tag_name TEXT NOT NULL,
                          ts_utc TIMESTAMPTZ NOT NULL,
                          source TEXT NULL,
                          gateway_name TEXT NULL,
                          device_name TEXT NULL,
                          plc_ip TEXT NULL,
                          database_name TEXT NULL,
                          value DOUBLE PRECISION NULL,
                          quality INTEGER NULL,
                          quality_label TEXT NULL,
                          updated_utc TIMESTAMPTZ NULL,
                          PRIMARY KEY(tenant_id, gateway_id, tag_name)
                        )
                        """
                    )
                )
                conn.execute(text(f'ALTER TABLE "{schema}"."live_latest" ADD COLUMN IF NOT EXISTS tenant_id TEXT'))
                try:
                    conn.execute(text(f'ALTER TABLE "{schema}"."live_latest" DROP CONSTRAINT IF EXISTS live_latest_pkey'))
                except Exception:
                    pass
                try:
                    conn.execute(text(f'ALTER TABLE "{schema}"."live_latest" ADD PRIMARY KEY (tenant_id, gateway_id, tag_name)'))
                except Exception:
                    pass
                conn.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS "ux_live_latest_tenant_gateway_tag" ON "{schema}"."live_latest"(tenant_id, gateway_id, tag_name)'))
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_live_latest_ts" ON "{schema}"."live_latest"(ts_utc DESC)'))
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_live_latest_tenant_ts" ON "{schema}"."live_latest"(tenant_id, ts_utc DESC)'))
            self._cloud_schema_ready_keys.add(target_key)

    def _resolve_db_path(self) -> str:
        env_path = os.environ.get("TRUSTNODE_APP_STORE_PATH", "").strip()
        if env_path:
            base = env_path
        else:
            data_dir = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
            if not data_dir:
                data_dir = os.path.join(os.path.expanduser("~"), ".trustnode_edge", "data")
            os.makedirs(data_dir, exist_ok=True)
            base = os.path.join(data_dir, "trustnode_app_store.db")
        base = os.path.abspath(base)
        os.makedirs(os.path.dirname(base), exist_ok=True)
        return base

    def _get_backup_dir(self) -> str:
        backup_dir = os.path.join(os.path.dirname(self._db_path), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        return backup_dir

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def _normalize_utc_filter(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            iso = text.replace("Z", "+00:00")
            if " " in iso and "T" not in iso:
                iso = iso.replace(" ", "T")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            # Operator 2026-06-16: ts_utc column stores ISO format
            # with `T` separator + microseconds + +00:00, e.g.
            # "2026-06-16T09:06:35.270897+00:00". Returning the
            # legacy "YYYY-MM-DD HH:MM:SS" format made lexicographic
            # comparisons against ISO rows wrong (every row's `T`
            # > the filter's space, so `ts_utc <= :to_utc` excluded
            # everything). Emit ISO matching the column shape.
            return dt.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
        except Exception:
            return ""

    def _hash_password_if_needed(self, password_value: Any) -> str:
        raw = str(password_value or "")
        if raw.startswith("pbkdf2_sha256$"):
            return raw
        salt = secrets.token_bytes(16)
        iterations = 120_000
        digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, iterations)
        salt_b64 = json.dumps(salt.hex()).strip('"')
        dig_b64 = json.dumps(digest.hex()).strip('"')
        return f"pbkdf2_sha256${iterations}${salt_b64}${dig_b64}"

    def _normalize_users_access_payload(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        users = payload.get("users")
        if not isinstance(users, list):
            return payload
        out_users = []
        for u in users:
            if not isinstance(u, dict):
                continue
            row = dict(u)
            row["password"] = self._hash_password_if_needed(u.get("password"))
            out_users.append(row)
        out = dict(payload)
        out["users"] = out_users
        return out

    def _normalize_database_configurations_payload(self, payload: Any, previous_payload: Any) -> Any:
        incoming = payload if isinstance(payload, list) else []
        previous = previous_payload if isinstance(previous_payload, list) else []
        prev_by_id: Dict[str, Dict[str, Any]] = {}
        for item in previous:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").strip()
            if item_id:
                prev_by_id[item_id] = item

        out: list[Any] = []
        for item in incoming:
            if not isinstance(item, dict):
                out.append(item)
                continue
            next_item = dict(item)
            item_id = str(next_item.get("id") or "").strip()
            prev_item = prev_by_id.get(item_id) if item_id else None
            if isinstance(prev_item, dict):
                # Preserve existing connection fields when stale/partial clients
                # save a row without sensitive fields (for example password).
                for key in ("host", "port", "username", "password", "database", "schema", "table", "tls"):
                    prev_val = prev_item.get(key)
                    next_val = next_item.get(key)
                    if prev_val not in (None, "") and next_val in (None, ""):
                        next_item[key] = prev_val
                prev_engine = str(prev_item.get("engine") or "").strip().lower()
                prev_host = str(prev_item.get("host") or "").strip().lower()
                prev_port = int(prev_item.get("port") or 0)
                next_host = str(next_item.get("host") or "").strip().lower()
                next_port = int(next_item.get("port") or 0)
                # Guard against stale clients reverting known-good direct Supabase
                # credentials back to pooler host/port.
                prev_is_direct_supabase = (
                    prev_engine == "postgresql"
                    and prev_host.startswith("db.")
                    and prev_host.endswith(".supabase.co")
                    and prev_port == 5432
                )
                next_is_pooler_supabase = (
                    next_host.endswith(".pooler.supabase.com")
                    and next_port in (5432, 6543)
                )
                if prev_is_direct_supabase and next_is_pooler_supabase:
                    for key in ("host", "port", "username", "password", "database", "schema", "table", "tls"):
                        if key in prev_item:
                            next_item[key] = prev_item.get(key)
            out.append(next_item)
        return out

    def _normalize_app_settings_payload(self, payload: Any, previous_payload: Any) -> Any:
        next_payload = payload if isinstance(payload, dict) else {}
        prev_payload = previous_payload if isinstance(previous_payload, dict) else {}

        # App settings writes are often partial patches from UI.
        # Merge with previous payload to avoid dropping activation/link fields
        # (edge_id/customer_id/license_id/tenant scope) on unrelated saves.
        out: Dict[str, Any] = dict(prev_payload)
        for key, value in dict(next_payload).items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                merged_child = dict(out.get(key) or {})
                merged_child.update(value)
                out[key] = merged_child
            else:
                out[key] = value

        for key in ("cloud_url", "cloud_api_url", "endpoint_mode"):
            prev_val = prev_payload.get(key)
            next_val = out.get(key)
            if prev_val not in (None, "") and next_val in (None, ""):
                out[key] = prev_val
        if "cloud_auto_sync_enabled" not in out and "cloud_auto_sync_enabled" in prev_payload:
            out["cloud_auto_sync_enabled"] = bool(prev_payload.get("cloud_auto_sync_enabled"))
        return out

    def _build_pg_sqlalchemy_url(self, host: str, port: int, database: str, username: str, password: str) -> str:
        user = quote_plus(username or "")
        pwd = quote_plus(password or "")
        db = quote_plus(database or "postgres")
        return f"postgresql+psycopg://{user}:{pwd}@{host}:{port}/{db}"

    def _backfill_outbox_for_existing_domains(self) -> None:
        now = self._utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sync_outbox(domain, entity_key, payload_json, status, retries, last_error, created_utc, updated_utc)
                    SELECT c.domain, c.domain, c.payload_json, 'pending', 0, NULL, ?, ?
                    FROM config_documents c
                    WHERE NOT EXISTS (SELECT 1 FROM sync_outbox s WHERE s.domain = c.domain)
                    """,
                    (now, now),
                )

    def _upsert_sync_target_state(
        self,
        *,
        enabled: bool,
        config: Dict[str, Any] | None = None,
        last_sync_utc: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = self._utc_now()
        cfg_json = json.dumps(config or {})
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sync_targets(id, name, target_type, config_json, enabled, last_sync_utc, last_error, updated_utc)
                    VALUES('auto_cloud_config', 'Automatic Cloud Config Sync', 'postgresql', ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      config_json = excluded.config_json,
                      enabled = excluded.enabled,
                      last_sync_utc = excluded.last_sync_utc,
                      last_error = excluded.last_error,
                      updated_utc = excluded.updated_utc
                    """,
                    (cfg_json, 1 if enabled else 0, last_sync_utc, last_error, now),
                )

    def _invalidate_cloud_target_cache(self) -> None:
        with self._cloud_target_cache_lock:
            self._cloud_target_cache_value = None
            self._cloud_target_cache_ts = 0.0

    def _seed_and_reconcile_report_templates_once(self) -> None:
        """Boot-time helper: install the demo templates for the locally-
        active tenant if missing, then push every report_templates row to
        Supabase so the Lite app sees them.

        Runs on a daemon thread because app.state.reports_store is created
        AFTER app_store during module import — importing it synchronously
        here would form a cycle. The seed work doesn't need to block boot.
        """
        def _do_seed() -> None:
            try:
                # state module finishes initialising shortly after
                # app_store; a short sleep avoids racing the singleton
                # creation. We don't busy-wait because import order is
                # deterministic — 0.5s is comfortably enough.
                import time as _t
                _t.sleep(0.5)
                from app.state import reports_store as _rs  # late import
            except Exception:
                return
            settings = {}
            try:
                settings = self._get_app_settings()
            except Exception:
                settings = {}
            tenant_id = str(settings.get("tenant_id") or "").strip() or "default"
            try:
                _rs.seed_demo_templates(tenant_id=tenant_id, created_by="system_demo_seed")
            except Exception:
                pass
            try:
                _rs.reconcile_templates_to_cloud()
            except Exception:
                pass
        try:
            threading.Thread(target=_do_seed, name="tn-demo-templates-seed", daemon=True).start()
        except Exception:
            pass

    def _reconcile_sync_targets_with_config(self) -> None:
        """Align the sync_targets row with whatever database_configurations
        currently resolves to. Two failure modes this fixes:

        1. A previous boot recorded `last_error="No enabled PostgreSQL cloud
           target configured"` and `enabled=0`. The UI later saved a valid
           cloud DB row, but `_set_data_sync_state` never cleared the
           sync_targets row, so the worker kept short-circuiting.
        2. The UI list-DBs view and the sync worker read different sources
           (UI may show ENABLED while sync_targets.enabled=0).

        Cheap and idempotent. Safe to call from startup and from the sync
        loop every few seconds.
        """
        # Force a fresh read — caller is recovering from a possibly-stale
        # cache state.
        self._invalidate_cloud_target_cache()
        try:
            cloud = self._get_cloud_database_target()
        except Exception:
            cloud = None
        if cloud:
            self._upsert_sync_target_state(
                enabled=True,
                config={
                    "name": str(cloud.get("name") or ""),
                    "host": str(cloud.get("host") or ""),
                    "schema": str(cloud.get("schema") or "public"),
                },
                last_error="",
            )
        else:
            self._upsert_sync_target_state(
                enabled=False,
                config={},
                last_error="No enabled PostgreSQL cloud target configured",
            )

    def _mirror_config_doc_to_customer_db(
        self,
        *,
        domain: str,
        scope_key: str,
        payload_json: str,
        version: int,
        updated_utc: str,
        actor: str = "system",
    ) -> None:
        """Mirror a config document into the customer Postgres.

        Operator 2026-06-17 (M5): every config domain (dashboards,
        alarms, gateway configs, power_management_config, users_access,
        triggers_limits, app_settings, etc.) lands in
        customer DB ``config_documents`` / ``config_documents_scoped``
        so the LAN Lite has a single read source for everything that
        is NOT timeseries data.

        Best-effort: failures never block the local save. If
        ``database_mode`` is not ``customer_sql`` the call is a
        no-op so this hook is cheap on default installs.
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM config_documents WHERE domain = ?",
                    ("app_settings",),
                ).fetchone()
        except Exception:
            return
        if not row:
            return
        try:
            settings = json.loads(str(row["payload_json"] or "{}")) or {}
        except Exception:
            return
        if str(settings.get("database_mode") or "local_sqlite").lower() != "customer_sql":
            return
        target = settings.get("customer_sql_target")
        if not isinstance(target, dict) or not target.get("host"):
            return
        try:
            from app.services import customer_sql as _cs
            from app.services import sinks_sql as _ss
            from sqlalchemy import text as _text
        except Exception:
            return
        engine, _err = _cs.get_engine(target)
        if engine is None:
            return
        try:
            _ss.bootstrap_customer_db(engine, schema=str(target.get("schema") or "public"), note="config-mirror")
        except Exception:
            return
        s = str(target.get("schema") or "public") or "public"
        tenant = self._current_tenant_id()
        try:
            if scope_key:
                sql = _text(
                    f'INSERT INTO "{s}"."config_documents_scoped" '
                    f'(domain, scope_key, tenant_id, version, payload_json, updated_utc, updated_by) '
                    f'VALUES (:domain, :scope_key, :tenant, :version, CAST(:payload AS JSONB), :updated_utc, :actor) '
                    f'ON CONFLICT (tenant_id, scope_key, domain) DO UPDATE SET '
                    f'  version = EXCLUDED.version, '
                    f'  payload_json = EXCLUDED.payload_json, '
                    f'  updated_utc = EXCLUDED.updated_utc, '
                    f'  updated_by = EXCLUDED.updated_by'
                )
                params = {
                    "domain": domain,
                    "scope_key": scope_key,
                    "tenant": tenant,
                    "version": int(version or 1),
                    "payload": payload_json or "null",
                    "updated_utc": updated_utc,
                    "actor": actor,
                }
            else:
                sql = _text(
                    f'INSERT INTO "{s}"."config_documents" '
                    f'(domain, tenant_id, version, payload_json, updated_utc, updated_by) '
                    f'VALUES (:domain, :tenant, :version, CAST(:payload AS JSONB), :updated_utc, :actor) '
                    f'ON CONFLICT (tenant_id, domain) DO UPDATE SET '
                    f'  version = EXCLUDED.version, '
                    f'  payload_json = EXCLUDED.payload_json, '
                    f'  updated_utc = EXCLUDED.updated_utc, '
                    f'  updated_by = EXCLUDED.updated_by'
                )
                params = {
                    "domain": domain,
                    "tenant": tenant,
                    "version": int(version or 1),
                    "payload": payload_json or "null",
                    "updated_utc": updated_utc,
                    "actor": actor,
                }
            with engine.begin() as conn:
                conn.execute(sql, params)
        except Exception:
            return

    def _mirror_config_doc_to_cloud(
        self,
        table_name: str,
        *,
        tenant_id: str,
        scope_key: str,
        payload_json: str,
        version: int,
        updated_utc: str,
    ) -> None:
        """Best-effort generic upsert into a (tenant_id, scope_key, payload_json,
        version, updated_utc) mirror table on Supabase. Used by both
        dashboard_configurations and alarms_setup so the Lite app can read
        them via RLS. Silent on any failure — never block the local save.

        IMPORTANT: dispatches to a background thread. Synchronous mirroring
        was blocking the bootstrap-save HTTP request for the entire round-trip
        to Supabase (often 200–800 ms), and because the edge frontend re-saves
        the bootstrap every time `alarms` changes (alarms fire on every poll
        cycle while active), the request thread serialized into a queue that
        starved live-data and gateway-control endpoints. The mirror itself
        is non-essential to the local save, so it never has to block.
        """
        # Cheap pre-check on the request thread — avoid spawning a thread
        # at all when no cloud target is configured.
        try:
            cloud = self._get_cloud_database_target()
        except Exception:
            cloud = None
        if not cloud:
            return

        # Snapshot the args (small, immutable) and run the upsert off-thread.
        kwargs = dict(
            tenant_id=tenant_id,
            scope_key=scope_key or "",
            payload_json=payload_json,
            version=int(version),
            updated_utc=updated_utc,
        )

        # Track the most recent mirror error per table so the operator can
        # see what's wrong from a diagnostic endpoint. Background threads
        # were swallowing every exception before this, which is why the
        # operator stared at an empty cloud table for a week without
        # knowing the upsert was being rejected.
        mirror_state = getattr(self, "_mirror_state", None)
        if mirror_state is None:
            mirror_state = {"last_error": {}, "last_success_utc": {}, "attempts": {}}
            setattr(self, "_mirror_state", mirror_state)

        def _do_upsert() -> None:
            import logging
            log = logging.getLogger("trustnode.mirror")
            mirror_state["attempts"][table_name] = mirror_state["attempts"].get(table_name, 0) + 1
            try:
                from sqlalchemy import text  # type: ignore
                engine, _ = self._get_or_create_cloud_engine(
                    cloud, str(cloud.get("schema") or "public")
                )
                schema = str(cloud.get("schema") or "public")
                # Auto-provision the mirror table the first time we write to
                # it from this process. Lets an edge come online with a fresh
                # Supabase project without requiring the DBA to run every
                # migration up-front. Cheap idempotent DDL; cached in a per-
                # process set so subsequent writes skip it.
                ensured_set = getattr(self, "_mirror_tables_ensured", None)
                if ensured_set is None:
                    ensured_set = set()
                    setattr(self, "_mirror_tables_ensured", ensured_set)
                if table_name not in ensured_set:
                    try:
                        with engine.begin() as conn:
                            conn.execute(
                                text(
                                    f'CREATE TABLE IF NOT EXISTS "{schema}"."{table_name}" ('
                                    f"  tenant_id    text NOT NULL,"
                                    f"  scope_key    text NOT NULL DEFAULT '',"
                                    f"  payload_json jsonb NOT NULL DEFAULT '[]'::jsonb,"
                                    f"  version      integer NOT NULL DEFAULT 1,"
                                    f"  updated_utc  timestamptz NOT NULL DEFAULT now(),"
                                    f"  PRIMARY KEY (tenant_id, scope_key)"
                                    f")"
                                )
                            )
                        ensured_set.add(table_name)
                    except Exception as ddl_exc:
                        log.warning("mirror %s: table-ensure failed: %s", table_name, ddl_exc)
                        # If we can't create it (e.g. permissions), let the
                        # upsert below try anyway — the DBA may have created
                        # it manually with a different shape we can still
                        # write to.
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."{table_name}"
                              (tenant_id, scope_key, payload_json, version, updated_utc)
                            VALUES (:tenant_id, :scope_key, CAST(:payload_json AS jsonb), :version, :updated_utc)
                            ON CONFLICT (tenant_id, scope_key) DO UPDATE SET
                              payload_json = EXCLUDED.payload_json,
                              version      = EXCLUDED.version,
                              updated_utc  = EXCLUDED.updated_utc
                            """
                        ),
                        kwargs,
                    )
                mirror_state["last_success_utc"][table_name] = self._utc_now()
                mirror_state["last_error"].pop(table_name, None)
                log.info("mirror %s: ok tenant=%s scope=%s", table_name,
                         kwargs.get("tenant_id"), kwargs.get("scope_key"))
            except Exception as exc:
                mirror_state["last_error"][table_name] = f"{type(exc).__name__}: {exc}"
                log.warning("mirror %s: upsert failed tenant=%s scope=%s err=%s",
                            table_name, kwargs.get("tenant_id"), kwargs.get("scope_key"), exc)

        try:
            import threading
            threading.Thread(target=_do_upsert, name=f"tn-mirror-{table_name}",
                             daemon=True).start()
        except Exception:
            pass

    # Backwards-compatible wrapper used by the existing call sites.
    def _mirror_dashboard_configurations_to_cloud(self, **kwargs) -> None:
        self._mirror_config_doc_to_cloud("dashboard_configurations", **kwargs)

    def _repair_scope_keys_with_customer_id(self) -> None:
        """One-time repair: scoped docs saved when customer_id was missing
        from app_settings ended up with a scope_key shaped 'tenant|-|edge[|user]'.
        Once customer_id is populated, rewrite those rows to the correct
        'tenant|customer|edge[|user]' shape so Lite's customer view finds them.

        Only repairs rows where the customer segment is literally '-'. Safe
        to run on every force_sync_now() — it's a no-op once repaired.
        """
        settings = self._get_app_settings()
        customer_id = str(settings.get("customer_id") or "").strip().lower()
        if not customer_id:
            return
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT scope_key, domain FROM config_documents_scoped"
                ).fetchall()
                for r in rows or []:
                    old_key = str(r["scope_key"] or "")
                    domain = str(r["domain"] or "")
                    parts = old_key.split("|")
                    if len(parts) < 3 or parts[1] != "-":
                        continue
                    parts[1] = customer_id
                    new_key = "|".join(parts)
                    if new_key == old_key:
                        continue
                    # If a row already exists at the repaired key, drop the
                    # old one (the new one is canonical). Otherwise rename.
                    exists = conn.execute(
                        "SELECT 1 FROM config_documents_scoped WHERE scope_key=? AND domain=?",
                        (new_key, domain),
                    ).fetchone()
                    if exists:
                        conn.execute(
                            "DELETE FROM config_documents_scoped WHERE scope_key=? AND domain=?",
                            (old_key, domain),
                        )
                    else:
                        conn.execute(
                            "UPDATE config_documents_scoped SET scope_key=? WHERE scope_key=? AND domain=?",
                            (new_key, old_key, domain),
                        )

    def _boot_remirror_scoped_docs_safe(self) -> None:
        """Background task: wait for the cloud target to be resolvable,
        then republish every Lite-visible scoped doc. Lets a freshly
        upgraded edge backfill dashboards/alarms/triggers/gateways/
        devices without the operator having to save them again.

        The previous edge_app build only mirrored on save, so an edge
        that ran for weeks without a config change had nothing in cloud
        even though it was happily publishing live_latest. This boot
        pulse closes that gap.
        """
        try:
            # Wait up to ~30 s for the cloud target + sync_targets row to
            # come up. The sync worker hasn't booted yet on this thread,
            # so we just poll _get_cloud_database_target.
            import time as _t
            import logging
            log = logging.getLogger("trustnode.boot-mirror")
            cloud = None
            for _ in range(30):
                try:
                    cloud = self._get_cloud_database_target()
                    if cloud is not None:
                        break
                except Exception:
                    pass
                _t.sleep(1.0)
            if cloud is None:
                # No cloud target configured locally — dashboards / alarms
                # will only ever be local. Log this so the operator can see
                # WHY the Lite view isn't picking them up.
                log.warning(
                    "boot-mirror: no cloud database target resolvable after 30 s; "
                    "Lite view will not receive dashboards or configs until a "
                    "postgresql sink with cloud_sync_enabled=true is configured."
                )
                return
            # Count what we're about to push so the log line is actionable.
            try:
                with self._lock:
                    with self._connect() as conn:
                        rows = conn.execute(
                            "SELECT domain, COUNT(*) AS n FROM config_documents_scoped "
                            "WHERE domain IN "
                            "('dashboard_configurations','alarms_setup','triggers_limits',"
                            " 'gateway_configurations','devices') "
                            "GROUP BY domain"
                        ).fetchall()
                summary = ", ".join(
                    f"{r['domain']}={int(r['n'])}" for r in (rows or [])
                ) or "(no scoped docs to mirror)"
                log.info(
                    "boot-mirror: cloud target reachable (%s:%s/%s); republishing %s",
                    cloud.get("host"), cloud.get("port"), cloud.get("database"), summary,
                )
            except Exception:
                pass
            self._remirror_scoped_docs_to_cloud()
            log.info("boot-mirror: republish pass complete.")
        except Exception:
            # Best-effort; the periodic reconciler will catch anything we miss.
            return

    def _remirror_scoped_docs_to_cloud(self) -> None:
        """Walk every config_documents_scoped row whose domain is mirrored to
        Supabase and re-publish it. Called from force_sync_now() so the
        operator's manual "push sync" actually re-uploads existing dashboards/
        alarms/triggers — not only the ones edited since boot.

        Synchronous on this thread (push sync is a foreground operator action)
        but each row is upserted in its own short transaction so a single
        failure doesn't abort the batch.
        """
        mirrored_domains = (
            "dashboard_configurations",
            "alarms_setup",
            "triggers_limits",
            "gateway_configurations",
            "devices",
            # Operator 2026-06-16: cloud Lite needs tariff config,
            # meter list and downtime rules to render Power widgets.
            "power_management_config",
        )
        cloud = self._get_cloud_database_target()
        if not cloud:
            return
        try:
            from sqlalchemy import text  # type: ignore
        except Exception:
            return
        schema = str(cloud.get("schema") or "public")
        engine, _ = self._get_or_create_cloud_engine(cloud, schema)
        placeholders = ",".join("?" * len(mirrored_domains))
        with self._lock:
            with self._connect() as conn:
                rows = list(conn.execute(
                    f"SELECT scope_key, domain, payload_json, version, updated_utc "
                    f"FROM config_documents_scoped WHERE domain IN ({placeholders})",
                    mirrored_domains,
                ).fetchall())
                # ─── Fallback: pick up unscoped configs too ───────────────────
                # Older edge builds (or any path that hit upsert_domain instead
                # of upsert_domain_scoped) wrote dashboards / alarms / triggers
                # / gateways / devices into config_documents WITHOUT a scope
                # key. The Lite mirror only reads scoped rows, so those edges
                # stayed invisible. Build a synthetic scope_key from
                # app_settings (tenant | customer | edge) so the unscoped doc
                # can finally land in cloud. If no edge_id is known we skip —
                # there's nothing meaningful to push a config under.
                covered_domains = {str(r["domain"] or "") for r in rows}
                missing_domains = [d for d in mirrored_domains if d not in covered_domains]
                if missing_domains:
                    try:
                        settings_row = conn.execute(
                            "SELECT payload_json FROM config_documents WHERE domain='app_settings'"
                        ).fetchone()
                        settings = {}
                        if settings_row and settings_row["payload_json"]:
                            settings = json.loads(str(settings_row["payload_json"] or "{}")) or {}
                        edge_profile = settings.get("edge_profile") if isinstance(settings.get("edge_profile"), dict) else {}
                        edge_id = (
                            str(edge_profile.get("edge_id") or "").strip().lower()
                            or str(settings.get("edge_id") or "").strip().lower()
                            or str(getattr(self, "_local_edge_id", "") or "").strip().lower()
                        )
                        tenant_id = str(settings.get("tenant_id") or "default").strip().lower() or "default"
                        customer_id = (
                            str(edge_profile.get("linked_customer_id") or "").strip().lower()
                            or str(settings.get("customer_id") or "").strip().lower()
                            or "-"
                        )
                        synthetic_scope = f"{tenant_id}|{customer_id}|{edge_id}" if edge_id else ""
                        if synthetic_scope:
                            qmark = ",".join("?" * len(missing_domains))
                            unscoped_rows = conn.execute(
                                f"SELECT domain, payload_json, version, updated_utc "
                                f"FROM config_documents WHERE domain IN ({qmark})",
                                missing_domains,
                            ).fetchall()
                            for ur in unscoped_rows or []:
                                rows.append({
                                    "scope_key": synthetic_scope,
                                    "domain": ur["domain"],
                                    "payload_json": ur["payload_json"],
                                    "version": ur["version"],
                                    "updated_utc": ur["updated_utc"],
                                })
                    except Exception:
                        # Best-effort fallback; the upgraded edge will start
                        # producing scoped rows once the operator saves once.
                        pass
        # Mirror-state tracking so a failed batch shows up in the diagnostic
        # endpoint exactly like the daemon-thread mirror does. Without this
        # a boot-remirror failure was invisible.
        mirror_state = getattr(self, "_mirror_state", None)
        if mirror_state is None:
            mirror_state = {"last_error": {}, "last_success_utc": {}, "attempts": {}}
            setattr(self, "_mirror_state", mirror_state)
        import logging
        log = logging.getLogger("trustnode.boot-mirror")
        batch_ok = 0
        batch_err = 0
        for r in rows or []:
            scope_key = str(r["scope_key"] or "")
            domain = str(r["domain"] or "")
            payload_json = str(r["payload_json"] or "null")
            version = int(r["version"] or 1)
            updated_utc = str(r["updated_utc"] or self._utc_now())
            tenant_from_scope = (scope_key.split("|") or ["default"])[0] or "default"
            mirror_state["attempts"][domain] = mirror_state["attempts"].get(domain, 0) + 1
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."{domain}"
                              (tenant_id, scope_key, payload_json, version, updated_utc)
                            VALUES (:tenant_id, :scope_key, CAST(:payload_json AS jsonb), :version, :updated_utc)
                            ON CONFLICT (tenant_id, scope_key) DO UPDATE SET
                              payload_json = EXCLUDED.payload_json,
                              version      = EXCLUDED.version,
                              updated_utc  = EXCLUDED.updated_utc
                            """
                        ),
                        {
                            "tenant_id": tenant_from_scope,
                            "scope_key": scope_key,
                            "payload_json": payload_json,
                            "version": version,
                            "updated_utc": updated_utc,
                        },
                    )
                mirror_state["last_success_utc"][domain] = self._utc_now()
                mirror_state["last_error"].pop(domain, None)
                batch_ok += 1
            except Exception as exc:
                mirror_state["last_error"][domain] = f"{type(exc).__name__}: {exc}"
                log.warning("boot-mirror upsert failed domain=%s scope=%s tenant=%s err=%s",
                            domain, scope_key, tenant_from_scope, exc)
                batch_err += 1
                continue
        log.info("boot-mirror batch complete: ok=%d err=%d (total_rows=%d)",
                 batch_ok, batch_err, len(rows or []))

    def _cloud_target_from_env(self) -> Dict[str, Any] | None:
        """Build a cloud target dict purely from environment variables.

        Cloud-only deployments (the VPS) never go through the desktop UI and
        therefore never get a `database_configurations` row written to their
        local SQLite. Without this fallback the VPS silently fell through to
        its empty local historian and served stale test data instead of
        Supabase, regardless of TRUSTNODE_PREFER_CLOUD_READS=true.

        Returns None when the required keys aren't present so callers keep
        their existing branch behavior on the edge (which DOES configure the
        target through the UI / config doc).
        """
        host = str(os.environ.get("TRUSTNODE_CLOUD_DB_HOST", "") or "").strip()
        user = str(os.environ.get("TRUSTNODE_CLOUD_DB_USER", "") or "").strip()
        password = str(os.environ.get("TRUSTNODE_CLOUD_DB_PASSWORD", "") or "")
        if not (host and user and password):
            return None
        try:
            port = int(os.environ.get("TRUSTNODE_CLOUD_DB_PORT", "5432") or "5432")
        except Exception:
            port = 5432
        database = str(os.environ.get("TRUSTNODE_CLOUD_DB_NAME", "postgres") or "postgres").strip() or "postgres"
        schema = str(os.environ.get("TRUSTNODE_CLOUD_DB_SCHEMA", "public") or "public").strip() or "public"
        sslmode = str(os.environ.get("TRUSTNODE_CLOUD_DB_SSLMODE", "require") or "require").strip().lower()
        return {
            "id": "env",
            "name": "Cloud DB (env)",
            "engine": "postgresql",
            "host": host,
            "port": port,
            "database": database,
            "username": user,
            "password": password,
            "schema": schema,
            "tls": sslmode != "disable",
        }

    def _get_cloud_database_target(self) -> Dict[str, Any] | None:
        # Cache the resolved target for a few seconds. The function reads two
        # SQL tables under the global lock; calling it on every API request
        # serializes the whole backend under load.
        now = time.time()
        with self._cloud_target_cache_lock:
            if self._cloud_target_cache_ts > 0 and (now - self._cloud_target_cache_ts) < self._cloud_target_cache_ttl:
                cached = self._cloud_target_cache_value
                if cached is not None:
                    return dict(cached)
                # Negative result was cached — return None without re-hitting DB.
                return None
        # Walk both the unscoped doc and every scoped doc — the sync worker
        # has no request context to pick a scope key, so it must consider all
        # writers. The UI always saves under the active scope, so the unscoped
        # doc is often empty/stale.
        payloads: list[list] = []
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM config_documents WHERE domain = ?",
                    ("database_configurations",),
                ).fetchone()
                if row:
                    try:
                        p = json.loads(str(row["payload_json"] or "[]"))
                        if isinstance(p, list):
                            payloads.append(p)
                    except Exception:
                        pass
                try:
                    scoped_rows = conn.execute(
                        "SELECT payload_json, updated_utc FROM config_documents_scoped WHERE domain = ? ORDER BY updated_utc DESC",
                        ("database_configurations",),
                    ).fetchall()
                except Exception:
                    scoped_rows = []
                for srow in scoped_rows:
                    try:
                        p = json.loads(str(srow["payload_json"] or "[]"))
                        if isinstance(p, list):
                            payloads.append(p)
                    except Exception:
                        continue
        if not payloads:
            # Cloud deployment path: no UI ever wrote a DB config row to the
            # local SQLite. Fall back to env vars before giving up.
            env_target = self._cloud_target_from_env()
            with self._cloud_target_cache_lock:
                self._cloud_target_cache_value = dict(env_target) if env_target else None
                self._cloud_target_cache_ts = now
            return env_target

        def _is_enabled(item: Dict[str, Any]) -> bool:
            if "enabled" in item:
                return bool(item.get("enabled"))
            return True

        merged: list[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for payload in payloads:
            for item in payload:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("id") or "")
                if key and key in seen_ids:
                    continue
                if key:
                    seen_ids.add(key)
                merged.append(item)

        candidates: list[Dict[str, Any]] = []
        for item in merged:
            if not isinstance(item, dict):
                continue
            engine = str(item.get("engine") or "").strip().lower()
            if engine != "postgresql":
                continue
            if not _is_enabled(item):
                continue
            if bool(item.get("cloud_sync_enabled", False)) is False and "cloud_sync_enabled" in item:
                continue
            host = str(item.get("host") or "").strip()
            username = str(item.get("username") or "").strip()
            password = str(item.get("password") or "")
            port = int(item.get("port") or 5432)
            database = str(item.get("database") or "postgres").strip() or "postgres"
            schema = str(item.get("schema") or "public").strip() or "public"
            if not host or not username or not password:
                continue
            candidates.append(
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "engine": "postgresql",
                    "host": host,
                    "port": port,
                    "database": database,
                    "username": username,
                    "password": password,
                    "schema": schema,
                    "tls": bool(item.get("tls", True)),
                }
            )
        if not candidates:
            env_target = self._cloud_target_from_env()
            with self._cloud_target_cache_lock:
                self._cloud_target_cache_value = dict(env_target) if env_target else None
                self._cloud_target_cache_ts = now
            return env_target
        supabase = [c for c in candidates if "supabase.co" in c["host"].lower()]
        chosen = supabase[0] if supabase else candidates[0]
        with self._cloud_target_cache_lock:
            self._cloud_target_cache_value = dict(chosen)
            self._cloud_target_cache_ts = now
        return chosen

    def _get_app_settings(self) -> Dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM config_documents WHERE domain = ?",
                    ("app_settings",),
                ).fetchone()
        if not row:
            return {}
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _is_cloud_auto_sync_enabled(self) -> bool:
        settings = self._get_app_settings()
        if "cloud_auto_sync_enabled" in settings:
            return bool(settings.get("cloud_auto_sync_enabled"))
        return True

    # Runtime-control flags that the background sync worker reads from the
    # unscoped app_settings doc. The UI saves these into a user-scoped doc,
    # so the scoped writer mirrors them into the unscoped doc to keep the
    # two views in sync without disturbing UI-only preferences (theme,
    # palette, ui-source mode, etc.).
    _RUNTIME_FLAGS_MIRRORED = (
        "cloud_auto_sync_enabled",
        "endpoint_mode",
        "cloud_url",
        "tenant_login_realm",
        "tenant_web_client_url",
    )

    def _mirror_runtime_flags_to_unscoped_app_settings(self, scoped_payload: Any) -> None:
        if not isinstance(scoped_payload, dict):
            return
        diff: Dict[str, Any] = {}
        for k in self._RUNTIME_FLAGS_MIRRORED:
            if k in scoped_payload:
                diff[k] = scoped_payload.get(k)
        if not diff:
            return
        # Read unscoped, merge, write back only if changed.
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT version, payload_json FROM config_documents WHERE domain = ?",
                    ("app_settings",),
                ).fetchone()
                current: Dict[str, Any] = {}
                old_version = 0
                if row:
                    old_version = int(row["version"] or 0)
                    try:
                        loaded = json.loads(str(row["payload_json"] or "{}"))
                        if isinstance(loaded, dict):
                            current = loaded
                    except Exception:
                        current = {}
                changed = False
                for k, v in diff.items():
                    if current.get(k) != v:
                        current[k] = v
                        changed = True
                if not changed:
                    return
                payload_json = self._canonical_json(current)
                now = self._utc_now()
                new_version = old_version + 1
                conn.execute(
                    """
                    INSERT INTO config_documents(domain, payload_json, version, updated_utc)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(domain) DO UPDATE SET
                      payload_json = excluded.payload_json,
                      version = excluded.version,
                      updated_utc = excluded.updated_utc
                    """,
                    ("app_settings", payload_json, new_version, now),
                )
                conn.execute(
                    """
                    INSERT INTO config_audit(domain, actor, old_version, new_version, changed_utc)
                    VALUES('app_settings', 'runtime_flag_mirror', ?, ?, ?)
                    """,
                    (old_version if old_version > 0 else None, new_version, now),
                )
        # Wake the sync worker so the new flag (often cloud_auto_sync_enabled)
        # takes effect on the next tick instead of after the next interval.
        self._sync_wakeup_event.set()

    def _mark_outbox_row_sent(self, row_id: int, when_utc: str) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE sync_outbox
                    SET status = 'sent', retries = retries, last_error = NULL, updated_utc = ?
                    WHERE id = ?
                    """,
                    (when_utc, int(row_id)),
                )

    def _mark_outbox_row_failed(self, row_id: int, error_message: str) -> None:
        now = self._utc_now()
        safe_error = (error_message or "unknown error")[:800]
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE sync_outbox
                    SET status = 'failed', retries = retries + 1, last_error = ?, updated_utc = ?
                    WHERE id = ?
                    """,
                    (safe_error, now, int(row_id)),
                )

    def _flush_config_outbox_once(self) -> None:
        cloud = self._get_cloud_database_target()
        if not cloud:
            self._upsert_sync_target_state(enabled=False, config={}, last_error="No enabled PostgreSQL cloud target configured")
            return
        tenant_id = self._current_tenant_id()
        tenant_prefix = f"{tenant_id}::"

        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, domain, payload_json, retries
                    FROM sync_outbox
                    WHERE status IN ('pending', 'failed')
                    ORDER BY id ASC
                    LIMIT 50
                    """
                ).fetchall()
        if not rows:
            self._upsert_sync_target_state(enabled=True, config={"name": cloud.get("name"), "host": cloud.get("host")})
            return

        try:
            from sqlalchemy import create_engine, text  # type: ignore
        except Exception as exc:
            self._upsert_sync_target_state(enabled=True, config={"name": cloud.get("name"), "host": cloud.get("host")}, last_error=f"SQLAlchemy unavailable: {exc}")
            return

        schema = str(cloud.get("schema") or "public")
        url = self._build_pg_sqlalchemy_url(
            str(cloud.get("host") or ""),
            int(cloud.get("port") or 5432),
            str(cloud.get("database") or "postgres"),
            str(cloud.get("username") or ""),
            str(cloud.get("password") or ""),
        )
        connect_args = {
            "sslmode": "require" if cloud.get("tls", True) else "disable",
            "connect_timeout": max(1, int(os.environ.get("TRUSTNODE_CLOUD_DB_CONNECT_TIMEOUT_SECONDS", "6") or "6")),
            # Supabase/PgBouncer transaction pooling is incompatible with
            # psycopg auto-prepared statements.
            "prepare_threshold": None,
            "options": os.environ.get(
                "TRUSTNODE_CLOUD_DB_OPTIONS",
                "-c lock_timeout=1200ms -c statement_timeout=4500ms",
            ),
        }
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        try:
            with engine.begin() as conn:
                if schema != "public":
                    conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."config_documents" (
                          domain TEXT PRIMARY KEY,
                          payload_json JSONB NOT NULL,
                          version INTEGER NOT NULL DEFAULT 1,
                          updated_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."config_audit" (
                          id BIGSERIAL PRIMARY KEY,
                          domain TEXT NOT NULL,
                          actor TEXT NULL,
                          old_version INTEGER NULL,
                          new_version INTEGER NOT NULL,
                          changed_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                )
        except Exception as exc:
            self._upsert_sync_target_state(enabled=True, config={"name": cloud.get("name"), "host": cloud.get("host")}, last_error=f"Cloud provisioning failed: {exc}")
            engine.dispose()
            return

        success_count = 0
        for row in rows:
            row_id = int(row["id"])
            domain = str(row["domain"] or "")
            cloud_domain = f"{tenant_prefix}{domain}"
            payload_json = str(row["payload_json"] or "null")
            try:
                with engine.begin() as conn:
                    old_v_row = conn.execute(
                        text(f'SELECT version FROM "{schema}"."config_documents" WHERE domain = :domain'),
                        {"domain": cloud_domain},
                    ).fetchone()
                    old_version = int(old_v_row[0]) if old_v_row else 0
                    new_version = old_version + 1
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."config_documents"(domain, payload_json, version, updated_utc)
                            VALUES(:domain, CAST(:payload_json AS JSONB), :new_version, NOW())
                            ON CONFLICT(domain) DO UPDATE SET
                              payload_json = excluded.payload_json,
                              version = excluded.version,
                              updated_utc = excluded.updated_utc
                            """
                        ),
                        {"domain": cloud_domain, "payload_json": payload_json, "new_version": new_version},
                    )
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."config_audit"(domain, actor, old_version, new_version, changed_utc)
                            VALUES(:domain, 'local_sync', :old_version, :new_version, NOW())
                            """
                        ),
                        {"domain": cloud_domain, "old_version": old_version if old_version > 0 else None, "new_version": new_version},
                    )
                now = self._utc_now()
                self._mark_outbox_row_sent(row_id, now)
                success_count += 1
            except Exception as exc:
                self._mark_outbox_row_failed(row_id, str(exc))

        engine.dispose()
        now = self._utc_now()
        sync_error = None if success_count == len(rows) else f"{len(rows) - success_count} config item(s) failed in last batch"
        self._upsert_sync_target_state(
            enabled=True,
            config={"name": cloud.get("name"), "host": cloud.get("host"), "schema": schema},
            last_sync_utc=now if success_count > 0 else None,
            last_error=sync_error,
        )

    def _pull_config_from_cloud_once(self) -> None:
        cloud = self._get_cloud_database_target()
        if not cloud:
            return
        tenant_id = self._current_tenant_id()
        tenant_prefix = f"{tenant_id}::"
        try:
            from sqlalchemy import create_engine, text  # type: ignore
        except Exception:
            return

        schema = str(cloud.get("schema") or "public")
        url = self._build_pg_sqlalchemy_url(
            str(cloud.get("host") or ""),
            int(cloud.get("port") or 5432),
            str(cloud.get("database") or "postgres"),
            str(cloud.get("username") or ""),
            str(cloud.get("password") or ""),
        )
        connect_args = {
            "sslmode": "require" if cloud.get("tls", True) else "disable",
            "connect_timeout": max(1, int(os.environ.get("TRUSTNODE_CLOUD_DB_CONNECT_TIMEOUT_SECONDS", "6") or "6")),
            "prepare_threshold": None,
            "options": os.environ.get(
                "TRUSTNODE_CLOUD_DB_OPTIONS",
                "-c lock_timeout=1200ms -c statement_timeout=4500ms",
            ),
        }
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        try:
            with engine.begin() as conn:
                rows = conn.execute(
                    text(
                        f"""
                        SELECT domain, payload_json::text AS payload_json_text, version, updated_utc
                        FROM "{schema}"."config_documents"
                        WHERE domain LIKE :prefix
                        ORDER BY domain
                        """
                    ),
                    {"prefix": f"{tenant_prefix}%"},
                ).fetchall()
                # Backward compatibility for legacy global config rows.
                if not rows and tenant_id == "default":
                    rows = conn.execute(
                        text(
                            f"""
                            SELECT domain, payload_json::text AS payload_json_text, version, updated_utc
                            FROM "{schema}"."config_documents"
                            WHERE domain NOT LIKE '%::%'
                            ORDER BY domain
                            """
                        )
                    ).fetchall()
        except Exception:
            engine.dispose()
            return
        finally:
            try:
                engine.dispose()
            except Exception:
                pass

        now = self._utc_now()
        allow_cloud_control_domains = (not self._edge_config_isolation_enabled) or self._prefer_cloud_reads()
        applied = 0
        with self._lock:
            with self._connect() as conn:
                for r in rows or []:
                    raw_domain = str(r[0] or "").strip()
                    domain = raw_domain[len(tenant_prefix):] if raw_domain.startswith(tenant_prefix) else raw_domain
                    if not domain:
                        continue
                    if (not allow_cloud_control_domains) and domain in {
                        "devices",
                        "gateway_configurations",
                        "database_configurations",
                        "power_management_config",
                    }:
                        # Edge runtime keeps local control configs isolated per machine.
                        # Cloud/web mode still pulls these domains for shared web clients.
                        continue
                    payload_text = str(r[1] or "null")
                    remote_version = int(r[2] or 0)
                    remote_updated = str(r[3] or now)

                    def _ts_ms(value: Any) -> int:
                        txt = str(value or "").strip()
                        if not txt:
                            return 0
                        try:
                            return int(datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp() * 1000)
                        except Exception:
                            return 0

                    local = conn.execute(
                        "SELECT version, payload_json, updated_utc FROM config_documents WHERE domain = ?",
                        (domain,),
                    ).fetchone()
                    local_version = int(local["version"] or 0) if local else 0
                    local_payload_text = str(local["payload_json"] or "null") if local else "null"
                    local_updated = str(local["updated_utc"] or "") if local else ""
                    remote_updated_ms = _ts_ms(remote_updated)
                    local_updated_ms = _ts_ms(local_updated)
                    should_apply = (
                        (not local)
                        or (remote_version > local_version)
                        or (remote_updated_ms > local_updated_ms)
                        or (payload_text != local_payload_text and remote_updated_ms >= local_updated_ms)
                    )
                    if not should_apply:
                        continue
                    conn.execute(
                        """
                        INSERT INTO config_documents(domain, payload_json, version, updated_utc)
                        VALUES(?, ?, ?, ?)
                        ON CONFLICT(domain) DO UPDATE SET
                          payload_json = excluded.payload_json,
                          version = excluded.version,
                          updated_utc = excluded.updated_utc
                        """,
                        (domain, payload_text, remote_version, remote_updated),
                    )
                    conn.execute(
                        """
                        INSERT INTO config_audit(domain, actor, old_version, new_version, changed_utc)
                        VALUES(?, ?, ?, ?, ?)
                        """,
                        (domain, "cloud_pull", local_version if local_version > 0 else None, remote_version, now),
                    )
                    applied += 1
        if applied:
            self._upsert_sync_target_state(
                enabled=True,
                config={"name": cloud.get("name"), "host": cloud.get("host"), "schema": schema},
                last_sync_utc=now,
            )

    def _config_sync_loop(self) -> None:
        next_config_pull_mono = 0.0
        next_bulk_sync_mono = 0.0
        next_mirror_reconcile_mono = 0.0
        next_target_reconcile_mono = 0.0
        next_report_templates_reconcile_mono = 0.0
        while not self._stop_event.is_set():
            try:
                if self._is_cloud_auto_sync_enabled():
                    now_mono = time.monotonic()
                    if now_mono >= next_bulk_sync_mono:
                        self._flush_data_outbox_burst()
                        next_bulk_sync_mono = now_mono + float(self._data_bulk_sync_interval_seconds)
                # Keep config operations on a slower cadence so they do not block
                # continuous data sync and cloud live updates.
                now_mono = time.monotonic()
                if now_mono >= next_config_pull_mono:
                    self._pull_config_from_cloud_once()
                    if not self._disable_config_push:
                        self._flush_config_outbox_once()
                    next_config_pull_mono = now_mono + float(self._config_pull_interval_seconds)
                # Reconcile Lite-readable mirror tables (alarms_setup,
                # triggers_limits, dashboard_configurations, gateway_configurations,
                # devices). The per-save daemon-thread mirror occasionally
                # drops writes; this idempotent catch-up compares local vs
                # cloud version and re-pushes anything stale. Cheap when
                # nothing has changed (one SELECT per domain).
                #
                # Cadence: every 2 s. The previous 5 s window meant a
                # dashboard edit on the desktop could lag the Lite view by
                # up to 5 s even when the per-save mirror landed
                # immediately. 2 s is fast enough to feel "live" without
                # turning the reconcile into a hot loop (each tick is one
                # SELECT per domain on Supabase + at most one UPSERT per
                # stale row, plus the historical-data sync is unaffected
                # because it runs on its own thread).
                if self._is_cloud_auto_sync_enabled() and now_mono >= next_mirror_reconcile_mono:
                    try:
                        self._reconcile_lite_mirror_tables_once()
                    except Exception:
                        # Reconciler is best-effort; never poison the loop.
                        pass
                    next_mirror_reconcile_mono = now_mono + 2.0  # every 2s
                # Re-align sync_targets row with database_configurations.
                # Picks up UI toggles without a backend restart and clears
                # stale "no target" errors after the user adds a cloud DB.
                if now_mono >= next_target_reconcile_mono:
                    try:
                        self._reconcile_sync_targets_with_config()
                    except Exception:
                        pass
                    next_target_reconcile_mono = now_mono + 10.0  # every 10s
                # Bulk re-push of report_templates to Supabase. The per-save
                # mirror handles the steady-state case, but a one-shot
                # startup hook can miss the window if the cloud target
                # wasn't resolvable yet. Periodic reconcile guarantees
                # eventual consistency for Lite — we cap the cadence at
                # 30 s because each invocation reads + upserts every local
                # template row (only a few KB), and Lite reads cache for
                # the operator session anyway.
                if self._is_cloud_auto_sync_enabled() and now_mono >= next_report_templates_reconcile_mono:
                    try:
                        from app.state import reports_store as _rs  # late import
                        _rs.reconcile_templates_to_cloud()
                    except Exception:
                        pass
                    next_report_templates_reconcile_mono = now_mono + 30.0  # every 30s
            except Exception as exc:
                self._upsert_sync_target_state(enabled=True, config={}, last_error=f"Config sync loop error: {exc}")
            self._sync_wakeup_event.wait(timeout=self._sync_interval_seconds)
            self._sync_wakeup_event.clear()

    def _reconcile_lite_mirror_tables_once(self) -> None:
        """Catch-up reconciliation between local config_documents_scoped
        and the cloud mirror tables Lite reads from. Fixes the gap where
        _mirror_config_doc_to_cloud's daemon thread silently dropped a
        write — without this, an operator clearing alarms on the edge
        would see Lite stuck on the old list until the next bootstrap
        save accidentally re-mirrored it.

        Idempotent: if cloud version >= local version we skip. If a
        domain isn't represented in the cloud mirror table at all we
        upsert it. Runs ~every 5s from the config sync loop."""
        cloud = self._get_cloud_database_target()
        if not cloud:
            return
        domains = (
            "alarms_setup",
            "triggers_limits",
            "dashboard_configurations",
            "gateway_configurations",
            "devices",
        )
        # Pull local rows for all mirrored domains in one trip. SQLite expects
        # one '?' per element, so we build the placeholders dynamically rather
        # than hard-coding a 3-tuple as before.
        placeholders = ",".join("?" * len(domains))
        with self._lock:
            with self._connect() as conn:
                rows = list(conn.execute(
                    f"SELECT scope_key, domain, payload_json, version, updated_utc "
                    f"FROM config_documents_scoped WHERE domain IN ({placeholders})",
                    domains,
                ).fetchall())
                # Fallback for older edges that only ever wrote unscoped
                # config_documents rows. Resolve a synthetic scope_key from
                # app_settings so the missing domains can finally land in
                # cloud. Once the operator hits Save in the new UI the
                # scoped writer takes over and this branch is a no-op.
                covered = {str(r["domain"] or "") for r in rows}
                missing = [d for d in domains if d not in covered]
                if missing:
                    try:
                        srow = conn.execute(
                            "SELECT payload_json FROM config_documents WHERE domain='app_settings'"
                        ).fetchone()
                        settings = {}
                        if srow and srow["payload_json"]:
                            settings = json.loads(str(srow["payload_json"] or "{}")) or {}
                        edge_profile = settings.get("edge_profile") if isinstance(settings.get("edge_profile"), dict) else {}
                        edge_id = (
                            str(edge_profile.get("edge_id") or "").strip().lower()
                            or str(settings.get("edge_id") or "").strip().lower()
                            or str(getattr(self, "_local_edge_id", "") or "").strip().lower()
                        )
                        tenant_id = str(settings.get("tenant_id") or "default").strip().lower() or "default"
                        customer_id = (
                            str(edge_profile.get("linked_customer_id") or "").strip().lower()
                            or str(settings.get("customer_id") or "").strip().lower()
                            or "-"
                        )
                        synthetic_scope = f"{tenant_id}|{customer_id}|{edge_id}" if edge_id else ""
                        if synthetic_scope:
                            qmark = ",".join("?" * len(missing))
                            for ur in conn.execute(
                                f"SELECT domain, payload_json, version, updated_utc "
                                f"FROM config_documents WHERE domain IN ({qmark})",
                                missing,
                            ).fetchall() or []:
                                rows.append({
                                    "scope_key": synthetic_scope,
                                    "domain": ur["domain"],
                                    "payload_json": ur["payload_json"],
                                    "version": ur["version"],
                                    "updated_utc": ur["updated_utc"],
                                })
                    except Exception:
                        pass
        if not rows:
            return
        try:
            from sqlalchemy import text  # type: ignore
        except Exception:
            return
        schema = str(cloud.get("schema") or "public")
        try:
            engine, _ = self._get_or_create_cloud_engine(cloud, schema)
        except Exception:
            return
        # Group by domain so we can do one SELECT per domain to discover
        # the cloud versions, then upsert what's stale.
        local_by_domain: Dict[str, List[Tuple[str, str, int, str]]] = {}
        for r in rows:
            scope_key = str(r["scope_key"] or "")
            domain = str(r["domain"] or "")
            payload = str(r["payload_json"] or "")
            version = int(r["version"] or 0)
            updated = str(r["updated_utc"] or "")
            local_by_domain.setdefault(domain, []).append((scope_key, payload, version, updated))
        try:
            with engine.connect() as conn:
                for domain, entries in local_by_domain.items():
                    scope_keys = [e[0] for e in entries]
                    if not scope_keys:
                        continue
                    placeholders = ",".join([f":k{i}" for i in range(len(scope_keys))])
                    params = {f"k{i}": k for i, k in enumerate(scope_keys)}
                    cloud_versions: Dict[str, int] = {}
                    try:
                        result = conn.execute(
                            text(
                                f'SELECT scope_key, version FROM "{schema}"."{domain}" '
                                f"WHERE scope_key IN ({placeholders})"
                            ),
                            params,
                        )
                        for row in result:
                            cloud_versions[str(row[0] or "")] = int(row[1] or 0)
                    except Exception:
                        # Table missing or permission error — skip this domain.
                        continue
                    for scope_key, payload, version, updated in entries:
                        if cloud_versions.get(scope_key, 0) >= version:
                            continue
                        tenant_from_scope = (scope_key.split("|") or ["default"])[0] or "default"
                        try:
                            with engine.begin() as wconn:
                                wconn.execute(
                                    text(
                                        f"""
                                        INSERT INTO "{schema}"."{domain}"
                                          (tenant_id, scope_key, payload_json, version, updated_utc)
                                        VALUES (:tenant_id, :scope_key, CAST(:payload_json AS jsonb), :version, :updated_utc)
                                        ON CONFLICT (tenant_id, scope_key) DO UPDATE SET
                                          payload_json = EXCLUDED.payload_json,
                                          version      = EXCLUDED.version,
                                          updated_utc  = EXCLUDED.updated_utc
                                        """
                                    ),
                                    dict(
                                        tenant_id=tenant_from_scope,
                                        scope_key=scope_key,
                                        payload_json=payload,
                                        version=version,
                                        updated_utc=updated,
                                    ),
                                )
                        except Exception:
                            # Don't let one bad row block the others.
                            continue
        except Exception:
            return

    def _live_sync_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._is_cloud_auto_sync_enabled():
                    # Keep this loop focused on low-latency live snapshots only.
                    # Full historian/log batch sync runs in the config/bulk loop;
                    # mixing both paths here causes multi-second stalls.
                    started = time.monotonic()
                    for _ in range(int(self._live_sync_burst_batches)):
                        self._flush_live_outbox_once()
                        if (time.monotonic() - started) >= float(self._live_sync_burst_seconds):
                            break
                    # Nudge bulk/config sync worker after every live flush so
                    # historian catch-up can run immediately without blocking live.
                    self._sync_wakeup_event.set()
            except Exception as exc:
                self._set_data_sync_state(last_data_error=f"Live sync loop error: {exc}")
            self._live_sync_wakeup_event.wait(timeout=self._live_sync_interval_seconds)
            self._live_sync_wakeup_event.clear()

    def _flush_data_outbox_burst(self) -> int:
        total_rows = 0
        started = time.monotonic()
        for _ in range(int(self._data_sync_burst_batches)):
            synced_rows = int(self._flush_data_outbox_once() or 0)
            if synced_rows <= 0:
                break
            total_rows += synced_rows
            if (time.monotonic() - started) >= float(self._data_sync_burst_seconds):
                break
        return total_rows

    def _cloud_live_cache_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                prefer_cloud = self._prefer_cloud_reads()
                if prefer_cloud:
                    rows = self._fetch_live_rows_from_cloud_fast(self._cloud_live_cache_limit)
                    if not rows:
                        rows = self._fetch_live_rows_from_cloud(self._cloud_live_cache_limit)
                    if rows:
                        # The background loop runs under whichever tenant
                        # context was active when it last ticked. Cache the
                        # rows under that tenant key so we never serve them
                        # to a different tenant's API call.
                        cache_tenant = self._current_tenant_id() or "default"
                        with self._cloud_live_cache_lock:
                            self._cloud_live_cache_rows_by_tenant[cache_tenant] = rows
                            self._cloud_live_cache_updated_utc_by_tenant[cache_tenant] = self._utc_now()
            except Exception:
                pass
            self._stop_event.wait(timeout=self._cloud_live_cache_interval_seconds)

    def _fetch_live_rows_from_cloud_fast(self, limit: int) -> list[dict[str, Any]]:
        if self._strict_cloud_mirror:
            return self._fetch_live_rows_from_cloud(limit)
        cloud = self._get_cloud_database_target()
        if not cloud:
            return []
        tenant_id = self._current_tenant_id()
        try:
            from sqlalchemy import text  # type: ignore
        except Exception:
            return []

        schema = str(cloud.get("schema") or "public")
        lim = max(50, min(int(limit or 1000), 5000))
        try:
            engine, _ = self._get_or_create_cloud_engine(cloud, schema)
        except Exception:
            return []
        try:
            def _top_ts_ms(rows_in: list[Any]) -> int:
                if not rows_in:
                    return 0
                raw = str(rows_in[0][0] or "").strip()
                if not raw:
                    return 0
                try:
                    txt = raw.replace("Z", "+00:00")
                    if " " in txt and "T" not in txt:
                        txt = txt.replace(" ", "T")
                    return int(datetime.fromisoformat(txt).timestamp() * 1000)
                except Exception:
                    return 0

            gateway_configs: list[dict[str, Any]] | None = None

            def _infer_gateway_id(source: str, tag: str, plc_ip: str) -> str:
                nonlocal gateway_configs
                if gateway_configs is None:
                    gateway_configs_raw = self.get_config_domain("gateway_configurations")
                    gateway_configs = gateway_configs_raw if isinstance(gateway_configs_raw, list) else []
                candidates: list[str] = []
                for g in gateway_configs:
                    if not isinstance(g, dict):
                        continue
                    gid = str(g.get("id") or "").strip()
                    if not gid:
                        continue
                    g_type = str(g.get("gateway_type") or "").strip()
                    g_ip = str(g.get("plc_ip") or "").strip()
                    g_tags_raw = g.get("tags")
                    g_tags = [str(t or "").strip() for t in g_tags_raw] if isinstance(g_tags_raw, list) else []
                    if source and g_type and source != g_type:
                        continue
                    if plc_ip and g_ip and plc_ip != g_ip:
                        continue
                    if tag and g_tags and tag not in g_tags:
                        continue
                    candidates.append(gid)
                if len(candidates) == 1:
                    return candidates[0]
                return ""

            with engine.begin() as conn:
                try:
                    conn.execute(text("SET LOCAL lock_timeout = '1200ms'"))
                    conn.execute(text("SET LOCAL statement_timeout = '3000ms'"))
                except Exception:
                    pass
                def _fetch_rows_with_freshness_fallback(table_name: str, fetch_limit: int) -> list[Any]:
                    # IMPORTANT: tenant-scoped reads only. The previous fallback to an
                    # unscoped query leaked other tenants' rows whenever the caller's
                    # tenant happened to have no live_latest entries yet (the route
                    # then relabeled the rows as the caller's tenant_id).
                    try:
                        return conn.execute(
                            text(
                                f"""
                                SELECT ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                                       tag_name, value, quality, quality_label
                                FROM "{schema}"."{table_name}"
                                WHERE tenant_id = :tenant
                                ORDER BY ts_utc DESC
                                LIMIT :lim
                                """
                            ),
                            {"tenant": tenant_id, "lim": fetch_limit},
                        ).fetchall()
                    except Exception:
                        return []

                live_rows = _fetch_rows_with_freshness_fallback("live_latest", lim)
                sample_limit = min(max(lim * 4, 500), 4000)
                plc_rows = _fetch_rows_with_freshness_fallback("plc_readings", sample_limit)

            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            # Merge sources by (gateway, tag) and keep strictly freshest row.
            # This avoids backward jumps caused by source switching between
            # live_latest and plc_readings snapshots.
            latest_row_by_key: dict[tuple[str, str], tuple[int, Any]] = {}
            for r in [*(live_rows or []), *(plc_rows or [])]:
                source = str(r[1] or "")
                gateway_id_raw = str(r[2] or "").strip()
                gateway_name_raw = str(r[3] or "").strip()
                plc_ip_raw = str(r[5] or "").strip()
                database_name_raw = str(r[6] or "").strip()
                tag_name = str(r[7] or "")
                if not tag_name:
                    continue
                inferred_id = ""
                if not gateway_id_raw and not gateway_name_raw:
                    inferred_id = _infer_gateway_id(source, tag_name, plc_ip_raw)
                fallback_gateway = "|".join([x for x in [source, plc_ip_raw, database_name_raw] if x]) or "unknown_gateway"
                gateway_id = gateway_id_raw or gateway_name_raw or inferred_id or fallback_gateway
                ts_ms = _top_ts_ms([r])
                if ts_ms > 0 and max(0, now_ms - ts_ms) > int(self._live_source_max_stale_ms * 4):
                    # Drop very stale rows from either source.
                    continue
                key = (gateway_id, tag_name)
                prev = latest_row_by_key.get(key)
                if prev and ts_ms <= int(prev[0]):
                    continue
                latest_row_by_key[key] = (ts_ms, r)

            merged = sorted(latest_row_by_key.values(), key=lambda x: int(x[0] or 0), reverse=True)
            out: list[dict[str, Any]] = []
            for _, r in merged[:lim]:
                source = str(r[1] or "")
                gateway_id_raw = str(r[2] or "").strip()
                gateway_name_raw = str(r[3] or "").strip()
                plc_ip_raw = str(r[5] or "").strip()
                database_name_raw = str(r[6] or "").strip()
                tag_name = str(r[7] or "")
                inferred_id = ""
                if not gateway_id_raw and not gateway_name_raw:
                    inferred_id = _infer_gateway_id(source, tag_name, plc_ip_raw)
                fallback_gateway = "|".join([x for x in [source, plc_ip_raw, database_name_raw] if x]) or "unknown_gateway"
                gateway_id = gateway_id_raw or gateway_name_raw or inferred_id or fallback_gateway
                out.append(
                    {
                        "ts": str(r[0] or ""),
                        "tenant_id": tenant_id,
                        "source": source,
                        "gateway_id": gateway_id,
                        "gateway_name": gateway_name_raw or gateway_id_raw or inferred_id or fallback_gateway,
                        "device_name": str(r[4] or ""),
                        "plc_ip": plc_ip_raw,
                        "database_name": database_name_raw,
                        "tag": tag_name,
                        "value": r[8],
                        "quality": r[9],
                        "quality_label": str(r[10] or ""),
                    }
                )
            return out
        except Exception:
            return []

    def _fetch_historian_rows_from_cloud(
        self,
        limit: int,
        gateway: str = "",
        device: str = "",
        tag: str = "",
        from_utc: str = "",
        to_utc: str = "",
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        cloud = self._get_cloud_database_target()
        if not cloud:
            return []
        tenant_id = self._current_tenant_id()
        try:
            from sqlalchemy import text  # type: ignore
        except Exception:
            return []
        schema = str(cloud.get("schema") or "public")
        lim = max(1, min(int(limit or 1000), 50000))
        off = max(0, int(offset or 0))
        gateway_txt = str(gateway or "").strip()
        device_txt = str(device or "").strip()
        tag_txt = str(tag or "").strip()
        from_txt = self._normalize_utc_filter(from_utc)
        to_txt = self._normalize_utc_filter(to_utc)
        filters_sql = ""
        params: dict[str, Any] = {"lim": lim, "off": off, "tenant": tenant_id}
        if gateway_txt:
            filters_sql += " AND (COALESCE(gateway_name,'') = :gateway OR COALESCE(gateway_id,'') = :gateway)"
            params["gateway"] = gateway_txt
        if device_txt:
            filters_sql += " AND LOWER(COALESCE(device_name,'')) LIKE LOWER(:device_like)"
            params["device_like"] = f"%{device_txt}%"
        if tag_txt:
            filters_sql += " AND LOWER(COALESCE(tag_name,'')) LIKE LOWER(:tag_like)"
            params["tag_like"] = f"%{tag_txt}%"
        if from_txt:
            filters_sql += " AND ts_utc >= :from_utc"
            params["from_utc"] = from_txt
        if to_txt:
            filters_sql += " AND ts_utc <= :to_utc"
            params["to_utc"] = to_txt
        try:
            engine, _ = self._get_or_create_cloud_engine(cloud, schema)
        except Exception:
            return []
        try:
            if self._strict_cloud_mirror:
                with engine.begin() as conn:
                    rows = conn.execute(
                        text(
                            f"""
                            SELECT local_id, tenant_id, ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                                   tag_name, value, quality, quality_label
                            FROM "{schema}"."historian_readings"
                            WHERE tenant_id = :tenant AND local_id IS NOT NULL{filters_sql}
                            ORDER BY ts_utc DESC, COALESCE(local_id, 0) DESC, id DESC
                            LIMIT :lim
                            OFFSET :off
                            """
                        ),
                        params,
                    ).fetchall()
                out: list[dict[str, Any]] = []
                for r in rows:
                    out.append(
                        {
                            "local_id": int(r[0] or 0),
                            "ts": str(r[2] or ""),
                            "tenant_id": str(r[1] or tenant_id),
                            "source": str(r[3] or ""),
                            "gateway_id": str(r[4] or ""),
                            "gateway_name": str(r[5] or ""),
                            "device_name": str(r[6] or ""),
                            "plc_ip": str(r[7] or ""),
                            "database_name": str(r[8] or ""),
                            "tag": str(r[9] or ""),
                            "value": r[10],
                            "quality": r[11],
                            "quality_label": str(r[12] or ""),
                        }
                    )
                return out
            with engine.begin() as conn:
                hist_rows: list[Any] = []
                plc_rows: list[Any] = []
                def _top_ts_ms(rows_in: list[Any]) -> int:
                    if not rows_in:
                        return 0
                    raw = str(rows_in[0][0] or "").strip()
                    if not raw:
                        return 0
                    try:
                        txt = raw.replace("Z", "+00:00")
                        if " " in txt and "T" not in txt:
                            txt = txt.replace(" ", "T")
                        return int(datetime.fromisoformat(txt).timestamp() * 1000)
                    except Exception:
                        return 0

                def _fetch_rows_with_freshness_fallback(table_name: str) -> list[Any]:
                    # IMPORTANT: tenant-scoped reads only. The previous fallback to an
                    # unscoped query leaked other tenants' historian rows whenever the
                    # caller's tenant had no rows yet on a given table.
                    try:
                        return conn.execute(
                            text(
                                f"""
                                SELECT ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                                       tag_name, value, quality, quality_label
                                FROM "{schema}"."{table_name}"
                                WHERE tenant_id = :tenant{filters_sql}
                                ORDER BY ts_utc DESC
                                LIMIT :lim
                                OFFSET :off
                                """
                            ),
                            params,
                        ).fetchall()
                    except Exception:
                        return []

                hist_rows = _fetch_rows_with_freshness_fallback("historian_readings")
                plc_rows = _fetch_rows_with_freshness_fallback("plc_readings")

                hist_top = _top_ts_ms(hist_rows)
                plc_top = _top_ts_ms(plc_rows)
                # Prefer the freshest data source. This avoids stale historian pages
                # when deployments actively write to plc_readings.
                if hist_rows and (hist_top >= plc_top or not plc_rows):
                    rows = hist_rows
                elif plc_rows:
                    rows = plc_rows
                else:
                    rows = []
            out: list[dict[str, Any]] = []
            gateway_configs_raw = self.get_config_domain("gateway_configurations")
            gateway_configs = gateway_configs_raw if isinstance(gateway_configs_raw, list) else []

            def _infer_gateway_id(source: str, tag: str, plc_ip: str) -> str:
                candidates: list[str] = []
                for g in gateway_configs:
                    if not isinstance(g, dict):
                        continue
                    gid = str(g.get("id") or "").strip()
                    if not gid:
                        continue
                    g_type = str(g.get("gateway_type") or "").strip()
                    g_ip = str(g.get("plc_ip") or "").strip()
                    g_tags_raw = g.get("tags")
                    g_tags = [str(t or "").strip() for t in g_tags_raw] if isinstance(g_tags_raw, list) else []
                    if source and g_type and source != g_type:
                        continue
                    if plc_ip and g_ip and plc_ip != g_ip:
                        continue
                    if tag and g_tags and tag not in g_tags:
                        continue
                    candidates.append(gid)
                if len(candidates) == 1:
                    return candidates[0]
                return ""

            for r in rows:
                source = str(r[1] or "")
                gateway_id_raw = str(r[2] or "").strip()
                gateway_name_raw = str(r[3] or "").strip()
                plc_ip_raw = str(r[5] or "").strip()
                database_name_raw = str(r[6] or "").strip()
                tag_name = str(r[7] or "")
                inferred_id = _infer_gateway_id(source, tag_name, plc_ip_raw)
                fallback_gateway = "|".join(
                    [x for x in [source, plc_ip_raw, database_name_raw] if x]
                ) or "unknown_gateway"
                gateway_id = gateway_id_raw or gateway_name_raw or inferred_id or fallback_gateway
                gateway_name = gateway_name_raw or gateway_id_raw or inferred_id or fallback_gateway
                out.append(
                    {
                        "ts": str(r[0] or ""),
                        "tenant_id": tenant_id,
                        "source": source,
                        "gateway_id": gateway_id,
                        "gateway_name": gateway_name,
                        "device_name": str(r[4] or ""),
                        "plc_ip": plc_ip_raw,
                        "database_name": database_name_raw,
                        "tag": tag_name,
                        "value": r[8],
                        "quality": r[9],
                        "quality_label": str(r[10] or ""),
                    }
                )
            return out
        except Exception:
            return []
    def _fetch_log_rows_from_cloud(self, limit: int) -> list[dict[str, Any]]:
        cloud = self._get_cloud_database_target()
        if not cloud:
            return []
        tenant_id = self._current_tenant_id()
        try:
            from sqlalchemy import text  # type: ignore
        except Exception:
            return []
        schema = str(cloud.get("schema") or "public")
        lim = max(1, min(int(limit or 2000), 10000))
        try:
            engine, _ = self._get_or_create_cloud_engine(cloud, schema)
        except Exception:
            return []
        try:
            with engine.begin() as conn:
                rows = conn.execute(
                    text(
                        f"""
                        SELECT local_id, ts_utc, level, category, message, gateway_id, gateway_name, device_name, database_name
                        FROM "{schema}"."app_logs"
                        WHERE tenant_id = :tenant AND local_id IS NOT NULL
                        ORDER BY COALESCE(local_id, 0) DESC, id DESC
                        LIMIT :lim
                        """
                    ),
                    {"lim": lim, "tenant": tenant_id},
                ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                out.append(
                    {
                        "local_id": int(r[0] or 0),
                        "ts": str(r[1] or ""),
                        "tenant_id": tenant_id,
                        "level": str(r[2] or "info"),
                        "category": str(r[3] or "system"),
                        "message": str(r[4] or ""),
                        "gateway_id": str(r[5] or ""),
                        "gateway_name": str(r[6] or ""),
                        "device_name": str(r[7] or ""),
                        "database_name": str(r[8] or ""),
                    }
                )
            return out
        except Exception:
            return []

    def _fetch_live_rows_from_cloud(self, limit: int) -> list[dict[str, Any]]:
        cloud = self._get_cloud_database_target()
        if not cloud:
            return []
        tenant_id = self._current_tenant_id()
        try:
            from sqlalchemy import text  # type: ignore
        except Exception:
            return []
        schema = str(cloud.get("schema") or "public")
        lim = max(1, min(int(limit or 1000), 20000))
        try:
            engine, _ = self._get_or_create_cloud_engine(cloud, schema)
        except Exception:
            return []
        try:
            if self._strict_cloud_mirror:
                with engine.begin() as conn:
                    def _row_ts_ms(raw_ts: str) -> int:
                        txt = str(raw_ts or "").strip()
                        if not txt:
                            return 0
                        try:
                            txt = txt.replace("Z", "+00:00")
                            if " " in txt and "T" not in txt:
                                txt = txt.replace(" ", "T")
                            return int(datetime.fromisoformat(txt).timestamp() * 1000)
                        except Exception:
                            return 0

                    # Fast path: use cloud live snapshot table first.
                    live_rows = conn.execute(
                        text(
                            f"""
                            SELECT tenant_id, ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                                   tag_name, value, quality, quality_label
                            FROM "{schema}"."live_latest"
                            WHERE tenant_id = :tenant
                            ORDER BY ts_utc DESC
                            LIMIT :lim
                            """
                        ),
                        {"lim": lim, "tenant": tenant_id},
                    ).fetchall()
                    live_top_ms = _row_ts_ms(str(live_rows[0][1] if live_rows else ""))
                    hist_top_row = conn.execute(
                        text(
                            f"""
                            SELECT ts_utc
                            FROM "{schema}"."historian_readings"
                            WHERE tenant_id = :tenant AND local_id IS NOT NULL
                            ORDER BY COALESCE(local_id, 0) DESC, id DESC
                            LIMIT 1
                            """
                        ),
                        {"tenant": tenant_id},
                    ).fetchone()
                    hist_top_ms = _row_ts_ms(str(hist_top_row[0] if hist_top_row else ""))
                    # Keep strict mirror, but avoid serving stale live_latest when
                    # historian has newer mirrored rows already available.
                    prefer_live_rows = bool(live_rows) and (
                        hist_top_ms <= 0 or live_top_ms >= (hist_top_ms - 1200)
                    )
                    if prefer_live_rows:
                        out_live: list[dict[str, Any]] = []
                        for r in live_rows:
                            out_live.append(
                                {
                                    "tenant_id": str(r[0] or tenant_id),
                                    "ts": str(r[1] or ""),
                                    "source": str(r[2] or ""),
                                    "gateway_id": str(r[3] or ""),
                                    "gateway_name": str(r[4] or ""),
                                    "device_name": str(r[5] or ""),
                                    "plc_ip": str(r[6] or ""),
                                    "database_name": str(r[7] or ""),
                                    "tag": str(r[8] or ""),
                                    "value": r[9],
                                    "quality": r[10],
                                    "quality_label": str(r[11] or ""),
                                }
                            )
                        return out_live

                    # Fallback: derive latest per (gateway, tag) from historian.
                    rows = conn.execute(
                        text(
                            f"""
                            SELECT local_id, tenant_id, ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                                   tag_name, value, quality, quality_label
                            FROM (
                              SELECT DISTINCT ON (gateway_id, tag_name)
                                     local_id, tenant_id, ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                                     tag_name, value, quality, quality_label
                              FROM "{schema}"."historian_readings"
                              WHERE tenant_id = :tenant AND local_id IS NOT NULL
                              ORDER BY gateway_id, tag_name, COALESCE(local_id, 0) DESC, id DESC
                            ) latest
                            ORDER BY ts_utc DESC
                            LIMIT :lim
                            """
                        ),
                        {"lim": lim, "tenant": tenant_id},
                    ).fetchall()
                out: list[dict[str, Any]] = []
                for r in rows:
                    out.append(
                        {
                            "local_id": int(r[0] or 0),
                            "tenant_id": str(r[1] or tenant_id),
                            "ts": str(r[2] or ""),
                            "source": str(r[3] or ""),
                            "gateway_id": str(r[4] or ""),
                            "gateway_name": str(r[5] or ""),
                            "device_name": str(r[6] or ""),
                            "plc_ip": str(r[7] or ""),
                            "database_name": str(r[8] or ""),
                            "tag": str(r[9] or ""),
                            "value": r[10],
                            "quality": r[11],
                            "quality_label": str(r[12] or ""),
                        }
                    )
                return out
            def _top_ts_ms(rows_in: list[Any]) -> int:
                if not rows_in:
                    return 0
                raw = str(rows_in[0][0] or "").strip()
                if not raw:
                    return 0
                try:
                    txt = raw.replace("Z", "+00:00")
                    if " " in txt and "T" not in txt:
                        txt = txt.replace(" ", "T")
                    return int(datetime.fromisoformat(txt).timestamp() * 1000)
                except Exception:
                    return 0

            def _fetch_rows_with_freshness_fallback(conn: Any, table_name: str, order_clause: str, fetch_limit: int) -> list[Any]:
                scoped_rows: list[Any] = []
                unscoped_rows: list[Any] = []
                base_sql = f"""
                            SELECT ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                                   tag_name, value, quality, quality_label
                            FROM "{schema}"."{table_name}"
                        """
                try:
                    scoped_rows = conn.execute(
                        text(
                            base_sql
                            + f"""
                            WHERE tenant_id = :tenant
                            ORDER BY {order_clause}
                            LIMIT :lim
                            """
                        ),
                        {"lim": fetch_limit, "tenant": tenant_id},
                    ).fetchall()
                except Exception:
                    scoped_rows = []
                try:
                    unscoped_rows = conn.execute(
                        text(
                            base_sql
                            + f"""
                            ORDER BY {order_clause}
                            LIMIT :lim
                            """
                        ),
                        {"lim": fetch_limit},
                    ).fetchall()
                except Exception:
                    unscoped_rows = []
                if scoped_rows:
                    return scoped_rows
                return unscoped_rows

            with engine.begin() as conn:
                live_rows: list[Any] = []
                hist_rows: list[Any] = []
                plc_rows: list[Any] = []
                live_rows = _fetch_rows_with_freshness_fallback(conn, "live_latest", "ts_utc DESC", lim)

                # Fetch historian/plc rows and prefer whichever source is freshest.
                # This prevents stale cloud live widgets when live_latest lags behind.
                sample_limit = min(max(lim * 2, 500), 3000)
                hist_rows = _fetch_rows_with_freshness_fallback(conn, "historian_readings", "ts_utc DESC", sample_limit)
                plc_rows = _fetch_rows_with_freshness_fallback(conn, "plc_readings", "ts_utc DESC", sample_limit)

                live_top = _top_ts_ms(live_rows)
                hist_top = _top_ts_ms(hist_rows)
                plc_top = _top_ts_ms(plc_rows)
                freshest = max(hist_top, plc_top)
                if live_rows and live_top >= freshest - 400:
                    rows = live_rows
                elif hist_rows and hist_top >= plc_top:
                    rows = hist_rows
                elif plc_rows:
                    rows = plc_rows
                else:
                    rows = live_rows
            out: list[dict[str, Any]] = []
            gateway_configs_raw = self.get_config_domain("gateway_configurations")
            gateway_configs = gateway_configs_raw if isinstance(gateway_configs_raw, list) else []

            def _infer_gateway_id(source: str, tag: str, plc_ip: str) -> str:
                candidates: list[str] = []
                for g in gateway_configs:
                    if not isinstance(g, dict):
                        continue
                    gid = str(g.get("id") or "").strip()
                    if not gid:
                        continue
                    g_type = str(g.get("gateway_type") or "").strip()
                    g_ip = str(g.get("plc_ip") or "").strip()
                    g_tags_raw = g.get("tags")
                    g_tags = [str(t or "").strip() for t in g_tags_raw] if isinstance(g_tags_raw, list) else []
                    if source and g_type and source != g_type:
                        continue
                    if plc_ip and g_ip and plc_ip != g_ip:
                        continue
                    if tag and g_tags and tag not in g_tags:
                        continue
                    candidates.append(gid)
                if len(candidates) == 1:
                    return candidates[0]
                return ""

            seen: set[tuple[str, str]] = set()
            for r in rows:
                source = str(r[1] or "")
                gateway_id_raw = str(r[2] or "").strip()
                gateway_name_raw = str(r[3] or "").strip()
                plc_ip_raw = str(r[5] or "").strip()
                database_name_raw = str(r[6] or "").strip()
                tag_name = str(r[7] or "")
                inferred_id = _infer_gateway_id(source, tag_name, plc_ip_raw)
                fallback_gateway = "|".join(
                    [x for x in [source, plc_ip_raw, database_name_raw] if x]
                ) or "unknown_gateway"
                gateway_id = gateway_id_raw or gateway_name_raw or inferred_id or fallback_gateway
                if not tag_name:
                    continue
                key = (gateway_id, tag_name)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "ts": str(r[0] or ""),
                        "tenant_id": tenant_id,
                        "source": source,
                        "gateway_id": gateway_id,
                        "gateway_name": gateway_name_raw or gateway_id_raw or inferred_id or fallback_gateway,
                        "device_name": str(r[4] or ""),
                        "plc_ip": plc_ip_raw,
                        "database_name": database_name_raw,
                        "tag": tag_name,
                        "value": r[8],
                        "quality": r[9],
                        "quality_label": str(r[10] or ""),
                    }
                )
                if len(out) >= lim:
                    break
            return out
        except Exception:
            return []

    def _get_data_sync_state(self) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT last_historian_id, last_log_id, last_data_sync_utc, last_data_error,
                           total_historian_synced, total_logs_synced
                    FROM data_sync_state WHERE id = 1
                    """
                ).fetchone()
                if not row:
                    return {
                        "last_historian_id": 0,
                        "last_log_id": 0,
                        "last_data_sync_utc": "",
                        "last_data_error": "",
                        "total_historian_synced": 0,
                        "total_logs_synced": 0,
                    }
                return {
                    "last_historian_id": int(row["last_historian_id"] or 0),
                    "last_log_id": int(row["last_log_id"] or 0),
                    "last_data_sync_utc": str(row["last_data_sync_utc"] or ""),
                    "last_data_error": str(row["last_data_error"] or ""),
                    "total_historian_synced": int(row["total_historian_synced"] or 0),
                    "total_logs_synced": int(row["total_logs_synced"] or 0),
                }

    def _set_data_sync_state(
        self,
        *,
        last_historian_id: int | None = None,
        last_log_id: int | None = None,
        last_data_sync_utc: str | None = None,
        last_data_error: str | None = None,
        total_historian_synced_delta: int = 0,
        total_logs_synced_delta: int = 0,
    ) -> None:
        now = self._utc_now()
        state = self._get_data_sync_state()
        hist_id = int(last_historian_id if last_historian_id is not None else state.get("last_historian_id", 0))
        log_id = int(last_log_id if last_log_id is not None else state.get("last_log_id", 0))
        sync_utc = str(
            last_data_sync_utc
            if last_data_sync_utc is not None
            else state.get("last_data_sync_utc", "")
        )
        sync_err = str(
            last_data_error
            if last_data_error is not None
            else state.get("last_data_error", "")
        )
        total_hist = int(state.get("total_historian_synced", 0)) + max(0, int(total_historian_synced_delta or 0))
        total_logs = int(state.get("total_logs_synced", 0)) + max(0, int(total_logs_synced_delta or 0))
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO data_sync_state
                    (id, last_historian_id, last_log_id, last_data_sync_utc, last_data_error, total_historian_synced, total_logs_synced, updated_utc)
                    VALUES(1, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      last_historian_id = excluded.last_historian_id,
                      last_log_id = excluded.last_log_id,
                      last_data_sync_utc = excluded.last_data_sync_utc,
                      last_data_error = excluded.last_data_error,
                      total_historian_synced = excluded.total_historian_synced,
                      total_logs_synced = excluded.total_logs_synced,
                      updated_utc = excluded.updated_utc
                    """,
                    (hist_id, log_id, sync_utc, sync_err, total_hist, total_logs, now),
                )

    def _upsert_cloud_live_latest_rows(self, conn: Any, schema: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        from sqlalchemy import text  # type: ignore
        ordered_rows = sorted(
            rows,
            key=lambda r: (
                str(r.get("tenant_id") or ""),
                str(r.get("gateway_id") or ""),
                str(r.get("tag_name") or ""),
            ),
        )

        conn.execute(
            text(
                f"""
                INSERT INTO "{schema}"."live_latest"
                (tenant_id, gateway_id, tag_name, ts_utc, source, gateway_name, device_name, plc_ip, database_name, value, quality, quality_label, updated_utc)
                VALUES
                (:tenant_id, :gateway_id, :tag_name, CAST(:ts_utc AS timestamptz), :source, :gateway_name, :device_name, :plc_ip, :database_name, :value, :quality, :quality_label, CAST(:updated_utc AS timestamptz))
                ON CONFLICT(tenant_id, gateway_id, tag_name) DO UPDATE SET
                  tenant_id = excluded.tenant_id,
                  ts_utc = excluded.ts_utc,
                  source = excluded.source,
                  gateway_name = excluded.gateway_name,
                  device_name = excluded.device_name,
                  plc_ip = excluded.plc_ip,
                  database_name = excluded.database_name,
                  value = excluded.value,
                  quality = excluded.quality,
                  quality_label = excluded.quality_label,
                  updated_utc = excluded.updated_utc
                """
            ),
            ordered_rows,
        )

    def _enqueue_live_fast_pending(self, rows: list[dict[str, Any]], max_local_id: int | None = None) -> None:
        if not rows:
            return
        with self._lock:
            for row in rows:
                tenant_id = normalize_tenant_id(str(row.get("tenant_id") or "default"))
                gateway_id = str(row.get("gateway_id") or "")
                tag_name = str(row.get("tag_name") or "")
                if not gateway_id or not tag_name:
                    continue
                self._live_fast_pending_latest[(tenant_id, gateway_id, tag_name)] = {
                    "tenant_id": tenant_id,
                    "gateway_id": gateway_id,
                    "tag_name": tag_name,
                    "ts_utc": str(row.get("ts_utc") or ""),
                    "source": str(row.get("source") or ""),
                    "gateway_name": str(row.get("gateway_name") or ""),
                    "device_name": str(row.get("device_name") or ""),
                    "plc_ip": str(row.get("plc_ip") or ""),
                    "database_name": str(row.get("database_name") or ""),
                    "value": row.get("value"),
                    "quality": row.get("quality"),
                    "quality_label": str(row.get("quality_label") or ""),
                    "updated_utc": str(row.get("updated_utc") or self._utc_now()),
                }
            if max_local_id and int(max_local_id) > 0:
                self._live_fast_last_local_id = max(self._live_fast_last_local_id, int(max_local_id))

    def _drain_live_fast_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self._live_fast_pending_latest:
                return []
            rows = list(self._live_fast_pending_latest.values())
            self._live_fast_pending_latest.clear()
            return rows

    def _collect_live_latest_incremental_rows(self) -> tuple[list[dict[str, Any]], int]:
        now = self._utc_now()
        latest_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        latest_id_by_key: dict[tuple[str, str, str], int] = {}
        max_id = int(self._live_fast_last_local_id or 0)
        pending_rows = self._drain_live_fast_pending()
        if pending_rows:
            for row in pending_rows:
                key = (
                    normalize_tenant_id(str(row.get("tenant_id") or "default")),
                    str(row.get("gateway_id") or ""),
                    str(row.get("tag_name") or ""),
                )
                if not key[1] or not key[2]:
                    continue
                latest_by_key[key] = {
                    "tenant_id": key[0],
                    "gateway_id": key[1],
                    "tag_name": key[2],
                    "ts_utc": str(row.get("ts_utc") or ""),
                    "source": str(row.get("source") or ""),
                    "gateway_name": str(row.get("gateway_name") or ""),
                    "device_name": str(row.get("device_name") or ""),
                    "plc_ip": str(row.get("plc_ip") or ""),
                    "database_name": str(row.get("database_name") or ""),
                    "value": row.get("value"),
                    "quality": row.get("quality"),
                    "quality_label": str(row.get("quality_label") or ""),
                    "updated_utc": now,
                }

        # When we already have fresh per-tag deltas from append_historian_rows,
        # skip extra local DB scans in the hot path.
        if latest_by_key:
            return list(latest_by_key.values()), max_id

        with self._lock:
            with self._connect() as conn:
                if max_id <= 0:
                    # ORDER BY ts_utc DESC uses idx_hist_tenant_ts directly;
                    # ORDER BY id DESC forces a full temp B-tree sort over the
                    # entire table — 100s of ms on a busy edge. Same result
                    # because ts_utc and id increase monotonically together.
                    db_rows = conn.execute(
                        """
                        SELECT id, tenant_id, ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name,
                               tag_name, value, quality, quality_label, source
                        FROM historian_readings
                        ORDER BY ts_utc DESC
                        LIMIT ?
                        """,
                        (self._live_fast_initial_rows,),
                    ).fetchall()
                else:
                    db_rows = conn.execute(
                        """
                        SELECT id, tenant_id, ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name,
                               tag_name, value, quality, quality_label, source
                        FROM historian_readings
                        WHERE id > ?
                        ORDER BY id ASC
                        LIMIT ?
                        """,
                        (max_id, self._live_fast_batch_size),
                    ).fetchall()

        if not db_rows:
            return [], max_id

        for r in db_rows:
            row_id = int(r["id"] or 0)
            if row_id > max_id:
                max_id = row_id
            tenant_id = normalize_tenant_id(str(r["tenant_id"] or "default"))
            gateway_id = str(r["gateway_id"] or "")
            tag_name = str(r["tag_name"] or "")
            if not tag_name or not gateway_id:
                continue
            key = (tenant_id, gateway_id, tag_name)
            prev_row_id = int(latest_id_by_key.get(key) or 0)
            # Keep only the newest row per (tenant, gateway, tag), regardless of
            # scan direction (initial DESC or incremental ASC).
            if prev_row_id and row_id <= prev_row_id:
                continue
            latest_id_by_key[key] = row_id
            latest_by_key[key] = {
                "tenant_id": tenant_id,
                "gateway_id": gateway_id,
                "tag_name": tag_name,
                "ts_utc": str(r["ts_utc"] or ""),
                "source": str(r["source"] or ""),
                "gateway_name": str(r["gateway_name"] or ""),
                "device_name": str(r["device_name"] or ""),
                "plc_ip": str(r["plc_ip"] or ""),
                "database_name": str(r["database_name"] or ""),
                "value": r["value"],
                "quality": r["quality"],
                "quality_label": str(r["quality_label"] or ""),
                "updated_utc": now,
            }

        return list(latest_by_key.values()), max_id

    def _flush_live_outbox_once(self) -> None:
        cloud = self._get_cloud_database_target()
        if not cloud:
            return
        live_rows, max_local_id = self._collect_live_latest_incremental_rows()
        if not live_rows:
            return
        try:
            from sqlalchemy import text  # type: ignore
        except Exception as exc:
            self._set_data_sync_state(last_data_error=f"Live sync unavailable: {exc}")
            return

        schema = str(cloud.get("schema") or "public")
        try:
            engine, target_key = self._get_or_create_cloud_engine(cloud, schema)
            try:
                self._ensure_cloud_schema_once(engine, schema, target_key)
            except Exception:
                pass
            with engine.begin() as conn:
                try:
                    conn.execute(text("SET LOCAL lock_timeout = '1800ms'"))
                    conn.execute(text("SET LOCAL statement_timeout = '4000ms'"))
                except Exception:
                    pass
                self._upsert_cloud_live_latest_rows(conn, schema, live_rows)
            self._live_fast_last_local_id = max(self._live_fast_last_local_id, int(max_local_id or 0))
        except Exception as exc:
            self._enqueue_live_fast_pending(live_rows, max_local_id=max_local_id)
            self._set_data_sync_state(last_data_error=f"Live sync failed: {exc}")

    def _flush_data_outbox_once(self) -> int:
        cloud = self._get_cloud_database_target()
        if not cloud:
            self._set_data_sync_state(last_data_error="No enabled PostgreSQL cloud target configured")
            return 0
        try:
            from sqlalchemy import text  # type: ignore
        except Exception as exc:
            self._set_data_sync_state(last_data_error=f"SQLAlchemy unavailable: {exc}")
            return 0

        schema = str(cloud.get("schema") or "public")
        try:
            engine, target_key = self._get_or_create_cloud_engine(cloud, schema)
        except Exception as exc:
            self._set_data_sync_state(last_data_error=f"Cloud engine setup failed: {exc}")
            return 0
        state = self._get_data_sync_state()
        last_hist_id = int(state.get("last_historian_id", 0))
        last_log_id = int(state.get("last_log_id", 0))
        batch_size = int(self._data_sync_batch_size)
        log_batch_size = int(self._data_sync_log_batch_size)
        self._data_sync_tick = int(self._data_sync_tick) + 1
        sync_logs_this_tick = (self._data_sync_tick % int(self._data_sync_log_every_n)) == 0

        try:
            with self._lock:
                with self._connect() as conn:
                    hist_rows = conn.execute(
                        """
                        SELECT id, tenant_id, ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name,
                               tag_name, value, quality, quality_label, source, created_utc
                        FROM historian_readings
                        WHERE id > ?
                        ORDER BY id ASC
                        LIMIT ?
                        """,
                        (last_hist_id, batch_size),
                    ).fetchall()
                    log_rows = []
                    if sync_logs_this_tick or not hist_rows:
                        log_rows = conn.execute(
                            """
                            SELECT id, tenant_id, ts_utc, level, category, message, gateway_id, gateway_name, device_name, database_name, created_utc
                            FROM app_logs
                            WHERE id > ?
                            ORDER BY id ASC
                            LIMIT ?
                            """,
                            (last_log_id, log_batch_size),
                        ).fetchall()
            if not hist_rows and not log_rows:
                # Don't clear last_data_error on a no-op tick. The error
                # from a failed real sync needs to stay visible until the
                # next SUCCESSFUL sync actually pushes rows, otherwise it
                # vanishes within a second and operators can't see why
                # their backlog isn't draining.
                self._set_data_sync_state(last_data_sync_utc=self._utc_now())
                return 0

            try:
                self._ensure_cloud_schema_once(engine, schema, target_key)
            except Exception:
                # Do not block data flow on transient DDL deadlocks/locks.
                # Writes below will still work if schema already exists.
                pass

            with engine.begin() as conn:
                if hist_rows:
                    hist_payload_rows = [
                        {
                            "local_id": int(r["id"]),
                            "tenant_id": normalize_tenant_id(str(r["tenant_id"] or "default")),
                            "ts_utc": str(r["ts_utc"] or ""),
                            "gateway_id": str(r["gateway_id"] or ""),
                            "gateway_name": str(r["gateway_name"] or ""),
                            "device_name": str(r["device_name"] or ""),
                            "plc_ip": str(r["plc_ip"] or ""),
                            "database_name": str(r["database_name"] or ""),
                            "tag_name": str(r["tag_name"] or ""),
                            "value": r["value"],
                            "quality": r["quality"],
                            "quality_label": str(r["quality_label"] or ""),
                            "source": str(r["source"] or ""),
                            "created_utc": str(r["created_utc"] or ""),
                            "updated_utc": str(r["created_utc"] or self._utc_now()),
                        }
                        for r in hist_rows
                    ]
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."historian_readings"
                            (local_id, tenant_id, ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name, value, quality, quality_label, source, created_utc)
                            VALUES
                            (:local_id, :tenant_id, CAST(:ts_utc AS timestamptz), :gateway_id, :gateway_name, :device_name, :plc_ip, :database_name, :tag_name, :value, :quality, :quality_label, :source, CAST(:created_utc AS timestamptz))
                            ON CONFLICT(tenant_id, gateway_id, local_id) DO NOTHING
                            """
                        ),
                        hist_payload_rows,
                    )
                    # Keep legacy/default table in sync for compatibility.
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."plc_readings"
                            (local_id, tenant_id, ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name, value, quality, quality_label, source, created_utc)
                            VALUES
                            (:local_id, :tenant_id, CAST(:ts_utc AS timestamptz), :gateway_id, :gateway_name, :device_name, :plc_ip, :database_name, :tag_name, :value, :quality, :quality_label, :source, CAST(:created_utc AS timestamptz))
                            ON CONFLICT(tenant_id, gateway_id, local_id) DO NOTHING
                            """
                        ),
                        hist_payload_rows,
                    )
                    # Optional compatibility path: keep this disabled by default
                    # to avoid lock contention with the dedicated fast live lane.
                    if str(os.environ.get("TRUSTNODE_DATA_SYNC_UPSERT_LIVE_LATEST", "")).strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }:
                        live_latest_rows = {}
                        for row in hist_payload_rows:
                            key = (
                                str(row.get("tenant_id") or "default"),
                                str(row.get("gateway_id") or ""),
                                str(row.get("tag_name") or ""),
                            )
                            if not key[1] or not key[2]:
                                continue
                            prev = live_latest_rows.get(key)
                            if not prev:
                                live_latest_rows[key] = row
                                continue
                            prev_id = int(prev.get("local_id") or 0)
                            curr_id = int(row.get("local_id") or 0)
                            if curr_id >= prev_id:
                                live_latest_rows[key] = row
                        if live_latest_rows:
                            self._upsert_cloud_live_latest_rows(conn, schema, list(live_latest_rows.values()))
                if log_rows:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."app_logs"
                            (local_id, tenant_id, ts_utc, level, category, message, gateway_id, gateway_name, device_name, database_name, created_utc)
                            VALUES
                            (:local_id, :tenant_id, CAST(:ts_utc AS timestamptz), :level, :category, :message, :gateway_id, :gateway_name, :device_name, :database_name, CAST(:created_utc AS timestamptz))
                            ON CONFLICT(tenant_id, gateway_id, local_id) DO NOTHING
                            """
                        ),
                        [
                            {
                                "local_id": int(r["id"]),
                                "tenant_id": normalize_tenant_id(str(r["tenant_id"] or "default")),
                                "ts_utc": str(r["ts_utc"] or ""),
                                "level": str(r["level"] or "info"),
                                "category": str(r["category"] or "system"),
                                "message": str(r["message"] or ""),
                                "gateway_id": str(r["gateway_id"] or ""),
                                "gateway_name": str(r["gateway_name"] or ""),
                                "device_name": str(r["device_name"] or ""),
                                "database_name": str(r["database_name"] or ""),
                                "created_utc": str(r["created_utc"] or ""),
                            }
                            for r in log_rows
                        ],
                    )

            if hist_rows:
                last_hist_id = max(int(r["id"]) for r in hist_rows)
            if log_rows:
                last_log_id = max(int(r["id"]) for r in log_rows)
            self._set_data_sync_state(
                last_historian_id=last_hist_id,
                last_log_id=last_log_id,
                last_data_sync_utc=self._utc_now(),
                last_data_error="",
                total_historian_synced_delta=len(hist_rows),
                total_logs_synced_delta=len(log_rows),
            )
            self._upsert_sync_target_state(
                enabled=True,
                config={"name": cloud.get("name"), "host": cloud.get("host"), "schema": schema},
                last_sync_utc=self._utc_now(),
                last_error="",
            )
            return len(hist_rows) + len(log_rows)
        except Exception as exc:
            # Persist the failure to app_logs as well as data_sync_state.
            # The state row is cleared on the next no-op tick, so without a
            # log entry there's no breadcrumb to diagnose why a backlog
            # stops draining when nothing is visible in the UI.
            err_msg = f"Data sync failed: {exc}"
            self._set_data_sync_state(last_data_error=err_msg)
            self._upsert_sync_target_state(
                enabled=True,
                config={"name": cloud.get("name"), "host": cloud.get("host"), "schema": schema},
                last_error=err_msg,
            )
            try:
                self.append_log_rows([
                    {
                        "ts_utc": self._utc_now(),
                        "level": "error",
                        "category": "cloud_sync",
                        "message": err_msg[:800],
                        "gateway_id": "",
                        "gateway_name": "",
                        "device_name": "",
                        "database_name": str(cloud.get("name") or ""),
                    }
                ])
            except Exception:
                pass
            return 0

    def _pre_migrate_tenant_columns(self, conn) -> None:
        """Add tenant_id to legacy tables before the schema script's indexes run.

        On the first launch of a newer build against an older app-store DB the
        executescript below creates CREATE INDEX statements that reference
        tenant_id on historian_readings / app_logs. SQLite's
        CREATE TABLE IF NOT EXISTS is a no-op when the table already exists, so
        the column never gets added inside the script — the index then fails
        with "no such column: tenant_id" and the entire backend aborts at
        startup. Run the ALTERs up front when the legacy tables exist without
        the column.
        """
        def _table_exists(name: str) -> bool:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
            return row is not None

        def _column_exists(table: str, column: str) -> bool:
            for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
                try:
                    if str(row["name"]) == column:
                        return True
                except Exception:
                    if column in tuple(row):
                        return True
            return False

        for table in ("historian_readings", "app_logs"):
            if _table_exists(table) and not _column_exists(table, "tenant_id"):
                try:
                    conn.execute(
                        f'ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT "default"'
                    )
                except Exception:
                    # Older SQLite builds disallow NOT NULL ADD COLUMN in
                    # some edge cases; fall back to nullable + backfill.
                    try:
                        conn.execute(f'ALTER TABLE {table} ADD COLUMN tenant_id TEXT')
                        conn.execute(f"UPDATE {table} SET tenant_id='default' WHERE tenant_id IS NULL")
                    except Exception:
                        pass

        # 2026-05-14: support string-typed tags (PLC text registers, OPC
        # String/ByteString, smart-meter strings). The numeric `value` column
        # stays for backward compat; text values go into `value_text`.
        if _table_exists("historian_readings") and not _column_exists("historian_readings", "value_text"):
            try:
                conn.execute("ALTER TABLE historian_readings ADD COLUMN value_text TEXT NULL")
            except Exception:
                pass

    def _ensure_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                # Pre-migrate columns that the executescript below references in
                # its CREATE INDEX statements. CREATE TABLE IF NOT EXISTS skips
                # column additions on pre-existing tables, so when an older DB
                # is opened the indexes would otherwise crash with
                # "no such column: tenant_id". Run these ALTERs first so the
                # whole executescript can assume tenant_id is always present.
                self._pre_migrate_tenant_columns(conn)
                conn.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA synchronous=NORMAL;
                    PRAGMA foreign_keys=ON;

                    CREATE TABLE IF NOT EXISTS config_documents (
                      domain TEXT PRIMARY KEY,
                      payload_json TEXT NOT NULL,
                      version INTEGER NOT NULL DEFAULT 1,
                      updated_utc TEXT NOT NULL
                    );
                    
                    CREATE TABLE IF NOT EXISTS config_documents_scoped (
                      scope_key TEXT NOT NULL,
                      domain TEXT NOT NULL,
                      payload_json TEXT NOT NULL,
                      version INTEGER NOT NULL DEFAULT 1,
                      updated_utc TEXT NOT NULL,
                      PRIMARY KEY (scope_key, domain)
                    );
                    CREATE INDEX IF NOT EXISTS idx_cfg_scoped_scope ON config_documents_scoped(scope_key);

                    CREATE TABLE IF NOT EXISTS config_audit (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      domain TEXT NOT NULL,
                      actor TEXT NULL,
                      old_version INTEGER NULL,
                      new_version INTEGER NOT NULL,
                      changed_utc TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS historian_readings (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      tenant_id TEXT NOT NULL DEFAULT 'default',
                      ts_utc TEXT NOT NULL,
                      gateway_id TEXT NULL,
                      gateway_name TEXT NULL,
                      device_name TEXT NULL,
                      plc_ip TEXT NULL,
                      database_name TEXT NULL,
                      tag_name TEXT NOT NULL,
                      value REAL NULL,
                      value_text TEXT NULL,
                      quality INTEGER NULL,
                      quality_label TEXT NULL,
                      source TEXT NULL,
                      created_utc TEXT NOT NULL
                    );
                    -- Indexes — kept minimal. Earlier revisions also created
                    -- idx_hist_ts / idx_hist_tag / idx_hist_gateway /
                    -- idx_hist_tenant_gw_tag_ts, all of which were subsumed
                    -- by the (tenant_id, ...) composites below. Carrying
                    -- duplicates roughly doubled per-row disk cost on a
                    -- busy edge (8 indexes × 50 tags × 1 Hz → +2.5 GB/month).
                    CREATE INDEX IF NOT EXISTS idx_hist_tenant_tag_ts
                      ON historian_readings(tenant_id, tag_name, ts_utc DESC);
                    -- Operator 2026-06-17: queries without a tag filter
                    -- (e.g. dashboard "all-tag" historian fetches and
                    -- the new /historian/agg endpoint's untagged path)
                    -- otherwise fall back to a TEMP B-TREE sort over
                    -- the entire tenant slice. (tenant_id, ts_utc DESC)
                    -- lets the planner walk the index in order.
                    CREATE INDEX IF NOT EXISTS idx_hist_tenant_ts
                      ON historian_readings(tenant_id, ts_utc DESC);
                    -- Same problem when filtering by gateway_id without a
                    -- tag (Power Overview's per-meter charts hit this).
                    CREATE INDEX IF NOT EXISTS idx_hist_tenant_gw_ts
                      ON historian_readings(tenant_id, gateway_id, ts_utc DESC);

                    CREATE TABLE IF NOT EXISTS app_logs (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      tenant_id TEXT NOT NULL DEFAULT 'default',
                      ts_utc TEXT NOT NULL,
                      level TEXT NOT NULL,
                      category TEXT NOT NULL,
                      message TEXT NOT NULL,
                      gateway_id TEXT NULL,
                      gateway_name TEXT NULL,
                      device_name TEXT NULL,
                      database_name TEXT NULL,
                      created_utc TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_logs_ts ON app_logs(ts_utc DESC);
                    CREATE INDEX IF NOT EXISTS idx_logs_level ON app_logs(level);
                    CREATE INDEX IF NOT EXISTS idx_logs_category ON app_logs(category);

                    CREATE TABLE IF NOT EXISTS sync_targets (
                      id TEXT PRIMARY KEY,
                      name TEXT NOT NULL,
                      target_type TEXT NOT NULL,
                      config_json TEXT NOT NULL,
                      enabled INTEGER NOT NULL DEFAULT 0,
                      last_sync_utc TEXT NULL,
                      last_error TEXT NULL,
                      updated_utc TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS sync_outbox (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      domain TEXT NOT NULL,
                      entity_key TEXT NOT NULL,
                      payload_json TEXT NOT NULL,
                      status TEXT NOT NULL DEFAULT 'pending',
                      retries INTEGER NOT NULL DEFAULT 0,
                      last_error TEXT NULL,
                      created_utc TEXT NOT NULL,
                      updated_utc TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_sync_outbox_status ON sync_outbox(status, id);

                    CREATE TABLE IF NOT EXISTS data_sync_state (
                      id INTEGER PRIMARY KEY CHECK (id = 1),
                      last_historian_id INTEGER NOT NULL DEFAULT 0,
                      last_log_id INTEGER NOT NULL DEFAULT 0,
                      updated_utc TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS historian_agg_minute (
                      bucket_utc TEXT NOT NULL,
                      gateway_id TEXT NULL,
                      gateway_name TEXT NULL,
                      device_name TEXT NULL,
                      plc_ip TEXT NULL,
                      database_name TEXT NULL,
                      tag_name TEXT NOT NULL,
                      avg_value REAL NULL,
                      min_value REAL NULL,
                      max_value REAL NULL,
                      sample_count INTEGER NOT NULL DEFAULT 0,
                      quality_min INTEGER NULL,
                      quality_max INTEGER NULL,
                      created_utc TEXT NOT NULL,
                      updated_utc TEXT NOT NULL,
                      PRIMARY KEY (bucket_utc, gateway_id, tag_name, database_name)
                    );
                    CREATE INDEX IF NOT EXISTS idx_hist_agg_minute_bucket ON historian_agg_minute(bucket_utc DESC);

                    CREATE TABLE IF NOT EXISTS historian_agg_hour (
                      bucket_utc TEXT NOT NULL,
                      gateway_id TEXT NULL,
                      gateway_name TEXT NULL,
                      device_name TEXT NULL,
                      plc_ip TEXT NULL,
                      database_name TEXT NULL,
                      tag_name TEXT NOT NULL,
                      avg_value REAL NULL,
                      min_value REAL NULL,
                      max_value REAL NULL,
                      sample_count INTEGER NOT NULL DEFAULT 0,
                      quality_min INTEGER NULL,
                      quality_max INTEGER NULL,
                      created_utc TEXT NOT NULL,
                      updated_utc TEXT NOT NULL,
                      PRIMARY KEY (bucket_utc, gateway_id, tag_name, database_name)
                    );
                    CREATE INDEX IF NOT EXISTS idx_hist_agg_hour_bucket ON historian_agg_hour(bucket_utc DESC);

                    CREATE TABLE IF NOT EXISTS historian_agg_day (
                      bucket_utc TEXT NOT NULL,
                      gateway_id TEXT NULL,
                      gateway_name TEXT NULL,
                      device_name TEXT NULL,
                      plc_ip TEXT NULL,
                      database_name TEXT NULL,
                      tag_name TEXT NOT NULL,
                      avg_value REAL NULL,
                      min_value REAL NULL,
                      max_value REAL NULL,
                      sample_count INTEGER NOT NULL DEFAULT 0,
                      quality_min INTEGER NULL,
                      quality_max INTEGER NULL,
                      created_utc TEXT NOT NULL,
                      updated_utc TEXT NOT NULL,
                      PRIMARY KEY (bucket_utc, gateway_id, tag_name, database_name)
                    );
                    CREATE INDEX IF NOT EXISTS idx_hist_agg_day_bucket ON historian_agg_day(bucket_utc DESC);

                    CREATE TABLE IF NOT EXISTS retention_policy (
                      id INTEGER PRIMARY KEY CHECK (id = 1),
                      enabled INTEGER NOT NULL DEFAULT 0,
                      schedule_minutes INTEGER NOT NULL DEFAULT 60,
                      raw_keep_days INTEGER NOT NULL DEFAULT 7,
                      minute_keep_days INTEGER NOT NULL DEFAULT 30,
                      hour_keep_days INTEGER NOT NULL DEFAULT 180,
                      day_keep_days INTEGER NOT NULL DEFAULT 730,
                      backup_before_cleanup INTEGER NOT NULL DEFAULT 1,
                      max_delete_rows_per_run INTEGER NOT NULL DEFAULT 50000,
                      updated_utc TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS retention_runs (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      run_utc TEXT NOT NULL,
                      dry_run INTEGER NOT NULL DEFAULT 0,
                      status TEXT NOT NULL,
                      details_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_retention_runs_utc ON retention_runs(run_utc DESC);

                    CREATE TABLE IF NOT EXISTS auth_settings (
                      id INTEGER PRIMARY KEY CHECK (id = 1),
                      secret TEXT NOT NULL,
                      updated_utc TEXT NOT NULL
                    );

                    -- Reporting module: templates persist the saved configuration
                    -- (sections + filters) that drives PDF generation. `definition_json`
                    -- holds the ordered list of sections (header/text/kpi/chart/pie/table)
                    -- each referencing the dashboard widget schema for queries.
                    CREATE TABLE IF NOT EXISTS report_templates (
                      id TEXT PRIMARY KEY,
                      tenant_id TEXT NOT NULL DEFAULT 'default',
                      name TEXT NOT NULL,
                      description TEXT NULL,
                      definition_json TEXT NOT NULL,
                      created_utc TEXT NOT NULL,
                      updated_utc TEXT NOT NULL,
                      created_by TEXT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_report_templates_tenant ON report_templates(tenant_id);
                    CREATE INDEX IF NOT EXISTS idx_report_templates_updated ON report_templates(updated_utc DESC);

                    -- Schedules wrap a template with a trigger (time/tag/both) and
                    -- delivery settings (email recipients, attach PDF).
                    CREATE TABLE IF NOT EXISTS scheduled_reports (
                      id TEXT PRIMARY KEY,
                      tenant_id TEXT NOT NULL DEFAULT 'default',
                      name TEXT NOT NULL,
                      template_id TEXT NOT NULL,
                      enabled INTEGER NOT NULL DEFAULT 1,
                      trigger_mode TEXT NOT NULL DEFAULT 'time',
                      recurrence TEXT NOT NULL DEFAULT 'daily',
                      hour INTEGER NOT NULL DEFAULT 8,
                      minute INTEGER NOT NULL DEFAULT 0,
                      day_of_week INTEGER NULL,
                      day_of_month INTEGER NULL,
                      tag_conditions_json TEXT NOT NULL DEFAULT '[]',
                      condition_logic TEXT NOT NULL DEFAULT 'all',
                      deliver_email INTEGER NOT NULL DEFAULT 0,
                      recipients_json TEXT NOT NULL DEFAULT '[]',
                      email_subject TEXT NULL,
                      email_body TEXT NULL,
                      email_profile_id TEXT NULL,
                      format TEXT NOT NULL DEFAULT 'pdf',
                      attach_pdf INTEGER NOT NULL DEFAULT 1,
                      attach_csv INTEGER NOT NULL DEFAULT 0,
                      attach_txt INTEGER NOT NULL DEFAULT 0,
                      require_gateway_running INTEGER NOT NULL DEFAULT 0,
                      created_utc TEXT NOT NULL,
                      updated_utc TEXT NOT NULL,
                      last_run_utc TEXT NULL,
                      next_run_utc TEXT NULL,
                      last_status TEXT NULL,
                      last_error TEXT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_scheduled_reports_tenant ON scheduled_reports(tenant_id, enabled);
                    CREATE INDEX IF NOT EXISTS idx_scheduled_reports_next_run ON scheduled_reports(next_run_utc);

                    -- One row per generated PDF. The actual file lives on disk at
                    -- `<TRUSTNODE_DATA_DIR>/reports/<id>.pdf`. `email_status` records
                    -- delivery state when a schedule asked for email.
                    CREATE TABLE IF NOT EXISTS generated_reports (
                      id TEXT PRIMARY KEY,
                      tenant_id TEXT NOT NULL DEFAULT 'default',
                      template_id TEXT NULL,
                      template_name TEXT NULL,
                      schedule_id TEXT NULL,
                      schedule_name TEXT NULL,
                      triggered_by TEXT NOT NULL DEFAULT 'manual',
                      file_path TEXT NOT NULL,
                      file_name TEXT NOT NULL,
                      file_bytes INTEGER NOT NULL DEFAULT 0,
                      file_sha256 TEXT NULL,
                      created_utc TEXT NOT NULL,
                      email_status TEXT NULL,
                      email_message TEXT NULL,
                      email_recipients_json TEXT NULL,
                      meta_json TEXT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_generated_reports_tenant_created ON generated_reports(tenant_id, created_utc DESC);
                    CREATE INDEX IF NOT EXISTS idx_generated_reports_schedule ON generated_reports(schedule_id, created_utc DESC);
                    """
                )
                hist_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(historian_readings)").fetchall()}
                if "tenant_id" not in hist_cols:
                    conn.execute('ALTER TABLE historian_readings ADD COLUMN tenant_id TEXT NOT NULL DEFAULT "default"')
                log_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(app_logs)").fetchall()}
                if "tenant_id" not in log_cols:
                    conn.execute('ALTER TABLE app_logs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT "default"')
                # 2026-05-13: schedules grew an opt-in "only when a gateway is running" gate.
                # Older DBs need the column added on the fly so the daemon and CRUD code
                # don't crash on a missing field.
                sched_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(scheduled_reports)").fetchall()}
                if "require_gateway_running" not in sched_cols:
                    conn.execute("ALTER TABLE scheduled_reports ADD COLUMN require_gateway_running INTEGER NOT NULL DEFAULT 0")
                # 2026-05-14: schedules can attach multiple file types (PDF/CSV/TXT)
                # to the scheduled email. Backfill defaults so old schedules keep
                # delivering just the PDF as they always did.
                if "attach_pdf" not in sched_cols:
                    conn.execute("ALTER TABLE scheduled_reports ADD COLUMN attach_pdf INTEGER NOT NULL DEFAULT 1")
                if "attach_csv" not in sched_cols:
                    conn.execute("ALTER TABLE scheduled_reports ADD COLUMN attach_csv INTEGER NOT NULL DEFAULT 0")
                if "attach_txt" not in sched_cols:
                    conn.execute("ALTER TABLE scheduled_reports ADD COLUMN attach_txt INTEGER NOT NULL DEFAULT 0")
                # 2026-05-18: generated_reports gained a `storage_path` column so the
                # edge can remember where each PDF was uploaded inside the Lite
                # Storage bucket (lets the Lite app render signed-URL downloads).
                gen_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(generated_reports)").fetchall()}
                if "storage_path" not in gen_cols:
                    conn.execute("ALTER TABLE generated_reports ADD COLUMN storage_path TEXT NULL")
                conn.execute('CREATE INDEX IF NOT EXISTS idx_hist_tenant_ts ON historian_readings(tenant_id, ts_utc DESC)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_hist_tenant_gwid_tag_ts ON historian_readings(tenant_id, gateway_id, tag_name, ts_utc DESC)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_hist_tenant_gwname_tag_ts ON historian_readings(tenant_id, gateway_name, tag_name, ts_utc DESC)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_tenant_ts ON app_logs(tenant_id, ts_utc DESC)')
                # 2026-05-18: drop the 4 redundant historian indexes on boot so
                # upgrading installs don't keep paying for them. They were
                # subsumed by the composites above. Idempotent — DROP IF EXISTS.
                for _legacy_ix in (
                    "idx_hist_ts", "idx_hist_tag", "idx_hist_gateway", "idx_hist_tenant_gw_tag_ts",
                ):
                    try:
                        conn.execute(f'DROP INDEX IF EXISTS {_legacy_ix}')
                    except Exception:
                        pass
                now = self._utc_now()
                # Retention defaults: enabled by default with 7d raw / 30d
                # minute / 180d hour / 730d day. Without retention the
                # historian grows unbounded (~250 GB/month at 50 tags @ 1 Hz),
                # so we ship enabled=1 out of the box. Users can tune or
                # disable from Settings → Retention.
                conn.execute(
                    """
                    INSERT INTO retention_policy
                    (id, enabled, schedule_minutes, raw_keep_days, minute_keep_days, hour_keep_days, day_keep_days, backup_before_cleanup, max_delete_rows_per_run, updated_utc)
                    VALUES (1, 1, 60, 7, 30, 180, 730, 1, 50000, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (now,),
                )
                secret = os.environ.get("TRUSTNODE_AUTH_SECRET", "").strip() or secrets.token_hex(32)
                conn.execute(
                    """
                    INSERT INTO auth_settings(id, secret, updated_utc)
                    VALUES(1, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (secret, now),
                )
                conn.execute(
                    """
                    INSERT INTO data_sync_state(id, last_historian_id, last_log_id, updated_utc)
                    VALUES(1, 0, 0, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (now,),
                )
                self._ensure_data_sync_state_columns(conn)

    def _ensure_data_sync_state_columns(self, conn: sqlite3.Connection) -> None:
        cols = {
            "last_data_sync_utc": "TEXT NULL",
            "last_data_error": "TEXT NULL",
            "total_historian_synced": "INTEGER NOT NULL DEFAULT 0",
            "total_logs_synced": "INTEGER NOT NULL DEFAULT 0",
        }
        existing = {
            str(r["name"])
            for r in conn.execute("PRAGMA table_info(data_sync_state)").fetchall()
        }
        for col_name, col_type in cols.items():
            if col_name in existing:
                continue
            conn.execute(f'ALTER TABLE data_sync_state ADD COLUMN "{col_name}" {col_type}')

    def _ensure_required_config_domains(self) -> None:
        now = self._utc_now()
        with self._lock:
            with self._connect() as conn:
                for domain, payload in self.REQUIRED_CONFIG_DOMAINS.items():
                    conn.execute(
                        """
                        INSERT INTO config_documents(domain, payload_json, version, updated_utc)
                        VALUES(?, ?, 1, ?)
                        ON CONFLICT(domain) DO NOTHING
                        """,
                        (domain, json.dumps(payload), now),
                    )

    def _default_local_database_configuration(self) -> Dict[str, Any]:
        # First-run safe local sink so gateway setup has a selectable database immediately.
        return {
            "id": self.DEFAULT_LOCAL_DB_ID,
            "name": "Local SQLite",
            "engine": "sqlite",
            "location": "local",
            "enabled": True,
            "use_gateway": True,
            "use_app": False,
            "use_backup": False,
            "cloud_sync_enabled": False,
            "sqlite_path": "./data/trustnode_edge.db",
            "table": "historian_readings",
            "schema": "",
            "tls": False,
        }

    def _ensure_default_database_configuration(self) -> None:
        should_seed = False
        merged_payload: list[Dict[str, Any]] | None = None
        seeded_utc = self._utc_now()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM config_documents WHERE domain = ?",
                    ("database_configurations",),
                ).fetchone()
                if not row:
                    should_seed = True
                else:
                    try:
                        payload = json.loads(str(row["payload_json"] or "[]"))
                    except Exception:
                        payload = []
                    if isinstance(payload, list) and len(payload) == 0:
                        should_seed = True
                    elif isinstance(payload, list):
                        has_default_id = any(
                            isinstance(item, dict) and str(item.get("id") or "").strip() == self.DEFAULT_LOCAL_DB_ID
                            for item in payload
                        )
                        has_any_local_db = any(
                            isinstance(item, dict)
                            and str(item.get("engine") or "").strip().lower() in ("sqlite", "csv_file", "txt_file", "legacy_http")
                            for item in payload
                        )
                        if (not has_default_id) and (not has_any_local_db):
                            merged_payload = [*payload, self._default_local_database_configuration()]
                            should_seed = True

        if should_seed:
            self.upsert_domain(
                "database_configurations",
                merged_payload if isinstance(merged_payload, list) else [self._default_local_database_configuration()],
                actor="system",
            )
            self._mark_default_local_database_seeded(seeded_utc)

    def _mark_default_local_database_seeded(self, seeded_utc: str) -> None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM config_documents WHERE domain = ?",
                    ("metadata",),
                ).fetchone()
                metadata: Dict[str, Any] = {}
                if row:
                    try:
                        raw = json.loads(str(row["payload_json"] or "{}"))
                        if isinstance(raw, dict):
                            metadata = raw
                    except Exception:
                        metadata = {}
        metadata["default_local_db_seeded"] = True
        metadata["default_local_db_seeded_utc"] = str(seeded_utc or self._utc_now())
        self.upsert_domain("metadata", metadata, actor="system")

    def _compact_sync_outbox_for_domains(self) -> None:
        # Keep only latest unsent row per domain so config sync backlog does not
        # grow unbounded when UI persists frequently.
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    DELETE FROM sync_outbox
                    WHERE status IN ('pending', 'failed')
                      AND id NOT IN (
                        SELECT MAX(id)
                        FROM sync_outbox
                        WHERE status IN ('pending', 'failed')
                        GROUP BY domain
                      )
                    """
                )

    def get_or_create_auth_secret(self) -> str:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT secret FROM auth_settings WHERE id = 1").fetchone()
                if row and str(row["secret"] or "").strip():
                    return str(row["secret"])
                now = self._utc_now()
                secret = secrets.token_hex(32)
                conn.execute(
                    """
                    INSERT INTO auth_settings(id, secret, updated_utc)
                    VALUES(1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET secret = excluded.secret, updated_utc = excluded.updated_utc
                    """,
                    (secret, now),
                )
                return secret

    def shutdown(self) -> None:
        self._stop_event.set()
        self._sync_wakeup_event.set()
        self._live_sync_wakeup_event.set()
        try:
            if self._scheduler_thread.is_alive():
                self._scheduler_thread.join(timeout=1.5)
        except Exception:
            pass
        try:
            if self._live_sync_thread.is_alive():
                self._live_sync_thread.join(timeout=2.0)
        except Exception:
            pass
        try:
            if self._cloud_live_cache_thread.is_alive():
                self._cloud_live_cache_thread.join(timeout=1.5)
        except Exception:
            pass
        try:
            if self._sync_thread.is_alive():
                self._sync_thread.join(timeout=2.0)
        except Exception:
            pass
        with self._cloud_engine_lock:
            for key, engine in list(self._cloud_engine_cache.items()):
                if engine is None:
                    continue
                try:
                    engine.dispose()
                except Exception:
                    pass
            self._cloud_engine_cache.clear()

    def _retention_scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                policy = self.get_retention_policy()
                if policy.get("enabled"):
                    # Avoid overlapping runs.
                    self.run_retention(dry_run=False, actor="scheduler")
                    sleep_s = max(60, int(policy.get("schedule_minutes", 60)) * 60)
                else:
                    sleep_s = 60
            except Exception:
                sleep_s = 60
            self._stop_event.wait(timeout=sleep_s)

    def get_retention_policy(self) -> Dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM retention_policy WHERE id = 1").fetchone()
                if not row:
                    return {
                        "enabled": False,
                        "schedule_minutes": 60,
                        "raw_keep_days": 7,
                        "minute_keep_days": 30,
                        "hour_keep_days": 180,
                        "day_keep_days": 730,
                        "backup_before_cleanup": True,
                        "max_delete_rows_per_run": 50000,
                    }
                return {
                    "enabled": bool(row["enabled"]),
                    "schedule_minutes": int(row["schedule_minutes"]),
                    "raw_keep_days": int(row["raw_keep_days"]),
                    "minute_keep_days": int(row["minute_keep_days"]),
                    "hour_keep_days": int(row["hour_keep_days"]),
                    "day_keep_days": int(row["day_keep_days"]),
                    "backup_before_cleanup": bool(row["backup_before_cleanup"]),
                    "max_delete_rows_per_run": int(row["max_delete_rows_per_run"]),
                    "updated_utc": row["updated_utc"],
                }

    def set_retention_policy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = self._utc_now()
        current = self.get_retention_policy()
        merged = {
            **current,
            **{
                "enabled": bool(payload.get("enabled", current.get("enabled", False))),
                "schedule_minutes": max(5, int(payload.get("schedule_minutes", current.get("schedule_minutes", 60)))),
                "raw_keep_days": max(1, int(payload.get("raw_keep_days", current.get("raw_keep_days", 7)))),
                "minute_keep_days": max(1, int(payload.get("minute_keep_days", current.get("minute_keep_days", 30)))),
                "hour_keep_days": max(1, int(payload.get("hour_keep_days", current.get("hour_keep_days", 180)))),
                "day_keep_days": max(1, int(payload.get("day_keep_days", current.get("day_keep_days", 730)))),
                "backup_before_cleanup": bool(payload.get("backup_before_cleanup", current.get("backup_before_cleanup", True))),
                "max_delete_rows_per_run": max(1000, int(payload.get("max_delete_rows_per_run", current.get("max_delete_rows_per_run", 50000)))),
            },
        }
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE retention_policy
                    SET enabled = ?, schedule_minutes = ?, raw_keep_days = ?, minute_keep_days = ?, hour_keep_days = ?, day_keep_days = ?,
                        backup_before_cleanup = ?, max_delete_rows_per_run = ?, updated_utc = ?
                    WHERE id = 1
                    """,
                    (
                        1 if merged["enabled"] else 0,
                        merged["schedule_minutes"],
                        merged["raw_keep_days"],
                        merged["minute_keep_days"],
                        merged["hour_keep_days"],
                        merged["day_keep_days"],
                        1 if merged["backup_before_cleanup"] else 0,
                        merged["max_delete_rows_per_run"],
                        now,
                    ),
                )
        merged["updated_utc"] = now
        return merged

    def _save_retention_run(self, run_utc: str, dry_run: bool, status: str, details: Dict[str, Any]) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO retention_runs(run_utc, dry_run, status, details_json) VALUES (?, ?, ?, ?)",
                    (run_utc, 1 if dry_run else 0, status, json.dumps(details)),
                )

    def list_backups(self, limit: int = 200) -> list[Dict[str, Any]]:
        lim = max(1, min(int(limit or 200), 1000))
        backup_dir = self._get_backup_dir()
        rows: list[Dict[str, Any]] = []
        try:
            for name in os.listdir(backup_dir):
                if not name.lower().endswith(".db"):
                    continue
                path = os.path.join(backup_dir, name)
                if not os.path.isfile(path):
                    continue
                st = os.stat(path)
                rows.append(
                    {
                        "filename": name,
                        "path": path,
                        "size_bytes": int(st.st_size),
                        "modified_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
        except Exception:
            return []
        rows.sort(key=lambda r: str(r.get("modified_utc", "")), reverse=True)
        return rows[:lim]

    def create_backup(self, actor: str = "manual", label: str = "") -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%d_%H%M%S")
        safe_label = "".join(ch for ch in str(label or "").strip() if ch.isalnum() or ch in ("_", "-"))[:32]
        suffix = f"_{safe_label}" if safe_label else ""
        filename = f"trustnode_app_store_{stamp}{suffix}.db"
        backup_dir = self._get_backup_dir()
        backup_path = os.path.join(backup_dir, filename)
        with self._lock:
            shutil.copy2(self._db_path, backup_path)
        return {
            "ok": True,
            "actor": actor,
            "filename": filename,
            "path": backup_path,
            "created_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def restore_backup(self, filename: str, actor: str = "manual") -> Dict[str, Any]:
        clean = os.path.basename(str(filename or "").strip())
        if not clean or clean in (".", ".."):
            return {"ok": False, "message": "Invalid backup filename."}
        source = os.path.join(self._get_backup_dir(), clean)
        if not os.path.isfile(source):
            return {"ok": False, "message": f"Backup not found: {clean}"}
        before = self.create_backup(actor=actor, label="before_restore")
        with self._lock:
            tmp = f"{self._db_path}.restore_tmp"
            shutil.copy2(source, tmp)
            os.replace(tmp, self._db_path)
        return {
            "ok": True,
            "message": f"Backup restored: {clean}",
            "restored_filename": clean,
            "safety_backup": before.get("filename"),
            "actor": actor,
        }

    def delete_backup(self, filename: str) -> Dict[str, Any]:
        clean = os.path.basename(str(filename or "").strip())
        if not clean or clean in (".", ".."):
            return {"ok": False, "message": "Invalid backup filename."}
        target = os.path.join(self._get_backup_dir(), clean)
        if not os.path.isfile(target):
            return {"ok": False, "message": f"Backup not found: {clean}"}
        try:
            os.remove(target)
            return {"ok": True, "message": f"Backup deleted: {clean}"}
        except Exception as exc:
            return {"ok": False, "message": f"Delete failed: {exc}"}

    def get_retention_runs(self, limit: int = 50) -> list[Dict[str, Any]]:
        lim = max(1, min(int(limit or 50), 500))
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, run_utc, dry_run, status, details_json FROM retention_runs ORDER BY id DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        out: list[Dict[str, Any]] = []
        for r in rows:
            try:
                details = json.loads(str(r["details_json"] or "{}"))
            except Exception:
                details = {}
            out.append(
                {
                    "id": int(r["id"]),
                    "run_utc": str(r["run_utc"]),
                    "dry_run": bool(r["dry_run"]),
                    "status": str(r["status"]),
                    "details": details,
                }
            )
        return out

    def run_retention(self, dry_run: bool = True, actor: str = "manual") -> Dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        run_utc = now_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        policy = self.get_retention_policy()
        raw_keep_days = int(policy.get("raw_keep_days", 7))
        minute_keep_days = int(policy.get("minute_keep_days", 30))
        hour_keep_days = int(policy.get("hour_keep_days", 180))
        day_keep_days = int(policy.get("day_keep_days", 730))
        max_delete = int(policy.get("max_delete_rows_per_run", 50000))

        raw_cutoff = (now_dt - timedelta(days=raw_keep_days)).strftime("%Y-%m-%d %H:%M:%S")
        minute_cutoff = (now_dt - timedelta(days=minute_keep_days)).strftime("%Y-%m-%d %H:%M:%S")
        hour_cutoff = (now_dt - timedelta(days=hour_keep_days)).strftime("%Y-%m-%d %H:%M:%S")
        day_cutoff = (now_dt - timedelta(days=day_keep_days)).strftime("%Y-%m-%d %H:%M:%S")

        details: Dict[str, Any] = {
            "actor": actor,
            "policy": policy,
            "cutoffs": {
                "raw_cutoff": raw_cutoff,
                "minute_cutoff": minute_cutoff,
                "hour_cutoff": hour_cutoff,
                "day_cutoff": day_cutoff,
            },
            "rollups": {},
            "deletes": {},
        }

        try:
            with self._lock:
                if not dry_run and bool(policy.get("backup_before_cleanup", True)):
                    backup_dir = os.path.join(os.path.dirname(self._db_path), "backups")
                    os.makedirs(backup_dir, exist_ok=True)
                    backup_path = os.path.join(backup_dir, f"trustnode_app_store_{now_dt.strftime('%Y%m%d_%H%M%S')}.db")
                    try:
                        shutil.copy2(self._db_path, backup_path)
                        details["backup_path"] = backup_path
                    except Exception as exc:
                        details["backup_error"] = str(exc)

                with self._connect() as conn:
                    # Rollup raw -> minute window [minute_cutoff, raw_cutoff)
                    rollup_minute_sql = """
                        INSERT OR REPLACE INTO historian_agg_minute
                        (bucket_utc, gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name,
                         avg_value, min_value, max_value, sample_count, quality_min, quality_max, created_utc, updated_utc)
                        SELECT
                          strftime('%Y-%m-%d %H:%M:00', ts_utc) AS bucket_utc,
                          gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name,
                          AVG(value), MIN(value), MAX(value), COUNT(*),
                          MIN(COALESCE(quality,0)), MAX(COALESCE(quality,0)),
                          ?, ?
                        FROM historian_readings
                        WHERE ts_utc >= ? AND ts_utc < ?
                        GROUP BY bucket_utc, gateway_id, database_name, tag_name
                    """
                    cur = conn.execute(rollup_minute_sql, (run_utc, run_utc, minute_cutoff, raw_cutoff))
                    details["rollups"]["minute_upserts"] = int(cur.rowcount if cur.rowcount is not None else 0)

                    # Rollup minute -> hour window [hour_cutoff, minute_cutoff)
                    rollup_hour_sql = """
                        INSERT OR REPLACE INTO historian_agg_hour
                        (bucket_utc, gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name,
                         avg_value, min_value, max_value, sample_count, quality_min, quality_max, created_utc, updated_utc)
                        SELECT
                          strftime('%Y-%m-%d %H:00:00', bucket_utc) AS bucket_utc,
                          gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name,
                          AVG(avg_value), MIN(min_value), MAX(max_value), SUM(sample_count),
                          MIN(COALESCE(quality_min,0)), MAX(COALESCE(quality_max,0)),
                          ?, ?
                        FROM historian_agg_minute
                        WHERE bucket_utc >= ? AND bucket_utc < ?
                        GROUP BY bucket_utc, gateway_id, database_name, tag_name
                    """
                    cur = conn.execute(rollup_hour_sql, (run_utc, run_utc, hour_cutoff, minute_cutoff))
                    details["rollups"]["hour_upserts"] = int(cur.rowcount if cur.rowcount is not None else 0)

                    # Rollup hour -> day window [day_cutoff, hour_cutoff)
                    rollup_day_sql = """
                        INSERT OR REPLACE INTO historian_agg_day
                        (bucket_utc, gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name,
                         avg_value, min_value, max_value, sample_count, quality_min, quality_max, created_utc, updated_utc)
                        SELECT
                          strftime('%Y-%m-%d 00:00:00', bucket_utc) AS bucket_utc,
                          gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name,
                          AVG(avg_value), MIN(min_value), MAX(max_value), SUM(sample_count),
                          MIN(COALESCE(quality_min,0)), MAX(COALESCE(quality_max,0)),
                          ?, ?
                        FROM historian_agg_hour
                        WHERE bucket_utc >= ? AND bucket_utc < ?
                        GROUP BY bucket_utc, gateway_id, database_name, tag_name
                    """
                    cur = conn.execute(rollup_day_sql, (run_utc, run_utc, day_cutoff, hour_cutoff))
                    details["rollups"]["day_upserts"] = int(cur.rowcount if cur.rowcount is not None else 0)

                    # Preview counts
                    raw_del_count = int(conn.execute("SELECT COUNT(*) c FROM historian_readings WHERE ts_utc < ?", (raw_cutoff,)).fetchone()["c"])
                    minute_del_count = int(conn.execute("SELECT COUNT(*) c FROM historian_agg_minute WHERE bucket_utc < ?", (minute_cutoff,)).fetchone()["c"])
                    hour_del_count = int(conn.execute("SELECT COUNT(*) c FROM historian_agg_hour WHERE bucket_utc < ?", (hour_cutoff,)).fetchone()["c"])
                    day_del_count = int(conn.execute("SELECT COUNT(*) c FROM historian_agg_day WHERE bucket_utc < ?", (day_cutoff,)).fetchone()["c"])
                    details["deletes"] = {
                        "raw_candidates": raw_del_count,
                        "minute_candidates": minute_del_count,
                        "hour_candidates": hour_del_count,
                        "day_candidates": day_del_count,
                    }

                    if not dry_run:
                        conn.execute(
                            "DELETE FROM historian_readings WHERE id IN (SELECT id FROM historian_readings WHERE ts_utc < ? LIMIT ?)",
                            (raw_cutoff, max_delete),
                        )
                        conn.execute(
                            "DELETE FROM historian_agg_minute WHERE rowid IN (SELECT rowid FROM historian_agg_minute WHERE bucket_utc < ? LIMIT ?)",
                            (minute_cutoff, max_delete),
                        )
                        conn.execute(
                            "DELETE FROM historian_agg_hour WHERE rowid IN (SELECT rowid FROM historian_agg_hour WHERE bucket_utc < ? LIMIT ?)",
                            (hour_cutoff, max_delete),
                        )
                        conn.execute(
                            "DELETE FROM historian_agg_day WHERE rowid IN (SELECT rowid FROM historian_agg_day WHERE bucket_utc < ? LIMIT ?)",
                            (day_cutoff, max_delete),
                        )
                        details["delete_batch_limit"] = max_delete
                        details["vacuum_recommended"] = True

            self._save_retention_run(run_utc, dry_run=dry_run, status="ok", details=details)
            return {"ok": True, "run_utc": run_utc, "dry_run": dry_run, "details": details}
        except Exception as exc:
            details["error"] = str(exc)
            self._save_retention_run(run_utc, dry_run=dry_run, status="error", details=details)
            return {"ok": False, "run_utc": run_utc, "dry_run": dry_run, "details": details, "message": str(exc)}

    def get_bootstrap(self, prefer_cloud_reads: bool | None = None) -> Dict[str, Any]:
        tenant_id = self._current_tenant_id()
        if prefer_cloud_reads is None:
            prefer_cloud = self._prefer_cloud_reads()
        else:
            prefer_cloud = bool(prefer_cloud_reads)
        if prefer_cloud:
            try:
                # In hosted mode, refresh local config cache from cloud first so
                # web clients see the latest edge-pushed configuration.
                self._pull_config_from_cloud_once()
            except Exception:
                pass
        out: Dict[str, Any] = {}
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute("SELECT domain, payload_json FROM config_documents").fetchall()
                for row in rows:
                    domain = str(row["domain"])
                    payload_text = str(row["payload_json"] or "null")
                    try:
                        out[domain] = json.loads(payload_text)
                    except Exception:
                        out[domain] = None
        if not out:
            try:
                self._pull_config_from_cloud_once()
            except Exception:
                pass
            with self._lock:
                with self._connect() as conn:
                    rows = conn.execute("SELECT domain, payload_json FROM config_documents").fetchall()
                    for row in rows:
                        domain = str(row["domain"])
                        payload_text = str(row["payload_json"] or "null")
                        try:
                            out[domain] = json.loads(payload_text)
                        except Exception:
                            out[domain] = None
        out.setdefault("metadata", {})
        if isinstance(out.get("metadata"), dict):
            out["metadata"]["tenant_id"] = tenant_id
        if prefer_cloud:
            try:
                out = self._apply_live_config_overrides(out)
            except Exception:
                pass
        out["tenant_context"] = {
            "tenant_id": tenant_id,
            "base_host": "trustnode.lsapps.app",
        }
        return out

    def get_bootstrap_scoped(self, scope_key: str, prefer_cloud_reads: bool | None = None) -> Dict[str, Any]:
        skey = str(scope_key or "").strip()
        if not skey:
            return self.get_bootstrap(prefer_cloud_reads=prefer_cloud_reads)
        out = self.get_bootstrap(prefer_cloud_reads=prefer_cloud_reads)
        # Legacy-key fallback: edges activated before linked_customer_id was
        # threaded into edge_profile saved scoped docs under
        # `tenant|-|edge` (2-segment). After the fix the running EXE
        # resolves scope to `tenant|customer|edge` (3-segment) and an
        # otherwise-unmigrated install would see empty arrays for the
        # picker dropdowns. Probe both and overlay so the operator never
        # loses data because of a scope-key shape change.
        legacy_skey = ""
        parts = skey.split("|")
        if len(parts) >= 3 and parts[1] and parts[1] != "-":
            # 3-segment shared scope: legacy is the same with '-' for customer.
            legacy_skey = "|".join([parts[0], "-", *parts[2:]])
        candidate_keys = [skey]
        if legacy_skey and legacy_skey != skey:
            # Read legacy FIRST so the current-scope row overlays it.
            candidate_keys.insert(0, legacy_skey)
        with self._lock:
            with self._connect() as conn:
                for k in candidate_keys:
                    rows = conn.execute(
                        "SELECT domain, payload_json FROM config_documents_scoped WHERE scope_key = ?",
                        (k,),
                    ).fetchall()
                    for row in rows:
                        domain = str(row["domain"] or "").strip()
                        if not domain:
                            continue
                        payload_text = str(row["payload_json"] or "null")
                        try:
                            out[domain] = json.loads(payload_text)
                        except Exception:
                            out[domain] = None
        if isinstance(out.get("metadata"), dict):
            out["metadata"]["scope_key"] = skey
        return out

    def _apply_live_config_overrides(self, bootstrap: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(bootstrap or {})
        live_rows = self._fetch_live_rows_from_cloud(limit=5000)
        if not live_rows:
            return out
        live_gateway_ids = {str(r.get("gateway_id") or "").strip() for r in live_rows if str(r.get("gateway_id") or "").strip()}
        if not live_gateway_ids:
            return out

        existing_gateways = out.get("gateway_configurations")
        configured_ids = set()
        if isinstance(existing_gateways, list):
            configured_ids = {str(g.get("id") or "").strip() for g in existing_gateways if isinstance(g, dict)}
            configured_ids.discard("")
        if configured_ids and configured_ids.intersection(live_gateway_ids):
            return out

        db_configs = out.get("database_configurations")
        db_lookup: Dict[str, str] = {}
        if isinstance(db_configs, list):
            for db in db_configs:
                if not isinstance(db, dict):
                    continue
                db_name = str(db.get("name") or "").strip().lower()
                db_id = str(db.get("id") or "").strip()
                if db_name and db_id:
                    db_lookup[db_name] = db_id

        by_gateway: Dict[str, Dict[str, Any]] = {}
        for row in live_rows:
            gid = str(row.get("gateway_id") or "").strip()
            if not gid:
                continue
            gw = by_gateway.get(gid)
            if not gw:
                source = str(row.get("source") or "").strip().lower()
                gw = {
                    "id": gid,
                    "name": str(row.get("gateway_name") or gid),
                    "tags": [],
                    "plc_ip": str(row.get("plc_ip") or ""),
                    "opc_url": "",
                    "device_id": f"dev-auto-{gid}",
                    "database_id": "",
                    "interval_ms": 1000,
                    "gateway_type": "siemens_opcua" if "siemens" in source else "allen_bradley",
                    "last_check_utc": str(row.get("ts") or ""),
                }
                by_gateway[gid] = gw
            tag = str(row.get("tag") or row.get("tag_name") or "").strip()
            if tag and tag not in gw["tags"]:
                gw["tags"].append(tag)
            db_name = str(row.get("database_name") or "").strip().lower()
            if db_name and not gw.get("database_id"):
                gw["database_id"] = db_lookup.get(db_name, "")

        inferred_gateways = list(by_gateway.values())
        inferred_devices = [
            {
                "id": g["device_id"],
                "name": g["name"] or g["id"],
                "notes": "Auto-inferred from live cloud rows",
                "plc_ip": g.get("plc_ip", ""),
                "opc_url": g.get("opc_url", ""),
                "ping_ok": True,
                "port_ok": True,
                "last_test": "Live cloud reading",
                "opc_node_id": "",
                "protocol_ok": True,
                "gateway_type": g.get("gateway_type", "allen_bradley"),
                "opc_node_ids": [],
                "connection_ok": True,
                "opc_node_ids_text": "",
                "last_check_utc": g.get("last_check_utc", ""),
            }
            for g in inferred_gateways
        ]
        inferred_widgets = []
        for g in inferred_gateways:
            for tag in list(g.get("tags") or [])[:4]:
                inferred_widgets.append(
                    {
                        "id": f"dw-auto-{g['id']}-{hashlib.md5(tag.encode('utf-8')).hexdigest()[:8]}",
                        "color": "#16a34a",
                        "title": tag,
                        "tag_name": tag,
                        "chart_type": "line",
                        "gateway_id": g["id"],
                        "readings_count": 120,
                    }
                )
                if len(inferred_widgets) >= 12:
                    break
            if len(inferred_widgets) >= 12:
                break

        out["devices"] = inferred_devices
        out["gateway_configurations"] = inferred_gateways
        dash = out.get("dashboard_configurations")
        if not isinstance(dash, dict):
            dash = {}
        dash["widgets"] = inferred_widgets
        dash["mode"] = dash.get("mode") or "chart"
        dash["per_row"] = int(dash.get("per_row") or 2)
        out["dashboard_configurations"] = dash
        return out

    def _normalize_edge_filter(self, edge_id: str) -> str:
        return str(edge_id or "").strip().lower()

    def _build_edge_selector_maps(self) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        gateway_to_edge_ids: dict[str, set[str]] = {}
        db_name_to_edge_ids: dict[str, set[str]] = {}
        try:
            db_configs_raw = self.get_config_domain("database_configurations")
            db_configs = db_configs_raw if isinstance(db_configs_raw, list) else []
            db_id_to_name: dict[str, str] = {}
            for db in db_configs:
                if not isinstance(db, dict):
                    continue
                if db.get("enabled") is False:
                    continue
                if db.get("cloud_sync_enabled") is False:
                    continue
                db_id = str(db.get("id") or "").strip()
                db_name = str(db.get("name") or "").strip()
                source = str(db.get("source") or "").strip() or "unknown-source"
                site = str(db.get("site") or "").strip() or "unknown-site"
                area = str(db.get("area") or "").strip() or "unknown-area"
                equipment = str(db.get("equipment") or "").strip() or "unknown-equipment"
                composite = f"{source}||{site}||{area}||{equipment}".lower()
                configured_edge = str(db.get("edge_id") or "").strip().lower()
                edge_ids = {composite}
                if configured_edge:
                    edge_ids.add(configured_edge)
                if db_name:
                    db_name_to_edge_ids.setdefault(db_name.lower(), set()).update(edge_ids)
                if db_id and db_name:
                    db_id_to_name[db_id] = db_name.lower()

            gw_configs_raw = self.get_config_domain("gateway_configurations")
            gw_configs = gw_configs_raw if isinstance(gw_configs_raw, list) else []
            for gw in gw_configs:
                if not isinstance(gw, dict):
                    continue
                gid = str(gw.get("id") or "").strip()
                if not gid:
                    continue
                db_id = str(gw.get("database_id") or "").strip()
                db_name_key = db_id_to_name.get(db_id, "")
                if not db_name_key:
                    continue
                edge_ids = db_name_to_edge_ids.get(db_name_key, set())
                if edge_ids:
                    gateway_to_edge_ids.setdefault(gid, set()).update(edge_ids)
        except Exception:
            return {}, {}
        return gateway_to_edge_ids, db_name_to_edge_ids

    def _row_matches_edge_filter(
        self,
        row: dict[str, Any],
        edge_filter: str,
        gateway_to_edge_ids: dict[str, set[str]],
        db_name_to_edge_ids: dict[str, set[str]],
    ) -> bool:
        if not edge_filter:
            return True
        source = str(row.get("source") or "").strip() or "unknown-source"
        site = str(row.get("site") or "").strip() or "unknown-site"
        area = str(row.get("area") or "").strip() or "unknown-area"
        equipment = str(row.get("equipment") or "").strip() or "unknown-equipment"
        row_composite = f"{source}||{site}||{area}||{equipment}".lower()
        if row_composite == edge_filter:
            return True
        gid = str(row.get("gateway_id") or "").strip()
        if gid and edge_filter in gateway_to_edge_ids.get(gid, set()):
            return True
        db_name = str(row.get("database_name") or "").strip().lower()
        if db_name and edge_filter in db_name_to_edge_ids.get(db_name, set()):
            return True
        return False

    def _filter_rows_by_tenant(self, rows: list[dict[str, Any]], tenant_id: str) -> list[dict[str, Any]]:
        """Drop any row whose tenant_id doesn't match the caller's tenant.

        Belt-and-braces against bugs in upstream fetchers — even if a code
        path forgets to scope its SQL query (or pulls from a shared cache),
        this filter ensures the route never returns another tenant's data.
        Rows missing tenant_id are kept (legacy local-only data); explicit
        mismatches are dropped.
        """
        tid = str(tenant_id or "").strip().lower()
        if not tid:
            return rows
        out: list[dict[str, Any]] = []
        for row in rows or []:
            row_tenant = str((row or {}).get("tenant_id") or "").strip().lower()
            if not row_tenant or row_tenant == tid:
                out.append(row)
        return out

    def _filter_rows_by_edge(self, rows: list[dict[str, Any]], edge_id: str) -> list[dict[str, Any]]:
        edge_filter = self._normalize_edge_filter(edge_id)
        if not edge_filter:
            return rows
        gateway_to_edge_ids, db_name_to_edge_ids = self._build_edge_selector_maps()
        return [
            row
            for row in rows
            if self._row_matches_edge_filter(
                row=row,
                edge_filter=edge_filter,
                gateway_to_edge_ids=gateway_to_edge_ids,
                db_name_to_edge_ids=db_name_to_edge_ids,
            )
        ]

    def force_sync_now(self, actor: str = "manual") -> Dict[str, Any]:
        started_utc = self._utc_now()
        errors: list[str] = []
        try:
            self._compact_sync_outbox_for_domains()
        except Exception:
            pass
        try:
            self._pull_config_from_cloud_once()
        except Exception as exc:
            errors.append(f"pull_config: {exc}")
        try:
            self._flush_config_outbox_once()
        except Exception as exc:
            errors.append(f"push_config: {exc}")
        # One-time repair: scope_keys stuck at 'tenant|-|edge' get rewritten
        # to 'tenant|customer|edge' once customer_id is back in app_settings.
        try:
            self._repair_scope_keys_with_customer_id()
        except Exception as exc:
            errors.append(f"repair_scope: {exc}")
        # Re-mirror every scoped config_documents_scoped row that targets the
        # Lite-visible tables. The fire-and-forget mirror only triggers on
        # save; a manual "push sync" must republish historical rows too —
        # otherwise edges that lost their scope (customer_id missing) won't
        # appear in Lite even after the scope key is repaired.
        try:
            self._remirror_scoped_docs_to_cloud()
        except Exception as exc:
            errors.append(f"remirror_scoped: {exc}")
        try:
            self._flush_live_outbox_once()
        except Exception as exc:
            errors.append(f"push_live: {exc}")
        try:
            self._flush_data_outbox_once()
        except Exception as exc:
            errors.append(f"push_data: {exc}")

        snap = self.get_inspector_snapshot(preview_limit=20)
        outbox = (snap.get("sync_outbox_status") or {}) if isinstance(snap, dict) else {}
        data_sync = (snap.get("data_sync") or {}) if isinstance(snap, dict) else {}
        sync_target = (snap.get("sync_target") or {}) if isinstance(snap, dict) else {}
        last_error = str(sync_target.get("last_error") or "")
        if last_error:
            errors.append(last_error)
        data_error = str(data_sync.get("last_data_error") or "")
        if data_error:
            errors.append(data_error)

        summary = {
            "config_pending": int(outbox.get("pending") or 0),
            "config_failed": int(outbox.get("failed") or 0),
            "config_sent_total": int(outbox.get("sent") or 0),
            "historian_backlog": int(data_sync.get("historian_backlog") or 0),
            "logs_backlog": int(data_sync.get("logs_backlog") or 0),
            "historian_synced_total": int(data_sync.get("total_historian_synced") or 0),
            "logs_synced_total": int(data_sync.get("total_logs_synced") or 0),
            "last_config_sync_utc": str(sync_target.get("last_sync_utc") or ""),
            "last_data_sync_utc": str(data_sync.get("last_data_sync_utc") or ""),
        }
        return {
            "ok": len(errors) == 0,
            "actor": actor,
            "started_utc": started_utc,
            "finished_utc": self._utc_now(),
            "errors": errors,
            "summary": summary,
            "inspector": snap,
        }

    def clear_sync_queue(self, actor: str = "manual", include_sent: bool = False) -> Dict[str, Any]:
        now = self._utc_now()
        with self._lock:
            with self._connect() as conn:
                if include_sent:
                    deleted = int(conn.execute("DELETE FROM sync_outbox").rowcount or 0)
                else:
                    deleted = int(conn.execute("DELETE FROM sync_outbox WHERE status IN ('pending','failed')").rowcount or 0)
        return {
            "ok": True,
            "actor": actor,
            "run_utc": now,
            "include_sent": bool(include_sent),
            "deleted_rows": deleted,
        }

    def drop_data_backlog(self, actor: str = "manual") -> Dict[str, Any]:
        now = self._utc_now()
        with self._lock:
            with self._connect() as conn:
                row_hist = conn.execute("SELECT COALESCE(MAX(id), 0) AS v FROM historian_readings").fetchone()
                row_logs = conn.execute("SELECT COALESCE(MAX(id), 0) AS v FROM app_logs").fetchone()
        max_hist = int((row_hist["v"] if row_hist else 0) or 0)
        max_logs = int((row_logs["v"] if row_logs else 0) or 0)
        self._set_data_sync_state(
            last_historian_id=max_hist,
            last_log_id=max_logs,
            last_data_sync_utc=now,
            last_data_error="",
        )
        return {
            "ok": True,
            "actor": actor,
            "run_utc": now,
            "last_historian_id": max_hist,
            "last_log_id": max_logs,
        }

    def manual_sync_data_period(
        self,
        from_utc: str,
        to_utc: str,
        actor: str = "manual",
        max_rows: int = 20000,
        include_logs: bool = False,
    ) -> Dict[str, Any]:
        cloud = self._get_cloud_database_target()
        if not cloud:
            return {"ok": False, "message": "No enabled PostgreSQL cloud target configured"}

        try:
            from_dt = datetime.fromisoformat(str(from_utc).replace("Z", "+00:00")).astimezone(timezone.utc)
            to_dt = datetime.fromisoformat(str(to_utc).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return {"ok": False, "message": "Invalid period format. Use UTC datetime values."}
        if to_dt <= from_dt:
            return {"ok": False, "message": "Invalid period: 'to' must be greater than 'from'."}

        lim = max(100, min(int(max_rows or 20000), 200000))
        tenant_id = self._current_tenant_id()
        from_txt = from_dt.strftime("%Y-%m-%d %H:%M:%S")
        to_txt = to_dt.strftime("%Y-%m-%d %H:%M:%S")
        run_utc = self._utc_now()

        try:
            from sqlalchemy import create_engine, text  # type: ignore
        except Exception as exc:
            return {"ok": False, "message": f"SQLAlchemy unavailable: {exc}"}

        with self._lock:
            with self._connect() as conn:
                hist_rows = conn.execute(
                    """
                    SELECT id, tenant_id, ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name,
                           tag_name, value, quality, quality_label, source, created_utc
                    FROM historian_readings
                    WHERE tenant_id = ? AND ts_utc >= ? AND ts_utc <= ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (tenant_id, from_txt, to_txt, lim),
                ).fetchall()
                log_rows = []
                if include_logs:
                    log_rows = conn.execute(
                        """
                        SELECT id, tenant_id, ts_utc, level, category, message, gateway_id, gateway_name, device_name, database_name, created_utc
                        FROM app_logs
                        WHERE tenant_id = ? AND ts_utc >= ? AND ts_utc <= ?
                        ORDER BY id ASC
                        LIMIT ?
                        """,
                        (tenant_id, from_txt, to_txt, lim),
                    ).fetchall()

        if not hist_rows and not log_rows:
            return {
                "ok": True,
                "actor": actor,
                "run_utc": run_utc,
                "hist_rows": 0,
                "log_rows": 0,
                "message": "No local rows found in selected period.",
            }

        schema = str(cloud.get("schema") or "public")
        url = self._build_pg_sqlalchemy_url(
            str(cloud.get("host") or ""),
            int(cloud.get("port") or 5432),
            str(cloud.get("database") or "postgres"),
            str(cloud.get("username") or ""),
            str(cloud.get("password") or ""),
        )
        connect_args = {
            "sslmode": "require" if cloud.get("tls", True) else "disable",
            "connect_timeout": max(1, int(os.environ.get("TRUSTNODE_CLOUD_DB_CONNECT_TIMEOUT_SECONDS", "6") or "6")),
            "prepare_threshold": None,
            "options": os.environ.get(
                "TRUSTNODE_CLOUD_DB_OPTIONS",
                "-c lock_timeout=1200ms -c statement_timeout=4500ms",
            ),
        }
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        target_key = self._cloud_target_schema_key(cloud, schema)
        try:
            try:
                self._ensure_cloud_schema_once(engine, schema, target_key)
            except Exception:
                pass

            live_latest_rows: list[dict[str, Any]] = []
            seen_latest: set[tuple[str, str, str]] = set()
            for r in reversed(hist_rows):
                t_id = normalize_tenant_id(str(r["tenant_id"] or "default"))
                gateway_id = str(r["gateway_id"] or "")
                tag_name = str(r["tag_name"] or "")
                if not tag_name:
                    continue
                key = (t_id, gateway_id, tag_name)
                if key in seen_latest:
                    continue
                seen_latest.add(key)
                live_latest_rows.append(
                    {
                        "tenant_id": t_id,
                        "gateway_id": gateway_id,
                        "tag_name": tag_name,
                        "ts_utc": str(r["ts_utc"] or ""),
                        "source": str(r["source"] or ""),
                        "gateway_name": str(r["gateway_name"] or ""),
                        "device_name": str(r["device_name"] or ""),
                        "plc_ip": str(r["plc_ip"] or ""),
                        "database_name": str(r["database_name"] or ""),
                        "value": r["value"],
                        "quality": r["quality"],
                        "quality_label": str(r["quality_label"] or ""),
                        "updated_utc": run_utc,
                    }
                )

            hist_payload = [
                {
                    "local_id": int(r["id"]),
                    "tenant_id": normalize_tenant_id(str(r["tenant_id"] or "default")),
                    "ts_utc": str(r["ts_utc"] or ""),
                    "gateway_id": str(r["gateway_id"] or ""),
                    "gateway_name": str(r["gateway_name"] or ""),
                    "device_name": str(r["device_name"] or ""),
                    "plc_ip": str(r["plc_ip"] or ""),
                    "database_name": str(r["database_name"] or ""),
                    "tag_name": str(r["tag_name"] or ""),
                    "value": r["value"],
                    "quality": r["quality"],
                    "quality_label": str(r["quality_label"] or ""),
                    "source": str(r["source"] or ""),
                    "created_utc": str(r["created_utc"] or ""),
                }
                for r in hist_rows
            ]
            logs_payload = [
                {
                    "local_id": int(r["id"]),
                    "tenant_id": normalize_tenant_id(str(r["tenant_id"] or "default")),
                    "ts_utc": str(r["ts_utc"] or ""),
                    "level": str(r["level"] or "info"),
                    "category": str(r["category"] or "system"),
                    "message": str(r["message"] or ""),
                    "gateway_id": str(r["gateway_id"] or ""),
                    "gateway_name": str(r["gateway_name"] or ""),
                    "device_name": str(r["device_name"] or ""),
                    "database_name": str(r["database_name"] or ""),
                    "created_utc": str(r["created_utc"] or ""),
                }
                for r in log_rows
            ]

            with engine.begin() as conn:
                if live_latest_rows:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."live_latest"
                            (tenant_id, gateway_id, tag_name, ts_utc, source, gateway_name, device_name, plc_ip, database_name, value, quality, quality_label, updated_utc)
                            VALUES
                            (:tenant_id, :gateway_id, :tag_name, CAST(:ts_utc AS timestamptz), :source, :gateway_name, :device_name, :plc_ip, :database_name, :value, :quality, :quality_label, CAST(:updated_utc AS timestamptz))
                            ON CONFLICT(tenant_id, gateway_id, tag_name) DO UPDATE SET
                              ts_utc = excluded.ts_utc,
                              source = excluded.source,
                              gateway_name = excluded.gateway_name,
                              device_name = excluded.device_name,
                              plc_ip = excluded.plc_ip,
                              database_name = excluded.database_name,
                              value = excluded.value,
                              quality = excluded.quality,
                              quality_label = excluded.quality_label,
                              updated_utc = excluded.updated_utc
                            """
                        ),
                        live_latest_rows,
                    )
                if hist_payload:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."historian_readings"
                            (local_id, tenant_id, ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name, value, quality, quality_label, source, created_utc)
                            VALUES
                            (:local_id, :tenant_id, CAST(:ts_utc AS timestamptz), :gateway_id, :gateway_name, :device_name, :plc_ip, :database_name, :tag_name, :value, :quality, :quality_label, :source, CAST(:created_utc AS timestamptz))
                            ON CONFLICT(tenant_id, gateway_id, local_id) DO NOTHING
                            """
                        ),
                        hist_payload,
                    )
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."plc_readings"
                            (local_id, tenant_id, ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name, value, quality, quality_label, source, created_utc)
                            VALUES
                            (:local_id, :tenant_id, CAST(:ts_utc AS timestamptz), :gateway_id, :gateway_name, :device_name, :plc_ip, :database_name, :tag_name, :value, :quality, :quality_label, :source, CAST(:created_utc AS timestamptz))
                            ON CONFLICT(tenant_id, gateway_id, local_id) DO NOTHING
                            """
                        ),
                        hist_payload,
                    )
                if logs_payload:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."app_logs"
                            (local_id, tenant_id, ts_utc, level, category, message, gateway_id, gateway_name, device_name, database_name, created_utc)
                            VALUES
                            (:local_id, :tenant_id, CAST(:ts_utc AS timestamptz), :level, :category, :message, :gateway_id, :gateway_name, :device_name, :database_name, CAST(:created_utc AS timestamptz))
                            ON CONFLICT(tenant_id, gateway_id, local_id) DO NOTHING
                            """
                        ),
                        logs_payload,
                    )

            self._set_data_sync_state(last_data_sync_utc=run_utc, last_data_error="")
            return {
                "ok": True,
                "actor": actor,
                "run_utc": run_utc,
                "from_utc": from_txt,
                "to_utc": to_txt,
                "hist_rows": len(hist_rows),
                "log_rows": len(log_rows),
                "truncated": bool(len(hist_rows) >= lim or len(log_rows) >= lim),
            }
        except Exception as exc:
            self._set_data_sync_state(last_data_error=f"Manual sync failed: {exc}")
            return {"ok": False, "message": f"Manual sync failed: {exc}"}
        finally:
            try:
                engine.dispose()
            except Exception:
                pass

    def reset_all_data_and_config(self, actor: str = "manual", clear_cloud_data: bool = True) -> Dict[str, Any]:
        now = self._utc_now()
        cloud_result: Dict[str, Any] = {"attempted": False, "ok": True, "message": ""}
        if clear_cloud_data:
            cloud = self._get_cloud_database_target()
            if cloud:
                cloud_result["attempted"] = True
                try:
                    from sqlalchemy import create_engine, text  # type: ignore

                    schema = str(cloud.get("schema") or "public")
                    url = self._build_pg_sqlalchemy_url(
                        str(cloud.get("host") or ""),
                        int(cloud.get("port") or 5432),
                        str(cloud.get("database") or "postgres"),
                        str(cloud.get("username") or ""),
                        str(cloud.get("password") or ""),
                    )
                    connect_args = {
                        "sslmode": "require" if cloud.get("tls", True) else "disable",
                        "connect_timeout": max(1, int(os.environ.get("TRUSTNODE_CLOUD_DB_CONNECT_TIMEOUT_SECONDS", "6") or "6")),
                        "prepare_threshold": None,
                        "options": os.environ.get(
                            "TRUSTNODE_CLOUD_DB_OPTIONS",
                            "-c lock_timeout=1200ms -c statement_timeout=4500ms",
                        ),
                    }
                    engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
                    try:
                        with engine.begin() as conn:
                            for table_name in (
                                "live_latest",
                                "historian_readings",
                                "plc_readings",
                                "app_logs",
                                "config_audit",
                                "config_documents",
                                "sync_outbox",
                                "sync_targets",
                            ):
                                try:
                                    conn.execute(text(f'DELETE FROM "{schema}"."{table_name}"'))
                                except Exception:
                                    pass
                    finally:
                        try:
                            engine.dispose()
                        except Exception:
                            pass
                except Exception as exc:
                    cloud_result["ok"] = False
                    cloud_result["message"] = str(exc)

        with self._lock:
            with self._connect() as conn:
                for table_name in (
                    "historian_readings",
                    "app_logs",
                    "historian_agg_minute",
                    "historian_agg_hour",
                    "historian_agg_day",
                    "sync_outbox",
                    "config_audit",
                    "retention_runs",
                ):
                    try:
                        conn.execute(f"DELETE FROM {table_name}")
                    except Exception:
                        pass
                try:
                    conn.execute("DELETE FROM config_documents")
                except Exception:
                    pass
                conn.execute(
                    """
                    INSERT INTO data_sync_state
                    (id, last_historian_id, last_log_id, last_data_sync_utc, last_data_error, total_historian_synced, total_logs_synced, updated_utc)
                    VALUES(1, 0, 0, ?, '', 0, 0, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      last_historian_id = 0,
                      last_log_id = 0,
                      last_data_sync_utc = excluded.last_data_sync_utc,
                      last_data_error = '',
                      total_historian_synced = 0,
                      total_logs_synced = 0,
                      updated_utc = excluded.updated_utc
                    """,
                    (now, now),
                )

        # Re-seed required docs/default local DB after wipe.
        self._ensure_required_config_domains()
        self._ensure_default_database_configuration()
        self._backfill_outbox_for_existing_domains()

        return {
            "ok": bool(cloud_result.get("ok", True)),
            "actor": actor,
            "run_utc": now,
            "cloud": cloud_result,
            "message": "Reset completed. Local data/config cleared and defaults re-seeded.",
        }

    def get_inspector_snapshot(self, preview_limit: int = 10) -> Dict[str, Any]:
        tenant_id = self._current_tenant_id()
        lim = max(1, min(int(preview_limit or 10), 100))
        db_exists = os.path.exists(self._db_path)
        db_size = os.path.getsize(self._db_path) if db_exists else 0
        tables: list[Dict[str, Any]] = []
        domains: list[Dict[str, Any]] = []
        outbox_status: Dict[str, int] = {"pending": 0, "failed": 0, "sent": 0, "other": 0}
        sync_target: Dict[str, Any] | None = None
        data_sync: Dict[str, Any] = {
            "last_historian_id": 0,
            "last_log_id": 0,
            "last_data_sync_utc": "",
            "last_data_error": "",
            "total_historian_synced": 0,
            "total_logs_synced": 0,
            "local_historian_rows": 0,
            "local_log_rows": 0,
            "historian_backlog": 0,
            "logs_backlog": 0,
        }

        with self._lock:
            with self._connect() as conn:
                table_rows = conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                ).fetchall()
                for r in table_rows:
                    table_name = str(r["name"])
                    row_count = 0
                    try:
                        row = conn.execute(f'SELECT COUNT(*) AS c FROM "{table_name}"').fetchone()
                        row_count = int(row["c"] if row else 0)
                    except Exception:
                        row_count = -1
                    tables.append({"name": table_name, "rows": row_count})
                    if table_name == "historian_readings":
                        data_sync["local_historian_rows"] = max(0, row_count)
                    if table_name == "app_logs":
                        data_sync["local_log_rows"] = max(0, row_count)

                domain_rows = conn.execute(
                    """
                    SELECT domain, version, updated_utc
                    FROM config_documents
                    ORDER BY updated_utc DESC
                    LIMIT ?
                    """,
                    (lim,),
                ).fetchall()
                for r in domain_rows:
                    domains.append(
                        {
                            "domain": str(r["domain"] or ""),
                            "version": int(r["version"] or 0),
                            "updated_utc": str(r["updated_utc"] or ""),
                        }
                    )

                status_rows = conn.execute(
                    "SELECT status, COUNT(*) AS c FROM sync_outbox GROUP BY status"
                ).fetchall()
                for r in status_rows:
                    status = str(r["status"] or "").lower()
                    count = int(r["c"] or 0)
                    if status in outbox_status:
                        outbox_status[status] = count
                    else:
                        outbox_status["other"] += count

                sync_row = conn.execute(
                    """
                    SELECT id, name, target_type, enabled, last_sync_utc, last_error, updated_utc, config_json
                    FROM sync_targets
                    WHERE id = 'auto_cloud_config'
                    """
                ).fetchone()
                if sync_row:
                    cfg_raw = str(sync_row["config_json"] or "{}")
                    try:
                        cfg = json.loads(cfg_raw)
                    except Exception:
                        cfg = {}
                    sync_target = {
                        "id": str(sync_row["id"] or ""),
                        "name": str(sync_row["name"] or ""),
                        "target_type": str(sync_row["target_type"] or ""),
                        "enabled": bool(sync_row["enabled"]),
                        "last_sync_utc": str(sync_row["last_sync_utc"] or ""),
                        "last_error": str(sync_row["last_error"] or ""),
                        "updated_utc": str(sync_row["updated_utc"] or ""),
                        "config": cfg,
                    }
                ds_row = conn.execute(
                    """
                    SELECT last_historian_id, last_log_id, last_data_sync_utc, last_data_error, total_historian_synced, total_logs_synced
                    FROM data_sync_state
                    WHERE id = 1
                    """
                ).fetchone()
                if ds_row:
                    data_sync.update(
                        {
                            "last_historian_id": int(ds_row["last_historian_id"] or 0),
                            "last_log_id": int(ds_row["last_log_id"] or 0),
                            "last_data_sync_utc": str(ds_row["last_data_sync_utc"] or ""),
                            "last_data_error": str(ds_row["last_data_error"] or ""),
                            "total_historian_synced": int(ds_row["total_historian_synced"] or 0),
                            "total_logs_synced": int(ds_row["total_logs_synced"] or 0),
                        }
                    )
                # Backlog must compare apples to apples: data_sync_state stores
                # the LAST AUTO-INCREMENT id we pushed, not a row count. After
                # retention deletes old rows, COUNT(*) drops below that id and
                # the old formula went negative, getting clamped to 0 — which
                # was the "backlog always 0" the operator saw. Use MAX(id)
                # (the current high-water-mark on the table) instead. Cheap:
                # SQLite reads it via the PK index in O(log N).
                max_hist_id = 0
                max_log_id = 0
                try:
                    row_max = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM historian_readings").fetchone()
                    max_hist_id = int(row_max["m"] if row_max else 0)
                except Exception:
                    max_hist_id = 0
                try:
                    row_max_l = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM app_logs").fetchone()
                    max_log_id = int(row_max_l["m"] if row_max_l else 0)
                except Exception:
                    max_log_id = 0
                data_sync["max_historian_id"] = max_hist_id
                data_sync["max_log_id"] = max_log_id
                data_sync["historian_backlog"] = max(0, max_hist_id - int(data_sync["last_historian_id"]))
                data_sync["logs_backlog"] = max(0, max_log_id - int(data_sync["last_log_id"]))

        cloud = self._get_cloud_database_target()
        if cloud:
            cloud = {
                "id": cloud.get("id"),
                "name": cloud.get("name"),
                "engine": cloud.get("engine"),
                "host": cloud.get("host"),
                "port": cloud.get("port"),
                "database": cloud.get("database"),
                "schema": cloud.get("schema"),
                "tls": cloud.get("tls"),
            }

        return {
            "tenant_id": tenant_id,
            "db_path": self._db_path,
            "db_exists": db_exists,
            "db_size_bytes": int(db_size),
            "table_count": len(tables),
            "tables": tables,
            "config_domains_preview": domains,
            "sync_outbox_status": outbox_status,
            "data_sync": data_sync,
            "cloud_target": cloud,
            "sync_target": sync_target,
        }

    def upsert_domain(self, domain: str, payload: Any, actor: str = "system") -> Dict[str, Any]:
        now = self._utc_now()
        domain_name = str(domain or "").strip()
        payload_to_store = payload
        if domain_name == "users_access":
            payload_to_store = self._normalize_users_access_payload(payload)
        with self._lock:
            with self._connect() as conn:
                prev = conn.execute(
                    "SELECT version, payload_json, updated_utc FROM config_documents WHERE domain = ?",
                    (domain,),
                ).fetchone()
                old_version = int(prev["version"]) if prev else 0
                prev_payload_obj: Any = None
                if prev and prev["payload_json"] is not None:
                    try:
                        prev_payload_obj = json.loads(str(prev["payload_json"] or "null"))
                    except Exception:
                        prev_payload_obj = None
                if domain_name == "database_configurations":
                    prev_payload: Any = prev_payload_obj if isinstance(prev_payload_obj, list) else []
                    payload_to_store = self._normalize_database_configurations_payload(payload_to_store, prev_payload)
                elif domain_name == "app_settings":
                    payload_to_store = self._normalize_app_settings_payload(payload_to_store, prev_payload_obj)
                elif domain_name in {
                    "metadata",
                    "devices",
                    "gateway_configurations",
                    "database_configurations",
                    "power_management_config",
                }:
                    payload_to_store = self._strip_runtime_fields_for_config_sync(payload_to_store)
                if domain_name == "gateway_configurations":
                    # Same stabiliser as scoped path: re-create at same plc_ip
                    # keeps the previous gateway id so widgets/alarms/triggers
                    # don't go blank.
                    try:
                        payload_to_store = self._stabilise_gateway_ids_by_plc_ip(
                            new_payload=payload_to_store,
                            prev_payload=prev_payload_obj,
                        )
                    except Exception:
                        pass
                payload_json = self._canonical_json(payload_to_store)
                prev_payload_json = ""
                if prev:
                    prev_raw = str(prev["payload_json"] or "")
                    try:
                        prev_payload_json = self._canonical_json(json.loads(prev_raw))
                    except Exception:
                        prev_payload_json = prev_raw
                # No-op write: avoid audit/outbox churn when payload is unchanged.
                if prev and prev_payload_json == payload_json:
                    # Still fire the cloud mirror even on no-op — same
                    # reason as the scoped path above. Older edges saved
                    # configs to the unscoped table before the mirror
                    # existed; once they upgrade, every save is a no-op
                    # and nothing reaches Lite. Synthesize a scope_key
                    # from app_settings so the mirror lands in a row
                    # Lite can actually read.
                    if domain in (
                        "dashboard_configurations",
                        "alarms_setup",
                        "triggers_limits",
                        "gateway_configurations",
                        "devices",
                        "power_management_config",
                    ):
                        try:
                            settings_row = conn.execute(
                                "SELECT payload_json FROM config_documents WHERE domain='app_settings'"
                            ).fetchone()
                            settings = {}
                            if settings_row and settings_row["payload_json"]:
                                settings = json.loads(str(settings_row["payload_json"] or "{}")) or {}
                            edge_profile = settings.get("edge_profile") if isinstance(settings.get("edge_profile"), dict) else {}
                            edge_id = (
                                str(edge_profile.get("edge_id") or "").strip().lower()
                                or str(settings.get("edge_id") or "").strip().lower()
                                or str(getattr(self, "_local_edge_id", "") or "").strip().lower()
                            )
                            tenant_id = str(settings.get("tenant_id") or "default").strip().lower() or "default"
                            customer_id = (
                                str(edge_profile.get("linked_customer_id") or "").strip().lower()
                                or str(settings.get("customer_id") or "").strip().lower()
                                or "-"
                            )
                            if edge_id:
                                synthetic_scope = f"{tenant_id}|{customer_id}|{edge_id}"
                                self._mirror_config_doc_to_cloud(
                                    domain,
                                    tenant_id=tenant_id,
                                    scope_key=synthetic_scope,
                                    payload_json=str(prev["payload_json"] or "null"),
                                    version=int(old_version),
                                    updated_utc=str(prev["updated_utc"] or now),
                                )
                        except Exception:
                            pass
                    return {
                        "domain": domain,
                        "tenant_id": self._current_tenant_id(),
                        "version": old_version,
                        "updated_utc": str(prev["updated_utc"] or now),
                        "unchanged": True,
                    }
                new_version = old_version + 1
                conn.execute(
                    """
                    INSERT INTO config_documents(domain, payload_json, version, updated_utc)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(domain) DO UPDATE SET
                      payload_json = excluded.payload_json,
                      version = excluded.version,
                      updated_utc = excluded.updated_utc
                    """,
                    (domain, payload_json, new_version, now),
                )
                conn.execute(
                    """
                    INSERT INTO config_audit(domain, actor, old_version, new_version, changed_utc)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (domain, actor, old_version if old_version > 0 else None, new_version, now),
                )
                if not self._disable_config_push:
                    pending_row = conn.execute(
                        """
                        SELECT id
                        FROM sync_outbox
                        WHERE domain = ? AND status IN ('pending', 'failed')
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (domain,),
                    ).fetchone()
                    if pending_row:
                        conn.execute(
                            """
                            UPDATE sync_outbox
                            SET payload_json = ?, status = 'pending', retries = 0, last_error = NULL, updated_utc = ?
                            WHERE id = ?
                            """,
                            (payload_json, now, int(pending_row["id"])),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO sync_outbox(domain, entity_key, payload_json, status, retries, last_error, created_utc, updated_utc)
                            VALUES(?, ?, ?, 'pending', 0, NULL, ?, ?)
                            """,
                            (domain, domain, payload_json, now, now),
                        )
        self._sync_wakeup_event.set()
        if domain == "database_configurations":
            self._invalidate_cloud_target_cache()
            # Reflect the new config into sync_targets immediately so the
            # sync worker doesn't have to wait up to 10s for the periodic
            # reconcile to pick it up.
            try:
                self._reconcile_sync_targets_with_config()
            except Exception:
                pass
        if domain in (
            "dashboard_configurations",
            "alarms_setup",
            "triggers_limits",
            "gateway_configurations",
            "devices",
        ):
            self._mirror_config_doc_to_cloud(
                domain,
                tenant_id=self._current_tenant_id(),
                scope_key="",
                payload_json=payload_json,
                version=new_version,
                updated_utc=now,
            )
        # Operator 2026-06-17 (M5): mirror EVERY config domain into the
        # customer DB when database_mode=customer_sql, so the LAN Lite
        # and any fresh-edge re-install can re-hydrate from there.
        try:
            self._mirror_config_doc_to_customer_db(
                domain=domain,
                scope_key="",
                payload_json=payload_json,
                version=new_version,
                updated_utc=now,
                actor=actor,
            )
        except Exception:
            pass
        return {"domain": domain, "tenant_id": self._current_tenant_id(), "version": new_version, "updated_utc": now}

    def upsert_domain_scoped(self, scope_key: str, domain: str, payload: Any, actor: str = "system") -> Dict[str, Any]:
        skey = str(scope_key or "").strip()
        if not skey:
            return self.upsert_domain(domain, payload, actor=actor)
        now = self._utc_now()
        domain_name = str(domain or "").strip()
        payload_to_store = payload
        if domain_name == "users_access":
            payload_to_store = self._normalize_users_access_payload(payload)
        if domain_name in {
            "metadata",
            "devices",
            "gateway_configurations",
            "database_configurations",
            "power_management_config",
        }:
            payload_to_store = self._strip_runtime_fields_for_config_sync(payload_to_store)
        # app_settings on the scoped path is also partial-patched from the UI
        # (e.g. Edge Identity saves only { edge_profile }). Merge with the
        # previous scoped doc so activation fields — customer_id, license_id,
        # edge_linked — survive unrelated saves. Without this the 3-segment
        # scope key collapses back to 'tenant|-|edge' on reopen and cloud
        # mirror writes drop out of the Lite customer view.
        if domain_name == "app_settings":
            try:
                prev_for_merge = self._load_previous_scoped_payload(skey, domain_name)
            except Exception:
                prev_for_merge = None
            payload_to_store = self._normalize_app_settings_payload(payload_to_store, prev_for_merge)
        # Stabilise gateway ids across re-create. If the operator deletes a
        # gateway and re-adds one at the same plc_ip + gateway_type, reuse
        # the old id so dashboards/widgets that reference it keep working
        # instead of going blank.
        if domain_name == "gateway_configurations":
            try:
                prev_payload_for_stab = self._load_previous_scoped_payload(skey, domain_name)
                payload_to_store = self._stabilise_gateway_ids_by_plc_ip(
                    new_payload=payload_to_store,
                    prev_payload=prev_payload_for_stab,
                )
            except Exception:
                pass
        payload_json = self._canonical_json(payload_to_store)
        with self._lock:
            with self._connect() as conn:
                prev = conn.execute(
                    "SELECT version, payload_json, updated_utc FROM config_documents_scoped WHERE scope_key = ? AND domain = ?",
                    (skey, domain_name),
                ).fetchone()
                old_version = int(prev["version"]) if prev else 0
                prev_payload_json = ""
                if prev:
                    prev_raw = str(prev["payload_json"] or "")
                    try:
                        prev_payload_json = self._canonical_json(json.loads(prev_raw))
                    except Exception:
                        prev_payload_json = prev_raw
                if prev and prev_payload_json == payload_json:
                    # No-op local write — BUT we still need to fire the
                    # cloud mirror for Lite-visible domains. Older edges
                    # saved dashboards locally before the mirror code
                    # existed; once they upgraded, every save was a no-op
                    # and the dashboard never reached cloud because the
                    # mirror was wired AFTER this early-return. The mirror
                    # is itself idempotent (versioned UPSERT), so firing
                    # it here is safe and finally lets historical edges
                    # backfill on the next operator save.
                    if domain_name in (
                        "dashboard_configurations",
                        "alarms_setup",
                        "triggers_limits",
                        "gateway_configurations",
                        "devices",
                    ):
                        try:
                            tenant_from_scope = (skey.split("|") or ["default"])[0] or "default"
                            self._mirror_config_doc_to_cloud(
                                domain_name,
                                tenant_id=tenant_from_scope,
                                scope_key=skey,
                                payload_json=str(prev["payload_json"] or "null"),
                                version=int(old_version),
                                updated_utc=str(prev["updated_utc"] or now),
                            )
                        except Exception:
                            pass
                    return {
                        "domain": domain_name,
                        "scope_key": skey,
                        "tenant_id": self._current_tenant_id(),
                        "version": old_version,
                        "updated_utc": str(prev["updated_utc"] or now),
                        "unchanged": True,
                    }
                new_version = old_version + 1
                conn.execute(
                    """
                    INSERT INTO config_documents_scoped(scope_key, domain, payload_json, version, updated_utc)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(scope_key, domain) DO UPDATE SET
                      payload_json = excluded.payload_json,
                      version = excluded.version,
                      updated_utc = excluded.updated_utc
                    """,
                    (skey, domain_name, payload_json, new_version, now),
                )
                conn.execute(
                    """
                    INSERT INTO config_audit(domain, actor, old_version, new_version, changed_utc)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (f"{domain_name}#{skey}", actor, old_version if old_version > 0 else None, new_version, now),
                )
        if domain_name == "database_configurations":
            self._invalidate_cloud_target_cache()
            try:
                self._reconcile_sync_targets_with_config()
            except Exception:
                pass
        if domain_name == "app_settings":
            # Runtime-control flags live on the user-scoped app_settings doc
            # (because the UI saves there), but the background sync worker
            # reads from the unscoped doc. Without mirroring, the user can
            # tick "Auto Sync" in the UI and the worker never sees it.
            #
            # Mirror only the small set of flags the worker actually reads;
            # leave UI-only preferences (theme, palette, ui-source) alone.
            try:
                self._mirror_runtime_flags_to_unscoped_app_settings(payload_to_store)
            except Exception:
                pass
        if domain_name in (
            "dashboard_configurations",
            "alarms_setup",
            "triggers_limits",
            # 2026-06-10: extend mirror to the operational config domains
            # so Lite can label widgets with the gateway's friendly name,
            # show device metadata, and the portal can review what's
            # actually deployed at each edge. The historian view already
            # needs gateway_id → gateway_name resolution and was falling
            # back to the raw id when the local edge moved offline.
            "gateway_configurations",
            "devices",
        ):
            # scope_key shape on the edge is 'tenant|-|edge_id|user'; the
            # leading segment is the tenant. RLS in Supabase uses tenant_id,
            # so we propagate it. The scope_key is preserved as-is so the
            # Lite app can pick the right row for the signed-in user.
            tenant_from_scope = (skey.split("|") or ["default"])[0] or "default"
            self._mirror_config_doc_to_cloud(
                domain_name,
                tenant_id=tenant_from_scope,
                scope_key=skey,
                payload_json=payload_json,
                version=new_version,
                updated_utc=now,
            )
        try:
            self._mirror_config_doc_to_customer_db(
                domain=domain_name,
                scope_key=skey,
                payload_json=payload_json,
                version=new_version,
                updated_utc=now,
                actor=actor,
            )
        except Exception:
            pass
        return {
            "domain": domain_name,
            "scope_key": skey,
            "tenant_id": self._current_tenant_id(),
            "version": new_version,
            "updated_utc": now,
        }

    def save_bootstrap(self, data: Dict[str, Any], actor: str = "system") -> Dict[str, Any]:
        versions: Dict[str, Any] = {}
        for domain, payload in data.items():
            if not isinstance(domain, str) or not domain.strip():
                continue
            versions[domain] = self.upsert_domain(domain.strip(), payload, actor=actor)
        return versions

    def save_bootstrap_scoped(self, scope_key: str, data: Dict[str, Any], actor: str = "system") -> Dict[str, Any]:
        skey = str(scope_key or "").strip()
        if not skey:
            return self.save_bootstrap(data=data, actor=actor)
        versions: Dict[str, Any] = {}
        for domain, payload in data.items():
            if not isinstance(domain, str) or not domain.strip():
                continue
            versions[domain] = self.upsert_domain_scoped(skey, domain.strip(), payload, actor=actor)
        return versions

    def append_historian_rows(self, rows: list[dict[str, Any]]) -> int:
        now = self._utc_now()
        tenant_id = self._current_tenant_id()
        safe_rows = []
        pending_live_rows: list[dict[str, Any]] = []
        for r in rows or []:
            row_tenant = normalize_tenant_id(str(r.get("tenant_id") or tenant_id))
            ts_utc = str(r.get("ts_utc") or r.get("ts") or now)
            gateway_id = str(r.get("gateway_id") or "")
            gateway_name = str(r.get("gateway_name") or "")
            device_name = str(r.get("device_name") or "")
            plc_ip = str(r.get("plc_ip") or "")
            database_name = str(r.get("database_name") or "")
            tag_name = str(r.get("tag_name") or r.get("tag") or "")
            value = float(r.get("value")) if r.get("value") is not None else None
            value_text_raw = r.get("value_text")
            value_text = str(value_text_raw) if value_text_raw is not None and value_text_raw != "" else None
            quality = int(r.get("quality")) if r.get("quality") is not None else None
            quality_label = str(r.get("quality_label") or "")
            source = str(r.get("source") or "")
            safe_rows.append(
                (
                    row_tenant,
                    ts_utc,
                    gateway_id,
                    gateway_name,
                    device_name,
                    plc_ip,
                    database_name,
                    tag_name,
                    value,
                    value_text,
                    quality,
                    quality_label,
                    source,
                    now,
                )
            )
            if gateway_id and tag_name:
                pending_live_rows.append(
                    {
                        "tenant_id": row_tenant,
                        "gateway_id": gateway_id,
                        "tag_name": tag_name,
                        "ts_utc": ts_utc,
                        "source": source,
                        "gateway_name": gateway_name,
                        "device_name": device_name,
                        "plc_ip": plc_ip,
                        "database_name": database_name,
                        "value": value,
                        "value_text": value_text,
                        "quality": quality,
                        "quality_label": quality_label,
                        "updated_utc": now,
                    }
                )
        if not safe_rows:
            return 0
        max_local_id = 0
        with self._lock:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO historian_readings
                    (tenant_id, ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name, value, value_text, quality, quality_label, source, created_utc)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    safe_rows,
                )
                try:
                    row = conn.execute("SELECT last_insert_rowid() AS id").fetchone()
                    max_local_id = int(row["id"] or 0) if row else 0
                except Exception:
                    max_local_id = 0
        if pending_live_rows:
            # Keep a fast local latest cache for UI/live endpoints.
            with self._lock:
                for row in pending_live_rows:
                    key = (
                        normalize_tenant_id(str(row.get("tenant_id") or tenant_id)),
                        str(row.get("gateway_id") or ""),
                        str(row.get("tag_name") or ""),
                    )
                    if key[1] and key[2]:
                        self._local_live_latest_cache[key] = dict(row)
            self._enqueue_live_fast_pending(pending_live_rows, max_local_id=max_local_id)
        self._sync_wakeup_event.set()
        self._live_sync_wakeup_event.set()
        return len(safe_rows)

    def append_log_rows(self, rows: list[dict[str, Any]]) -> int:
        now = self._utc_now()
        tenant_id = self._current_tenant_id()
        safe_rows = []
        for r in rows or []:
            row_tenant = normalize_tenant_id(str(r.get("tenant_id") or tenant_id))
            safe_rows.append(
                (
                    row_tenant,
                    str(r.get("ts") or r.get("ts_utc") or now),
                    str(r.get("level") or "info"),
                    str(r.get("category") or "system"),
                    str(r.get("message") or ""),
                    str(r.get("gateway_id") or ""),
                    str(r.get("gateway_name") or ""),
                    str(r.get("device_name") or ""),
                    str(r.get("database_name") or ""),
                    now,
                )
            )
        if not safe_rows:
            return 0
        with self._lock:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO app_logs
                    (tenant_id, ts_utc, level, category, message, gateway_id, gateway_name, device_name, database_name, created_utc)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    safe_rows,
                )
        self._sync_wakeup_event.set()
        return len(safe_rows)

    def get_historian_rows(
        self,
        limit: int = 1000,
        prefer_cloud_reads: bool | None = None,
        gateway: str = "",
        device: str = "",
        tag: str = "",
        edge_id: str = "",
        source: str = "",
    ) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit or 1000), 10000))
        edge_filter = self._normalize_edge_filter(edge_id)
        fetch_lim = lim if not edge_filter else max(lim, min(50000, lim * 6))
        tenant_id = self._current_tenant_id()
        gateway_txt = str(gateway or "").strip()
        device_txt = str(device or "").strip()
        tag_txt = str(tag or "").strip()
        # Hosted/web deployments should prefer cloud-backed historian reads so the
        # website mirrors edge-collected data even when no local gateways run on VPS.
        prefer_cloud = self._prefer_cloud_reads() if prefer_cloud_reads is None else bool(prefer_cloud_reads)
        if prefer_cloud:
            cloud_rows = self._fetch_historian_rows_from_cloud(
                fetch_lim,
                gateway=gateway_txt,
                device=device_txt,
                tag=tag_txt,
            )
            if cloud_rows:
                return self._filter_rows_by_edge(cloud_rows, edge_filter)[:lim]

        where = "WHERE tenant_id = :tenant"
        params: dict[str, Any] = {"tenant": tenant_id, "lim": fetch_lim}
        if gateway_txt:
            if gateway_txt.lower().startswith("gw-"):
                where += " AND gateway_id = :gateway"
            else:
                where += " AND (COALESCE(gateway_name,'') = :gateway OR COALESCE(gateway_id,'') = :gateway)"
            params["gateway"] = gateway_txt
        if device_txt:
            where += " AND LOWER(COALESCE(device_name,'')) LIKE LOWER(:device_like)"
            params["device_like"] = f"%{device_txt}%"
        if tag_txt:
            where += " AND LOWER(COALESCE(tag_name,'')) LIKE LOWER(:tag_like)"
            params["tag_like"] = f"%{tag_txt}%"
        # Operator 2026-06-16: push the source filter into SQL so the
        # Power Overview's /api/power/history call (which only ever
        # wants power_modbus + power_insight rows) doesn't fetch 8x
        # the data only to throw most of it away on the Python side.
        source_txt = str(source or "").strip()
        if source_txt:
            source_values = [s.strip() for s in source_txt.split(",") if s.strip()]
            if source_values:
                placeholders = []
                for idx, sv in enumerate(source_values):
                    key = f"src{idx}"
                    placeholders.append(f":{key}")
                    params[key] = sv
                where += f" AND source IN ({', '.join(placeholders)})"
        # Read-only path: parallel readers via WAL, no global lock.
        # IMPORTANT: ORDER BY ts_utc DESC (not id DESC) so SQLite can walk
        # the (tenant_id, ts_utc DESC) covering index in order — otherwise
        # it falls back to "USE TEMP B-TREE FOR ORDER BY" and full-sorts the
        # entire tenant slice (~1.4M rows on a busy edge = ~400ms per call).
        # Snapshot the last id the cloud-sync worker has pushed so each
        # returned row can be tagged "pushed" vs "pending cloud forward".
        # The Historian page exposes this so the operator can see at a
        # glance which rows are still buffered locally — exactly the
        # store-and-forward semantics that exist if the cloud goes down.
        last_pushed_id = 0
        try:
            with self._connect() as conn_state:
                state_row = conn_state.execute(
                    "SELECT last_historian_id FROM data_sync_state WHERE id = 1"
                ).fetchone()
                if state_row is not None:
                    last_pushed_id = int(state_row[0] or 0)
        except Exception:
            last_pushed_id = 0
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, tenant_id, ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                       tag_name, value, value_text, quality, quality_label
                FROM historian_readings
                {where}
                ORDER BY ts_utc DESC
                LIMIT :lim
                """,
                params,
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            row_id = int(r["id"] or 0)
            out.append(
                {
                    "id": row_id,
                    "ts": r["ts_utc"],
                    "tenant_id": str(r["tenant_id"] or tenant_id),
                    "source": r["source"] or "",
                    "gateway_id": r["gateway_id"] or "",
                    "gateway_name": r["gateway_name"] or "",
                    "device_name": r["device_name"] or "",
                    "plc_ip": r["plc_ip"] or "",
                    "database_name": r["database_name"] or "",
                    "tag": r["tag_name"] or "",
                    "value": r["value"],
                    "value_text": r["value_text"] if "value_text" in r.keys() else None,
                    "quality": r["quality"],
                    "quality_label": r["quality_label"] or "",
                    # True until the cloud sync worker advances last_historian_id
                    # past this row. While the cloud is unreachable or sync is
                    # paused, the count of "pending" rows is the store-forward
                    # buffer the operator can watch grow/drain.
                    "pending_cloud_push": row_id > last_pushed_id,
                }
            )
        out = self._filter_rows_by_edge(out, edge_filter)
        return out[:lim]

    def get_historian_agg_rows(
        self,
        bucket: str = "minute",
        from_utc: str = "",
        to_utc: str = "",
        gateway: str = "",
        tag: str = "",
        limit: int = 50000,
        source: str = "",
    ) -> list[dict[str, Any]]:
        """Read from the pre-aggregated `historian_agg_<bucket>` tables.

        Operator 2026-06-17: the Power Overview and any wide-window
        dashboard widget used to pull raw 1 Hz rows and bucket them in
        the browser. For a 24 h × Minute view that was ~17 000 rows
        every refresh. The retention worker has been writing the agg
        tables since the schema was added (see
        `_compute_retention_run` rolling buckets into
        historian_agg_minute/hour/day). This endpoint exposes them.

        Returns rows shaped like the raw historian:
            { ts, gateway_id, gateway_name, device_name, plc_ip,
              database_name, tag, value (= avg), value_min, value_max,
              sample_count, quality, quality_label }
        so the frontend can drop the response into the same memo
        pipeline without rewriting bucket logic.
        """
        bkt = str(bucket or "minute").strip().lower()
        if bkt not in {"minute", "hour", "day"}:
            bkt = "minute"
        table = f"historian_agg_{bkt}"
        lim = max(1, min(int(limit or 50000), 100000))
        tenant_id = self._current_tenant_id()
        where = "WHERE 1=1"
        params: dict[str, Any] = {"lim": lim}
        # The agg tables have no tenant_id column (see schema at
        # ~3512); they're written by the retention worker which only
        # processes the local tenant slice. We still scope by gateway/
        # tag/source/range so the same endpoint serves multi-tenant
        # edges when the schema gains a tenant column later.
        from_txt = str(from_utc or "").strip()
        if from_txt:
            where += " AND bucket_utc >= :from_utc"
            params["from_utc"] = from_txt
        to_txt = str(to_utc or "").strip()
        if to_txt:
            where += " AND bucket_utc <= :to_utc"
            params["to_utc"] = to_txt
        gw_txt = str(gateway or "").strip()
        if gw_txt:
            where += " AND gateway_id = :gateway"
            params["gateway"] = gw_txt
        tag_txt = str(tag or "").strip()
        if tag_txt:
            # LIKE so the frontend can pass patterns like 'active_power'
            # or 'insight.' without listing every variant.
            where += " AND LOWER(COALESCE(tag_name,'')) LIKE LOWER(:tag_like)"
            params["tag_like"] = f"%{tag_txt}%"
        source_txt = str(source or "").strip()
        if source_txt:
            # The agg tables don't carry source today. Map known power
            # sources to the database_name written at write-time
            # ("Power Management") so the filter still works for the
            # Power Overview path.
            srcs = [s.strip() for s in source_txt.split(",") if s.strip()]
            if any(s in {"power_modbus", "power_insight"} for s in srcs):
                where += " AND database_name = :pm_db"
                params["pm_db"] = "Power Management"

        with self._connect() as conn:
            try:
                rows = conn.execute(
                    f"""
                    SELECT bucket_utc, gateway_id, gateway_name, device_name,
                           plc_ip, database_name, tag_name,
                           avg_value, min_value, max_value, sample_count,
                           quality_min, quality_max
                    FROM {table}
                    {where}
                    ORDER BY bucket_utc DESC
                    LIMIT :lim
                    """,
                    params,
                ).fetchall()
            except Exception as exc:
                logger.warning("get_historian_agg_rows failed: %s", exc)
                return []
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "ts": r["bucket_utc"],
                    "tenant_id": tenant_id,
                    "source": "historian_agg",
                    "gateway_id": r["gateway_id"] or "",
                    "gateway_name": r["gateway_name"] or "",
                    "device_name": r["device_name"] or "",
                    "plc_ip": r["plc_ip"] or "",
                    "database_name": r["database_name"] or "",
                    "tag": r["tag_name"] or "",
                    # `value` is the avg so the frontend bucket builder
                    # can carry it forward; min/max travel alongside
                    # for chart band rendering when needed.
                    "value": r["avg_value"],
                    "value_min": r["min_value"],
                    "value_max": r["max_value"],
                    "sample_count": int(r["sample_count"] or 0),
                    "quality": r["quality_max"],
                    "quality_label": "GOOD" if int(r["quality_max"] or 0) >= 192 else "",
                }
            )
        return out

    def get_historian_rows_range(
        self,
        from_utc: str = "",
        to_utc: str = "",
        limit: int = 5000,
        offset: int = 0,
        prefer_cloud_reads: bool | None = None,
        gateway: str = "",
        device: str = "",
        tag: str = "",
        edge_id: str = "",
    ) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit or 5000), 50000))
        off = max(0, int(offset or 0))
        edge_filter = self._normalize_edge_filter(edge_id)
        tenant_id = self._current_tenant_id()
        gateway_txt = str(gateway or "").strip()
        device_txt = str(device or "").strip()
        tag_txt = str(tag or "").strip()
        from_txt = self._normalize_utc_filter(from_utc)
        to_txt = self._normalize_utc_filter(to_utc)
        prefer_cloud = self._prefer_cloud_reads() if prefer_cloud_reads is None else bool(prefer_cloud_reads)
        if prefer_cloud:
            cloud_rows = self._fetch_historian_rows_from_cloud(
                limit=lim,
                gateway=gateway_txt,
                device=device_txt,
                tag=tag_txt,
                from_utc=from_txt,
                to_utc=to_txt,
                offset=off,
            )
            if cloud_rows:
                return self._filter_rows_by_edge(cloud_rows, edge_filter)[:lim]

        where = "WHERE tenant_id = :tenant"
        where_unscoped = "WHERE 1=1"
        params: dict[str, Any] = {"tenant": tenant_id, "lim": lim, "off": off}
        params_unscoped: dict[str, Any] = {"lim": lim, "off": off}
        if gateway_txt:
            # Index-friendly: direct equality on gateway_id when caller passes a
            # canonical gw-* id (the only form widgets emit). Falls back to OR
            # over gateway_id/gateway_name for legacy/non-canonical names.
            if gateway_txt.lower().startswith("gw-"):
                where += " AND gateway_id = :gateway"
                where_unscoped += " AND gateway_id = :gateway"
            else:
                where += " AND (COALESCE(gateway_name,'') = :gateway OR COALESCE(gateway_id,'') = :gateway)"
                where_unscoped += " AND (COALESCE(gateway_name,'') = :gateway OR COALESCE(gateway_id,'') = :gateway)"
            params["gateway"] = gateway_txt
            params_unscoped["gateway"] = gateway_txt
        if device_txt:
            where += " AND LOWER(COALESCE(device_name,'')) LIKE LOWER(:device_like)"
            where_unscoped += " AND LOWER(COALESCE(device_name,'')) LIKE LOWER(:device_like)"
            params["device_like"] = f"%{device_txt}%"
            params_unscoped["device_like"] = f"%{device_txt}%"
        if tag_txt:
            if "%" in tag_txt or "_" in tag_txt:
                where += " AND LOWER(COALESCE(tag_name,'')) LIKE LOWER(:tag_like)"
                where_unscoped += " AND LOWER(COALESCE(tag_name,'')) LIKE LOWER(:tag_like)"
                params["tag_like"] = f"%{tag_txt}%"
                params_unscoped["tag_like"] = f"%{tag_txt}%"
            else:
                # Index-friendly direct equality (case-sensitive). Tag names are
                # stored exactly as configured, so this aligns with the composite
                # historian indexes for O(log N) seeks.
                where += " AND tag_name = :tag_exact"
                where_unscoped += " AND tag_name = :tag_exact"
                params["tag_exact"] = tag_txt
                params_unscoped["tag_exact"] = tag_txt
        if from_txt:
            where += " AND ts_utc >= :from_utc"
            where_unscoped += " AND ts_utc >= :from_utc"
            params["from_utc"] = from_txt
            params_unscoped["from_utc"] = from_txt
        if to_txt:
            where += " AND ts_utc <= :to_utc"
            where_unscoped += " AND ts_utc <= :to_utc"
            params["to_utc"] = to_txt
            params_unscoped["to_utc"] = to_txt

        # Read-only path: SQLite WAL allows concurrent readers without serializing.
        # We deliberately do NOT acquire self._lock here so dashboard widgets and
        # other queries can run in parallel instead of queueing on a single mutex.
        # Same store-and-forward visibility as get_historian_rows: tag each
        # row pending vs pushed against the cloud-sync watermark.
        last_pushed_id = 0
        try:
            with self._connect() as conn_state:
                state_row = conn_state.execute(
                    "SELECT last_historian_id FROM data_sync_state WHERE id = 1"
                ).fetchone()
                if state_row is not None:
                    last_pushed_id = int(state_row[0] or 0)
        except Exception:
            last_pushed_id = 0
        with self._connect() as conn:
            # ORDER BY ts_utc DESC alone matches idx_hist_tenant_gw_tag_ts so
            # SQLite can stream the LIMIT N most recent rows in index order
            # (no temp B-tree). The trailing `id DESC` tie-breaker was only
            # meaningful for rows sharing an identical ts_utc — rare in
            # practice and visually identical on the chart.
            rows = conn.execute(
                f"""
                SELECT id, tenant_id, ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                       tag_name, value, value_text, quality, quality_label
                FROM historian_readings
                {where}
                ORDER BY ts_utc DESC
                LIMIT :lim OFFSET :off
                """,
                params,
            ).fetchall()
            if not rows:
                # Fallback for legacy/unscoped rows: keep dashboard queries resilient
                # after tenant migrations or historical data imported without tenant tags.
                rows = conn.execute(
                    f"""
                    SELECT id, tenant_id, ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                           tag_name, value, quality, quality_label
                    FROM historian_readings
                    {where_unscoped}
                    ORDER BY ts_utc DESC
                    LIMIT :lim OFFSET :off
                    """,
                    params_unscoped,
                ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            row_id = int(r["id"] or 0)
            out.append(
                {
                    "id": row_id,
                    "ts": r["ts_utc"],
                    "tenant_id": str(r["tenant_id"] or tenant_id),
                    "source": r["source"] or "",
                    "gateway_id": r["gateway_id"] or "",
                    "gateway_name": r["gateway_name"] or "",
                    "device_name": r["device_name"] or "",
                    "plc_ip": r["plc_ip"] or "",
                    "database_name": r["database_name"] or "",
                    "tag": r["tag_name"] or "",
                    "value": r["value"],
                    "value_text": r["value_text"] if "value_text" in r.keys() else None,
                    "quality": r["quality"],
                    "quality_label": r["quality_label"] or "",
                    "pending_cloud_push": row_id > last_pushed_id,
                }
            )
        out = self._filter_rows_by_edge(out, edge_filter)
        return out[:lim]

    def get_historian_stats(
        self,
        from_utc: str = "",
        to_utc: str = "",
        gateway: str = "",
        device: str = "",
        tag: str = "",
        edge_id: str = "",
        prefer_cloud_reads: bool | None = None,
    ) -> list[dict[str, Any]]:
        edge_filter = self._normalize_edge_filter(edge_id)
        tenant_id = self._current_tenant_id()
        gateway_txt = str(gateway or "").strip()
        device_txt = str(device or "").strip()
        tag_txt = str(tag or "").strip()
        from_txt = self._normalize_utc_filter(from_utc)
        to_txt = self._normalize_utc_filter(to_utc)

        prefer_cloud = self._prefer_cloud_reads() if prefer_cloud_reads is None else bool(prefer_cloud_reads)
        if prefer_cloud:
            cloud_stats = self._fetch_historian_stats_from_cloud(
                gateway=gateway_txt,
                device=device_txt,
                tag=tag_txt,
                from_utc=from_txt,
                to_utc=to_txt,
            )
            if cloud_stats:
                return cloud_stats

        where = "WHERE tenant_id = :tenant"
        where_unscoped = "WHERE 1=1"
        params: dict[str, Any] = {"tenant": tenant_id}
        params_unscoped: dict[str, Any] = {}
        if gateway_txt:
            # Index-friendly direct equality when caller passes a canonical gw-* id.
            # Falls back to OR over both columns for legacy/non-canonical names.
            if gateway_txt.lower().startswith("gw-"):
                where += " AND gateway_id = :gateway"
                where_unscoped += " AND gateway_id = :gateway"
            else:
                where += " AND (COALESCE(gateway_name,'') = :gateway OR COALESCE(gateway_id,'') = :gateway)"
                where_unscoped += " AND (COALESCE(gateway_name,'') = :gateway OR COALESCE(gateway_id,'') = :gateway)"
            params["gateway"] = gateway_txt
            params_unscoped["gateway"] = gateway_txt
        if device_txt:
            where += " AND LOWER(COALESCE(device_name,'')) LIKE LOWER(:device_like)"
            where_unscoped += " AND LOWER(COALESCE(device_name,'')) LIKE LOWER(:device_like)"
            params["device_like"] = f"%{device_txt}%"
            params_unscoped["device_like"] = f"%{device_txt}%"
        if tag_txt:
            if "%" in tag_txt or "_" in tag_txt:
                where += " AND LOWER(COALESCE(tag_name,'')) LIKE LOWER(:tag_like)"
                where_unscoped += " AND LOWER(COALESCE(tag_name,'')) LIKE LOWER(:tag_like)"
                params["tag_like"] = f"%{tag_txt}%"
                params_unscoped["tag_like"] = f"%{tag_txt}%"
            else:
                # Index-friendly direct equality. Tag names are stored exactly as
                # configured, so case-sensitive match aligns with the composite
                # historian indexes (idx_hist_tenant_gw_tag_ts / idx_hist_tenant_tag_ts).
                where += " AND tag_name = :tag_exact"
                where_unscoped += " AND tag_name = :tag_exact"
                params["tag_exact"] = tag_txt
                params_unscoped["tag_exact"] = tag_txt
        if from_txt:
            where += " AND ts_utc >= :from_utc"
            where_unscoped += " AND ts_utc >= :from_utc"
            params["from_utc"] = from_txt
            params_unscoped["from_utc"] = from_txt
        if to_txt:
            where += " AND ts_utc <= :to_utc"
            where_unscoped += " AND ts_utc <= :to_utc"
            params["to_utc"] = to_txt
            params_unscoped["to_utc"] = to_txt

        def _fetch_aggregates(conn: sqlite3.Connection, where_sql: str, params_sql: dict[str, Any]) -> list[sqlite3.Row]:
            return conn.execute(
                f"""
                SELECT
                  COALESCE(tag_name,'') AS tag_name,
                  COUNT(*) AS row_count,
                  SUM(COALESCE(value, 0)) AS sum_value,
                  AVG(value) AS avg_value,
                  MIN(value) AS min_value,
                  MAX(value) AS max_value
                FROM historian_readings
                {where_sql}
                GROUP BY tag_name
                ORDER BY tag_name ASC
                """,
                params_sql,
            ).fetchall()

        def _fetch_latest_per_tag(
            conn: sqlite3.Connection, tag_names: list[str], where_sql: str, params_sql: dict[str, Any]
        ) -> dict[str, float]:
            # Per-tag most recent value within the same filter window. One query per
            # tag (small N — bounded by distinct tags returned in the aggregates).
            # Avoids the ambiguity of a JOIN where outer/inner share unqualified
            # column names in the dynamic WHERE clause.
            latest_map: dict[str, float] = {}
            for tag in tag_names:
                if not tag:
                    continue
                local_where = where_sql + " AND COALESCE(tag_name,'') = :__latest_tag"
                local_params = dict(params_sql)
                local_params["__latest_tag"] = tag
                row = conn.execute(
                    f"""
                    SELECT value
                    FROM historian_readings
                    {local_where}
                    ORDER BY ts_utc DESC, id DESC
                    LIMIT 1
                    """,
                    local_params,
                ).fetchone()
                try:
                    if row is not None and row["value"] is not None:
                        latest_map[tag] = float(row["value"])
                except Exception:
                    continue
            return latest_map

        # Read-only path: parallelize via WAL instead of serializing on self._lock.
        with self._connect() as conn:
            rows = _fetch_aggregates(conn, where, params)
            if rows:
                tags_for_latest = [str(r["tag_name"] or "") for r in rows if str(r["tag_name"] or "").strip()]
                latest_by_tag = _fetch_latest_per_tag(conn, tags_for_latest, where, params)
            else:
                rows = _fetch_aggregates(conn, where_unscoped, params_unscoped)
                tags_for_latest = [str(r["tag_name"] or "") for r in rows if str(r["tag_name"] or "").strip()]
                latest_by_tag = _fetch_latest_per_tag(conn, tags_for_latest, where_unscoped, params_unscoped) if rows else {}
        out = [
            {
                "tag": str(r["tag_name"] or ""),
                "count": int(r["row_count"] or 0),
                "sum": float(r["sum_value"] or 0.0),
                "avg": float(r["avg_value"] or 0.0) if r["avg_value"] is not None else None,
                "min": float(r["min_value"] or 0.0) if r["min_value"] is not None else None,
                "max": float(r["max_value"] or 0.0) if r["max_value"] is not None else None,
                "latest": latest_by_tag.get(str(r["tag_name"] or "")),
            }
            for r in rows
            if str(r["tag_name"] or "").strip()
        ]
        return out

    def get_historian_rule_stats(
        self,
        rules: list[dict[str, Any]] | None = None,
        from_utc: str = "",
        to_utc: str = "",
        gateway: str = "",
        edge_id: str = "",
        prefer_cloud_reads: bool | None = None,
    ) -> list[dict[str, Any]]:
        del edge_id  # rule stats are tenant-scoped historian aggregates
        del prefer_cloud_reads  # local edge must remain local-source-of-truth for widget rules
        tenant_id = self._current_tenant_id()
        from_txt = self._normalize_utc_filter(from_utc)
        to_txt = self._normalize_utc_filter(to_utc)
        request_gateway = str(gateway or "").strip()
        safe_rules = list(rules or [])[:64]
        out: list[dict[str, Any]] = []

        def _add_gateway_filter(where_sql: str, params_sql: dict[str, Any], gw: str) -> tuple[str, dict[str, Any]]:
            gw_txt = str(gw or "").strip()
            if not gw_txt:
                return where_sql, params_sql
            # Gateway ids in TrustNode are canonical `gw-*`; prefer exact id lookups
            # to keep queries index-friendly and deterministic.
            if gw_txt.lower().startswith("gw-"):
                where_sql += " AND COALESCE(gateway_id,'') = :gateway_id"
                params_sql["gateway_id"] = gw_txt
            else:
                where_sql += " AND (COALESCE(gateway_id,'') = :gateway_name OR COALESCE(gateway_name,'') = :gateway_name)"
                params_sql["gateway_name"] = gw_txt
            return where_sql, params_sql

        def _add_operator_filter(
            where_sql: str,
            params_sql: dict[str, Any],
            op_raw: str,
            v1_raw: Any,
            v2_raw: Any,
        ) -> tuple[str, dict[str, Any]]:
            op = str(op_raw or "any").strip().lower()
            op = {
                ">": "gt",
                ">=": "gte",
                "<": "lt",
                "<=": "lte",
                "=": "eq",
                "==": "eq",
                "!=": "ne",
                "<>": "ne",
            }.get(op, op)
            if op in {"any", ""}:
                return where_sql, params_sql

            def _to_float(value: Any) -> float | None:
                try:
                    n = float(value)
                    if math.isfinite(n):
                        return n
                except Exception:
                    return None
                return None

            v1 = _to_float(v1_raw)
            v2 = _to_float(v2_raw)
            if op in {"eq", "ne", "lt", "lte", "gt", "gte"} and v1 is None:
                # Invalid threshold means rule matches nothing.
                where_sql += " AND 1=0"
                return where_sql, params_sql
            if op == "between" and (v1 is None or v2 is None):
                where_sql += " AND 1=0"
                return where_sql, params_sql

            if op == "eq":
                where_sql += " AND value = :rule_v1"
                params_sql["rule_v1"] = v1
            elif op == "ne":
                where_sql += " AND value <> :rule_v1"
                params_sql["rule_v1"] = v1
            elif op == "lt":
                where_sql += " AND value < :rule_v1"
                params_sql["rule_v1"] = v1
            elif op == "lte":
                where_sql += " AND value <= :rule_v1"
                params_sql["rule_v1"] = v1
            elif op == "gt":
                where_sql += " AND value > :rule_v1"
                params_sql["rule_v1"] = v1
            elif op == "gte":
                where_sql += " AND value >= :rule_v1"
                params_sql["rule_v1"] = v1
            elif op == "between":
                lo = min(v1, v2)
                hi = max(v1, v2)
                where_sql += " AND value >= :rule_v1 AND value <= :rule_v2"
                params_sql["rule_v1"] = lo
                params_sql["rule_v2"] = hi
            return where_sql, params_sql

        # Normalize each rule into a dict the SQL helper can consume in one pass.
        # Grouping rules by (gateway, tag, from, to) lets us run a *single*
        # multi-rule aggregate SQL per group, instead of one query per rule.
        # That keeps total latency flat as rules are added — critical so the
        # computed-pie refresh can keep up with a 1s gateway interval.
        def _to_float(value: Any) -> float | None:
            try:
                n = float(value)
                if math.isfinite(n):
                    return n
            except Exception:
                return None
            return None

        op_alias = {
            ">": "gt", ">=": "gte", "<": "lt", "<=": "lte",
            "=": "eq", "==": "eq", "!=": "ne", "<>": "ne",
        }

        normalized: list[dict[str, Any]] = []
        for idx, rule in enumerate(safe_rules):
            tag_txt = str((rule or {}).get("tag_name") or "").strip()
            rule_gateway = str((rule or {}).get("gateway_id") or "").strip() or request_gateway
            op_raw = str((rule or {}).get("operator") or "any").strip().lower()
            op = op_alias.get(op_raw, op_raw)
            if op == "" or op not in {"any", "eq", "ne", "lt", "lte", "gt", "gte", "between"}:
                op = "any"
            aggregation = str((rule or {}).get("aggregation") or "count").strip().lower()
            if aggregation not in {"count", "sum", "avg", "min", "max", "latest"}:
                aggregation = "count"
            normalized.append({
                "idx": idx,
                "id": str((rule or {}).get("id") or f"rule-{idx + 1}"),
                "label": str((rule or {}).get("label") or f"Item {idx + 1}"),
                "color": str((rule or {}).get("color") or "#14a89a"),
                "tag_name": tag_txt,
                "gateway_id": rule_gateway,
                "operator": op,
                "aggregation": aggregation,
                "v1": _to_float((rule or {}).get("value1")),
                "v2": _to_float((rule or {}).get("value2")),
            })

        # Group rules with the same base scope so each group's data is scanned once.
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in normalized:
            key = (r["gateway_id"], r["tag_name"].lower())
            groups.setdefault(key, []).append(r)

        def _build_group_where(gw: str, tag: str, scoped: bool) -> tuple[str, dict[str, Any]]:
            params_g: dict[str, Any] = {}
            if scoped:
                where_g = "WHERE tenant_id = :tenant"
                params_g["tenant"] = tenant_id
            else:
                where_g = "WHERE 1=1"
            gw_txt = (gw or "").strip()
            if gw_txt:
                # Direct equality (no COALESCE wrapper) so SQLite can use the
                # composite index idx_hist_tenant_gw_tag_ts for a seek instead of
                # a full index scan. Historian rows always populate gateway_id;
                # the COALESCE fallback was only for legacy rows that no longer exist.
                if gw_txt.lower().startswith("gw-"):
                    where_g += " AND gateway_id = :gateway_id"
                    params_g["gateway_id"] = gw_txt
                else:
                    # Non-canonical gateway names: keep the COALESCE/OR fallback
                    # for backward compat. This path is rare and will still scan
                    # via the secondary tag index.
                    where_g += " AND (COALESCE(gateway_id,'') = :gateway_name OR COALESCE(gateway_name,'') = :gateway_name)"
                    params_g["gateway_name"] = gw_txt
            if tag:
                # Direct equality (case-sensitive) on tag_name. TrustNode stores
                # tag names exactly as the user typed them, so we don't need
                # LOWER() wrapping — that wrapping disabled index usage and
                # slowed every rule-stats query by ~4x.
                where_g += " AND tag_name = :tag_exact"
                params_g["tag_exact"] = tag
            if from_txt:
                where_g += " AND ts_utc >= :from_utc"
                params_g["from_utc"] = from_txt
            if to_txt:
                where_g += " AND ts_utc <= :to_utc"
                params_g["to_utc"] = to_txt
            return where_g, params_g

        def _rule_predicate(r: dict[str, Any], param_prefix: str) -> tuple[str, dict[str, Any]]:
            op = r["operator"]
            v1, v2 = r["v1"], r["v2"]
            if op in {"any"}:
                return "1=1", {}
            if op in {"eq", "ne", "lt", "lte", "gt", "gte"} and v1 is None:
                return "1=0", {}
            if op == "between" and (v1 is None or v2 is None):
                return "1=0", {}
            key1 = f"{param_prefix}_v1"
            key2 = f"{param_prefix}_v2"
            if op == "eq":
                return f"value = :{key1}", {key1: v1}
            if op == "ne":
                return f"value <> :{key1}", {key1: v1}
            if op == "lt":
                return f"value < :{key1}", {key1: v1}
            if op == "lte":
                return f"value <= :{key1}", {key1: v1}
            if op == "gt":
                return f"value > :{key1}", {key1: v1}
            if op == "gte":
                return f"value >= :{key1}", {key1: v1}
            if op == "between":
                lo, hi = min(v1, v2), max(v1, v2)
                return f"value >= :{key1} AND value <= :{key2}", {key1: lo, key2: hi}
            return "1=1", {}

        # Read-only path: one connection for the whole batch, no global lock
        # (SQLite WAL handles reader concurrency).
        results_by_id: dict[str, dict[str, Any]] = {}
        with self._connect() as conn:
            if True:
                for (gw, _tag_lower), group_rules in groups.items():
                    base_tag = group_rules[0]["tag_name"]
                    scoped_where, scoped_params = _build_group_where(gw, base_tag, scoped=True)
                    select_exprs: list[str] = []
                    extra_params: dict[str, Any] = {}
                    for r in group_rules:
                        prefix = f"r{r['idx']}"
                        pred_sql, pred_params = _rule_predicate(r, prefix)
                        extra_params.update(pred_params)
                        select_exprs.extend([
                            f"SUM(CASE WHEN {pred_sql} THEN 1 ELSE 0 END) AS {prefix}_cnt",
                            f"SUM(CASE WHEN {pred_sql} THEN value ELSE 0 END) AS {prefix}_sum",
                            f"AVG(CASE WHEN {pred_sql} THEN value END) AS {prefix}_avg",
                            f"MIN(CASE WHEN {pred_sql} THEN value END) AS {prefix}_min",
                            f"MAX(CASE WHEN {pred_sql} THEN value END) AS {prefix}_max",
                        ])
                    select_sql = ",\n                  ".join(select_exprs)
                    row = conn.execute(
                        f"""
                        SELECT {select_sql}
                        FROM historian_readings
                        {scoped_where}
                        """,
                        {**scoped_params, **extra_params},
                    ).fetchone()
                    # If the scoped query returned zero rows for every rule in the
                    # group, retry without the tenant predicate (preserves the
                    # original backward-compat fallback semantics).
                    if row is None or all(
                        int((row[f"r{r['idx']}_cnt"] if row else 0) or 0) == 0 for r in group_rules
                    ):
                        unscoped_where, unscoped_params = _build_group_where(gw, base_tag, scoped=False)
                        row = conn.execute(
                            f"""
                            SELECT {select_sql}
                            FROM historian_readings
                            {unscoped_where}
                            """,
                            {**unscoped_params, **extra_params},
                        ).fetchone()
                        active_where, active_params = unscoped_where, {**unscoped_params, **extra_params}
                    else:
                        active_where, active_params = scoped_where, {**scoped_params, **extra_params}

                    # Latest per rule only when needed (rare in pie configs).
                    latest_by_idx: dict[int, float | None] = {}
                    for r in group_rules:
                        if r["aggregation"] != "latest":
                            continue
                        prefix = f"r{r['idx']}"
                        pred_sql, _ = _rule_predicate(r, prefix)
                        if pred_sql == "1=0":
                            latest_by_idx[r["idx"]] = None
                            continue
                        lrow = conn.execute(
                            f"""
                            SELECT value FROM historian_readings
                            {active_where} AND ({pred_sql})
                            ORDER BY ts_utc DESC, id DESC
                            LIMIT 1
                            """,
                            active_params,
                        ).fetchone()
                        try:
                            latest_by_idx[r["idx"]] = (
                                float(lrow["value"]) if lrow is not None and lrow["value"] is not None else None
                            )
                        except Exception:
                            latest_by_idx[r["idx"]] = None

                    for r in group_rules:
                        prefix = f"r{r['idx']}"
                        row_count = int((row[f"{prefix}_cnt"] if row else 0) or 0)
                        sum_value = float((row[f"{prefix}_sum"] if row else 0.0) or 0.0)
                        avg_value = float(row[f"{prefix}_avg"]) if row and row[f"{prefix}_avg"] is not None else None
                        min_value = float(row[f"{prefix}_min"]) if row and row[f"{prefix}_min"] is not None else None
                        max_value = float(row[f"{prefix}_max"]) if row and row[f"{prefix}_max"] is not None else None
                        latest_value = latest_by_idx.get(r["idx"])
                        agg = r["aggregation"]
                        if agg == "count":
                            metric = float(row_count)
                        elif agg == "sum":
                            metric = sum_value
                        elif agg == "avg":
                            metric = float(avg_value if avg_value is not None else 0.0)
                        elif agg == "min":
                            metric = float(min_value if min_value is not None else 0.0)
                        elif agg == "max":
                            metric = float(max_value if max_value is not None else 0.0)
                        else:
                            metric = float(latest_value if latest_value is not None else 0.0)
                        results_by_id[r["id"]] = {
                            "id": r["id"],
                            "label": r["label"],
                            "value": metric,
                            "color": r["color"],
                            "tag_name": r["tag_name"],
                            "gateway_id": r["gateway_id"],
                            "aggregation": agg,
                            "operator": r["operator"],
                            "sample_count": row_count,
                            "count": row_count,
                            "sum": sum_value,
                            "avg": avg_value,
                            "min": min_value,
                            "max": max_value,
                            "latest": latest_value,
                        }

        # Preserve original rule order in the response.
        return [results_by_id[r["id"]] for r in normalized if r["id"] in results_by_id]

    def _fetch_historian_stats_from_cloud(
        self,
        gateway: str = "",
        device: str = "",
        tag: str = "",
        from_utc: str = "",
        to_utc: str = "",
    ) -> list[dict[str, Any]]:
        try:
            cloud = self._resolve_active_cloud_database()
        except Exception:
            cloud = None
        if not cloud:
            return []
        try:
            from sqlalchemy import text  # type: ignore
            schema = str(cloud.get("schema") or "public").strip() or "public"
            tenant_id = self._current_tenant_id()
            engine, key = self._get_or_create_cloud_engine(cloud, schema)
            self._ensure_cloud_schema_once(engine, schema, key)

            conditions = ["tenant_id = :tenant"]
            params: dict[str, Any] = {"tenant": tenant_id}

            gateway_txt = str(gateway or "").strip()
            device_txt = str(device or "").strip()
            tag_txt = str(tag or "").strip()
            from_txt = self._normalize_utc_filter(from_utc)
            to_txt = self._normalize_utc_filter(to_utc)

            if gateway_txt:
                conditions.append("(COALESCE(gateway_name,'') = :gateway OR COALESCE(gateway_id,'') = :gateway)")
                params["gateway"] = gateway_txt
            if device_txt:
                conditions.append("LOWER(COALESCE(device_name,'')) LIKE LOWER(:device_like)")
                params["device_like"] = f"%{device_txt}%"
            if tag_txt:
                conditions.append("LOWER(COALESCE(tag_name,'')) LIKE LOWER(:tag_like)")
                params["tag_like"] = f"%{tag_txt}%"
            if from_txt:
                conditions.append("ts_utc >= :from_utc")
                params["from_utc"] = from_txt
            if to_txt:
                conditions.append("ts_utc <= :to_utc")
                params["to_utc"] = to_txt

            where_sql = " AND ".join(conditions)
            sql = text(
                f"""
                SELECT
                  COALESCE(tag_name,'') AS tag_name,
                  COUNT(*) AS row_count,
                  SUM(COALESCE(value, 0)) AS sum_value,
                  AVG(value) AS avg_value,
                  MIN(value) AS min_value,
                  MAX(value) AS max_value
                FROM "{schema}"."historian_readings"
                WHERE {where_sql}
                GROUP BY tag_name
                ORDER BY tag_name ASC
                """
            )
            with engine.begin() as conn:
                rows = conn.execute(sql, params).mappings().all()

            return [
                {
                    "tag": str(r.get("tag_name") or ""),
                    "count": int(r.get("row_count") or 0),
                    "sum": float(r.get("sum_value") or 0.0),
                    "avg": float(r.get("avg_value") or 0.0) if r.get("avg_value") is not None else None,
                    "min": float(r.get("min_value") or 0.0) if r.get("min_value") is not None else None,
                    "max": float(r.get("max_value") or 0.0) if r.get("max_value") is not None else None,
                }
                for r in rows
                if str(r.get("tag_name") or "").strip()
            ]
        except Exception:
            return []

    def get_live_rows(self, limit: int = 5000, prefer_cloud_reads: bool | None = None, edge_id: str = "") -> list[dict[str, Any]]:
        lim = max(100, min(int(limit or 5000), 50000))
        edge_filter = self._normalize_edge_filter(edge_id)
        fetch_lim = lim if not edge_filter else max(lim, min(50000, lim * 6))
        tenant_id = self._current_tenant_id()
        prefer_cloud = self._prefer_cloud_reads() if prefer_cloud_reads is None else bool(prefer_cloud_reads)
        def _row_ts_ms(row: dict[str, Any]) -> int:
            raw = str(row.get("ts") or row.get("ts_utc") or "").strip()
            if not raw:
                return 0
            try:
                text = raw.replace("Z", "+00:00")
                if " " in text and "T" not in text:
                    text = text.replace(" ", "T")
                return int(datetime.fromisoformat(text).timestamp() * 1000)
            except Exception:
                return 0

        def _latest_per_gateway_tag(rows: list[dict[str, Any]], take: int) -> list[dict[str, Any]]:
            out_latest: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows:
                gateway_id = str(row.get("gateway_id") or "").strip()
                tag = str(row.get("tag") or row.get("tag_name") or "").strip()
                if not tag:
                    continue
                key = (gateway_id, tag)
                if key in out_latest:
                    continue
                out_latest[key] = {
                    "ts": str(row.get("ts") or row.get("ts_utc") or ""),
                    "source": str(row.get("source") or ""),
                    "gateway_id": gateway_id,
                    "gateway_name": str(row.get("gateway_name") or ""),
                    "device_name": str(row.get("device_name") or ""),
                    "plc_ip": str(row.get("plc_ip") or ""),
                    "database_name": str(row.get("database_name") or ""),
                    "tag": tag,
                    "value": row.get("value"),
                    "quality": row.get("quality"),
                    "quality_label": str(row.get("quality_label") or ""),
                }
                if len(out_latest) >= take:
                    break
            return list(out_latest.values())

        if prefer_cloud:
            # Tenant-scoped cache lookup — never serve another tenant's
            # cached live rows even if the underlying caller is the same
            # process. _filter_rows_by_tenant below is belt-and-braces.
            with self._cloud_live_cache_lock:
                cached_rows = list(self._cloud_live_cache_rows_by_tenant.get(tenant_id) or [])
                cache_updated = str(self._cloud_live_cache_updated_utc_by_tenant.get(tenant_id) or "")
            if cached_rows and cache_updated:
                try:
                    cached_dt = datetime.fromisoformat(cache_updated.replace("Z", "+00:00"))
                    if cached_dt.tzinfo is None:
                        cached_dt = cached_dt.replace(tzinfo=timezone.utc)
                    age_ms = int((datetime.now(timezone.utc) - cached_dt).total_seconds() * 1000)
                except Exception:
                    age_ms = 999999
                if age_ms <= int(max(1200, self._cloud_live_cache_interval_seconds * 2500)):
                    return self._filter_rows_by_tenant(self._filter_rows_by_edge(cached_rows, edge_filter), tenant_id)[:lim]
            cloud_live_fast = self._fetch_live_rows_from_cloud_fast(fetch_lim)
            if cloud_live_fast:
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                newest_fast_ms = max((_row_ts_ms(r) for r in cloud_live_fast), default=0)
                if newest_fast_ms > 0 and max(0, now_ms - newest_fast_ms) <= int(self._live_source_max_stale_ms):
                    return self._filter_rows_by_tenant(self._filter_rows_by_edge(cloud_live_fast, edge_filter), tenant_id)[:lim]
            cloud_live = self._fetch_live_rows_from_cloud(fetch_lim)
            if cloud_live:
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                latest_live_ms = max((_row_ts_ms(r) for r in cloud_live), default=0)
                live_age_ms = max(0, now_ms - latest_live_ms) if latest_live_ms > 0 else 999999
                # Fast path: avoid expensive historian reads on every live request.
                if live_age_ms <= int(max(1500, self._live_source_max_stale_ms)):
                    return self._filter_rows_by_tenant(self._filter_rows_by_edge(cloud_live, edge_filter), tenant_id)[:lim]
                cloud_hist = self._fetch_historian_rows_from_cloud(min(max(fetch_lim * 2, 500), 1500))
                if cloud_hist:
                    latest_hist_ms = max((_row_ts_ms(r) for r in cloud_hist), default=0)
                    # If live_latest lags materially behind historian, serve latest rows
                    # derived from historian so web charts remain visibly live.
                    if latest_hist_ms > 0 and latest_hist_ms > latest_live_ms + 1500:
                        hist_live = _latest_per_gateway_tag(cloud_hist, fetch_lim)
                        if hist_live:
                            return self._filter_rows_by_tenant(self._filter_rows_by_edge(hist_live, edge_filter), tenant_id)[:lim]
                return self._filter_rows_by_tenant(self._filter_rows_by_edge(cloud_live, edge_filter), tenant_id)[:lim]
            cloud_hist = self._fetch_historian_rows_from_cloud(min(max(fetch_lim * 2, 500), 1500))
            if cloud_live:
                return self._filter_rows_by_tenant(self._filter_rows_by_edge(cloud_live, edge_filter), tenant_id)[:lim]
            if cloud_hist:
                hist_live = _latest_per_gateway_tag(cloud_hist, fetch_lim)
                if hist_live:
                    return self._filter_rows_by_tenant(self._filter_rows_by_edge(hist_live, edge_filter), tenant_id)[:lim]

        # Local fast path: serve latest-per-tag rows from in-memory cache first.
        with self._lock:
            cache_rows = [
                {
                    "ts": str(v.get("ts_utc") or ""),
                    "tenant_id": str(v.get("tenant_id") or tenant_id),
                    "source": str(v.get("source") or ""),
                    "gateway_id": str(v.get("gateway_id") or ""),
                    "gateway_name": str(v.get("gateway_name") or ""),
                    "device_name": str(v.get("device_name") or ""),
                    "plc_ip": str(v.get("plc_ip") or ""),
                    "database_name": str(v.get("database_name") or ""),
                    "tag": str(v.get("tag_name") or ""),
                    "value": v.get("value"),
                    "quality": v.get("quality"),
                    "quality_label": str(v.get("quality_label") or ""),
                }
                for (tnt, _gw, _tag), v in self._local_live_latest_cache.items()
                if tnt == tenant_id
            ]
        if cache_rows:
            cache_rows.sort(key=lambda r: _row_ts_ms(r), reverse=True)
            local_rows = self._filter_rows_by_edge(cache_rows, edge_filter)
            if local_rows:
                return local_rows[:lim]

        with self._lock:
            with self._connect() as conn:
                # ORDER BY ts_utc DESC uses idx_hist_tenant_ts; ORDER BY id DESC
                # falls back to a temp B-tree sort over the full tenant slice.
                rows = conn.execute(
                    """
                    SELECT tenant_id, ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                           tag_name, value, quality, quality_label
                    FROM historian_readings
                    WHERE tenant_id = ?
                    ORDER BY ts_utc DESC
                    LIMIT ?
                    """,
                    (tenant_id, max(fetch_lim, 20000)),
                ).fetchall()

        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            gateway_id = str(r["gateway_id"] or "").strip()
            tag = str(r["tag_name"] or "").strip()
            if not tag:
                continue
            key = (gateway_id, tag)
            if key in latest:
                continue
            latest[key] = {
                "ts": str(r["ts_utc"] or ""),
                "tenant_id": str(r["tenant_id"] or tenant_id),
                "source": str(r["source"] or ""),
                "gateway_id": gateway_id,
                "gateway_name": str(r["gateway_name"] or ""),
                "device_name": str(r["device_name"] or ""),
                "plc_ip": str(r["plc_ip"] or ""),
                "database_name": str(r["database_name"] or ""),
                "tag": tag,
                "value": r["value"],
                "quality": r["quality"],
                "quality_label": str(r["quality_label"] or ""),
            }
            if len(latest) >= fetch_lim:
                break

        if latest:
            local_rows = self._filter_rows_by_edge(list(latest.values()), edge_filter)
            return local_rows[:lim]
        cloud_live = self._fetch_live_rows_from_cloud(fetch_lim)
        if cloud_live:
            return self._filter_rows_by_edge(cloud_live, edge_filter)[:lim]
        return []

    def build_gateway_statuses_from_live_rows(
        self,
        live_rows: list[dict[str, Any]] | None,
        freshness_ms: int = 12000,
    ) -> list[dict[str, Any]]:
        rows = live_rows if isinstance(live_rows, list) else []
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        min_fresh_ms = max(3000, int(freshness_ms or 12000))

        def _parse_ts_ms(raw: Any) -> int:
            text = str(raw or "").strip()
            if not text:
                return 0
            try:
                iso = text.replace("Z", "+00:00")
                if " " in iso and "T" not in iso:
                    iso = iso.replace(" ", "T")
                return int(datetime.fromisoformat(iso).timestamp() * 1000)
            except Exception:
                return 0

        gateway_rows: dict[str, dict[str, Any]] = {}
        gateway_counts: dict[str, int] = {}
        for row in rows:
            gateway_id = str(row.get("gateway_id") or "").strip()
            if not gateway_id:
                continue
            gateway_counts[gateway_id] = int(gateway_counts.get(gateway_id, 0)) + 1
            ts_txt = str(row.get("ts") or row.get("ts_utc") or "")
            ts_ms = _parse_ts_ms(ts_txt)
            prev = gateway_rows.get(gateway_id)
            if prev and int(prev.get("_ts_ms", 0)) >= ts_ms:
                continue
            gateway_rows[gateway_id] = {
                "_ts_ms": ts_ms,
                "last_check_utc": ts_txt,
                "source": str(row.get("source") or ""),
                "plc_ip": str(row.get("plc_ip") or ""),
                "gateway_name": str(row.get("gateway_name") or gateway_id),
            }

        gateway_cfg_raw = self.get_config_domain("gateway_configurations")
        gateway_cfgs = gateway_cfg_raw if isinstance(gateway_cfg_raw, list) else []
        gateway_cfg_by_id = {
            str(g.get("id") or "").strip(): g
            for g in gateway_cfgs
            if isinstance(g, dict) and str(g.get("id") or "").strip()
        }

        db_cfg_raw = self.get_config_domain("database_configurations")
        db_cfgs = db_cfg_raw if isinstance(db_cfg_raw, list) else []
        db_cfg_by_id = {
            str(d.get("id") or "").strip(): d
            for d in db_cfgs
            if isinstance(d, dict) and str(d.get("id") or "").strip()
        }

        statuses: list[dict[str, Any]] = []
        all_gateway_ids = set(gateway_cfg_by_id.keys()) | set(gateway_rows.keys())
        for gateway_id in sorted(all_gateway_ids):
            cfg = gateway_cfg_by_id.get(gateway_id) if gateway_id in gateway_cfg_by_id else {}
            cfg = cfg if isinstance(cfg, dict) else {}
            row = gateway_rows.get(gateway_id, {})
            ts_ms = int(row.get("_ts_ms", 0))
            interval_ms = int(cfg.get("interval_ms") or 1000)
            adaptive_fresh_ms = max(min_fresh_ms, min(60000, max(1, interval_ms) * 8))
            running = ts_ms > 0 and max(0, now_ms - ts_ms) <= adaptive_fresh_ms
            db_cfg = db_cfg_by_id.get(str(cfg.get("database_id") or "").strip(), {})
            db_cfg = db_cfg if isinstance(db_cfg, dict) else {}

            statuses.append(
                {
                    "running": bool(running),
                    "gateway_type": str(cfg.get("gateway_type") or row.get("source") or ""),
                    "plc_ip": str(cfg.get("plc_ip") or row.get("plc_ip") or ""),
                    "interval_ms": interval_ms,
                    "tags": cfg.get("tags") if isinstance(cfg.get("tags"), list) else [],
                    "last_error": None,
                    "db_sink_engine": str(db_cfg.get("engine") or ""),
                    "db_write_count": int(gateway_counts.get(gateway_id, 0)),
                    "db_last_write_utc": str(row.get("last_check_utc") or ""),
                    "db_last_error": None,
                    "db_pending_count": 0,
                    "collection_blocked": False,
                    "collection_block_reason": None,
                    "gateway_id": gateway_id,
                    "gateway_name": str(cfg.get("name") or row.get("gateway_name") or gateway_id),
                    "last_check_utc": str(row.get("last_check_utc") or ""),
                }
            )

        statuses.sort(key=lambda r: str(r.get("last_check_utc") or ""), reverse=True)
        return statuses

    def get_log_rows(self, limit: int = 2000, prefer_cloud_reads: bool | None = None, edge_id: str = "") -> list[dict[str, Any]]:
        lim = max(1, min(int(limit or 2000), 10000))
        edge_filter = self._normalize_edge_filter(edge_id)
        fetch_lim = lim if not edge_filter else max(lim, min(50000, lim * 6))
        tenant_id = self._current_tenant_id()
        # Hosted/web deployments should prefer cloud-backed log reads.
        prefer_cloud = self._prefer_cloud_reads() if prefer_cloud_reads is None else bool(prefer_cloud_reads)
        if prefer_cloud:
            cloud_rows = self._fetch_log_rows_from_cloud(fetch_lim)
            if cloud_rows:
                return self._filter_rows_by_edge(cloud_rows, edge_filter)[:lim]

        with self._lock:
            with self._connect() as conn:
                # ORDER BY ts_utc DESC walks idx_logs_tenant_ts in order.
                rows = conn.execute(
                    """
                    SELECT tenant_id, ts_utc, level, category, message, gateway_id, gateway_name, device_name, database_name
                    FROM app_logs
                    WHERE tenant_id = ?
                    ORDER BY ts_utc DESC
                    LIMIT ?
                    """,
                    (tenant_id, fetch_lim),
                ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "ts": r["ts_utc"],
                    "tenant_id": str(r["tenant_id"] or tenant_id),
                    "level": r["level"] or "info",
                    "category": r["category"] or "system",
                    "message": r["message"] or "",
                    "gateway_id": r["gateway_id"] or "",
                    "gateway_name": r["gateway_name"] or "",
                    "device_name": r["device_name"] or "",
                    "database_name": r["database_name"] or "",
                }
            )
        out = self._filter_rows_by_edge(out, edge_filter)
        if not out:
            cloud_rows = self._fetch_log_rows_from_cloud(fetch_lim)
            if cloud_rows:
                return self._filter_rows_by_edge(cloud_rows, edge_filter)[:lim]
        return out[:lim]

    def get_mirror_check(self) -> dict[str, Any]:
        tenant_id = self._current_tenant_id()
        now = self._utc_now()
        result: dict[str, Any] = {
            "tenant_id": tenant_id,
            "checked_utc": now,
            "strict_cloud_mirror": bool(self._strict_cloud_mirror),
            "local": {},
            "cloud": {},
            "ok": False,
            "message": "",
        }

        with self._lock:
            with self._connect() as conn:
                l_hist = conn.execute(
                    """
                    SELECT COALESCE(MAX(id),0) AS max_id,
                           COALESCE(MAX(ts_utc),'') AS max_ts,
                           COUNT(*) AS c
                    FROM historian_readings
                    WHERE tenant_id = ?
                    """,
                    (tenant_id,),
                ).fetchone()
                l_logs = conn.execute(
                    """
                    SELECT COALESCE(MAX(id),0) AS max_id,
                           COALESCE(MAX(ts_utc),'') AS max_ts,
                           COUNT(*) AS c
                    FROM app_logs
                    WHERE tenant_id = ?
                    """,
                    (tenant_id,),
                ).fetchone()
        result["local"] = {
            "historian": {
                "max_local_id": int(l_hist["max_id"] or 0),
                "rows": int(l_hist["c"] or 0),
                "latest_ts": str(l_hist["max_ts"] or ""),
            },
            "logs": {
                "max_local_id": int(l_logs["max_id"] or 0),
                "rows": int(l_logs["c"] or 0),
                "latest_ts": str(l_logs["max_ts"] or ""),
            },
        }

        cloud = self._get_cloud_database_target()
        if not cloud:
            result["message"] = "No cloud target configured."
            return result

        try:
            from sqlalchemy import text  # type: ignore
        except Exception as exc:
            result["message"] = f"SQLAlchemy unavailable: {exc}"
            return result

        schema = str(cloud.get("schema") or "public")
        try:
            engine, target_key = self._get_or_create_cloud_engine(cloud, schema)
            try:
                self._ensure_cloud_schema_once(engine, schema, target_key)
            except Exception:
                pass
            with engine.begin() as c:
                c_hist = c.execute(
                    text(
                        f"""
                        SELECT COALESCE(MAX(local_id),0) AS max_local_id,
                               COALESCE(MAX(ts_utc)::text,'') AS max_ts,
                               COUNT(*) AS c
                        FROM "{schema}"."historian_readings"
                        WHERE tenant_id = :tenant
                        """
                    ),
                    {"tenant": tenant_id},
                ).mappings().first()
                c_logs = c.execute(
                    text(
                        f"""
                        SELECT COALESCE(MAX(local_id),0) AS max_local_id,
                               COALESCE(MAX(ts_utc)::text,'') AS max_ts,
                               COUNT(*) AS c
                        FROM "{schema}"."app_logs"
                        WHERE tenant_id = :tenant
                        """
                    ),
                    {"tenant": tenant_id},
                ).mappings().first()
                c_plc = c.execute(
                    text(
                        f"""
                        SELECT COALESCE(MAX(local_id),0) AS max_local_id,
                               COALESCE(MAX(ts_utc)::text,'') AS max_ts,
                               COUNT(*) AS c
                        FROM "{schema}"."plc_readings"
                        WHERE tenant_id = :tenant
                        """
                    ),
                    {"tenant": tenant_id},
                ).mappings().first()
        except Exception as exc:
            result["message"] = f"Cloud mirror check failed: {exc}"
            return result

        cloud_hist_max = int((c_hist or {}).get("max_local_id") or 0)
        cloud_logs_max = int((c_logs or {}).get("max_local_id") or 0)
        local_hist_max = int(result["local"]["historian"]["max_local_id"])
        local_logs_max = int(result["local"]["logs"]["max_local_id"])
        hist_gap = max(0, local_hist_max - cloud_hist_max)
        logs_gap = max(0, local_logs_max - cloud_logs_max)

        result["cloud"] = {
            "historian": {
                "max_local_id": cloud_hist_max,
                "rows": int((c_hist or {}).get("c") or 0),
                "latest_ts": str((c_hist or {}).get("max_ts") or ""),
            },
            "logs": {
                "max_local_id": cloud_logs_max,
                "rows": int((c_logs or {}).get("c") or 0),
                "latest_ts": str((c_logs or {}).get("max_ts") or ""),
            },
            "plc_readings": {
                "max_local_id": int((c_plc or {}).get("max_local_id") or 0),
                "rows": int((c_plc or {}).get("c") or 0),
                "latest_ts": str((c_plc or {}).get("max_ts") or ""),
            },
        }
        result["gaps"] = {
            "historian_local_minus_cloud": hist_gap,
            "logs_local_minus_cloud": logs_gap,
        }
        result["ok"] = hist_gap <= int(self._data_sync_batch_size) and logs_gap <= int(self._data_sync_batch_size)
        result["message"] = (
            "Mirror healthy."
            if result["ok"]
            else f"Mirror lag detected: historian gap={hist_gap}, logs gap={logs_gap}"
        )
        return result

    def cleanup_data(self, mode: str, actor: str = "manual") -> Dict[str, Any]:
        tenant_id = self._current_tenant_id()
        mode_clean = str(mode or "").strip().lower()
        now_dt = datetime.now(timezone.utc)
        cutoff: datetime | None
        if mode_clean == "last_hours":
            cutoff = now_dt - timedelta(hours=1)
        elif mode_clean == "last_day":
            cutoff = now_dt - timedelta(days=1)
        elif mode_clean == "last_week":
            cutoff = now_dt - timedelta(days=7)
        elif mode_clean == "last_month":
            cutoff = now_dt - timedelta(days=30)
        elif mode_clean == "period":
            cutoff = now_dt - timedelta(days=90)
        elif mode_clean == "all":
            cutoff = None
        else:
            return {"ok": False, "message": f"Unsupported cleanup mode: {mode}"}

        deleted = {
            "historian_readings": 0,
            "app_logs": 0,
            "historian_agg_minute": 0,
            "historian_agg_hour": 0,
            "historian_agg_day": 0,
        }

        with self._lock:
            with self._connect() as conn:
                if cutoff is None:
                    deleted["historian_readings"] = int(conn.execute("DELETE FROM historian_readings WHERE tenant_id = ?", (tenant_id,)).rowcount or 0)
                    deleted["app_logs"] = int(conn.execute("DELETE FROM app_logs WHERE tenant_id = ?", (tenant_id,)).rowcount or 0)
                    deleted["historian_agg_minute"] = int(conn.execute("DELETE FROM historian_agg_minute").rowcount or 0)
                    deleted["historian_agg_hour"] = int(conn.execute("DELETE FROM historian_agg_hour").rowcount or 0)
                    deleted["historian_agg_day"] = int(conn.execute("DELETE FROM historian_agg_day").rowcount or 0)
                else:
                    cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")
                    deleted["historian_readings"] = int(conn.execute("DELETE FROM historian_readings WHERE tenant_id = ? AND ts_utc < ?", (tenant_id, cutoff_text)).rowcount or 0)
                    deleted["app_logs"] = int(conn.execute("DELETE FROM app_logs WHERE tenant_id = ? AND ts_utc < ?", (tenant_id, cutoff_text)).rowcount or 0)
                    deleted["historian_agg_minute"] = int(conn.execute("DELETE FROM historian_agg_minute WHERE bucket_utc < ?", (cutoff_text,)).rowcount or 0)
                    deleted["historian_agg_hour"] = int(conn.execute("DELETE FROM historian_agg_hour WHERE bucket_utc < ?", (cutoff_text,)).rowcount or 0)
                    deleted["historian_agg_day"] = int(conn.execute("DELETE FROM historian_agg_day WHERE bucket_utc < ?", (cutoff_text,)).rowcount or 0)

        summary = (
            f"Cleanup '{mode_clean}' complete by {actor}. "
            f"Deleted readings={deleted['historian_readings']}, logs={deleted['app_logs']}, "
            f"agg_min={deleted['historian_agg_minute']}, agg_hour={deleted['historian_agg_hour']}, agg_day={deleted['historian_agg_day']}."
        )
        return {"ok": True, "message": summary, "deleted": deleted}
