from typing import Literal, Any
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone
import re
from urllib.parse import quote_plus

from fastapi import APIRouter
from pydantic import BaseModel

from app.state import plc_manager

router = APIRouter(prefix="/api/database", tags=["database"])


class DatabaseConnectionTestRequest(BaseModel):
    engine: Literal["mysql", "postgresql", "mssql", "influxdb", "sqlite", "legacy_http", "csv_file", "txt_file"] = "mysql"
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    sqlite_path: str = ""
    file_path: str = ""
    legacy_url: str = ""
    legacy_api_token: str = ""
    tls: bool = True
    timeout_ms: int = 2500


class DatabaseConnectionTestResult(BaseModel):
    ok: bool
    tcp_ok: bool
    message: str


class DatabaseProvisionRequest(BaseModel):
    engine: Literal["mysql", "postgresql", "mssql", "influxdb", "sqlite", "legacy_http", "csv_file", "txt_file"] = "postgresql"
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    sqlite_path: str = ""
    file_path: str = ""
    schema: str = "public"
    table: str = "plc_readings"
    tls: bool = True


class DatabaseProvisionResult(BaseModel):
    ok: bool
    created_schema: bool = False
    created_table: bool = False
    message: str


class ActiveSinkRequest(BaseModel):
    engine: Literal["postgresql", "sqlite", "legacy_http", "csv_file", "txt_file"] = "postgresql"
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    sqlite_path: str = ""
    file_path: str = ""
    legacy_url: str = ""
    legacy_api_token: str = ""
    source: str = ""
    site: str = ""
    area: str = ""
    equipment: str = ""
    schema: str = "public"
    table: str = "plc_readings"
    tls: bool = True


class ActiveSinkResult(BaseModel):
    ok: bool
    message: str
    sink: dict | None = None


class RecoveryConnection(BaseModel):
    id: str = ""
    name: str = ""
    engine: Literal["postgresql", "sqlite", "legacy_http", "csv_file", "txt_file"] = "postgresql"
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    sqlite_path: str = ""
    file_path: str = ""
    legacy_url: str = ""
    legacy_api_token: str = ""
    schema: str = "public"
    table: str = "plc_readings"
    tls: bool = True
    source: str = ""
    site: str = ""
    area: str = ""
    equipment: str = ""
    enabled: bool = True


class RecoveryRequest(BaseModel):
    connections: list[RecoveryConnection] = []
    activate_first_healthy: bool = False


class RecoveryItemResult(BaseModel):
    id: str
    name: str
    engine: str
    ok: bool
    message: str
    tested: bool = False
    provisioned: bool = False
    activated: bool = False


class RecoveryResult(BaseModel):
    ok: bool
    summary: str
    results: list[RecoveryItemResult] = []


def _safe_name(raw: str, default: str) -> str:
    name = (raw or "").strip()
    if not name:
        return default
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return name
    return default


def _build_pg_sqlalchemy_url(host: str, port: int, database: str, username: str, password: str) -> str:
    user = quote_plus(username)
    pwd = quote_plus(password)
    db = quote_plus(database or "postgres")
    return f"postgresql+psycopg://{user}:{pwd}@{host}:{port}/{db}"


def _build_sqlite_sqlalchemy_url(path: str) -> str:
    clean = (path or "").strip()
    if not clean:
        clean = "./data/trustnode_edge.db"
    clean = clean.replace("\\", "/")
    if clean == ":memory:":
        return "sqlite+pysqlite:///:memory:"
    if re.match(r"^[A-Za-z]:/", clean):
        return f"sqlite+pysqlite:///{clean}"
    if clean.startswith("/"):
        return f"sqlite+pysqlite:///{clean}"
    return f"sqlite+pysqlite:///./{clean}"


def _ensure_app_tables_postgresql(conn: Any, schema_name: str) -> None:
    from sqlalchemy import text  # type: ignore
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS "{schema_name}"."config_documents" (
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
            CREATE TABLE IF NOT EXISTS "{schema_name}"."config_audit" (
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
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS "{schema_name}"."historian_readings" (
              id BIGSERIAL PRIMARY KEY,
              ts_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
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
              created_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS "{schema_name}"."app_logs" (
              id BIGSERIAL PRIMARY KEY,
              ts_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              level TEXT NOT NULL,
              category TEXT NOT NULL,
              message TEXT NOT NULL,
              gateway_id TEXT NULL,
              gateway_name TEXT NULL,
              device_name TEXT NULL,
              database_name TEXT NULL,
              created_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS "{schema_name}"."sync_targets" (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              target_type TEXT NOT NULL,
              config_json JSONB NOT NULL,
              enabled BOOLEAN NOT NULL DEFAULT FALSE,
              last_sync_utc TIMESTAMPTZ NULL,
              last_error TEXT NULL,
              updated_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS "{schema_name}"."sync_outbox" (
              id BIGSERIAL PRIMARY KEY,
              domain TEXT NOT NULL,
              entity_key TEXT NOT NULL,
              payload_json JSONB NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              retries INTEGER NOT NULL DEFAULT 0,
              last_error TEXT NULL,
              created_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(text(f'CREATE INDEX IF NOT EXISTS "idx_sync_outbox_status" ON "{schema_name}"."sync_outbox"(status, id)'))
    conn.execute(text(f'CREATE INDEX IF NOT EXISTS "idx_hist_ts" ON "{schema_name}"."historian_readings"(ts_utc DESC)'))
    conn.execute(text(f'CREATE INDEX IF NOT EXISTS "idx_logs_ts" ON "{schema_name}"."app_logs"(ts_utc DESC)'))


def _ensure_app_tables_sqlite(conn: Any) -> None:
    from sqlalchemy import text  # type: ignore
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS config_documents (
              domain TEXT PRIMARY KEY,
              payload_json TEXT NOT NULL,
              version INTEGER NOT NULL DEFAULT 1,
              updated_utc TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS config_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              domain TEXT NOT NULL,
              actor TEXT NULL,
              old_version INTEGER NULL,
              new_version INTEGER NOT NULL,
              changed_utc TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS historian_readings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
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
              created_utc TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS app_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
              level TEXT NOT NULL,
              category TEXT NOT NULL,
              message TEXT NOT NULL,
              gateway_id TEXT NULL,
              gateway_name TEXT NULL,
              device_name TEXT NULL,
              database_name TEXT NULL,
              created_utc TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS sync_targets (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              target_type TEXT NOT NULL,
              config_json TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 0,
              last_sync_utc TEXT NULL,
              last_error TEXT NULL,
              updated_utc TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS sync_outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              domain TEXT NOT NULL,
              entity_key TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              retries INTEGER NOT NULL DEFAULT 0,
              last_error TEXT NULL,
              created_utc TEXT NOT NULL DEFAULT (datetime('now')),
              updated_utc TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sync_outbox_status ON sync_outbox(status, id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_hist_ts ON historian_readings(ts_utc DESC)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_logs_ts ON app_logs(ts_utc DESC)"))


@router.post("/test-connection", response_model=DatabaseConnectionTestResult)
def test_connection(payload: DatabaseConnectionTestRequest) -> DatabaseConnectionTestResult:
    if payload.engine == "legacy_http":
        url = payload.legacy_url.strip()
        token = payload.legacy_api_token.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return DatabaseConnectionTestResult(ok=False, tcp_ok=False, message="Legacy URL must start with http:// or https://")
        if not token:
            return DatabaseConnectionTestResult(ok=False, tcp_ok=False, message="Legacy API token is required")
        req_payload = {
            "test": "connection",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        }
        timeout_s = max(1.0, min(payload.timeout_ms, 10_000) / 1000.0)

        # Keep parity with old tray app behavior (requests + JSON + X-API-TOKEN).
        try:
            import requests  # type: ignore

            headers = {
                "Content-Type": "application/json",
                "X-API-TOKEN": token,
                "User-Agent": "python-requests/trustnode-edge"
            }
            response = requests.post(url, json=req_payload, headers=headers, timeout=timeout_s)
            code = int(response.status_code)
            if code in (200, 201, 400):
                return DatabaseConnectionTestResult(ok=True, tcp_ok=True, message=f"Legacy API reachable (HTTP {code})")
            body_preview = (response.text or "").strip().replace("\n", " ")[:180]
            return DatabaseConnectionTestResult(
                ok=False,
                tcp_ok=False,
                message=f"Legacy API HTTP error {code}{': ' + body_preview if body_preview else ''}"
            )
        except Exception:
            # Fallback path when requests is unavailable.
            req = urllib.request.Request(
                url,
                data=json.dumps(req_payload).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-API-TOKEN": token,
                    "User-Agent": "python-requests/trustnode-edge"
                }
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    code = int(resp.status)
                    if code in (200, 201, 400):
                        return DatabaseConnectionTestResult(ok=True, tcp_ok=True, message=f"Legacy API reachable (HTTP {code})")
                    return DatabaseConnectionTestResult(ok=False, tcp_ok=False, message=f"Legacy API unexpected HTTP {code}")
            except urllib.error.HTTPError as err:
                code = int(err.code)
                body = ""
                try:
                    body = err.read().decode("utf-8", errors="ignore").strip().replace("\n", " ")[:180]
                except Exception:
                    body = ""
                if code in (200, 201, 400):
                    return DatabaseConnectionTestResult(ok=True, tcp_ok=True, message=f"Legacy API reachable (HTTP {code})")
                if code == 403:
                    return DatabaseConnectionTestResult(
                        ok=False,
                        tcp_ok=False,
                        message="Legacy API HTTP 403 (forbidden). Check API token and ensure there are no hidden characters/spaces."
                    )
                return DatabaseConnectionTestResult(
                    ok=False,
                    tcp_ok=False,
                    message=f"Legacy API HTTP error {code}{': ' + body if body else ''}"
                )
            except Exception as err:  # pragma: no cover - runtime dependent
                return DatabaseConnectionTestResult(ok=False, tcp_ok=False, message=f"Legacy API connection failed: {err}")

    if payload.engine == "postgresql":
        try:
            from sqlalchemy import create_engine, text
        except Exception:
            return DatabaseConnectionTestResult(
                ok=False,
                tcp_ok=False,
                message="SQLAlchemy is not installed. Install backend dependencies first."
            )
        host = payload.host.strip()
        port = int(payload.port or 0)
        dbname = payload.database.strip() or "postgres"
        user = payload.username.strip()
        if not host or port <= 0 or not user:
            return DatabaseConnectionTestResult(
                ok=False,
                tcp_ok=False,
                message="PostgreSQL test requires host, port, database, and username."
            )
        timeout_s = max(1, int(min(payload.timeout_ms, 10_000) / 1000))
        url = _build_pg_sqlalchemy_url(host, port, dbname, user, payload.password)
        try:
            engine = create_engine(
                url,
                pool_pre_ping=True,
                connect_args={
                    "sslmode": "require" if payload.tls else "disable",
                    "connect_timeout": timeout_s
                },
            )
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return DatabaseConnectionTestResult(
                ok=True,
                tcp_ok=True,
                message=f"PostgreSQL connection/auth successful for {host}:{port}/{dbname}"
            )
        except Exception as err:  # pragma: no cover - runtime dependent
            return DatabaseConnectionTestResult(
                ok=False,
                tcp_ok=False,
                message=f"PostgreSQL connection failed: {err}"
            )

    if payload.engine == "sqlite":
        try:
            from sqlalchemy import create_engine, text
        except Exception:
            return DatabaseConnectionTestResult(
                ok=False,
                tcp_ok=False,
                message="SQLAlchemy is not installed. Install backend dependencies first."
            )
        sqlite_path = payload.sqlite_path.strip() or "./data/trustnode_edge.db"
        try:
            engine = create_engine(_build_sqlite_sqlalchemy_url(sqlite_path), pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return DatabaseConnectionTestResult(
                ok=True,
                tcp_ok=True,
                message=f"SQLite connection successful: {sqlite_path}"
            )
        except Exception as err:
            return DatabaseConnectionTestResult(
                ok=False,
                tcp_ok=False,
                message=f"SQLite connection failed: {err}"
            )

    if payload.engine in ("csv_file", "txt_file"):
        import os

        path = payload.file_path.strip()
        if not path:
            return DatabaseConnectionTestResult(ok=False, tcp_ok=False, message="File path is required.")
        try:
            full = os.path.abspath(path)
            parent = os.path.dirname(full)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(full, "a", encoding="utf-8"):
                pass
            return DatabaseConnectionTestResult(ok=True, tcp_ok=True, message=f"File path writable: {full}")
        except Exception as err:
            return DatabaseConnectionTestResult(ok=False, tcp_ok=False, message=f"File path not writable: {err}")

    # For non-PostgreSQL engines in MVP we keep explicit unsupported messaging
    # to avoid false positives from TCP-only tests.
    return DatabaseConnectionTestResult(
        ok=False,
        tcp_ok=False,
        message=f"Engine '{payload.engine}' test is not implemented yet in SQLAlchemy mode."
    )


@router.post("/provision", response_model=DatabaseProvisionResult)
def provision_database(payload: DatabaseProvisionRequest) -> DatabaseProvisionResult:
    if payload.engine == "legacy_http":
        return DatabaseProvisionResult(
            ok=True,
            created_schema=False,
            created_table=False,
            message="Legacy HTTP mode does not require DB schema/table provisioning."
        )

    if payload.engine == "postgresql":
        try:
            from sqlalchemy import create_engine, text
        except Exception:
            return DatabaseProvisionResult(
                ok=False,
                message="SQLAlchemy/psycopg is not installed. Install backend dependencies."
            )

        host = payload.host.strip()
        port = int(payload.port or 0)
        dbname = payload.database.strip() or "postgres"
        user = payload.username.strip()
        if not host or not port or not user:
            return DatabaseProvisionResult(
                ok=False,
                message="PostgreSQL provisioning requires host, port, database, and username."
            )

        schema_name = _safe_name(payload.schema, "public")
        table_name = _safe_name(payload.table, "plc_readings")
        sslmode = "require" if payload.tls else "disable"
        url = _build_pg_sqlalchemy_url(host, port, dbname, user, payload.password)
        try:
            engine = create_engine(
                url,
                pool_pre_ping=True,
                connect_args={"sslmode": sslmode, "connect_timeout": 8},
            )
            with engine.begin() as conn:
                if schema_name != "public":
                    conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{schema_name}"."{table_name}" (
                          id BIGSERIAL PRIMARY KEY,
                          ts_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                          tag_name TEXT NOT NULL,
                          value DOUBLE PRECISION NULL,
                          quality INTEGER NULL,
                          source TEXT NULL,
                          site TEXT NULL,
                          area TEXT NULL,
                          equipment TEXT NULL,
                          seq BIGINT NULL,
                          raw_payload JSONB NULL
                        )
                        """
                    )
                )
            engine.dispose()
            return DatabaseProvisionResult(
                ok=True,
                created_schema=True,
                created_table=True,
                message=f"Provisioned PostgreSQL objects: {schema_name}.{table_name}"
            )
        except Exception as err:
            return DatabaseProvisionResult(ok=False, message=f"PostgreSQL provisioning failed: {err}")

    if payload.engine == "sqlite":
        try:
            from sqlalchemy import create_engine, text
        except Exception:
            return DatabaseProvisionResult(
                ok=False,
                message="SQLAlchemy is not installed. Install backend dependencies first."
            )
        table_name = _safe_name(payload.table, "plc_readings")
        sqlite_path = payload.sqlite_path.strip() or "./data/trustnode_edge.db"
        try:
            engine = create_engine(_build_sqlite_sqlalchemy_url(sqlite_path), pool_pre_ping=True)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{table_name}" (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
                          tag_name TEXT NOT NULL,
                          value REAL NULL,
                          quality INTEGER NULL,
                          source TEXT NULL,
                          site TEXT NULL,
                          area TEXT NULL,
                          equipment TEXT NULL,
                          seq INTEGER NULL,
                          raw_payload TEXT NULL
                        )
                        """
                    )
                )
            engine.dispose()
            return DatabaseProvisionResult(
                ok=True,
                created_schema=False,
                created_table=True,
                message=f"Provisioned SQLite table: {table_name} ({sqlite_path})"
            )
        except Exception as err:
            return DatabaseProvisionResult(ok=False, message=f"SQLite provisioning failed: {err}")

    if payload.engine in ("csv_file", "txt_file"):
        import os

        path = payload.file_path.strip()
        if not path:
            return DatabaseProvisionResult(ok=False, message="File path is required.")
        try:
            full = os.path.abspath(path)
            parent = os.path.dirname(full)
            if parent:
                os.makedirs(parent, exist_ok=True)
            created = not os.path.exists(full)
            if payload.engine == "csv_file":
                needs_header = created or os.path.getsize(full) == 0
                with open(full, "a", encoding="utf-8", newline="") as f:
                    if needs_header:
                        f.write("ts_utc,tag_name,value,quality,quality_label,source,site,area,equipment\n")
            else:
                with open(full, "a", encoding="utf-8"):
                    pass
            return DatabaseProvisionResult(
                ok=True,
                created_schema=False,
                created_table=created,
                message=f"Provisioned file sink: {full}"
            )
        except Exception as err:
            return DatabaseProvisionResult(ok=False, message=f"File sink provisioning failed: {err}")

    return DatabaseProvisionResult(
        ok=False,
        message=f"Provisioning for engine '{payload.engine}' is not implemented yet."
    )


@router.post("/activate-sink", response_model=ActiveSinkResult)
def activate_sink(payload: ActiveSinkRequest) -> ActiveSinkResult:
    plc_manager.set_db_sink(payload.model_dump())
    return ActiveSinkResult(
        ok=True,
        message=f"Active sink set to {payload.engine}",
        sink=plc_manager.get_db_sink(),
    )


@router.get("/active-sink", response_model=ActiveSinkResult)
def get_active_sink() -> ActiveSinkResult:
    sink = plc_manager.get_db_sink()
    if not sink:
        return ActiveSinkResult(ok=False, message="No active sink configured", sink=None)
    return ActiveSinkResult(ok=True, message=f"Active sink is {sink.get('engine', 'unknown')}", sink=sink)


def _recover_one_connection(conn_cfg: RecoveryConnection, activate: bool = False) -> RecoveryItemResult:
    name = conn_cfg.name or conn_cfg.id or "unnamed"
    engine = conn_cfg.engine
    tested = False
    provisioned = False
    activated = False

    test_req = DatabaseConnectionTestRequest(
        engine=engine,
        host=conn_cfg.host,
        port=conn_cfg.port,
        database=conn_cfg.database,
        username=conn_cfg.username,
        password=conn_cfg.password,
        sqlite_path=conn_cfg.sqlite_path,
        file_path=conn_cfg.file_path,
        legacy_url=conn_cfg.legacy_url,
        legacy_api_token=conn_cfg.legacy_api_token,
        tls=conn_cfg.tls,
        timeout_ms=3500,
    )
    test_res = test_connection(test_req)
    tested = True
    if not test_res.ok:
        return RecoveryItemResult(
            id=conn_cfg.id,
            name=name,
            engine=engine,
            ok=False,
            message=f"Test failed: {test_res.message}",
            tested=tested,
            provisioned=provisioned,
            activated=activated,
        )

    # Provision/create required objects automatically.
    if engine in ("postgresql", "sqlite", "legacy_http", "csv_file", "txt_file"):
        prov_req = DatabaseProvisionRequest(
            engine=engine,
            host=conn_cfg.host,
            port=conn_cfg.port,
            database=conn_cfg.database,
            username=conn_cfg.username,
            password=conn_cfg.password,
            sqlite_path=conn_cfg.sqlite_path,
            file_path=conn_cfg.file_path,
            schema=conn_cfg.schema or "public",
            table=conn_cfg.table or "plc_readings",
            tls=conn_cfg.tls,
        )
        prov_res = provision_database(prov_req)
        if not prov_res.ok:
            return RecoveryItemResult(
                id=conn_cfg.id,
                name=name,
                engine=engine,
                ok=False,
                message=f"Provision failed: {prov_res.message}",
                tested=tested,
                provisioned=provisioned,
                activated=activated,
            )
        provisioned = True

    # Extended provisioning for app-level tables on local/cloud relational DB.
    if engine in ("postgresql", "sqlite"):
        try:
            from sqlalchemy import create_engine
        except Exception as err:
            return RecoveryItemResult(
                id=conn_cfg.id,
                name=name,
                engine=engine,
                ok=False,
                message=f"SQLAlchemy unavailable for extended provisioning: {err}",
                tested=tested,
                provisioned=provisioned,
                activated=activated,
            )
        try:
            if engine == "postgresql":
                schema_name = _safe_name(conn_cfg.schema, "public")
                url = _build_pg_sqlalchemy_url(
                    conn_cfg.host.strip(),
                    int(conn_cfg.port or 0),
                    conn_cfg.database.strip() or "postgres",
                    conn_cfg.username.strip(),
                    conn_cfg.password,
                )
                db_engine = create_engine(
                    url,
                    pool_pre_ping=True,
                    connect_args={"sslmode": "require" if conn_cfg.tls else "disable", "connect_timeout": 8},
                )
                with db_engine.begin() as tx:
                    if schema_name != "public":
                        from sqlalchemy import text
                        tx.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))
                    _ensure_app_tables_postgresql(tx, schema_name)
                db_engine.dispose()
            else:
                url = _build_sqlite_sqlalchemy_url(conn_cfg.sqlite_path)
                db_engine = create_engine(url, pool_pre_ping=True)
                with db_engine.begin() as tx:
                    _ensure_app_tables_sqlite(tx)
                db_engine.dispose()
            provisioned = True
        except Exception as err:
            return RecoveryItemResult(
                id=conn_cfg.id,
                name=name,
                engine=engine,
                ok=False,
                message=f"Extended table provisioning failed: {err}",
                tested=tested,
                provisioned=provisioned,
                activated=activated,
            )

    if activate:
        sink_req = ActiveSinkRequest(
            engine=engine,
            host=conn_cfg.host,
            port=conn_cfg.port,
            database=conn_cfg.database,
            username=conn_cfg.username,
            password=conn_cfg.password,
            sqlite_path=conn_cfg.sqlite_path,
            file_path=conn_cfg.file_path,
            legacy_url=conn_cfg.legacy_url,
            legacy_api_token=conn_cfg.legacy_api_token,
            source=conn_cfg.source,
            site=conn_cfg.site,
            area=conn_cfg.area,
            equipment=conn_cfg.equipment,
            schema=conn_cfg.schema or "public",
            table=conn_cfg.table or "plc_readings",
            tls=conn_cfg.tls,
        )
        plc_manager.set_db_sink(sink_req.model_dump())
        activated = True

    return RecoveryItemResult(
        id=conn_cfg.id,
        name=name,
        engine=engine,
        ok=True,
        message="Recovery check/provision completed.",
        tested=tested,
        provisioned=provisioned,
        activated=activated,
    )


@router.post("/recovery/check", response_model=RecoveryResult)
def recovery_check(payload: RecoveryRequest) -> RecoveryResult:
    results: list[RecoveryItemResult] = []
    for conn_cfg in payload.connections or []:
        if not conn_cfg.enabled:
            continue
        results.append(_recover_one_connection(conn_cfg, activate=False))
    ok = all(r.ok for r in results) if results else True
    summary = f"Checked {len(results)} connection(s): {sum(1 for r in results if r.ok)} healthy, {sum(1 for r in results if not r.ok)} failed."
    return RecoveryResult(ok=ok, summary=summary, results=results)


@router.post("/recovery/repair", response_model=RecoveryResult)
def recovery_repair(payload: RecoveryRequest) -> RecoveryResult:
    results: list[RecoveryItemResult] = []
    activate_done = False
    for conn_cfg in payload.connections or []:
        if not conn_cfg.enabled:
            continue
        activate_this = bool(payload.activate_first_healthy and not activate_done)
        result = _recover_one_connection(conn_cfg, activate=activate_this)
        if result.ok and result.activated:
            activate_done = True
        results.append(result)
    ok = all(r.ok for r in results) if results else True
    activated_count = sum(1 for r in results if r.activated)
    summary = (
        f"Repaired {len(results)} connection(s): {sum(1 for r in results if r.ok)} healthy, "
        f"{sum(1 for r in results if not r.ok)} failed, {activated_count} activated."
    )
    return RecoveryResult(ok=ok, summary=summary, results=results)
