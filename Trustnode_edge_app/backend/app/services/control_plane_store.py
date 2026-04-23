import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from app.tenant import normalize_tenant_id


class ControlPlaneStore:
    MODULE_CATALOG: list[dict[str, Any]] = [
        {"key": "dashboard", "label": "Dashboard", "default_enabled": True},
        {"key": "power_overview", "label": "Power Management Overview", "default_enabled": True},
        {"key": "historian", "label": "Historian", "default_enabled": True},
        {"key": "reporting", "label": "Reporting", "default_enabled": True},
        {"key": "alarms", "label": "Alarms", "default_enabled": True},
        {"key": "interface", "label": "Interface", "default_enabled": True},
        {"key": "tags", "label": "Tags", "default_enabled": False},
        {"key": "gateway_configuration", "label": "Gateway Configuration", "default_enabled": False},
        {"key": "gateway_runtime_control", "label": "Gateway Runtime Control", "default_enabled": False},
        {"key": "database", "label": "Database", "default_enabled": False},
        {"key": "users_and_access_control", "label": "Users and Access Control", "default_enabled": False},
    ]

    def __init__(self) -> None:
        # Re-entrant lock avoids self-deadlock for nested store calls
        # (e.g. activation flow upserting edge metadata).
        self._lock = threading.RLock()
        self._db_path = self._resolve_db_path()
        self._ensure_schema()
        self._seed_defaults()

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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def _sha256(self, raw: str) -> str:
        return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()

    def _b64url_encode(self, raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    def _b64url_decode(self, raw: str) -> bytes:
        pad = "=" * ((4 - (len(raw) % 4)) % 4)
        return base64.urlsafe_b64decode((raw + pad).encode("utf-8"))

    def _hash_password(self, password: str, iterations: int = 120_000) -> str:
        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return f"pbkdf2_sha256${iterations}${self._b64url_encode(salt)}${self._b64url_encode(digest)}"

    def _verify_password(self, password: str, stored: str) -> bool:
        raw = str(stored or "")
        if raw.startswith("pbkdf2_sha256$"):
            try:
                _, iter_txt, salt_txt, hash_txt = raw.split("$", 3)
                iterations = int(iter_txt)
                salt = self._b64url_decode(salt_txt)
                expected = self._b64url_decode(hash_txt)
                digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
                return hmac.compare_digest(digest, expected)
            except Exception:
                return False
        return hmac.compare_digest(password, raw)

    def _ensure_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                cur = conn.cursor()
                cur.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA foreign_keys=ON;

                    CREATE TABLE IF NOT EXISTS cp_tenants (
                      tenant_id TEXT PRIMARY KEY,
                      name TEXT NOT NULL,
                      status TEXT NOT NULL DEFAULT 'active',
                      primary_domain TEXT,
                      timezone TEXT NOT NULL DEFAULT 'UTC',
                      metadata_json TEXT NOT NULL DEFAULT '{}',
                      created_utc TEXT NOT NULL,
                      updated_utc TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS cp_customers (
                      customer_id TEXT PRIMARY KEY,
                      tenant_id TEXT NOT NULL,
                      company_name TEXT NOT NULL,
                      contact_email TEXT,
                      status TEXT NOT NULL DEFAULT 'active',
                      metadata_json TEXT NOT NULL DEFAULT '{}',
                      created_utc TEXT NOT NULL,
                      updated_utc TEXT NOT NULL,
                      FOREIGN KEY (tenant_id) REFERENCES cp_tenants(tenant_id)
                    );

                    CREATE TABLE IF NOT EXISTS cp_edges (
                      edge_id TEXT PRIMARY KEY,
                      tenant_id TEXT NOT NULL,
                      customer_id TEXT,
                      edge_name TEXT NOT NULL,
                      site TEXT,
                      area TEXT,
                      equipment TEXT,
                      status TEXT NOT NULL DEFAULT 'inactive',
                      activation_code_hash TEXT,
                      activated_utc TEXT,
                      last_heartbeat_utc TEXT,
                      heartbeat_payload_json TEXT NOT NULL DEFAULT '{}',
                      metadata_json TEXT NOT NULL DEFAULT '{}',
                      created_utc TEXT NOT NULL,
                      updated_utc TEXT NOT NULL,
                      FOREIGN KEY (tenant_id) REFERENCES cp_tenants(tenant_id),
                      FOREIGN KEY (customer_id) REFERENCES cp_customers(customer_id)
                    );

                    CREATE TABLE IF NOT EXISTS cp_licenses (
                      license_id TEXT PRIMARY KEY,
                      tenant_id TEXT NOT NULL,
                      customer_id TEXT,
                      plan_code TEXT NOT NULL DEFAULT 'standard',
                      status TEXT NOT NULL DEFAULT 'active',
                      start_utc TEXT,
                      end_utc TEXT,
                      max_edges INTEGER NOT NULL DEFAULT 3,
                      max_users INTEGER NOT NULL DEFAULT 10,
                      metadata_json TEXT NOT NULL DEFAULT '{}',
                      created_utc TEXT NOT NULL,
                      updated_utc TEXT NOT NULL,
                      FOREIGN KEY (tenant_id) REFERENCES cp_tenants(tenant_id),
                      FOREIGN KEY (customer_id) REFERENCES cp_customers(customer_id)
                    );

                    CREATE TABLE IF NOT EXISTS cp_license_modules (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      license_id TEXT NOT NULL,
                      module_key TEXT NOT NULL,
                      enabled INTEGER NOT NULL DEFAULT 1,
                      updated_utc TEXT NOT NULL,
                      UNIQUE(license_id, module_key),
                      FOREIGN KEY (license_id) REFERENCES cp_licenses(license_id)
                    );

                    CREATE TABLE IF NOT EXISTS cp_users (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      tenant_id TEXT NOT NULL,
                      username TEXT NOT NULL,
                      password_hash TEXT NOT NULL,
                      role TEXT NOT NULL DEFAULT 'viewer',
                      status TEXT NOT NULL DEFAULT 'active',
                      email TEXT,
                      mfa_enabled INTEGER NOT NULL DEFAULT 0,
                      modules_json TEXT NOT NULL DEFAULT '[]',
                      permissions_json TEXT NOT NULL DEFAULT '{}',
                      created_utc TEXT NOT NULL,
                      updated_utc TEXT NOT NULL,
                      last_login_utc TEXT,
                      UNIQUE(tenant_id, username),
                      FOREIGN KEY (tenant_id) REFERENCES cp_tenants(tenant_id)
                    );

                    CREATE TABLE IF NOT EXISTS cp_user_tenant_memberships (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      tenant_id TEXT NOT NULL,
                      username TEXT NOT NULL,
                      edge_id TEXT,
                      module_key TEXT,
                      granted INTEGER NOT NULL DEFAULT 1,
                      created_utc TEXT NOT NULL,
                      UNIQUE(tenant_id, username, edge_id, module_key)
                    );

                    CREATE TABLE IF NOT EXISTS cp_edge_activation_codes (
                      code_hash TEXT PRIMARY KEY,
                      tenant_id TEXT NOT NULL,
                      customer_id TEXT,
                      edge_name TEXT,
                      expires_utc TEXT NOT NULL,
                      used_utc TEXT,
                      status TEXT NOT NULL DEFAULT 'issued',
                      metadata_json TEXT NOT NULL DEFAULT '{}',
                      created_utc TEXT NOT NULL,
                      FOREIGN KEY (tenant_id) REFERENCES cp_tenants(tenant_id),
                      FOREIGN KEY (customer_id) REFERENCES cp_customers(customer_id)
                    );

                    CREATE TABLE IF NOT EXISTS cp_password_reset_events (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      tenant_id TEXT NOT NULL,
                      username TEXT NOT NULL,
                      token_hash TEXT NOT NULL,
                      expires_utc TEXT NOT NULL,
                      used_utc TEXT,
                      status TEXT NOT NULL DEFAULT 'issued',
                      created_utc TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS cp_security_audit_log (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      ts_utc TEXT NOT NULL,
                      actor_type TEXT NOT NULL,
                      actor_id TEXT NOT NULL,
                      tenant_id TEXT NOT NULL,
                      action TEXT NOT NULL,
                      outcome TEXT NOT NULL,
                      correlation_id TEXT NOT NULL,
                      details_json TEXT NOT NULL DEFAULT '{}'
                    );

                    CREATE INDEX IF NOT EXISTS ix_cp_customers_tenant ON cp_customers(tenant_id);
                    CREATE INDEX IF NOT EXISTS ix_cp_edges_tenant ON cp_edges(tenant_id);
                    CREATE INDEX IF NOT EXISTS ix_cp_users_tenant ON cp_users(tenant_id);
                    CREATE INDEX IF NOT EXISTS ix_cp_licenses_tenant ON cp_licenses(tenant_id);
                    CREATE INDEX IF NOT EXISTS ix_cp_audit_tenant_ts ON cp_security_audit_log(tenant_id, ts_utc DESC);
                    """
                )
                conn.commit()

    def _seed_defaults(self) -> None:
        now = self._utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.cursor()
                cur.execute("SELECT tenant_id FROM cp_tenants WHERE tenant_id='default'")
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO cp_tenants(tenant_id, name, status, primary_domain, timezone, metadata_json, created_utc, updated_utc) VALUES(?,?,?,?,?,?,?,?)",
                        ("default", "Default Tenant", "active", "trustnode.lsapps.app", "Europe/Dublin", "{}", now, now),
                    )
                cur.execute("SELECT customer_id FROM cp_customers WHERE customer_id='cust-default'")
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO cp_customers(customer_id, tenant_id, company_name, contact_email, status, metadata_json, created_utc, updated_utc) VALUES(?,?,?,?,?,?,?,?)",
                        ("cust-default", "default", "Default Customer", "", "active", "{}", now, now),
                    )
                cur.execute("SELECT license_id FROM cp_licenses WHERE license_id='lic-default'")
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO cp_licenses(license_id, tenant_id, customer_id, plan_code, status, start_utc, end_utc, max_edges, max_users, metadata_json, created_utc, updated_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("lic-default", "default", "cust-default", "standard", "active", now, "", 5, 50, "{}", now, now),
                    )
                for module in self.MODULE_CATALOG:
                    cur.execute(
                        "INSERT OR IGNORE INTO cp_license_modules(license_id, module_key, enabled, updated_utc) VALUES(?,?,?,?)",
                        ("lic-default", module["key"], int(bool(module.get("default_enabled", True))), now),
                    )
                cur.execute("SELECT id FROM cp_users WHERE tenant_id='default' AND username='admin'")
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO cp_users(tenant_id, username, password_hash, role, status, email, mfa_enabled, modules_json, permissions_json, created_utc, updated_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            "default",
                            "admin",
                            self._hash_password("admin"),
                            "admin",
                            "active",
                            "",
                            0,
                            json.dumps([m["key"] for m in self.MODULE_CATALOG], separators=(",", ":")),
                            json.dumps({}, separators=(",", ":"), sort_keys=True),
                            now,
                            now,
                        ),
                    )
                conn.commit()

    def audit(self, *, actor_type: str, actor_id: str, tenant_id: str, action: str, outcome: str, correlation_id: str, details: Dict[str, Any]) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO cp_security_audit_log(ts_utc, actor_type, actor_id, tenant_id, action, outcome, correlation_id, details_json) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        self._utc_now(),
                        str(actor_type or "system"),
                        str(actor_id or "system"),
                        normalize_tenant_id(tenant_id),
                        str(action or "unknown"),
                        str(outcome or "ok"),
                        str(correlation_id or "-"),
                        json.dumps(details or {}, separators=(",", ":"), sort_keys=True),
                    ),
                )
                conn.commit()

    def module_catalog(self) -> list[dict[str, Any]]:
        return [dict(m) for m in self.MODULE_CATALOG]

    def list_tenants(self, *, include_suspended: bool = True) -> list[dict[str, Any]]:
        q = "SELECT * FROM cp_tenants"
        if not include_suspended:
            q += " WHERE status='active'"
        q += " ORDER BY created_utc ASC"
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(q).fetchall()
        return [dict(r) for r in rows]

    def upsert_tenant(self, *, tenant_id: str, name: str, status: str = "active", primary_domain: str = "", timezone_name: str = "UTC", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        now = self._utc_now()
        payload = json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO cp_tenants(tenant_id, name, status, primary_domain, timezone, metadata_json, created_utc, updated_utc)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(tenant_id) DO UPDATE SET
                      name=excluded.name,
                      status=excluded.status,
                      primary_domain=excluded.primary_domain,
                      timezone=excluded.timezone,
                      metadata_json=excluded.metadata_json,
                      updated_utc=excluded.updated_utc
                    """,
                    (tid, str(name or tid), str(status or "active"), str(primary_domain or ""), str(timezone_name or "UTC"), payload, now, now),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM cp_tenants WHERE tenant_id=?", (tid,)).fetchone()
        return dict(row) if row else {}

    def list_customers(self, *, tenant_id: str) -> list[dict[str, Any]]:
        tid = normalize_tenant_id(tenant_id)
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM cp_customers WHERE tenant_id=? ORDER BY created_utc ASC", (tid,)).fetchall()
        return [dict(r) for r in rows]

    def upsert_customer(self, *, tenant_id: str, customer_id: str, company_name: str, contact_email: str = "", status: str = "active", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        cid = str(customer_id or "").strip() or f"cust-{secrets.token_hex(4)}"
        now = self._utc_now()
        payload = json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO cp_customers(customer_id, tenant_id, company_name, contact_email, status, metadata_json, created_utc, updated_utc)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(customer_id) DO UPDATE SET
                      tenant_id=excluded.tenant_id,
                      company_name=excluded.company_name,
                      contact_email=excluded.contact_email,
                      status=excluded.status,
                      metadata_json=excluded.metadata_json,
                      updated_utc=excluded.updated_utc
                    """,
                    (cid, tid, str(company_name or cid), str(contact_email or ""), str(status or "active"), payload, now, now),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM cp_customers WHERE customer_id=?", (cid,)).fetchone()
        return dict(row) if row else {}

    def list_edges(self, *, tenant_id: str) -> list[dict[str, Any]]:
        tid = normalize_tenant_id(tenant_id)
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM cp_edges WHERE tenant_id=? ORDER BY created_utc ASC", (tid,)).fetchall()
        return [dict(r) for r in rows]

    def upsert_edge(self, *, tenant_id: str, edge_id: str, edge_name: str, customer_id: str = "", site: str = "", area: str = "", equipment: str = "", status: str = "inactive", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        eid = str(edge_id or "").strip() or f"edge-{secrets.token_hex(5)}"
        now = self._utc_now()
        payload = json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO cp_edges(edge_id, tenant_id, customer_id, edge_name, site, area, equipment, status, metadata_json, created_utc, updated_utc)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(edge_id) DO UPDATE SET
                      tenant_id=excluded.tenant_id,
                      customer_id=excluded.customer_id,
                      edge_name=excluded.edge_name,
                      site=excluded.site,
                      area=excluded.area,
                      equipment=excluded.equipment,
                      status=excluded.status,
                      metadata_json=excluded.metadata_json,
                      updated_utc=excluded.updated_utc
                    """,
                    (eid, tid, str(customer_id or ""), str(edge_name or eid), str(site or ""), str(area or ""), str(equipment or ""), str(status or "inactive"), payload, now, now),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM cp_edges WHERE edge_id=?", (eid,)).fetchone()
        return dict(row) if row else {}

    def heartbeat_edge(self, *, tenant_id: str, edge_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        now = self._utc_now()
        hb = json.dumps(payload or {}, separators=(",", ":"), sort_keys=True)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE cp_edges SET status='active', last_heartbeat_utc=?, heartbeat_payload_json=?, updated_utc=? WHERE tenant_id=? AND edge_id=?",
                    (now, hb, now, tid, str(edge_id or "")),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM cp_edges WHERE tenant_id=? AND edge_id=?", (tid, str(edge_id or ""))).fetchone()
        return dict(row) if row else {}

    def upsert_license(self, *, tenant_id: str, license_id: str, customer_id: str = "", plan_code: str = "standard", status: str = "active", start_utc: str = "", end_utc: str = "", max_edges: int = 3, max_users: int = 10, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        lid = str(license_id or "").strip() or f"lic-{secrets.token_hex(4)}"
        now = self._utc_now()
        payload = json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO cp_licenses(license_id, tenant_id, customer_id, plan_code, status, start_utc, end_utc, max_edges, max_users, metadata_json, created_utc, updated_utc)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(license_id) DO UPDATE SET
                      tenant_id=excluded.tenant_id,
                      customer_id=excluded.customer_id,
                      plan_code=excluded.plan_code,
                      status=excluded.status,
                      start_utc=excluded.start_utc,
                      end_utc=excluded.end_utc,
                      max_edges=excluded.max_edges,
                      max_users=excluded.max_users,
                      metadata_json=excluded.metadata_json,
                      updated_utc=excluded.updated_utc
                    """,
                    (lid, tid, str(customer_id or ""), str(plan_code or "standard"), str(status or "active"), str(start_utc or ""), str(end_utc or ""), int(max_edges or 0), int(max_users or 0), payload, now, now),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM cp_licenses WHERE license_id=?", (lid,)).fetchone()
        return dict(row) if row else {}

    def list_licenses(self, *, tenant_id: str) -> list[dict[str, Any]]:
        tid = normalize_tenant_id(tenant_id)
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM cp_licenses WHERE tenant_id=? ORDER BY created_utc ASC", (tid,)).fetchall()
        return [dict(r) for r in rows]

    def set_license_modules(self, *, license_id: str, modules: list[dict[str, Any]]) -> dict[str, Any]:
        lid = str(license_id or "").strip()
        now = self._utc_now()
        with self._lock:
            with self._connect() as conn:
                for row in modules or []:
                    module_key = str((row or {}).get("module_key") or "").strip()
                    if not module_key:
                        continue
                    enabled = int(bool((row or {}).get("enabled", True)))
                    conn.execute(
                        "INSERT INTO cp_license_modules(license_id, module_key, enabled, updated_utc) VALUES(?,?,?,?) ON CONFLICT(license_id,module_key) DO UPDATE SET enabled=excluded.enabled, updated_utc=excluded.updated_utc",
                        (lid, module_key, enabled, now),
                    )
                conn.commit()
                rows = conn.execute("SELECT module_key, enabled FROM cp_license_modules WHERE license_id=? ORDER BY module_key", (lid,)).fetchall()
        return {"license_id": lid, "modules": [{"module_key": r[0], "enabled": bool(r[1])} for r in rows]}

    def list_license_modules(self, *, license_id: str) -> list[dict[str, Any]]:
        lid = str(license_id or "").strip()
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute("SELECT module_key, enabled FROM cp_license_modules WHERE license_id=? ORDER BY module_key", (lid,)).fetchall()
        return [{"module_key": r[0], "enabled": bool(r[1])} for r in rows]

    def upsert_user(self, *, tenant_id: str, username: str, password: str | None = None, role: str = "viewer", status: str = "active", email: str = "", mfa_enabled: bool = False, modules: list[str] | None = None, permissions: dict[str, Any] | None = None) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        uname = str(username or "").strip()
        if not uname:
            raise ValueError("username required")
        now = self._utc_now()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM cp_users WHERE tenant_id=? AND username=?", (tid, uname)).fetchone()
                if row:
                    updates = [
                        "role=?",
                        "status=?",
                        "email=?",
                        "mfa_enabled=?",
                        "modules_json=?",
                        "permissions_json=?",
                        "updated_utc=?",
                    ]
                    params: list[Any] = [
                        str(role or "viewer"),
                        str(status or "active"),
                        str(email or ""),
                        int(bool(mfa_enabled)),
                        json.dumps(modules or [], separators=(",", ":")),
                        json.dumps(permissions or {}, separators=(",", ":"), sort_keys=True),
                        now,
                    ]
                    if password is not None and str(password) != "":
                        updates.append("password_hash=?")
                        params.append(self._hash_password(str(password)))
                    params.extend([tid, uname])
                    conn.execute(
                        f"UPDATE cp_users SET {', '.join(updates)} WHERE tenant_id=? AND username=?",
                        tuple(params),
                    )
                else:
                    conn.execute(
                        "INSERT INTO cp_users(tenant_id, username, password_hash, role, status, email, mfa_enabled, modules_json, permissions_json, created_utc, updated_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            tid,
                            uname,
                            self._hash_password(str(password or "ChangeMe123!")),
                            str(role or "viewer"),
                            str(status or "active"),
                            str(email or ""),
                            int(bool(mfa_enabled)),
                            json.dumps(modules or [], separators=(",", ":")),
                            json.dumps(permissions or {}, separators=(",", ":"), sort_keys=True),
                            now,
                            now,
                        ),
                    )
                conn.commit()
                out = conn.execute("SELECT * FROM cp_users WHERE tenant_id=? AND username=?", (tid, uname)).fetchone()
        return dict(out) if out else {}

    def list_users(self, *, tenant_id: str) -> list[dict[str, Any]]:
        tid = normalize_tenant_id(tenant_id)
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute("SELECT id, tenant_id, username, role, status, email, mfa_enabled, modules_json, permissions_json, created_utc, updated_utc, last_login_utc FROM cp_users WHERE tenant_id=? ORDER BY username", (tid,)).fetchall()
        output: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["modules"] = json.loads(d.get("modules_json") or "[]")
            d["permissions"] = json.loads(d.get("permissions_json") or "{}")
            d.pop("modules_json", None)
            d.pop("permissions_json", None)
            output.append(d)
        return output

    def delete_user(self, *, tenant_id: str, username: str) -> bool:
        tid = normalize_tenant_id(tenant_id)
        uname = str(username or "").strip()
        if not uname or uname == "admin":
            return False
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM cp_users WHERE tenant_id=? AND username=?", (tid, uname))
                conn.commit()
                return int(cur.rowcount or 0) > 0

    def authenticate_user(self, *, tenant_id: str, username: str, password: str) -> dict[str, Any] | None:
        tid = normalize_tenant_id(tenant_id)
        uname = str(username or "").strip()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM cp_users WHERE tenant_id=? AND username=?", (tid, uname)).fetchone()
                if not row:
                    return None
                r = dict(row)
                if str(r.get("status") or "active") != "active":
                    return None
                if not self._verify_password(str(password or ""), str(r.get("password_hash") or "")):
                    return None
                now = self._utc_now()
                conn.execute("UPDATE cp_users SET last_login_utc=?, updated_utc=? WHERE tenant_id=? AND username=?", (now, now, tid, uname))
                conn.commit()
        modules = json.loads(r.get("modules_json") or "[]") if isinstance(r.get("modules_json"), str) else []
        perms = json.loads(r.get("permissions_json") or "{}") if isinstance(r.get("permissions_json"), str) else {}
        return {
            "username": uname,
            "role": str(r.get("role") or "viewer"),
            "permissions": perms,
            "tenant_id": tid,
            "email": str(r.get("email") or ""),
            "mfa_enabled": bool(r.get("mfa_enabled") or 0),
            "modules": modules,
        }

    def issue_activation_code(self, *, tenant_id: str, customer_id: str = "", edge_name: str = "", ttl_minutes: int = 30, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        code = secrets.token_urlsafe(24)
        code_hash = self._sha256(code)
        now_dt = datetime.now(timezone.utc)
        exp = (now_dt + timedelta(minutes=max(5, int(ttl_minutes or 30)))).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        now = now_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        payload = json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO cp_edge_activation_codes(code_hash, tenant_id, customer_id, edge_name, expires_utc, used_utc, status, metadata_json, created_utc) VALUES(?,?,?,?,?,?,?,?,?)",
                    (code_hash, tid, str(customer_id or ""), str(edge_name or ""), exp, None, "issued", payload, now),
                )
                conn.commit()
        return {
            "tenant_id": tid,
            "customer_id": str(customer_id or ""),
            "edge_name": str(edge_name or ""),
            "activation_code": code,
            "expires_utc": exp,
        }

    def activate_edge_with_code(self, *, activation_code: str, edge_id: str, edge_name: str = "", site: str = "", area: str = "", equipment: str = "") -> dict[str, Any]:
        code_hash = self._sha256(str(activation_code or ""))
        now = self._utc_now()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM cp_edge_activation_codes WHERE code_hash=?", (code_hash,)).fetchone()
                if not row:
                    raise ValueError("activation_code_not_found")
                r = dict(row)
                if str(r.get("status") or "") != "issued":
                    raise ValueError("activation_code_not_issued")
                if str(r.get("expires_utc") or "") < now:
                    conn.execute("UPDATE cp_edge_activation_codes SET status='expired' WHERE code_hash=?", (code_hash,))
                    conn.commit()
                    raise ValueError("activation_code_expired")
                tid = normalize_tenant_id(str(r.get("tenant_id") or "default"))
                self.upsert_edge(
                    tenant_id=tid,
                    edge_id=str(edge_id or "").strip(),
                    edge_name=str(edge_name or r.get("edge_name") or edge_id),
                    customer_id=str(r.get("customer_id") or ""),
                    site=site,
                    area=area,
                    equipment=equipment,
                    status="active",
                    metadata={"activated_via": "code"},
                )
                conn.execute(
                    "UPDATE cp_edge_activation_codes SET status='used', used_utc=? WHERE code_hash=?",
                    (now, code_hash),
                )
                conn.commit()
                edge = conn.execute("SELECT * FROM cp_edges WHERE edge_id=?", (str(edge_id or ""),)).fetchone()
        return dict(edge) if edge else {}

    def issue_password_reset(self, *, tenant_id: str, username: str, ttl_minutes: int = 15) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        uname = str(username or "").strip()
        token = secrets.token_urlsafe(28)
        token_hash = self._sha256(token)
        now_dt = datetime.now(timezone.utc)
        exp = (now_dt + timedelta(minutes=max(5, int(ttl_minutes or 15)))).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        now = now_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO cp_password_reset_events(tenant_id, username, token_hash, expires_utc, used_utc, status, created_utc) VALUES(?,?,?,?,?,?,?)",
                    (tid, uname, token_hash, exp, None, "issued", now),
                )
                conn.commit()
        return {"tenant_id": tid, "username": uname, "reset_token": token, "expires_utc": exp}

    def reset_password_with_token(self, *, tenant_id: str, username: str, reset_token: str, new_password: str) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        uname = str(username or "").strip()
        token_hash = self._sha256(str(reset_token or ""))
        now = self._utc_now()
        with self._lock:
            with self._connect() as conn:
                ev = conn.execute(
                    "SELECT * FROM cp_password_reset_events WHERE tenant_id=? AND username=? AND token_hash=? ORDER BY id DESC LIMIT 1",
                    (tid, uname, token_hash),
                ).fetchone()
                if not ev:
                    raise ValueError("reset_token_not_found")
                evt = dict(ev)
                if str(evt.get("status") or "") != "issued":
                    raise ValueError("reset_token_not_active")
                if str(evt.get("expires_utc") or "") < now:
                    conn.execute("UPDATE cp_password_reset_events SET status='expired' WHERE id=?", (int(evt.get("id")),))
                    conn.commit()
                    raise ValueError("reset_token_expired")
                conn.execute(
                    "UPDATE cp_users SET password_hash=?, updated_utc=? WHERE tenant_id=? AND username=?",
                    (self._hash_password(str(new_password or "")), now, tid, uname),
                )
                conn.execute(
                    "UPDATE cp_password_reset_events SET status='used', used_utc=? WHERE id=?",
                    (now, int(evt.get("id"))),
                )
                conn.commit()
                row = conn.execute("SELECT id, tenant_id, username, role, status, email, mfa_enabled, modules_json, permissions_json, created_utc, updated_utc, last_login_utc FROM cp_users WHERE tenant_id=? AND username=?", (tid, uname)).fetchone()
        out = dict(row) if row else {}
        if out:
            out["modules"] = json.loads(out.get("modules_json") or "[]")
            out["permissions"] = json.loads(out.get("permissions_json") or "{}")
            out.pop("modules_json", None)
            out.pop("permissions_json", None)
        return out

    def tenant_summary(self, *, tenant_id: str) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        with self._lock:
            with self._connect() as conn:
                tenant = conn.execute("SELECT * FROM cp_tenants WHERE tenant_id=?", (tid,)).fetchone()
                customers = conn.execute("SELECT COUNT(*) FROM cp_customers WHERE tenant_id=?", (tid,)).fetchone()[0]
                edges = conn.execute("SELECT COUNT(*) FROM cp_edges WHERE tenant_id=?", (tid,)).fetchone()[0]
                active_edges = conn.execute("SELECT COUNT(*) FROM cp_edges WHERE tenant_id=? AND status='active'", (tid,)).fetchone()[0]
                users = conn.execute("SELECT COUNT(*) FROM cp_users WHERE tenant_id=?", (tid,)).fetchone()[0]
                licenses = conn.execute("SELECT COUNT(*) FROM cp_licenses WHERE tenant_id=?", (tid,)).fetchone()[0]
                recent_audit = conn.execute("SELECT ts_utc, actor_type, actor_id, action, outcome, correlation_id FROM cp_security_audit_log WHERE tenant_id=? ORDER BY id DESC LIMIT 20", (tid,)).fetchall()
        return {
            "tenant": dict(tenant) if tenant else None,
            "counts": {
                "customers": int(customers or 0),
                "edges": int(edges or 0),
                "active_edges": int(active_edges or 0),
                "users": int(users or 0),
                "licenses": int(licenses or 0),
            },
            "recent_audit": [dict(r) for r in recent_audit],
        }



