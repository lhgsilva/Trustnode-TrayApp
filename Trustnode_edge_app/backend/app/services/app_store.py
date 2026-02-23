import json
import os
import sqlite3
import threading
import shutil
import hashlib
import secrets
from urllib.parse import quote_plus
from datetime import timedelta
from datetime import datetime, timezone
from typing import Any, Dict


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
        "reporting_setup": {"filters": {}, "documents": [], "schedules": []},
        "tags": {"alarm_prefs": {}},
        "email_notifications": {"settings": {}, "profiles": [], "active_profile_id": ""},
        "metadata": {},
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._db_path = self._resolve_db_path()
        self._stop_event = threading.Event()
        self._sync_wakeup_event = threading.Event()
        # Fast default cadence for cloud/live products; tunable via env.
        self._sync_interval_seconds = max(1, int(os.environ.get("TRUSTNODE_CONFIG_SYNC_SECONDS", "1") or "1"))
        self._data_sync_batch_size = max(
            200,
            min(10000, int(os.environ.get("TRUSTNODE_DATA_SYNC_BATCH_SIZE", "8000") or "8000")),
        )
        self._live_sync_sample_rows = max(
            500,
            min(50000, int(os.environ.get("TRUSTNODE_LIVE_SYNC_SAMPLE_ROWS", "10000") or "10000")),
        )
        self._ensure_schema()
        self._ensure_required_config_domains()
        self._compact_sync_outbox_for_domains()
        self._backfill_outbox_for_existing_domains()
        self._scheduler_thread = threading.Thread(target=self._retention_scheduler_loop, daemon=True)
        self._sync_thread = threading.Thread(target=self._config_sync_loop, daemon=True)
        self._scheduler_thread.start()
        self._sync_thread.start()

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

    def _get_cloud_database_target(self) -> Dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM config_documents WHERE domain = ?",
                    ("database_configurations",),
                ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(str(row["payload_json"] or "[]"))
        except Exception:
            return None
        if not isinstance(payload, list):
            return None

        def _is_enabled(item: Dict[str, Any]) -> bool:
            if "enabled" in item:
                return bool(item.get("enabled"))
            return True

        candidates: list[Dict[str, Any]] = []
        for item in payload:
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
            return None
        supabase = [c for c in candidates if "supabase.co" in c["host"].lower()]
        return supabase[0] if supabase else candidates[0]

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
            "connect_timeout": 8,
            # Supabase/PgBouncer transaction pooling is incompatible with
            # psycopg auto-prepared statements.
            "prepare_threshold": None,
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
            payload_json = str(row["payload_json"] or "null")
            try:
                with engine.begin() as conn:
                    old_v_row = conn.execute(
                        text(f'SELECT version FROM "{schema}"."config_documents" WHERE domain = :domain'),
                        {"domain": domain},
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
                        {"domain": domain, "payload_json": payload_json, "new_version": new_version},
                    )
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."config_audit"(domain, actor, old_version, new_version, changed_utc)
                            VALUES(:domain, 'local_sync', :old_version, :new_version, NOW())
                            """
                        ),
                        {"domain": domain, "old_version": old_version if old_version > 0 else None, "new_version": new_version},
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
            "connect_timeout": 8,
            "prepare_threshold": None,
        }
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        try:
            with engine.begin() as conn:
                rows = conn.execute(
                    text(
                        f"""
                        SELECT domain, payload_json::text AS payload_json_text, version, updated_utc
                        FROM "{schema}"."config_documents"
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
        applied = 0
        with self._lock:
            with self._connect() as conn:
                for r in rows or []:
                    domain = str(r[0] or "").strip()
                    if not domain:
                        continue
                    payload_text = str(r[1] or "null")
                    remote_version = int(r[2] or 0)
                    remote_updated = str(r[3] or now)
                    local = conn.execute(
                        "SELECT version FROM config_documents WHERE domain = ?",
                        (domain,),
                    ).fetchone()
                    local_version = int(local["version"] or 0) if local else 0
                    if remote_version <= local_version:
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
        while not self._stop_event.is_set():
            try:
                self._flush_data_outbox_once()
                # Prioritize live/data freshness; config sync can follow.
                self._pull_config_from_cloud_once()
                self._flush_config_outbox_once()
            except Exception as exc:
                self._upsert_sync_target_state(enabled=True, config={}, last_error=f"Config sync loop error: {exc}")
            self._sync_wakeup_event.wait(timeout=self._sync_interval_seconds)
            self._sync_wakeup_event.clear()

    def _fetch_historian_rows_from_cloud(self, limit: int) -> list[dict[str, Any]]:
        cloud = self._get_cloud_database_target()
        if not cloud:
            return []
        try:
            from sqlalchemy import create_engine, text  # type: ignore
        except Exception:
            return []
        schema = str(cloud.get("schema") or "public")
        lim = max(1, min(int(limit or 1000), 10000))
        url = self._build_pg_sqlalchemy_url(
            str(cloud.get("host") or ""),
            int(cloud.get("port") or 5432),
            str(cloud.get("database") or "postgres"),
            str(cloud.get("username") or ""),
            str(cloud.get("password") or ""),
        )
        connect_args = {
            "sslmode": "require" if cloud.get("tls", True) else "disable",
            "connect_timeout": 8,
            "prepare_threshold": None,
        }
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        try:
            with engine.begin() as conn:
                rows = conn.execute(
                    text(
                        f"""
                        SELECT ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                               tag_name, value, quality, quality_label
                        FROM "{schema}"."historian_readings"
                        ORDER BY id DESC
                        LIMIT :lim
                        """
                    ),
                    {"lim": lim},
                ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                out.append(
                    {
                        "ts": str(r[0] or ""),
                        "source": str(r[1] or ""),
                        "gateway_id": str(r[2] or ""),
                        "gateway_name": str(r[3] or ""),
                        "device_name": str(r[4] or ""),
                        "plc_ip": str(r[5] or ""),
                        "database_name": str(r[6] or ""),
                        "tag": str(r[7] or ""),
                        "value": r[8],
                        "quality": r[9],
                        "quality_label": str(r[10] or ""),
                    }
                )
            return out
        except Exception:
            return []
        finally:
            try:
                engine.dispose()
            except Exception:
                pass

    def _fetch_log_rows_from_cloud(self, limit: int) -> list[dict[str, Any]]:
        cloud = self._get_cloud_database_target()
        if not cloud:
            return []
        try:
            from sqlalchemy import create_engine, text  # type: ignore
        except Exception:
            return []
        schema = str(cloud.get("schema") or "public")
        lim = max(1, min(int(limit or 2000), 10000))
        url = self._build_pg_sqlalchemy_url(
            str(cloud.get("host") or ""),
            int(cloud.get("port") or 5432),
            str(cloud.get("database") or "postgres"),
            str(cloud.get("username") or ""),
            str(cloud.get("password") or ""),
        )
        connect_args = {
            "sslmode": "require" if cloud.get("tls", True) else "disable",
            "connect_timeout": 8,
            "prepare_threshold": None,
        }
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        try:
            with engine.begin() as conn:
                rows = conn.execute(
                    text(
                        f"""
                        SELECT ts_utc, level, category, message, gateway_id, gateway_name, device_name, database_name
                        FROM "{schema}"."app_logs"
                        ORDER BY id DESC
                        LIMIT :lim
                        """
                    ),
                    {"lim": lim},
                ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                out.append(
                    {
                        "ts": str(r[0] or ""),
                        "level": str(r[1] or "info"),
                        "category": str(r[2] or "system"),
                        "message": str(r[3] or ""),
                        "gateway_id": str(r[4] or ""),
                        "gateway_name": str(r[5] or ""),
                        "device_name": str(r[6] or ""),
                        "database_name": str(r[7] or ""),
                    }
                )
            return out
        except Exception:
            return []
        finally:
            try:
                engine.dispose()
            except Exception:
                pass

    def _fetch_live_rows_from_cloud(self, limit: int) -> list[dict[str, Any]]:
        cloud = self._get_cloud_database_target()
        if not cloud:
            return []
        try:
            from sqlalchemy import create_engine, text  # type: ignore
        except Exception:
            return []
        schema = str(cloud.get("schema") or "public")
        lim = max(1, min(int(limit or 1000), 20000))
        url = self._build_pg_sqlalchemy_url(
            str(cloud.get("host") or ""),
            int(cloud.get("port") or 5432),
            str(cloud.get("database") or "postgres"),
            str(cloud.get("username") or ""),
            str(cloud.get("password") or ""),
        )
        connect_args = {
            "sslmode": "require" if cloud.get("tls", True) else "disable",
            "connect_timeout": 8,
            "prepare_threshold": None,
        }
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        try:
            with engine.begin() as conn:
                rows = conn.execute(
                    text(
                        f"""
                        SELECT ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                               tag_name, value, quality, quality_label
                        FROM "{schema}"."live_latest"
                        ORDER BY ts_utc DESC
                        LIMIT :lim
                        """
                    ),
                    {"lim": lim},
                ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                out.append(
                    {
                        "ts": str(r[0] or ""),
                        "source": str(r[1] or ""),
                        "gateway_id": str(r[2] or ""),
                        "gateway_name": str(r[3] or ""),
                        "device_name": str(r[4] or ""),
                        "plc_ip": str(r[5] or ""),
                        "database_name": str(r[6] or ""),
                        "tag": str(r[7] or ""),
                        "value": r[8],
                        "quality": r[9],
                        "quality_label": str(r[10] or ""),
                    }
                )
            return out
        except Exception:
            return []
        finally:
            try:
                engine.dispose()
            except Exception:
                pass

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

    def _flush_data_outbox_once(self) -> None:
        cloud = self._get_cloud_database_target()
        if not cloud:
            self._set_data_sync_state(last_data_error="No enabled PostgreSQL cloud target configured")
            return
        try:
            from sqlalchemy import create_engine, text  # type: ignore
        except Exception as exc:
            self._set_data_sync_state(last_data_error=f"SQLAlchemy unavailable: {exc}")
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
            "connect_timeout": 8,
            "prepare_threshold": None,
        }
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        state = self._get_data_sync_state()
        last_hist_id = int(state.get("last_historian_id", 0))
        last_log_id = int(state.get("last_log_id", 0))
        batch_size = int(self._data_sync_batch_size)
        live_sample_size = int(self._live_sync_sample_rows)

        try:
            with self._lock:
                with self._connect() as conn:
                    hist_rows = conn.execute(
                        """
                        SELECT id, ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name,
                               tag_name, value, quality, quality_label, source, created_utc
                        FROM historian_readings
                        WHERE id > ?
                        ORDER BY id ASC
                        LIMIT ?
                        """,
                        (last_hist_id, batch_size),
                    ).fetchall()
                    log_rows = conn.execute(
                        """
                        SELECT id, ts_utc, level, category, message, gateway_id, gateway_name, device_name, database_name, created_utc
                        FROM app_logs
                        WHERE id > ?
                        ORDER BY id ASC
                        LIMIT ?
                        """,
                        (last_log_id, batch_size),
                    ).fetchall()
                    live_sample_rows = conn.execute(
                        """
                        SELECT id, ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name,
                               tag_name, value, quality, quality_label, source
                        FROM historian_readings
                        ORDER BY id DESC
                        LIMIT ?
                        """,
                        (live_sample_size,),
                    ).fetchall()

            live_latest_rows: list[dict[str, Any]] = []
            seen_live_keys: set[tuple[str, str]] = set()
            for r in live_sample_rows:
                gateway_id = str(r["gateway_id"] or "")
                tag_name = str(r["tag_name"] or "")
                if not tag_name:
                    continue
                key = (gateway_id, tag_name)
                if key in seen_live_keys:
                    continue
                seen_live_keys.add(key)
                live_latest_rows.append(
                    {
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
                        "updated_utc": self._utc_now(),
                    }
                )

            if not hist_rows and not log_rows and not live_latest_rows:
                self._set_data_sync_state(last_data_sync_utc=self._utc_now(), last_data_error="")
                return

            with engine.begin() as conn:
                if schema != "public":
                    conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."historian_readings" (
                          id BIGSERIAL PRIMARY KEY,
                          local_id BIGINT UNIQUE NOT NULL,
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
                # Backward-compatible migration for existing cloud tables that
                # were created without local_id in older builds.
                conn.execute(
                    text(
                        f"""
                        ALTER TABLE "{schema}"."historian_readings"
                        ADD COLUMN IF NOT EXISTS local_id BIGINT
                        """
                    )
                )
                conn.execute(
                    text(
                        f"""
                        CREATE UNIQUE INDEX IF NOT EXISTS "ux_hist_local_id"
                        ON "{schema}"."historian_readings"(local_id)
                        """
                    )
                )
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."app_logs" (
                          id BIGSERIAL PRIMARY KEY,
                          local_id BIGINT UNIQUE NOT NULL,
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
                conn.execute(
                    text(
                        f"""
                        ALTER TABLE "{schema}"."app_logs"
                        ADD COLUMN IF NOT EXISTS local_id BIGINT
                        """
                    )
                )
                conn.execute(
                    text(
                        f"""
                        CREATE UNIQUE INDEX IF NOT EXISTS "ux_logs_local_id"
                        ON "{schema}"."app_logs"(local_id)
                        """
                    )
                )
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."live_latest" (
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
                          PRIMARY KEY(gateway_id, tag_name)
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        f"""
                        CREATE INDEX IF NOT EXISTS "ix_live_latest_ts"
                        ON "{schema}"."live_latest"(ts_utc DESC)
                        """
                    )
                )

                if live_latest_rows:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."live_latest"
                            (gateway_id, tag_name, ts_utc, source, gateway_name, device_name, plc_ip, database_name, value, quality, quality_label, updated_utc)
                            VALUES
                            (:gateway_id, :tag_name, CAST(:ts_utc AS timestamptz), :source, :gateway_name, :device_name, :plc_ip, :database_name, :value, :quality, :quality_label, CAST(:updated_utc AS timestamptz))
                            ON CONFLICT(gateway_id, tag_name) DO UPDATE SET
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
                if hist_rows:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."historian_readings"
                            (local_id, ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name, value, quality, quality_label, source, created_utc)
                            VALUES
                            (:local_id, CAST(:ts_utc AS timestamptz), :gateway_id, :gateway_name, :device_name, :plc_ip, :database_name, :tag_name, :value, :quality, :quality_label, :source, CAST(:created_utc AS timestamptz))
                            ON CONFLICT(local_id) DO NOTHING
                            """
                        ),
                        [
                            {
                                "local_id": int(r["id"]),
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
                        ],
                    )
                if log_rows:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."app_logs"
                            (local_id, ts_utc, level, category, message, gateway_id, gateway_name, device_name, database_name, created_utc)
                            VALUES
                            (:local_id, CAST(:ts_utc AS timestamptz), :level, :category, :message, :gateway_id, :gateway_name, :device_name, :database_name, CAST(:created_utc AS timestamptz))
                            ON CONFLICT(local_id) DO NOTHING
                            """
                        ),
                        [
                            {
                                "local_id": int(r["id"]),
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
            )
        except Exception as exc:
            self._set_data_sync_state(last_data_error=f"Data sync failed: {exc}")
            self._upsert_sync_target_state(
                enabled=True,
                config={"name": cloud.get("name"), "host": cloud.get("host"), "schema": schema},
                last_error=f"Data sync failed: {exc}",
            )
        finally:
            engine.dispose()

    def _ensure_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS config_documents (
                      domain TEXT PRIMARY KEY,
                      payload_json TEXT NOT NULL,
                      version INTEGER NOT NULL DEFAULT 1,
                      updated_utc TEXT NOT NULL
                    );

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
                      ts_utc TEXT NOT NULL,
                      gateway_id TEXT NULL,
                      gateway_name TEXT NULL,
                      device_name TEXT NULL,
                      plc_ip TEXT NULL,
                      database_name TEXT NULL,
                      tag_name TEXT NOT NULL,
                      value REAL NULL,
                      quality INTEGER NULL,
                      quality_label TEXT NULL,
                      source TEXT NULL,
                      created_utc TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_hist_ts ON historian_readings(ts_utc DESC);
                    CREATE INDEX IF NOT EXISTS idx_hist_tag ON historian_readings(tag_name);
                    CREATE INDEX IF NOT EXISTS idx_hist_gateway ON historian_readings(gateway_id);

                    CREATE TABLE IF NOT EXISTS app_logs (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                    """
                )
                now = self._utc_now()
                conn.execute(
                    """
                    INSERT INTO retention_policy
                    (id, enabled, schedule_minutes, raw_keep_days, minute_keep_days, hour_keep_days, day_keep_days, backup_before_cleanup, max_delete_rows_per_run, updated_utc)
                    VALUES (1, 0, 60, 7, 30, 180, 730, 1, 50000, ?)
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
        try:
            if self._scheduler_thread.is_alive():
                self._scheduler_thread.join(timeout=1.5)
        except Exception:
            pass
        try:
            if self._sync_thread.is_alive():
                self._sync_thread.join(timeout=2.0)
        except Exception:
            pass

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

    def get_bootstrap(self) -> Dict[str, Any]:
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
        return out

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

    def get_inspector_snapshot(self, preview_limit: int = 10) -> Dict[str, Any]:
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
                data_sync["historian_backlog"] = max(0, int(data_sync["local_historian_rows"]) - int(data_sync["last_historian_id"]))
                data_sync["logs_backlog"] = max(0, int(data_sync["local_log_rows"]) - int(data_sync["last_log_id"]))

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
        payload_to_store = payload
        if str(domain or "").strip() == "users_access":
            payload_to_store = self._normalize_users_access_payload(payload)
        with self._lock:
            with self._connect() as conn:
                prev = conn.execute(
                    "SELECT version FROM config_documents WHERE domain = ?",
                    (domain,),
                ).fetchone()
                old_version = int(prev["version"]) if prev else 0
                new_version = old_version + 1
                payload_json = json.dumps(payload_to_store)
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
        return {"domain": domain, "version": new_version, "updated_utc": now}

    def save_bootstrap(self, data: Dict[str, Any], actor: str = "system") -> Dict[str, Any]:
        versions: Dict[str, Any] = {}
        for domain, payload in data.items():
            if not isinstance(domain, str) or not domain.strip():
                continue
            versions[domain] = self.upsert_domain(domain.strip(), payload, actor=actor)
        return versions

    def append_historian_rows(self, rows: list[dict[str, Any]]) -> int:
        now = self._utc_now()
        safe_rows = []
        for r in rows or []:
            safe_rows.append(
                (
                    str(r.get("ts_utc") or r.get("ts") or now),
                    str(r.get("gateway_id") or ""),
                    str(r.get("gateway_name") or ""),
                    str(r.get("device_name") or ""),
                    str(r.get("plc_ip") or ""),
                    str(r.get("database_name") or ""),
                    str(r.get("tag_name") or r.get("tag") or ""),
                    float(r.get("value")) if r.get("value") is not None else None,
                    int(r.get("quality")) if r.get("quality") is not None else None,
                    str(r.get("quality_label") or ""),
                    str(r.get("source") or ""),
                    now,
                )
            )
        if not safe_rows:
            return 0
        with self._lock:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO historian_readings
                    (ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name, value, quality, quality_label, source, created_utc)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    safe_rows,
                )
        self._sync_wakeup_event.set()
        return len(safe_rows)

    def append_log_rows(self, rows: list[dict[str, Any]]) -> int:
        now = self._utc_now()
        safe_rows = []
        for r in rows or []:
            safe_rows.append(
                (
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
                    (ts_utc, level, category, message, gateway_id, gateway_name, device_name, database_name, created_utc)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    safe_rows,
                )
        self._sync_wakeup_event.set()
        return len(safe_rows)

    def get_historian_rows(self, limit: int = 1000) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit or 1000), 10000))
        # Hosted/web deployments should prefer cloud-backed historian reads so the
        # website mirrors edge-collected data even when no local gateways run on VPS.
        prefer_cloud = str(os.environ.get("TRUSTNODE_PREFER_CLOUD_READS", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if prefer_cloud:
            cloud_rows = self._fetch_historian_rows_from_cloud(lim)
            if cloud_rows:
                return cloud_rows

        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                           tag_name, value, quality, quality_label
                    FROM historian_readings
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (lim,),
                ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "ts": r["ts_utc"],
                    "source": r["source"] or "",
                    "gateway_id": r["gateway_id"] or "",
                    "gateway_name": r["gateway_name"] or "",
                    "device_name": r["device_name"] or "",
                    "plc_ip": r["plc_ip"] or "",
                    "database_name": r["database_name"] or "",
                    "tag": r["tag_name"] or "",
                    "value": r["value"],
                    "quality": r["quality"],
                    "quality_label": r["quality_label"] or "",
                }
            )
        if not out:
            cloud_rows = self._fetch_historian_rows_from_cloud(lim)
            if cloud_rows:
                return cloud_rows
        return out

    def get_live_rows(self, limit: int = 5000) -> list[dict[str, Any]]:
        lim = max(100, min(int(limit or 5000), 50000))
        prefer_cloud = str(os.environ.get("TRUSTNODE_PREFER_CLOUD_READS", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if prefer_cloud:
            cloud_live = self._fetch_live_rows_from_cloud(lim)
            if cloud_live:
                return cloud_live

        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT ts_utc, source, gateway_id, gateway_name, device_name, plc_ip, database_name,
                           tag_name, value, quality, quality_label
                    FROM historian_readings
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (max(lim, 20000),),
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
            if len(latest) >= lim:
                break

        if latest:
            return list(latest.values())
        cloud_live = self._fetch_live_rows_from_cloud(lim)
        if cloud_live:
            return cloud_live
        return []

    def get_log_rows(self, limit: int = 2000) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit or 2000), 10000))
        # Hosted/web deployments should prefer cloud-backed log reads.
        prefer_cloud = str(os.environ.get("TRUSTNODE_PREFER_CLOUD_READS", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if prefer_cloud:
            cloud_rows = self._fetch_log_rows_from_cloud(lim)
            if cloud_rows:
                return cloud_rows

        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT ts_utc, level, category, message, gateway_id, gateway_name, device_name, database_name
                    FROM app_logs
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (lim,),
                ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "ts": r["ts_utc"],
                    "level": r["level"] or "info",
                    "category": r["category"] or "system",
                    "message": r["message"] or "",
                    "gateway_id": r["gateway_id"] or "",
                    "gateway_name": r["gateway_name"] or "",
                    "device_name": r["device_name"] or "",
                    "database_name": r["database_name"] or "",
                }
            )
        if not out:
            cloud_rows = self._fetch_log_rows_from_cloud(lim)
            if cloud_rows:
                return cloud_rows
        return out

    def cleanup_data(self, mode: str, actor: str = "manual") -> Dict[str, Any]:
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
                    deleted["historian_readings"] = int(conn.execute("DELETE FROM historian_readings").rowcount or 0)
                    deleted["app_logs"] = int(conn.execute("DELETE FROM app_logs").rowcount or 0)
                    deleted["historian_agg_minute"] = int(conn.execute("DELETE FROM historian_agg_minute").rowcount or 0)
                    deleted["historian_agg_hour"] = int(conn.execute("DELETE FROM historian_agg_hour").rowcount or 0)
                    deleted["historian_agg_day"] = int(conn.execute("DELETE FROM historian_agg_day").rowcount or 0)
                else:
                    cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")
                    deleted["historian_readings"] = int(conn.execute("DELETE FROM historian_readings WHERE ts_utc >= ?", (cutoff_text,)).rowcount or 0)
                    deleted["app_logs"] = int(conn.execute("DELETE FROM app_logs WHERE ts_utc >= ?", (cutoff_text,)).rowcount or 0)
                    deleted["historian_agg_minute"] = int(conn.execute("DELETE FROM historian_agg_minute WHERE bucket_utc >= ?", (cutoff_text,)).rowcount or 0)
                    deleted["historian_agg_hour"] = int(conn.execute("DELETE FROM historian_agg_hour WHERE bucket_utc >= ?", (cutoff_text,)).rowcount or 0)
                    deleted["historian_agg_day"] = int(conn.execute("DELETE FROM historian_agg_day WHERE bucket_utc >= ?", (cutoff_text,)).rowcount or 0)

        summary = (
            f"Cleanup '{mode_clean}' complete by {actor}. "
            f"Deleted readings={deleted['historian_readings']}, logs={deleted['app_logs']}, "
            f"agg_min={deleted['historian_agg_minute']}, agg_hour={deleted['historian_agg_hour']}, agg_day={deleted['historian_agg_day']}."
        )
        return {"ok": True, "message": summary, "deleted": deleted}
