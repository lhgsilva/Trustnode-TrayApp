import asyncio
import json
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
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
from app.state import plc_manager, app_store, telemetry_service, ingest_store
from app.tenant import resolve_request_tenant, resolve_websocket_tenant, set_current_tenant

app = FastAPI(title="Trustnode Edge API", version="0.1.0")

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


PUBLIC_PATHS = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/me",
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
    # v1 telemetry endpoints use explicit auth inside router handlers
    # (device tokens for ingest, user tokens for query/admin).
    if path.startswith("/api/v1/"):
        return _apply_no_cache_headers(await call_next(request))
    if request.url.path in PUBLIC_PATHS:
        return _apply_no_cache_headers(await call_next(request))
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer ") else ""
    if not token:
        return _apply_no_cache_headers(JSONResponse(status_code=401, content={"detail": "Authentication required"}))
    try:
        payload = decode_access_token(token)
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
    live_limit = 300
    sample_interval_seconds = 0.15

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

    try:
        while True:
            latest_rows = ingest_store.query_latest(tenant_id=tenant_id, limit=live_limit)
            live_rows = _flatten_latest(latest_rows)
            # Migration-safe fallback: if v1 latest is empty, keep legacy cloud
            # mirror rows flowing so edge selection and dashboards remain usable.
            if not live_rows:
                live_rows = app_store.get_live_rows(limit=live_limit, prefer_cloud_reads=True)
            gateway_statuses = app_store.build_gateway_statuses_from_live_rows(live_rows, freshness_ms=20000)
            running_gateways = [g for g in gateway_statuses if bool(g.get("running"))]
            newest_ts = max((str(g.get("last_check_utc") or "") for g in gateway_statuses), default="")
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
    telemetry_service.shutdown()
    app_store.shutdown()
