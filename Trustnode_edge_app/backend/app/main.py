import asyncio
import json
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

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

app = FastAPI(title="Trustnode Edge API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path or ""
    method = (request.method or "GET").upper()
    if method == "OPTIONS":
        return await call_next(request)
    if not path.startswith("/api/"):
        return await call_next(request)
    if path.startswith("/api/health") or path.startswith("/api/auth/"):
        return await call_next(request)
    if method in ("GET", "HEAD"):
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer ") else ""
    if not token:
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})
    try:
        decode_access_token(token)
    except Exception as exc:
        return JSONResponse(status_code=401, content={"detail": f"Invalid token: {exc}"})
    return await call_next(request)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Trustnode Edge API is running", "docs": "/docs", "health": "/api/health"}


@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket) -> None:
    token = (websocket.query_params.get("token") or "").strip()
    if not token:
        await websocket.close(code=1008)
        return
    try:
        decode_access_token(token)
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
    token = (websocket.query_params.get("token") or "").strip()
    if not token:
        await websocket.close(code=1008)
        return
    try:
        decode_access_token(token)
    except Exception:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        while True:
            payload = {
                "type": "cloud_snapshot",
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "live_rows": app_store.get_live_rows(limit=5000),
                "historian_rows": app_store.get_historian_rows(limit=1500),
                "log_rows": app_store.get_log_rows(limit=2500),
                "inspector": app_store.get_inspector_snapshot(preview_limit=20),
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
