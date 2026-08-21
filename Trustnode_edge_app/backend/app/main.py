from app import boot_clock as _boot_clock  # noqa: E402  (BOOT-HEALTH: capture T0 first)
import asyncio
import json
import logging
import os
import sys
import time as _time_mod
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
    # Operator 2026-08-21: the build-time boot self-test (`--boot-probe`) runs
    # the bundled EXE against a throwaway data dir and must NOT pick up a
    # developer/operator .env (cloud credentials) lying next to the binary.
    if str(os.environ.get("TRUSTNODE_SKIP_DOTENV", "")).strip().lower() in {"1", "true", "yes", "on"}:
        return
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
def _lite_view_resolve(token: str, request: Request):
    # Owner decision 2026-08-21: no-login share links are a licence option.
    from app.services import access_policy as _acc
    _acc.require_module("view_share_links")(request)
    return resolve_edge_view_link_public(token)
app.include_router(reports_router)
app.include_router(historian_export_router)
app.include_router(workspace_router)
# Retention / storage / backup API (operator 2026-08-21). Admin-gated in
# the router itself; shares the /api/app-store prefix on purpose so the
# frontend keeps one base URL.
try:
    from app.routers.retention import router as retention_router
    app.include_router(retention_router)
except Exception as _exc:  # pragma: no cover - never block boot on it
    print(f"[trustnode][boot] retention router mount skipped: {_exc!r}", flush=True)

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


# --------------------------------------------------------------------------
# Operator 2026-08-21 (BOOT-HEALTH FIX): boot-time integrity check policy.
# On an 8 GB app store the boot quick_check ran 126-203 s on EVERY boot and
# saturated the disk exactly while the splash probed /api/health and the
# first historian writes landed ("database is locked", 10 s flushes).
#   TRUSTNODE_BOOT_INTEGRITY_CHECK   = weekly (default) | always | never
#   TRUSTNODE_BOOT_INTEGRITY_DELAY_S = 180  (start only after boot settles)
# The last successful run is persisted next to the DB so "weekly" survives
# restarts. /api/health reports the current state either way.
# --------------------------------------------------------------------------
def _integrity_marker_path() -> str | None:
    try:
        db_path = str(getattr(app_store, "_db_path", "") or "")
        if not db_path:
            return None
        return os.path.join(os.path.dirname(db_path), ".boot_integrity.json")
    except Exception:
        return None


def _integrity_policy() -> tuple[str, str | None, float | None]:
    policy = str(os.environ.get("TRUSTNODE_BOOT_INTEGRITY_CHECK", "weekly") or "weekly").strip().lower()
    if policy not in {"weekly", "always", "never"}:
        policy = "weekly"
    marker = _integrity_marker_path()
    last_ok: float | None = None
    if marker and os.path.isfile(marker):
        try:
            with open(marker, "r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
            v = data.get("last_ok_ts")
            if v is not None:
                last_ok = float(v)
        except Exception:
            last_ok = None
    return policy, marker, last_ok


def _iso_from_ts(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()[:19] + "Z"
    except Exception:
        return None


def _write_integrity_marker(marker: str | None, results: dict) -> None:
    if not marker:
        return
    try:
        all_ok = bool(results) and all(str(v).lower() == "ok" for v in results.values())
        prev: dict = {}
        if os.path.isfile(marker):
            try:
                with open(marker, "r", encoding="utf-8") as fh:
                    prev = json.load(fh) or {}
            except Exception:
                prev = {}
        now_ts = _time_mod.time()
        data = {
            "last_run_ts": now_ts,
            "last_run_utc": _iso_from_ts(now_ts),
            "last_results": {k: str(v) for k, v in results.items()},
            "last_ok_ts": now_ts if all_ok else prev.get("last_ok_ts"),
        }
        data["last_ok_utc"] = _iso_from_ts(data.get("last_ok_ts"))
        tmp = marker + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, marker)
    except Exception:
        pass


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
    # Operator 2026-08-21 (BOOT-HEALTH FIX): the restore/mirror pair was the
    # FIRST deferred step and took 12 s -> 22 s -> 24 s -> 59 s across the last
    # four boots (mirror -> export_activation_state -> get_bootstrap under
    # app_store._lock, contended during boot). Nothing below needs the mirror,
    # so it now runs LAST; the restore (only meaningful on an empty install)
    # stays first but is BOUNDED: if it hasn't returned in 15 s we move on and
    # let the worker finish in the background (its result is still logged).
    async def _bounded(label: str, fn, timeout_s: float):
        t0 = _time_mod.monotonic()
        fut = asyncio.ensure_future(asyncio.to_thread(fn))
        try:
            res = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout_s)
            return res
        except asyncio.TimeoutError:
            print(f"[trustnode][boot] {label} still running after {timeout_s:.0f}s — continuing boot "
                  f"without it (+{_boot_clock.age_s():.1f}s)", flush=True)

            def _late(f):
                try:
                    exc = f.exception()
                except Exception:
                    exc = None
                print(f"[trustnode][boot] {label} finished late after "
                      f"{_time_mod.monotonic() - t0:.1f}s" + (f" with error {exc!r}" if exc else ""),
                      flush=True)
            fut.add_done_callback(_late)
            return None
        except Exception as exc:
            print(f"[trustnode][boot] {label} failed: {exc!r}", flush=True)
            return None

    try:
        result = await _bounded("activation registry restore",
                                control_plane_store.restore_activation_from_registry_if_empty, 15.0)
        if isinstance(result, dict) and result.get("restored"):
            print(f"[trustnode][boot] activation restored from registry: {result.get('applied')}", flush=True)
    except Exception as exc:
        print(f"[trustnode][boot] activation registry restore failed: {exc!r}", flush=True)
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
    # Operator 2026-07-24 (STARTUP DELAY FIX): these checks used to run
    # SEQUENTIALLY and AWAITED here, before any gateway was started. On a real
    # install that is brutal: a 2 GB telemetry DB takes ~46 s for a single
    # quick_check, so collection did not begin until ~47 s after boot — the
    # "app takes minutes to start collecting" report.
    #
    # The check is diagnostic, not a precondition for collecting: nothing below
    # consumes its result except /api/health. So it now runs as a BACKGROUND
    # task (both DBs concurrently) while startup proceeds to start gateways.
    # Corruption is still detected and logged, just without blocking data.
    async def _run_integrity_checks_bg() -> None:
        try:
            import sqlite3 as _sqlite_check
            import time as _time_integrity

            def _check(p):
                con = _sqlite_check.connect(f"file:{p}?mode=ro", uri=True, timeout=10.0)
                try:
                    return str((con.execute("PRAGMA quick_check").fetchone() or ["unknown"])[0])
                finally:
                    con.close()

            targets = []
            for label, path_attr in (("app_store", "_db_path"), ("telemetry", "_db_path")):
                target = app_store if label == "app_store" else telemetry_service
                db_path = getattr(target, path_attr, None)
                if db_path:
                    targets.append((label, db_path))

            async def _one(label: str, db_path: str) -> tuple[str, str]:
                try:
                    return label, await asyncio.to_thread(_check, db_path)
                except Exception as exc:
                    return label, f"error: {exc!r}"

            t0 = _time_integrity.time()
            pairs = await asyncio.gather(*(_one(l, p) for l, p in targets))
            integrity_results = dict(pairs)
            for label, result in integrity_results.items():
                if str(result).lower() != "ok":
                    print(
                        f"[trustnode][boot][integrity] {label} quick_check returned {result!r}"
                        " — investigate before extended use",
                        flush=True,
                    )
            print(
                f"[trustnode][boot][integrity] background check finished in {_time_integrity.time() - t0:.1f}s: "
                f"{integrity_results}",
                flush=True,
            )
            try:
                setattr(app_store, "_last_integrity_results", integrity_results)
            except Exception:
                pass
            _write_integrity_marker(_integrity_marker_path(), integrity_results)
        except Exception as exc:
            print(f"[trustnode][boot][integrity] check skipped: {exc!r}", flush=True)

    _integ_policy, _integ_marker, _integ_last_ok = _integrity_policy()
    try:
        _integ_delay_s = max(0.0, float(os.environ.get("TRUSTNODE_BOOT_INTEGRITY_DELAY_S", "180") or "180"))
    except Exception:
        _integ_delay_s = 180.0
    if _integ_policy == "never":
        setattr(app_store, "_last_integrity_results", {"status": "disabled"})
        print("[trustnode][boot][integrity] check disabled by policy", flush=True)
    elif _integ_policy == "weekly" and _integ_last_ok is not None and (_time_mod.time() - _integ_last_ok) < 7 * 86400:
        setattr(app_store, "_last_integrity_results",
                {"status": "skipped_recent", "last_ok_utc": _iso_from_ts(_integ_last_ok)})
        print(f"[trustnode][boot][integrity] skipped — last OK check "
              f"{(_time_mod.time() - _integ_last_ok) / 3600:.1f}h ago (weekly policy)", flush=True)
    else:
        setattr(app_store, "_last_integrity_results",
                {"status": "scheduled", "starts_in_s": _integ_delay_s, "policy": _integ_policy})
        print(f"[trustnode][boot][integrity] scheduled in {_integ_delay_s:.0f}s (policy={_integ_policy})", flush=True)

        async def _delayed_integrity() -> None:
            try:
                await asyncio.sleep(_integ_delay_s)
                await _run_integrity_checks_bg()
            except Exception:
                pass
        asyncio.create_task(_delayed_integrity())

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
                print(f"[trustnode][boot] auto-resumed {resumed} gateway(s) +{_boot_clock.age_s():.1f}s after process start", flush=True)
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
            _lan_socket.sync_with_settings(True, int(settings.trustnode_port), s)
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

    # Operator 2026-08-21: activation mirror runs LAST (belt-and-braces copy of
    # the activation rows into the registry). Bounded so a contended lock can
    # never hold up anything — there is nothing after it anyway.
    try:
        mirror = await _bounded("activation registry mirror",
                                control_plane_store.mirror_activation_to_registry, 20.0)
        if isinstance(mirror, dict) and mirror.get("ok"):
            print(f"[trustnode][boot] activation mirrored to registry: hive={mirror.get('hive')}", flush=True)
    except Exception:
        pass
    # Retention engine LAST: it is the only subsystem that may delete data, so
    # it starts after everything else is up. It then waits out its own boot
    # delay + health gate before the first pass (see retention_engine.I4).
    try:
        from app.state import retention_engine as _retention
        _retention.start()
    except Exception as exc:
        print(f"[trustnode][boot] retention engine failed to start: {exc!r}", flush=True)

    print(f"[trustnode][boot] deferred init complete +{_boot_clock.age_s():.1f}s after process start", flush=True)


# 2026-07-25: defense-in-depth against DOUBLE startup. The in-process LAN
# server (lan_socket.py) shares this FastAPI app and used to re-fire the
# lifespan (observed: every boot ran auto-resume twice — gateway start, stop,
# start — and armed two watchdogs). lan_socket now runs with lifespan="off";
# this guard keeps boot single-shot even if some future embedder forgets that.
_STARTUP_RAN = False


@app.on_event("startup")
async def startup_event() -> None:
    global _STARTUP_RAN
    if _STARTUP_RAN:
        print("[trustnode][boot] duplicate startup_event suppressed", flush=True)
        return
    _STARTUP_RAN = True
    # Operator 2026-08-21 (NO-ORPHANS): exit with the desktop app. Armed only
    # when Electron passed TRUSTNODE_PARENT_PID (dev runs are unaffected).
    try:
        from app import parent_watch as _parent_watch
        _parent_watch.start_from_env()
    except Exception as _pw_exc:
        print(f"[trustnode][boot] WARN: parent-watch not armed: {_pw_exc!r}", flush=True)
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

    # Operator 2026-08-21 (BOOT-HEALTH FIX): boot-health watchdog. If the
    # first /api/health has not been SERVED within 10 s of uvicorn startup
    # (and again at 30 s), dump every thread's top frames once so a field
    # log names whatever is holding boot up. Pure diagnostics — never kills.
    try:
        import threading as _threading_bh
        import time as _time_bh
        import sys as _sys_bh
        import traceback as _tb_bh
        from app.routers import health as _health_mod

        def _boot_health_watchdog() -> None:
            thresholds = [10.0, 30.0]
            t0 = _time_bh.monotonic()
            fired: set[float] = set()
            while thresholds and (_time_bh.monotonic() - t0) < 90.0:
                _time_bh.sleep(1.0)
                if _health_mod.first_health_served_age_s() is not None:
                    return
                elapsed = _time_bh.monotonic() - t0
                due = [t for t in thresholds if elapsed >= t and t not in fired]
                if not due:
                    continue
                for t in due:
                    fired.add(t)
                    thresholds.remove(t)
                names = {th.ident: th.name for th in _threading_bh.enumerate()}
                print(f"[trustnode][boot][health-watchdog] /api/health NOT served {elapsed:.0f}s after "
                      f"uvicorn startup (+{_boot_clock.age_s():.1f}s process age) — thread dump follows",
                      flush=True)
                for tid, frame in _sys_bh._current_frames().items():
                    try:
                        stack = _tb_bh.extract_stack(frame)[-8:]
                        app_frames = [f for f in stack if "app" in (f.filename or "").replace("\\", "/")] or stack[-3:]
                        desc = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in reversed(app_frames))
                        print(f"[trustnode][boot][health-watchdog]   {names.get(tid, tid)}: {desc}", flush=True)
                    except Exception:
                        pass

        _threading_bh.Thread(target=_boot_health_watchdog, name="trustnode-boot-health-watchdog", daemon=True).start()
    except Exception as exc:
        print(f"[trustnode][boot] WARN: boot-health watchdog setup failed: {exc!r}", flush=True)

    # Operator 2026-08-21 (POOL-EXHAUSTION WATCHDOG): when the sync-handler
    # pool fills up (observed 205-231 threads, 0% CPU, 15-22 MB/s page reads,
    # health unanswered), log WHAT the workers are doing — top distinct
    # stacks of the asyncio_* threads, deduplicated with counts. Diagnostic
    # only; at most one report every 120 s.
    try:
        import threading as _threading_pw
        import time as _time_pw
        import sys as _sys_pw
        import traceback as _tb_pw
        import collections as _collections_pw

        def _pool_watchdog() -> None:
            try:
                threshold = int(os.environ.get("TRUSTNODE_POOL_WATCHDOG_THREADS", "120") or "120")
            except Exception:
                threshold = 120
            last = 0.0
            while True:
                _time_pw.sleep(15.0)
                try:
                    n = _threading_pw.active_count()
                    if n < threshold or (_time_pw.monotonic() - last) < 120.0:
                        continue
                    last = _time_pw.monotonic()
                    names = {t.ident: t.name for t in _threading_pw.enumerate()}
                    buckets: "_collections_pw.Counter[str]" = _collections_pw.Counter()
                    workers = 0
                    for tid, frame in _sys_pw._current_frames().items():
                        nm = str(names.get(tid, tid))
                        if not (nm.startswith("asyncio_") or nm.startswith("ThreadPoolExecutor")):
                            continue
                        workers += 1
                        try:
                            frames = _tb_pw.extract_stack(frame, limit=16)
                        except Exception:
                            continue
                        app_frames = [f for f in frames if "app" in (f.filename or "").replace("\\", "/")] or frames[-4:]
                        key = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in reversed(app_frames[-7:]))
                        buckets[key] += 1
                    print(f"[trustnode][pool-watchdog] {n} threads alive, {workers} pool workers — "
                          f"top worker stacks:", flush=True)
                    for key, c in buckets.most_common(6):
                        print(f"[trustnode][pool-watchdog]   x{c}: {key}", flush=True)
                except Exception:
                    pass

        _threading_pw.Thread(target=_pool_watchdog, name="trustnode-pool-watchdog", daemon=True).start()
    except Exception as exc:
        print(f"[trustnode][boot] WARN: pool watchdog setup failed: {exc!r}", flush=True)


PUBLIC_PATHS = {
    "/api/health",
    "/api/boot-probe",
    # Operator 2026-08-21 (Remote Access): the LAN certificate is a public key
    # (needed BEFORE a browser can trust the HTTPS listener); logout must work
    # with an expired token so the cookie can always be cleared.
    "/api/lan-sharing/certificate",
    "/api/auth/logout",
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


from app.services import access_policy as _access

_tv_cache: dict = {}


def _token_version_ok(payload: dict) -> tuple:
    """Reject tokens whose `tv` claim is older than the user's current token
    version (bumped on revoke). Cached 5 s per user — the AuthStore read is a
    tiny indexed SELECT on its own SQLite file, never app_store._lock."""
    try:
        import time as _t
        username = str(payload.get("sub") or payload.get("username") or "")
        if not username:
            return True, ""
        claim = int(payload.get("tv") or 0)
        now = _t.monotonic()
        _key = username.lower()
        hit = _access.TV_CACHE.get(_key)
        if hit and now - hit[0] < 5.0:
            current = hit[1]
        else:
            from app.state import auth_store as _as_mw
            current = int(_as_mw.get_token_version(username))
            _access.TV_CACHE[_key] = (now, current)
            if len(_access.TV_CACHE) > 5000:
                _access.TV_CACHE.clear()
        if claim < current:
            return False, "Session revoked — please sign in again"
        return True, ""
    except Exception:
        return True, ""


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
        # Operator 2026-08-21 (plan §3.3 item 3): the LAN surface bundles
        # (/trustnode/{full|client|lite}/app/) require a verified session —
        # HttpOnly cookie set at login, or a Bearer for XHR. Previously the
        # admin bundle was downloadable by any LAN peer and the only gate was
        # a client-side redirect that failed OPEN on a network error. Landings
        # and /trustnode/login/ stay public.
        _surface = _access.surface_of(path)
        if _surface:
            from fastapi.responses import RedirectResponse as _Redir
            _tok = _access.token_from_request(request)
            _login = f"/trustnode/login/?variant={_surface}&return={path}"
            if not _tok:
                return _Redir(url=_login, status_code=302)
            try:
                _pl = decode_access_token(_tok)
                _tv_ok, _ = _token_version_ok(_pl)
                if not _tv_ok:
                    return _Redir(url=_login, status_code=302)
            except Exception:
                return _Redir(url=_login, status_code=302)
            _remote = _access.request_is_remote(request)
            _ok, _why = _access.surface_access(_surface, _pl, _remote)
            if not _ok:
                _access.audit("surface", outcome="denied", request=request, payload=_pl,
                              details={"surface": _surface, "reason": _why},
                              rate_key=f"surf:{_surface}:{_pl.get('sub')}:{_why}")
                if _why.startswith("licence:"):
                    return JSONResponse(status_code=404, content={"detail": "Not found"})
                return JSONResponse(status_code=403, content={"detail": f"Access to this surface is not allowed ({_why})"})
            try:
                from app.services import view_sessions as _vs
                _vs.mark_active(str(_pl.get("sub") or ""), str(_pl.get("role") or ""),
                                ip=_access.client_host(request), surface=f"lan_{_surface}")
            except Exception:
                pass
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
        # 2026-08-21: browser-native loads from a LAN surface carry the session cookie
        token = str(request.cookies.get(_access.SESSION_COOKIE) or "").strip()
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
        # Operator 2026-08-21: revoked sessions (Remote Access "Revoke" bumps
        # the user's token version; older tokens are refused).
        _tv_ok, _tv_why = _token_version_ok(payload)
        if not _tv_ok:
            return _apply_no_cache_headers(JSONResponse(status_code=401, content={"detail": _tv_why}))
        try:
            from app.services import view_sessions
            view_sessions.mark_active(
                str(payload.get("sub") or payload.get("username") or ""),
                str(payload.get("role") or ""),
                ip=_access.client_host(request),
                surface=str(request.headers.get("X-Trustnode-Surface") or "").strip()[:24],
            )
        except Exception:
            pass
    except Exception as exc:
        return _apply_no_cache_headers(JSONResponse(status_code=401, content={"detail": f"Invalid token: {exc}"}))
    # Operator 2026-08-21 (plan §3.3): central role + network policy. Reads are
    # free for any authenticated role; configuration mutations need
    # engineer/admin; remote mutations additionally need `remote_admin_lan`.
    # Mode "lan" (default): ENFORCE for non-loopback clients, LOG for loopback.
    _allowed, _reason, _eff = _access.evaluate(request, payload)
    if not _allowed:
        return _apply_no_cache_headers(JSONResponse(status_code=403, content={"detail": f"Forbidden: {_reason}"}))
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
                        # 2026-07-26: carry STRING tag text + declared type so
                        # cloud-live consumers stop rendering 0.000 for text tags.
                        "value_text": (t or {}).get("value_text"),
                        "data_type": str((t or {}).get("data_type") or ""),
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
        from app.state import retention_engine as _retention
        _retention.stop()
    except Exception:
        pass
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
