import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


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


@app.on_event("startup")
async def startup_event() -> None:
    # Manual-control default: never auto-run gateways on backend boot.
    # Users start gateways explicitly from UI/runtime controls.
    try:
        await plc_manager.stop_all_gateways()
    except Exception:
        pass
    # Ensure telemetry ingest URL and tenant context are hydrated even before
    # any gateway loop iteration runs.
    try:
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False)
        telemetry_service.configure_from_bootstrap({"data": bootstrap})
    except Exception:
        bootstrap = None
    # One-shot migration: lift templates/schedules out of the legacy
    # `reporting_setup` JSON blob into the dedicated tables.
    try:
        if isinstance(bootstrap, dict):
            reports_store.migrate_from_bootstrap(bootstrap, tenant_id="default")
    except Exception:
        pass
    # Start the report scheduler daemon (15s tick, idle when no schedules).
    try:
        report_scheduler.start()
    except Exception:
        pass
    # Operator 2026-06-17: bring the in-process LAN socket up if the
    # operator previously turned LAN sharing on. This replaces the
    # old "restart backend" requirement — the second uvicorn server
    # runs in a daemon thread alongside the primary 127.0.0.1 one.
    try:
        from app.services import lan_socket as _lan_socket
        s = bootstrap.get("app_settings") if isinstance(bootstrap, dict) and isinstance(bootstrap.get("app_settings"), dict) else {}
        if bool(s.get("lan_sharing_enabled")):
            _lan_socket.sync_with_settings(True, int(settings.trustnode_port))
    except Exception:
        pass
    # Operator 2026-06-17 (Phase 3): re-apply previously enabled OPC UA
    # / MQTT toggles on boot, same pattern as LAN sharing. Lazy imports
    # so a stripped-down build without asyncua/amqtt still boots fine.
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


PUBLIC_PATHS = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/me",
    "/api/control-plane/portal-context",
    "/api/control-plane/activation-code/apply",
    "/api/control-plane/edge-link/bootstrap",
    "/api/control-plane/edge-link/register",
    "/api/control-plane/edge-link/local-finalize",
    "/api/control-plane/password-reset/public/issue",
    "/api/control-plane/password-reset/public/apply",
    "/api/v1/healthz",
    "/api/v1/readyz",
}


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
