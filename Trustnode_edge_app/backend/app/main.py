import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Operator 2026-06-23: wire Python logging so the `trustnode.gateway`
# and `trustnode.watchdog` loggers reach stdout (which Electron pipes
# into backend.log). Without this, INFO records are silently dropped —
# only WARNING+ reaches stderr via Python's lastResort handler. The
# Invariant A audit-trail requires INFO transitions (start, stop,
# run-loop-exit, historian-buffer-drained) to land in the log too.
#
# CRITICAL: keep the ROOT level at WARNING. Setting root=INFO in build
# 5 turned on the `trustnode.mirror` logger which emits 2 lines every
# 5 s, plus uvicorn.access at ~10 lines/s. The Electron parent does a
# synchronous fs.appendFileSync per stdout line — at ~20 lines/s into
# a multi-MB log, the writer back-pressures uvicorn's stdout pipe,
# which back-pressures the worker, and cycles stall. We saw this live
# at 15:21-15:25 on 2026-06-23 (gateway "running" but no rows).
# Solution: route only OUR loggers to INFO; everything else stays at
# WARNING. Same audit trail, no spam.
if not logging.getLogger().handlers:
    _h = logging.StreamHandler(stream=sys.stdout)
    _h.setFormatter(logging.Formatter(
        "%(asctime)sZ %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logging.getLogger().addHandler(_h)
    logging.getLogger().setLevel(logging.WARNING)
# Promote only the trustnode operational loggers we explicitly added
# in this round of hardening. Anything else stays at WARNING.
for _name in ("trustnode.gateway", "trustnode.watchdog",
              "trustnode.intelligence.service", "trustnode.intelligence.ai"):
    try:
        logging.getLogger(_name).setLevel(logging.INFO)
    except Exception:
        pass
# Defensive: explicitly quiet known-chatty INFO loggers in case some
# other module elevates them later.
for _name in ("trustnode.mirror", "uvicorn.access"):
    try:
        logging.getLogger(_name).setLevel(logging.WARNING)
    except Exception:
        pass


def _bootstrap_env_from_dotenv() -> None:
    """Push every KEY=VALUE pair from .env into os.environ.

    Why this is necessary:
      - pydantic-settings only loads vars declared on its model, so the
        `Settings` class in config.py never populates the TRUSTNODE_*
        runtime vars used by lite_user_mirror, reports_cloud_uploader,
        etc.
      - The packaged Electron launcher inherits Windows env, but the
        operator typically never sets TRUSTNODE_* at the OS level — the
        .env file IS the configuration vehicle.
      - So we read .env explicitly at the very top of the backend boot
        and copy each key into os.environ. Vars already set in the real
        environment take precedence (we use setdefault).

    Search order: TRUSTNODE_ENV_FILE (explicit), then walk up from the
    binary location looking for `.env`, then from the source tree's
    backend directory. The first hit wins. Silent on missing file.
    """
    explicit = os.environ.get("TRUSTNODE_ENV_FILE", "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    # When frozen by PyInstaller the binary lives under
    # %TEMP%/<random>/resources/backend/trustnode-service.exe — walk up
    # to find the resources dir, then check ../../.env (next to the EXE
    # the operator launched).
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for up in [exe_dir, exe_dir.parent, exe_dir.parent.parent]:
            candidates.append(up / ".env")
    # Common dev tree positions (when running `uvicorn app.main:app` from
    # backend/). Resolve relative to this file's location.
    here = Path(__file__).resolve()
    for up in [here.parent, here.parent.parent, here.parent.parent.parent,
               here.parent.parent.parent.parent]:
        candidates.append(up / ".env")
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_file():
            continue
        try:
            for line in resolved.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except Exception:
            continue
        # Stop after the first successful parse so a deeper file doesn't
        # overwrite values from a closer one.
        break


_bootstrap_env_from_dotenv()


from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from app.auth import decode_access_token
from app.config import settings
from app.routers.auth import router as auth_router
from app.routers.database import router as database_router
from app.routers.app_store import router as app_store_router
from app.routers.health import router as health_router
from app.routers.plc import router as plc_router
from app.routers.ui_source import router as ui_source_router
from app.routers.notifications import router as notifications_router
from app.routers.telemetry_v1 import router as telemetry_v1_router
from app.routers.power import router as power_router
from app.routers.control_plane import router as control_plane_router, resolve_edge_view_link_public
from app.routers.customer_db import router as customer_db_router
from app.routers.lan_sharing import router as lan_sharing_router
from app.routers.lite_local import router as lite_local_router
from app.routers.connections import router as connections_router
from app.routers.directories import router as directories_router
from app.routers.reports import router as reports_router
from app.routers.cloud_live import router as cloud_live_router
from app.routers.historian_export import router as historian_export_router
from app.routers.workspace import router as workspace_router
from app.state import (
    plc_manager,
    app_store,
    telemetry_service,
    ingest_store,
    power_manager,
    reports_store,
    report_scheduler,
    lite_report_poller,
    control_plane_store,
)
from app.services.cp_users_puller import build_from_env as build_cp_users_puller
import app.state as _state
from app.tenant import resolve_request_tenant, resolve_websocket_tenant, set_current_tenant

# Boot perf (2026-06-18): pre-warm the asyncua (and amqtt) import in a
# background thread the moment this module is loaded by uvicorn.  By the
# time startup_event fires (after ASGI wiring, usually 300–800 ms later)
# asyncua is already in sys.modules so the lazy import inside opcua_server
# becomes a free dict-lookup instead of a 1–3 s parse-and-compile.
# Python's import lock serialises concurrent imports safely; this is a
# no-op if either library is absent (stripped build) or already imported.
def _prewarm_optional_imports() -> None:
    try:
        import asyncua  # noqa: F401
    except Exception:
        pass
    try:
        import amqtt  # noqa: F401
    except Exception:
        pass

import threading as _threading_prewarm
_threading_prewarm.Thread(
    target=_prewarm_optional_imports,
    name="tn-prewarm-opcua-mqtt",
    daemon=True,
).start()
del _threading_prewarm


# Phase 3d (operator 2026-06-18): initialize Sentry crash reporting BEFORE
# the FastAPI app so unhandled exceptions during route setup get captured.
# Opt-in via TRUSTNODE_SENTRY_DSN env var — no DSN means no telemetry.
# The free-tier Sentry plan (5K events/month) is plenty for plant edges.
def _init_sentry() -> None:
    import os as _os
    dsn = _os.environ.get("TRUSTNODE_SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.0,  # disable perf tracing — only crashes
            send_default_pii=False,  # never send usernames / IPs by default
            release=_os.environ.get("TRUSTNODE_RELEASE_TAG", "edge-dev"),
            environment=_os.environ.get("TRUSTNODE_SENTRY_ENV", "production"),
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                LoggingIntegration(level=None, event_level=None),  # logs only as breadcrumbs
            ],
            # Anything below ERROR isn't worth our 5K-event budget.
            before_send=lambda event, _hint: event if event.get("level") in (None, "error", "fatal") else None,
        )
    except Exception:
        # Never let telemetry block boot.
        pass


_init_sentry()

app = FastAPI(title="Trustnode Edge API", version="0.1.0")


# Operator 2026-06-30: TrustNode Intelligence module — bolt-on under
# <repo>/trustnode_intelligence/. LICENSE-gated: every route depends on
# require_intelligence_license(), which 404s when the customer's license
# doesn't list `trustnode_intelligence` (see trustnode_intelligence/backend/
# license.py). So the ROUTES are safe to always mount — the license is the
# real control.
#
# Operator 2026-07-08 (SINGLE-BUILD / LICENSE-DRIVEN): previously this only
# loaded when TRUSTNODE_INTELLIGENCE=on was set in the environment. That env
# var was set on dev machines but NOT in the shipped EXE, so the module code
# never loaded for customers, /api/intelligence/status 404'd, and the menu
# self-hid EVEN WHEN the license included the module. That created a
# dev-vs-production divergence. Now the module is loaded BY DEFAULT and only
# skipped if explicitly turned OFF (TRUSTNODE_INTELLIGENCE=off/0/false/no) —
# so ONE production build behaves identically for every customer/computer,
# and access is decided purely by the license gate inside the routes.
#
# If the import or include_router fails for ANY reason, the failure is logged
# and the rest of the app continues normally — Intelligence has zero hooks
# into PLC, historian, sync loops, or any other working flow.
if str(os.environ.get("TRUSTNODE_INTELLIGENCE", "on")).strip().lower() not in {"off", "0", "false", "no"}:
    try:
        import sys as _sys
        _tn_intel_root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "trustnode_intelligence",
        )
        # Make `trustnode_intelligence` importable both in dev (repo
        # layout) and packaged (PyInstaller _MEIPASS). Insert the parent
        # so `import trustnode_intelligence.backend.router` resolves.
        _tn_intel_repo_root = os.path.dirname(_tn_intel_root)
        if _tn_intel_repo_root not in _sys.path:
            _sys.path.insert(0, _tn_intel_repo_root)
        from trustnode_intelligence.backend.router import router as _intelligence_router
        app.include_router(_intelligence_router)
        print("[trustnode][boot] TrustNode Intelligence module loaded", flush=True)
    except Exception as _intel_exc:
        print(
            f"[trustnode][boot] Intelligence module not loaded "
            f"({type(_intel_exc).__name__}: {_intel_exc}) — continuing without it.",
            flush=True,
        )
else:
    print("[trustnode][boot] TrustNode Intelligence module explicitly DISABLED via TRUSTNODE_INTELLIGENCE=off "
          "(access is normally license-gated; set to on/unset to restore).", flush=True)


class CloudLiveAuthMiddleware:
    """Pure ASGI middleware that handles auth for the SSE stream.

    Starlette's `BaseHTTPMiddleware` (used by `@app.middleware("http")`)
    materializes the response before returning, which breaks SSE
    streams ("RuntimeError: No response returned."). This pure ASGI
    middleware checks auth for /api/cloud-live/ paths and forwards
    everything else to the next ASGI app — which is the FastAPI app
    *with* the regular BaseHTTPMiddleware still running for non-SSE
    routes.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        # Token can come from Authorization: Bearer OR ?token= query
        # (EventSource cannot set custom headers). The cloud-live router
        # then reads `get_current_tenant()` to scope rows.
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers") or []}
        token = ""
        authz = headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            token = authz.split(" ", 1)[1].strip()
        if not token:
            qs = scope.get("query_string") or b""
            try:
                from urllib.parse import parse_qs
                token = (parse_qs(qs.decode("latin1")).get("token") or [""])[0].strip()
            except Exception:
                token = ""
        if not token:
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"detail":"Authentication required"}'})
            return
        try:
            payload = decode_access_token(token)
        except Exception as exc:
            body = ('{"detail":"Invalid token: ' + str(exc).replace('"', "'") + '"}').encode("utf-8")
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        # Tenant resolution mirrors the http middleware below: host header
        # tenant must match the token tenant unless host resolves to default.
        try:
            host_tenant = resolve_request_tenant(_FakeRequest(scope))
        except Exception:
            host_tenant = "default"
        token_tenant = str(payload.get("tenant_id") or "").strip()
        if token_tenant:
            normalized = set_current_tenant(token_tenant)
            if host_tenant and host_tenant != "default" and normalized != host_tenant:
                await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"detail":"Token tenant mismatch"}'})
                return
        elif host_tenant:
            set_current_tenant(host_tenant)
        # Stash payload onto scope so the endpoint can read it if needed.
        scope.setdefault("state", {})["user_payload"] = payload
        return await self.app(scope, receive, send)


class _FakeRequest:
    """Minimal stand-in for `Request` that satisfies resolve_request_tenant."""

    def __init__(self, scope):
        self._scope = scope

    @property
    def headers(self):
        out = {}
        for k, v in self._scope.get("headers") or []:
            out[k.decode("latin1").lower()] = v.decode("latin1")
        return out

    @property
    def url(self):
        from urllib.parse import urlparse
        path = self._scope.get("path") or "/"
        return urlparse(path)


app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(plc_router)
app.include_router(database_router)
app.include_router(app_store_router)
app.include_router(ui_source_router)
app.include_router(notifications_router)
app.include_router(telemetry_v1_router)
app.include_router(power_router)
app.include_router(control_plane_router)
# Operator 2026-06-17 (M2): Customer DB mode + connectivity surface.
app.include_router(customer_db_router)
# Operator 2026-06-17 (M6): LAN sharing toggle + endpoint discovery.
app.include_router(lan_sharing_router)
# Operator 2026-06-17 (M7): public-token-gated Local Lite API.
app.include_router(lite_local_router)
# Operator 2026-06-17 (Phase 3): outbound connections (OPC UA / MQTT).
app.include_router(connections_router)
# Operator 2026-06-18: directories management (Settings → Directories).
app.include_router(directories_router)

# Operator 2026-06-18: THREE LAN-served UIs, distinct paths.
#
#   GET /trustnode/lite/               → landing (token entry) — SLIM
#   GET /trustnode/lite/app/           → SLIM Lite (forked cloud Lite, vanilla JS,
#                                        wired to /api/lite-local/*) — same design
#                                        as trustnode.lsapps.app/lite/
#   GET /trustnode/full/               → landing — FULL
#   GET /trustnode/full/app/           → FULL React app (frontend/dist) — admin
#   GET /trustnode/client/             → landing — CLIENT VIEW
#   GET /trustnode/client/app/         → React clientview (frontend/dist_client_view)
#   GET /api/lite-local/...            → token-validate + scoped data (shared)
try:
    import sys as _sys
    _landing_candidates = [
        Path(__file__).resolve().parent.parent / "static" / "lite",
    ]
    _login_candidates = [
        Path(__file__).resolve().parent.parent / "static" / "login",
    ]
    # SLIM Lite = fork of cloud Lite, wired to /api/lite-local/*
    _slim_lite_candidates = [
        Path(__file__).resolve().parent.parent / "static" / "lite_view",
    ]
    # CLIENT VIEW = Vite build:clientview output
    _clientview_candidates = [
        Path(__file__).resolve().parent.parent.parent / "frontend" / "dist_client_view",
        Path(__file__).resolve().parent.parent.parent.parent / "frontend" / "dist_client_view",
    ]
    # FULL = Vite default build
    _react_candidates = [
        Path(__file__).resolve().parent.parent.parent / "frontend" / "dist",
        Path(__file__).resolve().parent.parent.parent.parent / "frontend" / "dist",
    ]
    if hasattr(_sys, "_MEIPASS"):
        _meipass = Path(getattr(_sys, "_MEIPASS"))
        _landing_candidates.append(_meipass / "static" / "lite")
        _login_candidates.append(_meipass / "static" / "login")
        _slim_lite_candidates.append(_meipass / "static" / "lite_view")
        _clientview_candidates.append(_meipass / "frontend" / "dist_client_view")
        _react_candidates.append(_meipass / "frontend" / "dist")

    # Mount specific /app/ paths FIRST (more specific wins on route lookup).
    for _slim_dir in _slim_lite_candidates:
        if _slim_dir.exists() and (_slim_dir / "index.html").exists():
            app.mount("/trustnode/lite/app", StaticFiles(directory=str(_slim_dir), html=True), name="trustnode_lite_app")
            break
    for _cv_dir in _clientview_candidates:
        if _cv_dir.exists() and (_cv_dir / "index.html").exists():
            app.mount("/trustnode/client/app", StaticFiles(directory=str(_cv_dir), html=True), name="trustnode_client_app")
            break
    for _react_dir in _react_candidates:
        if _react_dir.exists() and (_react_dir / "index.html").exists():
            app.mount("/trustnode/full/app", StaticFiles(directory=str(_react_dir), html=True), name="trustnode_full_app")
            break
    # Then the three landings at the parents. Landing reads
    # window.location.pathname to decide which /app/ to redirect to.
    for _landing_dir in _landing_candidates:
        if _landing_dir.exists():
            app.mount("/trustnode/lite", StaticFiles(directory=str(_landing_dir), html=True), name="trustnode_lite_landing")
            app.mount("/trustnode/full", StaticFiles(directory=str(_landing_dir), html=True), name="trustnode_full_landing")
            app.mount("/trustnode/client", StaticFiles(directory=str(_landing_dir), html=True), name="trustnode_client_landing")
            break
    # Shared LAN login page (operator 2026-06-18). Lives at
    # /trustnode/login/. Pages under /trustnode/{full|lite|client}/app/
    # check the JWT client-side; this page issues it via /api/auth/login
    # and then verifies access via /api/lite-local/check-access.
    for _login_dir in _login_candidates:
        if _login_dir.exists():
            app.mount("/trustnode/login", StaticFiles(directory=str(_login_dir), html=True), name="trustnode_login")
            break
except Exception:
    pass


# Public resolver for read-only Lite share links. Mounted directly on the
# app (not inside a prefixed router) so it lives at /api/lite-view/resolve/
# which the auth middleware allow-lists by prefix. Returns the tenant +
# edge scope a view-link viewer is allowed to see.
@app.get("/api/lite-view/resolve/{token}")
def _lite_view_resolve(token: str):
    return resolve_edge_view_link_public(token)
app.include_router(reports_router)
app.include_router(historian_export_router)
app.include_router(workspace_router)

# Operator 2026-06-23: Batch Management & Traceability module. Always
# mounted — every endpoint inside the router is individually gated by
# require_batch_management_license(), so the module is invisible (404)
# to customers without the license. Code lives at
# app/modules/batch_management/* so it is easy to find and to detach.
from app.modules.batch_management import batch_router  # noqa: E402
app.include_router(batch_router)
# 2026-07-14 CLEAN REBUILD: mount the v2 router (spec-named endpoints) alongside
# the legacy one. Both share the license gate; the new UI targets v2. Seeding the
# 4 batch report templates is idempotent + best-effort (reuses the Report module).
try:
    from app.modules.batch_management import batch_router_v2  # noqa: E402
    if batch_router_v2 is not None:
        app.include_router(batch_router_v2)
    from app.modules.batch_management.reports_v2 import seed_report_templates as _seed_batch_tpls
    try:
        _n = _seed_batch_tpls()
        if _n:
            print(f"[trustnode][boot] batch v2: seeded {_n} report template(s)", flush=True)
    except Exception:
        pass
except Exception as _e:  # pragma: no cover - keep boot resilient
    print(f"[trustnode][boot] batch v2 router mount skipped: {_e}", flush=True)

# The cloud-live SSE endpoint lives in a SEPARATE FastAPI app so it does
# NOT inherit the main app's BaseHTTPMiddleware (which buffers responses
# and breaks `StreamingResponse` with "No response returned"). A top-level
# ASGI dispatcher (see bottom of file) routes /api/cloud-live/* directly
# into this sub-app, bypassing the main app's middleware chain entirely.
from fastapi import APIRouter as _APIRouter
from app.routers.cloud_live import cloud_live_stream as _cloud_live_stream_handler
_cloud_live_app = FastAPI(title="TrustNode Cloud Live", openapi_url=None, docs_url=None, redoc_url=None)
_cloud_live_inner_router = _APIRouter()
_cloud_live_inner_router.add_api_route("/stream", _cloud_live_stream_handler, methods=["GET"])
_cloud_live_app.include_router(_cloud_live_inner_router)
_cloud_live_app.add_middleware(CloudLiveAuthMiddleware)


async def _deferred_startup() -> None:
    """Phase 4a-2 (operator 2026-06-18): heavy boot work moved off the
    startup_event handler so uvicorn can answer /api/health the moment
    it binds. None of these steps are required before the first request
    arrives — they re-apply persistent state (LAN sharing, OPC UA, MQTT)
    and start background pollers. Errors here only delay full feature
    availability; they never block boot.
    """
    # Operator 2026-06-18: activation registry restore + mirror. Both
    # take the SQLite lock and hit netsh on Windows — synchronous
    # operations that, if run inline, BLOCK the asyncio event loop and
    # prevent uvicorn from binding. Symptom: customer's backend started,
    # printed [boot] markers, but /api/health never responded. Move to a
    # worker thread via asyncio.to_thread so uvicorn keeps its loop free.
    try:
        result = await asyncio.to_thread(control_plane_store.restore_activation_from_registry_if_empty)
        if result.get("restored"):
            print(f"[trustnode][boot] activation restored from registry: {result.get('applied')}", flush=True)
    except Exception as exc:
        print(f"[trustnode][boot] activation registry restore failed: {exc!r}", flush=True)
    try:
        mirror = await asyncio.to_thread(control_plane_store.mirror_activation_to_registry)
        if mirror.get("ok"):
            print(f"[trustnode][boot] activation mirrored to registry: hive={mirror.get('hive')}", flush=True)
    except Exception:
        pass
    try:
        await plc_manager.stop_all_gateways()
    except Exception:
        pass

    # Operator 2026-06-19 (L5): SQLite integrity check on boot. Quick
    # PRAGMA quick_check is O(N) over pages but bounded — typically a
    # few hundred ms on a healthy 500MB DB. We log the result and DO
    # NOT block boot on failure, but we surface the result in the
    # health endpoint so support can spot a corrupted DB before the
    # operator does. A failed integrity check usually points at a
    # disk-full / power-loss event that left the WAL inconsistent;
    # the operator should restore from .preVACUUM_* / .bak_* backup.
    try:
        import sqlite3 as _sqlite_check
        integrity_results = {}
        for label, path_attr in (("app_store", "_db_path"), ("telemetry", "_db_path")):
            try:
                target = app_store if label == "app_store" else telemetry_service
                db_path = getattr(target, path_attr, None)
                if not db_path:
                    continue

                def _check(p):
                    con = _sqlite_check.connect(f"file:{p}?mode=ro", uri=True, timeout=10.0)
                    try:
                        return str((con.execute("PRAGMA quick_check").fetchone() or ["unknown"])[0])
                    finally:
                        con.close()
                result = await asyncio.to_thread(_check, db_path)
                integrity_results[label] = result
                if result.lower() != "ok":
                    print(f"[trustnode][boot][integrity] {label} quick_check returned {result!r} — investigate before extended use", flush=True)
            except Exception as exc:
                integrity_results[label] = f"error: {exc!r}"
                print(f"[trustnode][boot][integrity] {label} check raised: {exc!r}", flush=True)
        # Stash the result on app_store so /api/health can surface it.
        try:
            setattr(app_store, "_last_integrity_results", integrity_results)
        except Exception:
            pass
    except Exception as exc:
        print(f"[trustnode][boot][integrity] check skipped: {exc!r}", flush=True)

    bootstrap = None
    try:
        # Same rationale: get_bootstrap acquires app_store._lock; if a
        # cloud-sync thread holds it, this blocks the event loop and
        # uvicorn can't serve. Run on a worker thread.
        bootstrap = await asyncio.to_thread(app_store.get_bootstrap, False)
        telemetry_service.configure_from_bootstrap({"data": bootstrap})
    except Exception:
        bootstrap = None

    # Operator 2026-06-19: auto-resume gateways that were running before
    # this backend process restarted. The customer hit "I started the
    # gateway after re-activation but historian shows only 1 cycle" —
    # exactly the symptom of a backend restart wiping in-memory worker
    # state. We persist `last_running` in telemetry_service on every
    # /gateways/start and /stop call; here we walk that list and
    # re-issue the start for each still-configured gateway.
    try:
        running_ids = telemetry_service.list_running_gateways()
        scoped_bootstrap = bootstrap
        if running_ids and isinstance(bootstrap, dict):
            # The customer's gateway lives in a SCOPED config doc
            # (tenant|customer|edge), not the unscoped one. Walk the
            # scope-key candidates that match this install's app_settings
            # so we can find the same gateway_configurations array the
            # UI's bootstrap returns to the operator.
            try:
                app_settings = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
                tenant_seg = str(app_settings.get("tenant_id") or "").strip().lower() or "default"
                customer_seg = str(app_settings.get("customer_id") or "").strip().lower() or "-"
                edge_seg = str(app_settings.get("edge_id") or "").strip().lower()
                if not edge_seg:
                    try:
                        edge_seg = str(getattr(app_store, "_local_edge_id", "") or "").strip().lower()
                    except Exception:
                        edge_seg = ""
                if edge_seg:
                    scope_key = f"{tenant_seg}|{customer_seg}|{edge_seg}"
                    scoped_bootstrap = await asyncio.to_thread(
                        app_store.get_bootstrap_scoped, scope_key, False
                    )
            except Exception:
                scoped_bootstrap = bootstrap

        if running_ids and isinstance(scoped_bootstrap, dict):
            from app.models import GatewayConfig as _GwCfg
            # Set the tenant ContextVar so the auto-resumed worker
            # writes historian rows under the same tenant the operator
            # would have on a normal /gateways/start call. asyncio.Task
            # copies the parent context at creation time, so this needs
            # to be set BEFORE plc_manager.start_gateway spawns the loop.
            try:
                app_settings = (scoped_bootstrap.get("app_settings") if isinstance(scoped_bootstrap.get("app_settings"), dict) else {}) or {}
                effective_tenant = str(app_settings.get("tenant_id") or "").strip() or "default"
                set_current_tenant(effective_tenant)
            except Exception:
                pass
            gw_rows = scoped_bootstrap.get("gateway_configurations") or []
            db_rows = scoped_bootstrap.get("database_configurations") or []
            # Index gateways and databases for O(1) lookup.
            by_id = {str(g.get("id") or ""): g for g in gw_rows if isinstance(g, dict)}
            db_by_id = {str(d.get("id") or ""): d for d in db_rows if isinstance(d, dict)}

            def _to_sink(c: dict) -> dict:
                return {
                    "id": str(c.get("id") or ""),
                    "name": str(c.get("name") or ""),
                    "engine": c.get("engine"),
                    "host": str(c.get("host") or ""),
                    "port": int(c.get("port") or 0),
                    "database": str(c.get("database") or ""),
                    "username": str(c.get("username") or ""),
                    "password": c.get("password") or "",
                    "sqlite_path": str(c.get("sqlite_path") or ""),
                    "file_path": str(c.get("file_path") or ""),
                    "legacy_url": str(c.get("legacy_url") or ""),
                    "legacy_api_token": str(c.get("legacy_api_token") or ""),
                    "source": str(c.get("source") or ""),
                    "site": str(c.get("site") or ""),
                    "area": str(c.get("area") or ""),
                    "equipment": str(c.get("equipment") or ""),
                    "schema": str(c.get("schema") or "public"),
                    "table": str(c.get("table") or "plc_readings"),
                    "tls": bool(c.get("tls")),
                    "tag_filters": [],
                    "gateway_filters": [],
                    "csv_format": "",
                    "csv_header": "",
                }

            resumed = 0
            skipped_opt_out = 0
            for gid in running_ids:
                gw = by_id.get(gid)
                if not gw:
                    # Stale flag — gateway was deleted while stopped. Clear it
                    # so we don't keep retrying on every boot.
                    try:
                        telemetry_service.mark_gateway_running(gid, False)
                    except Exception:
                        pass
                    continue
                # Operator 2026-06-25 (final-fix): auto-resume is now
                # the DEFAULT. The watchdog-restores-running-gateways
                # contract requires any gateway whose last_running=1
                # to come back after a backend restart, unless the
                # operator EXPLICITLY suppressed it via
                # auto_recover_enabled=False on this gateway. The old
                # auto_resume opt-in was an unsafe footgun: a
                # production gateway whose backend crashed at 3am
                # would silently stay down until someone clicked
                # Start in the morning. The same explicit-Stop set
                # used by the supervisor is honored here too — if the
                # operator stopped it before the crash, it stays
                # stopped (last_running would be 0 in that case).
                disable_recover = (gw.get("auto_recover_enabled") is False)
                if disable_recover:
                    try:
                        telemetry_service.mark_gateway_running(gid, False)
                    except Exception:
                        pass
                    skipped_opt_out += 1
                    continue
                db_id = str(gw.get("database_id") or "")
                db_cfg = db_by_id.get(db_id) or {}
                primary = _to_sink(db_cfg) if db_cfg else None
                config = _GwCfg(
                    gateway_type=str(gw.get("gateway_type") or ""),
                    plc_ip=str(gw.get("plc_ip") or ""),
                    opc_url=str(gw.get("opc_url") or ""),
                    tags=list(gw.get("tags") or []),
                    interval_ms=int(gw.get("interval_ms") or 1000),
                    site=str(gw.get("site") or ""),
                    area=str(gw.get("area") or ""),
                    equipment=str(gw.get("equipment") or ""),
                )
                try:
                    await plc_manager.start_gateway(
                        gateway_id=gid,
                        config=config,
                        db_sink=primary,
                        db_sinks=[primary] if primary else [],
                    )
                    resumed += 1
                except Exception as exc:
                    print(f"[trustnode][boot] auto-resume failed for {gid}: {exc!r}", flush=True)
            if resumed:
                print(f"[trustnode][boot] auto-resumed {resumed} gateway(s)", flush=True)
            if skipped_opt_out:
                print(f"[trustnode][boot] skipped {skipped_opt_out} gateway(s) — auto_resume=false (opt-in)", flush=True)
    except Exception as exc:
        print(f"[trustnode][boot] gateway auto-resume scan failed: {exc!r}", flush=True)
    # Operator 2026-06-25: kick the watchdog/supervisor task so the
    # schedule + auto-recover policies can run even when no gateway
    # is currently started in-memory. The task is idempotent — if a
    # start_gateway call already spawned it, this is a no-op.
    try:
        plc_manager._ensure_watchdog_running()
        print("[trustnode][boot] watchdog/supervisor task ensured", flush=True)
    except Exception as exc:
        print(f"[trustnode][boot] watchdog ensure failed: {exc!r}", flush=True)
    # Operator 2026-06-18: one-shot migration of existing customer users
    # from app_store.users_access (unscoped + scoped) into the dedicated
    # AuthStore. Idempotent: only runs when AuthStore is empty. Customer's
    # users + passwords + roles + permissions + modules are preserved.
    # Wrapped in to_thread so a hot bootstrap dict doesn't ever block the
    # loop. Errors here only mean the migration is retried next boot —
    # never a crash.
    try:
        from app.state import auth_store as _auth_store
        if _auth_store.is_empty() and isinstance(bootstrap, dict):
            def _migrate():
                unscoped = bootstrap.get("users_access") if isinstance(bootstrap.get("users_access"), dict) else None
                # Pull every scoped users_access doc directly — lock-free.
                scoped_payloads = []
                try:
                    import sqlite3, json as _json
                    db_path = getattr(app_store, "_db_path", None)
                    if db_path:
                        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
                        try:
                            for row in con.execute(
                                "SELECT payload_json FROM config_documents_scoped WHERE domain='users_access'"
                            ).fetchall():
                                try:
                                    scoped_payloads.append(_json.loads(row[0]) if isinstance(row[0], str) else row[0])
                                except Exception:
                                    pass
                        finally:
                            con.close()
                except Exception:
                    pass
                return _auth_store.migrate_from_app_store_payload(unscoped, scoped_payloads)
            counts = await asyncio.to_thread(_migrate)
            print(f"[trustnode][boot] AuthStore migration: {counts}", flush=True)
    except Exception as exc:
        print(f"[trustnode][boot] AuthStore migration skipped: {exc!r}", flush=True)
    try:
        if isinstance(bootstrap, dict):
            reports_store.migrate_from_bootstrap(bootstrap, tenant_id="default")
    except Exception:
        pass
    try:
        report_scheduler.start()
    except Exception:
        pass
    try:
        from app.services import lan_socket as _lan_socket
        s = bootstrap.get("app_settings") if isinstance(bootstrap, dict) and isinstance(bootstrap.get("app_settings"), dict) else {}
        if bool(s.get("lan_sharing_enabled")):
            _lan_socket.sync_with_settings(True, int(settings.trustnode_port))
    except Exception:
        pass
    try:
        s = bootstrap.get("app_settings") if isinstance(bootstrap, dict) and isinstance(bootstrap.get("app_settings"), dict) else {}
        conn = s.get("connections") if isinstance(s, dict) and isinstance(s.get("connections"), dict) else {}
        if isinstance(conn.get("opcua"), dict) and bool(conn["opcua"].get("enabled")):
            cfg = conn["opcua"]
            rt = str(cfg.get("runtime") or "python").lower()
            if rt == "native":
                from app.services import opcua_server_dotnet as _opcua
            else:
                from app.services import opcua_server as _opcua
            _opcua.start(
                port=int(cfg.get("port") or 4840),
                server_name=str(cfg.get("server_name") or "TrustNode Edge OPC UA"),
                anonymous=bool(cfg.get("anonymous", True)),
                username=str(cfg.get("username") or ""),
                password=str(cfg.get("password") or ""),
            )
        if isinstance(conn.get("mqtt"), dict) and bool(conn["mqtt"].get("enabled")):
            cfg = conn["mqtt"]
            rt = str(cfg.get("runtime") or "python").lower()
            if rt == "native":
                from app.services import mqtt_broker_mosquitto as _mqtt
            else:
                from app.services import mqtt_broker as _mqtt
            tenant = str(s.get("tenant_id") or "default")
            edge = str(s.get("edge_id") or "edge-01")
            _mqtt.start(
                port=int(cfg.get("port") or 1883),
                anonymous=bool(cfg.get("anonymous", True)),
                username=str(cfg.get("username") or ""),
                password=str(cfg.get("password") or ""),
                tenant_id=tenant, edge_id=edge,
            )
    except Exception:
        pass
    # Drains the Supabase `lite_report_requests` queue (Lite "Generate" button).
    # Idle and silent when no cloud DB target is configured.
    try:
        lite_report_poller.start()
    except Exception:
        pass
    # Pull portal-side cp_users changes into the local edge SQLite so an
    # operator deactivated via the portal can't keep logging into the
    # local edge. No-ops on the portal/VPS itself (it loops back to its
    # own /api/cp/users — harmless but wasteful), so we skip when the
    # cloud URL matches our own host. Skips entirely when cloud sync is
    # disabled or no cloud URL is configured.
    try:
        app_settings = {}
        if isinstance(bootstrap, dict):
            data = bootstrap.get("data") if isinstance(bootstrap.get("data"), dict) else bootstrap
            cand = data.get("app_settings") if isinstance(data, dict) else None
            if isinstance(cand, dict): app_settings = cand
        puller = build_cp_users_puller(control_plane_store, app_settings)
        if puller is not None:
            # Skip on the VPS itself — pointless self-loop.
            self_loop = False
            try:
                cu = (app_settings.get("cloud_url") or app_settings.get("cloud_api_url") or "")
                if "trustnode.lsapps.app" in cu and not app_settings.get("endpoint_mode", "").lower() == "local":
                    # heuristic: if our /api/health is what cloud_url points at
                    # (i.e. we're the VPS itself), don't bother. This is best-effort.
                    import socket
                    self_loop = socket.gethostname().startswith("localhost")
            except Exception:
                self_loop = False
            if not self_loop:
                puller.start()
                _state.cp_users_puller = puller
    except Exception:
        pass


@app.on_event("startup")
async def startup_event() -> None:
    # Operator 2026-07-03 (COLLECTION-STARVATION FIX): raise the anyio
    # default thread limiter from 40 to 200. FastAPI runs every sync route
    # AND every `asyncio.to_thread` on this ONE shared pool. The PLC worker
    # does read + persist via to_thread each cycle; with 2 gateways at ~1Hz
    # plus cloud-sync + UI polling, 40 tokens is too tight — collection
    # to_thread calls QUEUED behind request handling, producing the historian
    # gaps the operator reported. Raising the ceiling is purely additive
    # (more concurrency, never less) and lets collection + the UI coexist.
    try:
        import anyio
        limiter = anyio.to_thread.current_default_thread_limiter()
        if limiter.total_tokens < 200:
            limiter.total_tokens = 200
            print(f"[trustnode][boot] anyio thread limiter raised to {limiter.total_tokens}", flush=True)
    except Exception as _exc:
        print(f"[trustnode][boot] could not raise thread limiter: {_exc!r}", flush=True)

    # Phase 4a-2: kick the deferred boot work onto the event loop so the
    # startup handler returns immediately and uvicorn starts answering
    # /api/health. The 2026-06-17 in-process LAN socket + OPC UA/MQTT
    # re-apply moved into _deferred_startup() above.
    print("[trustnode][boot] startup_event fired; scheduling deferred init", flush=True)
    import asyncio as _asyncio
    try:
        _asyncio.get_event_loop().create_task(_deferred_startup())
        print("[trustnode][boot] deferred init scheduled — uvicorn should now serve /api/health", flush=True)
    except Exception as exc:
        # If task creation fails, run inline as a safety net so functional
        # parity with the pre-Phase-4a code is preserved.
        print(f"[trustnode][boot] WARN: create_task failed ({exc!r}); running deferred init inline", flush=True)
        try:
            await _deferred_startup()
        except Exception:
            pass

    # Operator 2026-06-25 (proven in 4h soak): if the asyncio event loop
    # wedges (stdio backpressure, blocking syscall in a coroutine, etc.)
    # nothing INSIDE the loop can detect or recover from it. The
    # in-process worker watchdog runs in the SAME loop and is just as
    # dead. We need recovery that LIVES OUTSIDE the loop.
    #
    # This thread does ONE thing: a coroutine on the event loop bumps a
    # monotonic timestamp every 5 s; the thread checks the timestamp
    # every 10 s; if it hasn't moved in 60 s the loop is dead and we
    # os._exit(1) so the Electron supervisor respawns the backend in
    # a fresh process. The whole gateway comes back within ~15 s of any
    # wedge — the same recovery target users had configured per-gateway,
    # now enforced at the process level.
    try:
        import threading as _threading
        import time as _time
        from app.state import plc_manager as _pm  # type: ignore
        _loop_heartbeat = [_time.monotonic()]  # mutable container

        async def _loop_heartbeat_bump() -> None:
            # Operator 2026-06-26: bump every 2 s (was 5 s) so the
            # watchdog's freshness check has finer resolution. A
            # healthy loop ticks thousands of times per second so 2 s
            # is still negligible overhead.
            while True:
                try:
                    _loop_heartbeat[0] = _time.monotonic()
                    await asyncio.sleep(2.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(2.0)

        def _wedge_watchdog_thread() -> None:
            # Operator 2026-06-26: aggressive recovery target (~25 s
            # end-to-end). Loop must heartbeat every 2 s; if silent
            # for 15 s the loop is wedged. Check every 5 s so we
            # detect within at most STALE_S + CHECK_S = 20 s. Then
            # os._exit + Electron respawn + auto-resume = ~25 s total.
            # Daemon thread — never blocks process exit on its own.
            STALE_S = float(os.environ.get("TRUSTNODE_WEDGE_TIMEOUT_S", "15") or "15")
            CHECK_S = 5.0
            # Grace period after process start so a slow boot doesn't
            # trip — kept at 60 s because PyInstaller cold-start +
            # AppStore init can take that long on a fresh boot.
            BOOT_GRACE_S = 60.0
            boot_mono = _time.monotonic()
            while True:
                _time.sleep(CHECK_S)
                age = _time.monotonic() - (_loop_heartbeat[0] or 0)
                if (_time.monotonic() - boot_mono) < BOOT_GRACE_S:
                    continue
                if age > STALE_S:
                    print(
                        f"[trustnode][wedge-watchdog] event loop stale for {age:.0f}s "
                        f"(threshold {STALE_S:.0f}s) — killing process so supervisor "
                        f"can respawn a fresh backend.",
                        flush=True,
                    )
                    # Hard exit so Electron sees the process drop and
                    # respawns. os._exit skips atexit and finalizers —
                    # exactly what we want from a wedged loop where
                    # nothing finalizes cleanly anyway.
                    try:
                        os._exit(2)
                    except Exception:
                        pass
                    return

        _asyncio.get_event_loop().create_task(_loop_heartbeat_bump())
        _t = _threading.Thread(target=_wedge_watchdog_thread, name="trustnode-wedge-watchdog", daemon=True)
        _t.start()
        print("[trustnode][boot] wedge watchdog armed (60s loop-stale threshold)", flush=True)
    except Exception as exc:
        print(f"[trustnode][boot] WARN: wedge watchdog setup failed: {exc!r}", flush=True)


PUBLIC_PATHS = {
    "/api/health",
    "/api/boot-probe",
    # Intelligence status probe — must be public so the sidebar menu
    # component can decide whether to render. Endpoint still returns
    # 404 when the license doesn't list the module (license_inspect
    # gating in trustnode_intelligence/backend/license.py).
    "/api/intelligence/status",
    "/api/auth/login",
    "/api/auth/me",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/control-plane/portal-context",
    "/api/control-plane/activation-code/apply",
    "/api/control-plane/edge-link/bootstrap",
    "/api/control-plane/edge-link/register",
    "/api/control-plane/edge-link/local-finalize",
    # Operator 2026-07-01: public so the Edge process can pull the
    # portal-admin's AI Endpoint config without holding a Supabase JWT.
    # Gated inside the handler by the edge_id's active license.
    "/api/control-plane/edge-link/ai-endpoint",
    # Operator 2026-07-05: public for the SAME reason as ai-endpoint — the
    # Edge process authenticates users against its LOCAL AuthStore (tokens
    # signed with a different key than the cloud), so it cannot present a
    # valid cloud JWT. When a license is renewed in the portal, "Re-check
    # now" calls the cloud's license-check to pull the new end_utc; if that
    # route requires cloud auth the forwarded local token gets 401, the
    # hydrate fails, and the renewed license never reaches the edge (the
    # trial banner never clears). Exposes only license status/modules/expiry
    # for an edge_id that has an active license — strictly less sensitive
    # than ai-endpoint (which already returns the customer's API key).
    "/api/control-plane/edge-link/license-check",
    "/api/control-plane/password-reset/public/issue",
    "/api/control-plane/password-reset/public/apply",
    "/api/v1/healthz",
    "/api/v1/readyz",
}


def _allows_query_token(path: str) -> bool:
    """Routes that may take the auth token as a ?access_token= query param
    because they're loaded by browser-native GETs (iframe/download) that can't
    set an Authorization header. Kept to file-serving report routes only — the
    token is still a valid JWT, just carried differently for these GETs."""
    p = path or ""
    return (
        p.startswith("/api/reports/generated/") and p.endswith("/file")
    ) or (
        p.startswith("/api/reports/templates/") and p.endswith("/preview-data")
    )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    def _apply_no_cache_headers(response):
        try:
            path = request.url.path or ""
            if path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
        except Exception:
            pass
        return response

    tenant_id = resolve_request_tenant(request)
    set_current_tenant(tenant_id)
    path = request.url.path or ""
    method = (request.method or "GET").upper()
    if method == "OPTIONS":
        return _apply_no_cache_headers(await call_next(request))
    if not path.startswith("/api/"):
        return await call_next(request)
    # /api/cloud-live/* is handled by the pure-ASGI CloudLiveAuthMiddleware
    # above (BaseHTTPMiddleware can't stream SSE).
    if path.startswith("/api/cloud-live/"):
        return await call_next(request)
    # v1 telemetry endpoints use explicit auth inside router handlers
    # (device tokens for ingest, user tokens for query/admin).
    if path.startswith("/api/v1/"):
        return _apply_no_cache_headers(await call_next(request))
    if request.url.path in PUBLIC_PATHS:
        return _apply_no_cache_headers(await call_next(request))
    # Read-only Lite share-link resolver. Anyone with the URL token can
    # convert it to {tenant_id, customer_id, edge_id} so the no-login Lite
    # view can scope its queries. No JWT/auth required by design.
    if request.url.path.startswith("/api/lite-view/resolve/"):
        return _apply_no_cache_headers(await call_next(request))
    # Operator 2026-06-17 (M7): the Local Lite API does its own token
    # check inside each handler — let traffic through here.
    if request.url.path.startswith("/api/lite-local/"):
        return _apply_no_cache_headers(await call_next(request))
    # Operator 2026-06-17: LAN sharing toggle is operator-local — the
    # tray process calls it from 127.0.0.1 without a JWT. Allow only
    # loopback callers through; any LAN/remote caller still needs a
    # Bearer token, so a curious user on the LAN cannot flip the
    # toggle without being logged in. The LAN socket itself only
    # binds 0.0.0.0 AFTER an authenticated/loopback enable, so this
    # cannot be used to bootstrap from nothing.
    if request.url.path.startswith("/api/lan-sharing/") or request.url.path.startswith("/api/connections/"):
        try:
            client_host = (request.client.host if request.client else "") or ""
        except Exception:
            client_host = ""
        if client_host in ("127.0.0.1", "::1", "localhost"):
            return _apply_no_cache_headers(await call_next(request))
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer ") else ""
    # Browser-native GETs (iframe src, <a download>) cannot set an Authorization
    # header, so the report-file endpoint also accepts the token as a query param
    # (?access_token=...). Scoped to GET on the generated-report file route only,
    # so this does not broaden auth for the rest of the API. Without this, the
    # batch/report PREVIEW iframe rendered {"detail":"Authentication required"}.
    if not token and method == "GET" and _allows_query_token(path):
        token = str(request.query_params.get("access_token") or request.query_params.get("token") or "").strip()
    if not token:
        return _apply_no_cache_headers(JSONResponse(status_code=401, content={"detail": "Authentication required"}))
    try:
        payload = decode_access_token(token)
        request.state.user_payload = payload
        token_tenant = str(payload.get("tenant_id") or "").strip()
        if token_tenant:
            normalized_token_tenant = set_current_tenant(token_tenant)
            # Strict mismatch only when request explicitly targets a non-default tenant.
            if tenant_id != "default" and normalized_token_tenant != tenant_id:
                return _apply_no_cache_headers(JSONResponse(status_code=403, content={"detail": "Token tenant mismatch"}))
            tenant_id = normalized_token_tenant
        elif tenant_id:
            set_current_tenant(tenant_id)
        # Operator 2026-06-23: refresh view-session liveness on every
        # authenticated request so the concurrent-session counter
        # accurately reflects who is actively using the View UI right
        # now. Failure here MUST NOT block the request.
        try:
            from app.services import view_sessions
            view_sessions.mark_active(
                str(payload.get("sub") or payload.get("username") or ""),
                str(payload.get("role") or ""),
            )
        except Exception:
            pass
    except Exception as exc:
        return _apply_no_cache_headers(JSONResponse(status_code=401, content={"detail": f"Invalid token: {exc}"}))
    return _apply_no_cache_headers(await call_next(request))


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Trustnode Edge API is running", "docs": "/docs", "health": "/api/health"}


@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket) -> None:
    tenant_id = resolve_websocket_tenant(websocket)
    set_current_tenant(tenant_id)
    token = (websocket.query_params.get("token") or "").strip()
    if not token:
        await websocket.close(code=1008)
        return
    try:
        payload = decode_access_token(token)
        token_tenant = str(payload.get("tenant_id") or "").strip()
        if token_tenant:
            normalized_token_tenant = set_current_tenant(token_tenant)
            if tenant_id != "default" and normalized_token_tenant != tenant_id:
                await websocket.close(code=1008)
                return
            tenant_id = normalized_token_tenant
    except Exception:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    queue = plc_manager.subscribe()
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "status",
                    "status": plc_manager.get_status().model_dump(),
                    "readings": [r.model_dump() for r in plc_manager.get_snapshot()],
                }
            )
        )
        while True:
            event = await queue.get()
            await websocket.send_text(json.dumps(event))
    except WebSocketDisconnect:
        pass
    finally:
        plc_manager.unsubscribe(queue)


async def _websocket_cloud_live_loop(websocket: WebSocket, tenant_id: str) -> None:
    live_limit = 220
    sample_interval_seconds = 0.15
    heartbeat_interval_seconds = 2.0

    def _flatten_latest(rows: list[dict]) -> list[dict]:
        flat: list[dict] = []
        for state in rows:
            tags = state.get("tags_json") if isinstance(state.get("tags_json"), list) else []
            for t in tags:
                flat.append(
                    {
                        "ts": state.get("sample_ts_utc"),
                        "source": "",
                        "gateway_id": state.get("gateway_id") or "",
                        "gateway_name": state.get("gateway_id") or "",
                        "device_name": state.get("machine_id") or "",
                        "plc_ip": "",
                        "database_name": "cloud_v1",
                        "tag": str((t or {}).get("tag_name") or ""),
                        "value": (t or {}).get("value"),
                        "quality": int((t or {}).get("quality_code") or state.get("quality_code") or 0),
                        "quality_label": str((t or {}).get("quality_label") or ""),
                        "edge_monotonic_seq": int(state.get("edge_monotonic_seq") or 0),
                        "tenant_id": state.get("tenant_id") or tenant_id,
                    }
                )
        return flat

    def _fingerprint(live_rows: list[dict], gateway_statuses: list[dict]) -> str:
        # Lightweight deterministic fingerprint for change detection.
        # Include only fields needed for chart/status refresh.
        live_head = [
            (
                str(r.get("gateway_id") or ""),
                str(r.get("tag") or ""),
                str(r.get("ts") or ""),
                str(r.get("value")),
                str(r.get("quality") or ""),
            )
            for r in live_rows[:180]
        ]
        status_head = [
            (
                str(g.get("gateway_id") or ""),
                bool(g.get("running")),
                str(g.get("last_check_utc") or ""),
            )
            for g in gateway_statuses
        ]
        return json.dumps([live_head, status_head], separators=(",", ":"), ensure_ascii=False)

    try:
        last_fp = ""
        last_sent_mono = 0.0
        while True:
            # IMPORTANT: these are sync DB calls (SQLite + sometimes Supabase
            # via SQLAlchemy). They MUST run on a worker thread, not the
            # asyncio event loop — otherwise every active WebSocket starves
            # the loop and freezes unrelated endpoints (/api/health etc).
            latest_rows = await asyncio.to_thread(
                ingest_store.query_latest, tenant_id=tenant_id, limit=live_limit
            )
            live_rows = _flatten_latest(latest_rows)
            # Migration-safe fallback: if v1 latest is empty, keep legacy cloud
            # mirror rows flowing so edge selection and dashboards remain usable.
            if not live_rows:
                live_rows = await asyncio.to_thread(
                    app_store.get_live_rows, limit=live_limit, prefer_cloud_reads=True
                )
            gateway_statuses = await asyncio.to_thread(
                app_store.build_gateway_statuses_from_live_rows, live_rows, freshness_ms=20000
            )
            running_gateways = [g for g in gateway_statuses if bool(g.get("running"))]
            newest_ts = max((str(g.get("last_check_utc") or "") for g in gateway_statuses), default="")
            fp = _fingerprint(live_rows, gateway_statuses)
            now_mono = asyncio.get_running_loop().time()
            should_send = fp != last_fp or (now_mono - last_sent_mono) >= heartbeat_interval_seconds
            if not should_send:
                await asyncio.sleep(sample_interval_seconds)
                continue
            payload = {
                # Keep legacy type for older web clients.
                "type": "cloud_snapshot",
                "stream": "cloud_live",
                "tenant_id": tenant_id,
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "live_rows": live_rows,
                "historian_rows": [],
                "log_rows": [],
                "inspector": None,
                "gateway_statuses": gateway_statuses,
                "gateway_status": {
                    "running": bool(running_gateways),
                    "gateway_count": len(gateway_statuses),
                    "running_count": len(running_gateways),
                    "last_check_utc": newest_ts,
                },
            }
            await websocket.send_text(json.dumps(payload))
            last_fp = fp
            last_sent_mono = now_mono
            await asyncio.sleep(sample_interval_seconds)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/cloud-stream")
@app.websocket("/ws/cloud-live")
async def websocket_cloud_stream(websocket: WebSocket) -> None:
    tenant_id = resolve_websocket_tenant(websocket)
    set_current_tenant(tenant_id)
    token = (websocket.query_params.get("token") or "").strip()
    if not token:
        await websocket.close(code=1008)
        return
    try:
        payload = decode_access_token(token)
        token_tenant = str(payload.get("tenant_id") or "").strip()
        if token_tenant:
            normalized_token_tenant = set_current_tenant(token_tenant)
            if tenant_id != "default" and normalized_token_tenant != tenant_id:
                await websocket.close(code=1008)
                return
            tenant_id = normalized_token_tenant
    except Exception:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    await _websocket_cloud_live_loop(websocket, tenant_id)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    try:
        report_scheduler.stop()
    except Exception:
        pass
    try:
        lite_report_poller.stop()
    except Exception:
        pass
    try:
        if _state.cp_users_puller is not None:
            _state.cp_users_puller.stop()
    except Exception:
        pass
    telemetry_service.shutdown()
    power_manager.shutdown()
    app_store.shutdown()


_main_app = app


class _AsgiDispatcher:
    """Top-level ASGI dispatcher that splits /api/cloud-live/* off the main
    app's middleware chain so SSE can stream without being buffered by
    BaseHTTPMiddleware. All other traffic — including the lifespan
    protocol — flows through the original FastAPI app unchanged.
    """

    def __init__(self, main, cloud_live):
        self._main = main
        self._cloud_live = cloud_live

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = str(scope.get("path") or "")
            if path.startswith("/api/cloud-live/"):
                sub_scope = dict(scope)
                sub_scope["path"] = path[len("/api/cloud-live"):] or "/"
                raw_path = scope.get("raw_path")
                if isinstance(raw_path, (bytes, bytearray)):
                    sub_scope["raw_path"] = bytes(raw_path)[len(b"/api/cloud-live"):] or b"/"
                sub_scope["root_path"] = (scope.get("root_path") or "") + "/api/cloud-live"
                return await self._cloud_live(sub_scope, receive, send)
        return await self._main(scope, receive, send)


app = _AsgiDispatcher(_main_app, _cloud_live_app)
