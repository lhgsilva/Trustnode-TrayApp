"""Customer DB mode + connectivity (operator 2026-06-17, M2).

Endpoints under /api/customer-db/* let the Settings UI:

  * GET  /status            — current mode + cached connection state.
  * POST /test-connection   — try a fresh connect with the operator's
                              proposed credentials. Does NOT persist.
  * POST /activate          — flip the mode to `customer_sql` once a
                              recent test has succeeded. Persists the
                              target in app_settings.
  * POST /deactivate        — revert to local SQLite. Configs + data
                              both DBs already carry stay in place; the
                              edge just stops promoting customer-sql as
                              the canonical store on next boot.

M2 stops at persistence + connectivity. M3 wires the actual schema
bootstrap; M4 starts pushing rows to the customer DB.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import customer_sql, sinks_sql
from app.state import app_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/customer-db", tags=["customer-db"])


# How long a successful test-connection result keeps Activate enabled.
ACTIVATION_GRACE_S = 60.0


class CustomerDbTarget(BaseModel):
    engine: str = Field(default="postgresql")
    host: str = ""
    port: int = 5432
    database: str = ""
    schema: str = "public"
    username: str = ""
    password: str = ""
    tls: bool = False
    # Optional friendly name so the operator can label the connection
    # in the UI (e.g. "Site DB"). Defaults to the database name.
    name: str = ""


class TestConnectionRequest(BaseModel):
    target: CustomerDbTarget


class ActivateRequest(BaseModel):
    target: CustomerDbTarget
    confirm_backup: bool = False


def _load_app_settings() -> Dict[str, Any]:
    try:
        bootstrap = app_store.get_bootstrap() or {}
    except Exception:
        bootstrap = {}
    settings = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
    return dict(settings)


def _save_app_settings(settings: Dict[str, Any]) -> None:
    app_store.upsert_domain("app_settings", settings, actor="customer_db_router")


def _redact(target: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the password before returning a target object to clients.

    The Settings UI never needs to read the password back — it only
    writes. Removing it from responses stops it appearing in browser
    devtools / network logs by accident.
    """
    out = {k: v for k, v in target.items() if k != "password"}
    out["password_set"] = bool(target.get("password"))
    return out


@router.get("/status")
def get_status() -> dict:
    settings = _load_app_settings()
    mode = str(settings.get("database_mode") or "local_sqlite").strip().lower()
    target = settings.get("customer_sql_target") if isinstance(settings.get("customer_sql_target"), dict) else {}
    last_test = settings.get("customer_sql_last_test") if isinstance(settings.get("customer_sql_last_test"), dict) else {}
    return {
        "ok": True,
        "mode": mode if mode in ("local_sqlite", "customer_sql") else "local_sqlite",
        "supported_engines": list(customer_sql.SUPPORTED_ENGINES),
        "target": _redact(target),
        "last_test": last_test,
    }


@router.post("/test-connection")
def post_test_connection(payload: TestConnectionRequest) -> dict:
    target = payload.target.model_dump()
    res = customer_sql.test_connection(target)
    # Persist the test result so the Activate route can confirm a
    # recent positive answer without storing the password in the
    # response. Only the (ok, latency, ts, error) shape lands here.
    settings = _load_app_settings()
    settings["customer_sql_last_test"] = {
        "ok": bool(res.get("ok")),
        "latency_ms": int(res.get("latency_ms") or 0),
        "error": str(res.get("error") or ""),
        "tested_utc": time.time(),
        "target_cache_key": customer_sql._cache_key(target),
    }
    _save_app_settings(settings)
    return {"ok": True, **res}


@router.post("/activate")
def post_activate(payload: ActivateRequest) -> dict:
    if not payload.confirm_backup:
        raise HTTPException(
            status_code=400,
            detail="Refusing to switch database mode without confirm_backup=true. "
                   "Take a backup of the current local SQLite first.",
        )
    target = payload.target.model_dump()
    # The recent test-connection must match the target we are about to
    # persist. This stops an operator from typing one set of creds in
    # the test box and saving a different set by accident.
    settings = _load_app_settings()
    last = settings.get("customer_sql_last_test") or {}
    if not last.get("ok"):
        raise HTTPException(
            status_code=400,
            detail="No successful test-connection in the last 60s. "
                   "Run /api/customer-db/test-connection first.",
        )
    cached_key = str(last.get("target_cache_key") or "")
    expected_key = customer_sql._cache_key(target)
    if cached_key != expected_key:
        raise HTTPException(
            status_code=400,
            detail="The test-connection target does not match the activation target. "
                   "Re-run /api/customer-db/test-connection with the same credentials.",
        )
    tested_utc = float(last.get("tested_utc") or 0)
    if tested_utc <= 0 or (time.time() - tested_utc) > ACTIVATION_GRACE_S:
        raise HTTPException(
            status_code=400,
            detail=f"Last test-connection is older than {int(ACTIVATION_GRACE_S)}s. "
                   "Run /api/customer-db/test-connection again.",
        )
    # M3: bootstrap the schema BEFORE persisting the mode flip so a
    # broken schema doesn't leave the operator stranded in a "we said
    # customer_sql but nothing works" state. Roll back the flip on
    # failure.
    engine, eng_err = customer_sql.get_engine(target)
    if engine is None:
        raise HTTPException(status_code=400, detail=f"engine not ready: {eng_err}")
    boot = sinks_sql.bootstrap_customer_db(
        engine,
        schema=str(target.get("schema") or "public"),
        note="activate via /api/customer-db/activate",
    )
    if not boot.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=f"schema bootstrap failed: {boot.get('error') or 'unknown'}",
        )

    settings["database_mode"] = "customer_sql"
    settings["customer_sql_target"] = target
    settings["customer_sql_schema_version"] = int(boot.get("version") or 0)
    settings["customer_sql_last_bootstrap_utc"] = time.time()
    _save_app_settings(settings)
    return {
        "ok": True,
        "mode": "customer_sql",
        "target": _redact(target),
        "schema": boot,
    }


@router.post("/deactivate")
def post_deactivate() -> dict:
    settings = _load_app_settings()
    settings["database_mode"] = "local_sqlite"
    _save_app_settings(settings)
    customer_sql.reset_engine()
    sinks_sql.force_rebootstrap()
    return {"ok": True, "mode": "local_sqlite"}


@router.post("/bootstrap")
def post_bootstrap() -> dict:
    """Force the schema bootstrap against the currently-active target.

    Useful after the customer DBA recreates the DB / drops the
    `trustnode` schema. Idempotent — re-running is a no-op when the
    schema is already up to date.
    """
    settings = _load_app_settings()
    target = settings.get("customer_sql_target") if isinstance(settings.get("customer_sql_target"), dict) else {}
    if not target:
        raise HTTPException(status_code=400, detail="no customer_sql target configured")
    engine, err = customer_sql.get_engine(target)
    if engine is None:
        raise HTTPException(status_code=400, detail=f"engine not ready: {err}")
    sinks_sql.force_rebootstrap()
    res = sinks_sql.bootstrap_customer_db(
        engine,
        schema=str(target.get("schema") or "public"),
        note="manual rebootstrap",
    )
    if res.get("ok"):
        settings["customer_sql_schema_version"] = int(res.get("version") or 0)
        settings["customer_sql_last_bootstrap_utc"] = time.time()
        _save_app_settings(settings)
    return {"ok": bool(res.get("ok")), "schema": res}
