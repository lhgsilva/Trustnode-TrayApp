"""Where the control-plane (portal) database lives, as a SETTING.

2026-10-13, owner's design: "the portal database can also be moved to a
different database where the portal website would be pointed out (to be
configured from the portal)".

Until now the answer was environment variables read once at import in
`state.py` and again in `ControlPlaneStoreCloud._get_engine()`:

    TRUSTNODE_CONTROL_PLANE_BACKEND = local | cloud
    TRUSTNODE_CLOUD_DB_HOST / _PORT / _NAME / _USER / _PASSWORD / _SSLMODE

So moving the portal database meant editing a systemd drop-in over SSH and
restarting - not something the portal could do, which is the whole point of
the requirement.

This module makes the target a persisted, portal-editable setting, with the
environment kept as the break-glass path.

Resolution order
----------------
1. ``TRUSTNODE_CONTROL_PLANE_FORCE_ENV`` truthy -> the environment, always.
   This is the lock-out escape hatch: a bad saved target cannot stop the
   portal from booting, because an operator with shell access can force the
   old behaviour without deleting anything.
2. the saved file, when present and complete.
3. the environment.

The saved file wins over the environment by default *on purpose*. Env-wins
would have meant the portal's own setting was silently ignored on exactly the
host that already has the env set - the VPS - which is where it matters.

Why a file and not a row
------------------------
The pointer to the control-plane database cannot live inside the
control-plane database. It is stored next to the other local state, in
TRUSTNODE_DATA_DIR, written 0600 because it carries a database password.

A change takes effect on the next restart. Modules do
``from app.state import control_plane_store``, binding the instance directly,
so swapping a module global would leave every existing importer on the old
store - a split brain far worse than asking for a restart. ``describe()``
reports ``restart_required`` so the portal can say so plainly.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any

#: Keys that together describe a Postgres control-plane target.
_DB_FIELDS = ("host", "port", "database", "username", "password", "sslmode")

#: The env vars this replaces, in the same order as _DB_FIELDS.
_ENV_NAMES = {
    "host": "TRUSTNODE_CLOUD_DB_HOST",
    "port": "TRUSTNODE_CLOUD_DB_PORT",
    "database": "TRUSTNODE_CLOUD_DB_NAME",
    "username": "TRUSTNODE_CLOUD_DB_USER",
    "password": "TRUSTNODE_CLOUD_DB_PASSWORD",
    "sslmode": "TRUSTNODE_CLOUD_DB_SSLMODE",
}

FILE_NAME = "control_plane_target.json"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def target_path() -> str:
    """Where the saved target lives. Follows TRUSTNODE_DATA_DIR like the
    rest of the local state, so a test workspace never reads the live one."""
    data_dir = str(os.environ.get("TRUSTNODE_DATA_DIR") or "").strip()
    if not data_dir:
        data_dir = os.path.join(os.path.expanduser("~"), ".trustnode_edge", "data")
    return os.path.join(data_dir, FILE_NAME)


def _from_env() -> dict[str, Any]:
    backend = str(os.environ.get("TRUSTNODE_CONTROL_PLANE_BACKEND", "") or "").strip().lower()
    out: dict[str, Any] = {"backend": "cloud" if backend == "cloud" else "local"}
    for field, env_name in _ENV_NAMES.items():
        out[field] = str(os.environ.get(env_name, "") or "").strip()
    if not out.get("port"):
        out["port"] = "5432"
    if not out.get("database"):
        out["database"] = "postgres"
    if not out.get("sslmode"):
        out["sslmode"] = "require"
    return out


def _read_file() -> dict[str, Any] | None:
    path = target_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return None
    except Exception:
        # A corrupt file must never stop the portal booting; fall through to
        # the environment instead.
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _complete(cfg: dict[str, Any]) -> bool:
    """A cloud target needs a host and a username to be usable at all; a
    local target needs nothing. An incomplete saved file is ignored rather
    than half-applied."""
    if str(cfg.get("backend") or "").strip().lower() != "cloud":
        return True
    return bool(str(cfg.get("host") or "").strip()
                and str(cfg.get("username") or "").strip())


def resolve() -> tuple[dict[str, Any], str]:
    """The effective target and where it came from.

    Returns (config, source) where source is one of
    "env_forced" | "file" | "env".
    """
    if _truthy(os.environ.get("TRUSTNODE_CONTROL_PLANE_FORCE_ENV")):
        return _from_env(), "env_forced"
    saved = _read_file()
    if isinstance(saved, dict) and _complete(saved):
        merged = _from_env()
        merged.update({k: v for k, v in saved.items() if k in ("backend",) + _DB_FIELDS})
        backend = str(merged.get("backend") or "").strip().lower()
        merged["backend"] = "cloud" if backend == "cloud" else "local"
        return merged, "file"
    return _from_env(), "env"


def backend() -> str:
    """"cloud" or "local" - what state.py builds."""
    cfg, _src = resolve()
    return str(cfg.get("backend") or "local")


def db_settings() -> dict[str, Any]:
    """The Postgres connection fields the cloud store needs."""
    cfg, _src = resolve()
    out = {f: cfg.get(f) for f in _DB_FIELDS}
    out["port"] = str(out.get("port") or "5432")
    out["database"] = str(out.get("database") or "postgres")
    out["sslmode"] = str(out.get("sslmode") or "require")
    return out


def _mask(value: Any) -> str:
    """A password, rendered so the portal can show that one is set without
    showing it. Never return the secret itself over the API."""
    raw = str(value or "")
    return ("set (%d chars)" % len(raw)) if raw else ""


def describe() -> dict[str, Any]:
    """Safe-to-display view of the current target, for the portal."""
    cfg, source = resolve()
    saved = _read_file()
    return {
        "backend": cfg.get("backend"),
        "host": cfg.get("host"),
        "port": cfg.get("port"),
        "database": cfg.get("database"),
        "username": cfg.get("username"),
        "password": _mask(cfg.get("password")),
        "sslmode": cfg.get("sslmode"),
        "source": source,
        "saved_file": target_path(),
        "has_saved_file": bool(saved),
        # A change only takes effect on the next start - see the module
        # docstring for why a hot swap would be worse than a restart.
        "restart_required": bool(saved) and source == "file",
        "env_override_active": source == "env_forced",
    }


def save(cfg: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
    """Persist a new target. Returns describe() for the saved state.

    Writes 0600 via a temp file + atomic replace, so a crash mid-write
    cannot leave a half-written pointer behind.
    """
    backend_val = str(cfg.get("backend") or "").strip().lower()
    out: dict[str, Any] = {"backend": "cloud" if backend_val == "cloud" else "local"}
    for field in _DB_FIELDS:
        if field in cfg:
            out[field] = str(cfg.get(field) or "").strip() if field != "password" \
                else str(cfg.get(field) or "")
    if out["backend"] == "cloud" and not _complete(out):
        raise ValueError("a cloud target needs at least host and username")
    out["updated_by"] = str(actor or "").strip()

    path = target_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cp-target-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
    return describe()


def clear() -> dict[str, Any]:
    """Forget the saved target and go back to the environment."""
    try:
        os.unlink(target_path())
    except FileNotFoundError:
        pass
    return describe()


def test_connection(cfg: dict[str, Any], *, timeout_s: int = 8) -> dict[str, Any]:
    """Can we actually reach this target, and does it look like a control
    plane? Never saves. This is what makes moving the portal database a
    safe operation rather than a leap.
    """
    backend_val = str(cfg.get("backend") or "").strip().lower()
    if backend_val != "cloud":
        return {"ok": True, "backend": "local",
                "detail": "Local SQLite control plane needs no connection test."}
    host = str(cfg.get("host") or "").strip()
    username = str(cfg.get("username") or "").strip()
    if not host or not username:
        return {"ok": False, "error": "host and username are required"}
    try:
        from sqlalchemy import create_engine, text  # type: ignore
        from urllib.parse import quote_plus as q
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": "sqlalchemy unavailable: %s" % exc}

    url = "postgresql+psycopg://%s:%s@%s:%s/%s" % (
        q(username), q(str(cfg.get("password") or "")), host,
        str(cfg.get("port") or "5432"), str(cfg.get("database") or "postgres"))
    engine = None
    try:
        engine = create_engine(
            url, pool_pre_ping=False, pool_size=1, max_overflow=0,
            connect_args={"sslmode": str(cfg.get("sslmode") or "require"),
                          "connect_timeout": int(timeout_s),
                          "prepare_threshold": None})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            # Report which cp_* tables are present so the operator can tell a
            # populated control plane from an empty database that merely
            # accepts connections.
            rows = conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() "
                "AND table_name LIKE 'cp\\_%' ORDER BY table_name"))
            tables = [r[0] for r in rows]
        return {"ok": True, "backend": "cloud", "cp_tables": tables,
                "detail": ("Connected. %d control-plane table(s) found."
                           % len(tables)) if tables else
                          "Connected, but no cp_* tables - this database has "
                          "not been provisioned as a control plane yet."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:400]}
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
