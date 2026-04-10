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


async def _websocket_cloud_live_loop(websocket: WebSocket, tenant_id: str) -> None:
    live_limit = 300
    sample_interval_seconds = 0.7

    def _ts_ms(raw: str) -> int:
        text = str(raw or "").strip()
        if not text:
            return 0
        try:
            iso = text.replace("Z", "+00:00")
            if " " in iso and "T" not in iso:
                iso = iso.replace(" ", "T")
            return int(datetime.fromisoformat(iso).timestamp() * 1000)
        except Exception:
            return 0

    try:
        while True:
            live_rows = app_store.get_live_rows(limit=live_limit, prefer_cloud_reads=True)
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            latest_by_gateway: dict[str, dict[str, str]] = {}
            write_count_by_gateway: dict[str, int] = {}
            for row in live_rows:
                gateway_id = str(row.get("gateway_id") or "").strip()
                if not gateway_id:
                    continue
                write_count_by_gateway[gateway_id] = int(write_count_by_gateway.get(gateway_id, 0)) + 1
                ts_txt = str(row.get("ts") or row.get("ts_utc") or "")
                ts_epoch = _ts_ms(ts_txt)
                prev = latest_by_gateway.get(gateway_id)
                prev_epoch = _ts_ms(prev.get("ts", "")) if prev else 0
                if not prev or ts_epoch >= prev_epoch:
                    latest_by_gateway[gateway_id] = {
                        "ts": ts_txt,
                        "gateway_name": str(row.get("gateway_name") or gateway_id),
                        "gateway_type": str(row.get("source") or ""),
                        "plc_ip": str(row.get("plc_ip") or ""),
                    }

            gateway_statuses = []
            for gateway_id, meta in latest_by_gateway.items():
                ts_txt = str(meta.get("ts") or "")
                ts_epoch = _ts_ms(ts_txt)
                running = ts_epoch > 0 and max(0, now_ms - ts_epoch) <= 12000
                gateway_statuses.append(
                    {
                        "running": bool(running),
                        "gateway_type": str(meta.get("gateway_type") or ""),
                        "plc_ip": str(meta.get("plc_ip") or ""),
                        "interval_ms": 1000,
                        "tags": [],
                        "last_error": None,
                        "db_sink_engine": "",
                        "db_write_count": int(write_count_by_gateway.get(gateway_id, 0)),
                        "db_last_write_utc": ts_txt,
                        "db_last_error": None,
                        "db_pending_count": 0,
                        "collection_blocked": False,
                        "collection_block_reason": None,
                        "gateway_id": gateway_id,
                        "gateway_name": str(meta.get("gateway_name") or gateway_id),
                        "last_check_utc": ts_txt,
                    }
                )
            gateway_statuses.sort(key=lambda g: str(g.get("last_check_utc") or ""), reverse=True)
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
    app_store.shutdown()
