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
from app.state import plc_manager, app_store
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


PUBLIC_PATHS = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/me",
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


@app.websocket("/ws/cloud-stream")
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
    tick = 0
    live_limit = 1200
    try:
        while True:
            tick += 1
            include_heavy = (tick % 8) == 0
            payload = {
                "type": "cloud_snapshot",
                "tenant_id": tenant_id,
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "live_rows": app_store.get_live_rows(limit=live_limit),
                "historian_rows": app_store.get_historian_rows(limit=250) if include_heavy else [],
                "log_rows": app_store.get_log_rows(limit=300) if include_heavy else [],
                "inspector": app_store.get_inspector_snapshot(preview_limit=20) if include_heavy else None,
                "gateway_statuses": plc_manager.list_gateway_statuses(),
                "gateway_status": plc_manager.get_status().model_dump(),
            }
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass


@app.on_event("shutdown")
async def on_shutdown() -> None:
    app_store.shutdown()
