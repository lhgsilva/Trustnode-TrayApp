"""HTTP router for TrustNode Intelligence.

Mounted at /api/intelligence. All routes require the
`trustnode_intelligence` module in the active license (404 otherwise).
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

# Operator 2026-07-02: AI completions can be SLOW (20-80s per turn). We run
# them on a SEPARATE dedicated pool so they never touch the shared anyio
# threadpool.
_AI_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tn-intel-ai")

# Operator 2026-07-02 (DEFINITIVE WEDGE FIX): a SECOND dedicated pool for the
# fast CRUD/status DB work. Previously these routes used asyncio.to_thread,
# which draws from the SHARED anyio default pool (~40 slots) that the whole
# FastAPI app relies on. Under real use (a message running for many seconds
# on the AI pool + the UI polling status + create/delete + FastAPI resolving
# the sync `require_intelligence_license` dependency for each async route via
# that same shared pool), the shared pool drained and intelligence routes —
# specifically the async ones whose Depends() must run there — hung, while
# plain sync routes (which run inline) still answered. Giving intelligence
# its OWN generous pool means it can never starve the shared pool, and its
# own work is bounded independently of the slow AI turns.
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="tn-intel-db")


async def _run_ai(fn, *args):
    """Run a blocking AI function on the dedicated AI pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_AI_EXECUTOR, fn, *args)


async def _run_db(fn, *args):
    """Run fast blocking DB/license work on the dedicated intelligence pool
    (NOT the shared anyio pool), so intelligence can never drain the pool the
    rest of the app depends on."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_DB_EXECUTOR, fn, *args)

from . import config as _cfg
from . import service, store
from .license import (
    MODULE_KEY,
    get_feature_flag,
    get_module_config,
    get_rate_limit,
    has_intelligence_module,
    require_intelligence_license,
)


router = APIRouter(prefix="/api/intelligence", tags=["intelligence"],
                   dependencies=[Depends(require_intelligence_license)])

# Kick the insight scheduler on first import. Idempotent + daemon thread,
# so this is safe even if router.py is imported multiple times.
try:
    from . import insight_scheduler as _scheduler
    _scheduler.start_scheduler()
except Exception:
    pass


# --- Models ---------------------------------------------------------------

class CreateChatRequest(BaseModel):
    title: str = "New chat"
    data_source: str = "local"


class SendMessageRequest(BaseModel):
    chat_id: str
    message: str
    data_source: Optional[str] = None
    # "turbo" = prefer the instant local/fast path; "smart" = full AI reasoning.
    mode: Optional[str] = None


class RenameChatRequest(BaseModel):
    title: str


class CreateInsightRequest(BaseModel):
    title: str
    description: str = ""
    prompt: str
    tool_plan: List[Dict[str, Any]] = Field(default_factory=list)
    data_source: str = "local"
    schedule_cron: str = ""
    email_to: str = ""


# --- Helpers --------------------------------------------------------------

def _user_ctx(request: Request) -> Dict[str, str]:
    # Operator 2026-07-02: the auth middleware sets `request.state.user_payload`
    # (see backend/app/main.py). The previous code read `current_user`, which
    # was NEVER set — so tenant_id always fell back to "default" and user_id
    # was always empty. That silently scoped every chat to an empty user.
    # Read the correct attribute; keep the old one as a fallback.
    user = getattr(request.state, "user_payload", None)
    if not isinstance(user, dict):
        user = getattr(request.state, "current_user", None) or {}
    if not isinstance(user, dict):
        user = {}
    tenant_id = str(user.get("tenant_id") or "default")
    user_id = str(user.get("sub") or user.get("username") or user.get("id") or "")
    return {"tenant_id": tenant_id, "user_id": user_id}


# --- Status ---------------------------------------------------------------


# Operator 2026-07-02 (WEDGE FIX): every route below is `async def` and
# runs its blocking work (SQLite on the dedicated intelligence DB, plus
# the license/config reads that may touch the app_store lock) via
# asyncio.to_thread. Previously these were plain `def` handlers that
# FastAPI ran on the SHARED anyio threadpool. When `get_status` (polled by
# the UI) blocked waiting on the app_store global lock — which the
# historian write path holds while batch-inserting into a 1 GB table — it
# occupied shared pool slots. Enough blocked polls drained the pool and
# every OTHER handler (create/delete/list/send) queued behind it → the UI
# saw "Failed to fetch". Moving the blocking work to to_thread keeps the
# anyio pool free: the coroutine awaits without holding a pool slot, so
# CRUD never starves even while status waits on a busy lock.

def _build_status() -> Dict[str, Any]:
    cfg = _cfg.get_ai_config()
    portal = get_module_config()
    return {
        "ok": True,
        "module": MODULE_KEY,
        "licensed": has_intelligence_module(),
        "endpoint_configured": cfg.is_configured,
        "model_configured": bool(cfg.model),
        "endpoint_url_set": bool(cfg.endpoint_url),
        "features": {
            "insights": get_feature_flag("insights", True),
            "email_schedule": get_feature_flag("email_schedule", True),
        },
        "rate_limits": {
            "queries_per_day": get_rate_limit("queries_per_day", 500),
            "max_tokens_per_query": get_rate_limit("max_tokens_per_query", 2048),
        },
        "allowed_tools": portal.get("allowed_tools") or ["read_only"],
    }


@router.get("/status")
async def get_status() -> Dict[str, Any]:
    """Public-within-module status: is the AI endpoint configured?
    Runs off the anyio pool so a busy app_store lock never wedges it."""
    return await _run_db(_build_status)


# --- Chats ----------------------------------------------------------------

@router.post("/chats")
async def create_chat(payload: CreateChatRequest, request: Request) -> Dict[str, Any]:
    ctx = _user_ctx(request)
    chat_id = await _run_db(
        store.create_chat, ctx["tenant_id"], ctx["user_id"], payload.title, payload.data_source
    )
    return {"ok": True, "id": chat_id}


@router.get("/chats")
async def list_chats(request: Request) -> Dict[str, Any]:
    ctx = _user_ctx(request)
    chats = await _run_db(store.list_chats, ctx["tenant_id"], ctx["user_id"])
    return {"ok": True, "chats": chats}


@router.get("/chats/{chat_id}")
async def get_chat(chat_id: str) -> Dict[str, Any]:
    chat = await _run_db(store.get_chat, chat_id)
    if not chat:
        raise HTTPException(404, "Chat not found")
    chat["messages"] = await _run_db(store.list_messages, chat_id)
    return {"ok": True, "chat": chat}


@router.patch("/chats/{chat_id}")
async def rename_chat(chat_id: str, payload: RenameChatRequest) -> Dict[str, Any]:
    existing = await _run_db(store.get_chat, chat_id)
    if not existing:
        raise HTTPException(404, "Chat not found")
    await _run_db(store.rename_chat, chat_id, payload.title)
    return {"ok": True}


@router.delete("/chats/{chat_id}")
async def delete_chat(chat_id: str) -> Dict[str, Any]:
    await _run_db(store.delete_chat, chat_id)
    return {"ok": True}


@router.post("/chats/{chat_id}/messages")
async def send_message(chat_id: str, payload: SendMessageRequest) -> Dict[str, Any]:
    chat = await _run_db(store.get_chat, chat_id)
    if not chat:
        raise HTTPException(404, "Chat not found")
    ds = payload.data_source or chat.get("data_source") or "local"
    if payload.data_source and payload.data_source != chat.get("data_source"):
        await _run_db(store.set_chat_data_source, chat_id, payload.data_source)
    # Run the LLM round on the DEDICATED AI pool (not the shared FastAPI
    # threadpool) so a slow model can't starve the CRUD endpoints.
    mode = (payload.mode or "high").strip().lower()
    result = await _run_ai(service.run_chat_turn, chat_id, payload.message, ds, mode)
    return result


# --- Insights -------------------------------------------------------------

@router.post("/insights")
async def create_insight(payload: CreateInsightRequest, request: Request) -> Dict[str, Any]:
    if not get_feature_flag("insights", True):
        raise HTTPException(403, "Insights feature is not enabled for this license.")
    ctx = _user_ctx(request)
    iid = await _run_db(
        lambda: store.create_insight(
            tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
            title=payload.title, description=payload.description,
            prompt=payload.prompt, tool_plan=payload.tool_plan,
            data_source=payload.data_source, schedule_cron=payload.schedule_cron,
            email_to=payload.email_to,
        )
    )
    return {"ok": True, "id": iid}


@router.get("/insights")
async def list_insights(request: Request) -> Dict[str, Any]:
    ctx = _user_ctx(request)
    insights = await _run_db(store.list_insights, ctx["tenant_id"], ctx["user_id"])
    return {"ok": True, "insights": insights}


@router.get("/insights/{insight_id}")
async def get_insight(insight_id: str) -> Dict[str, Any]:
    item = await _run_db(store.get_insight, insight_id)
    if not item:
        raise HTTPException(404, "Insight not found")
    return {"ok": True, "insight": item}


@router.post("/insights/{insight_id}/run")
async def run_insight_now(insight_id: str) -> Dict[str, Any]:
    # Operator 2026-07-02: was `store.get_insight()` on the event loop — a
    # blocking SQLite call (incl. one-time ensure_schema DDL) that FROZE the
    # whole event loop → status timeouts. Route it off-loop like every other.
    item = await _run_db(store.get_insight, insight_id)
    if not item:
        raise HTTPException(404, "Insight not found")
    import time as _time
    started_utc = _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime())
    result = await _run_ai(
        service.run_insight, item["prompt"], item["tool_plan"], item.get("data_source", "local"),
    )
    finished_utc = _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime())
    ok = bool(result.get("ok"))
    content = result.get("content") if ok else None
    err = result.get("error") if not ok else None
    store.update_insight_run(insight_id, content, err)
    # Append to per-insight run history so the Insights page's right
    # column shows this in the timeline.
    try:
        store.record_insight_run(
            insight_id,
            triggered_by="manual",
            started_utc=started_utc,
            finished_utc=finished_utc,
            ok=ok,
            content=content,
            error=err,
            tool_results=result.get("tool_results") or result.get("tool_log") or [],
        )
    except Exception:
        pass
    return result


# --- Insight runs (history) -------------------------------------------------

@router.get("/insights/{insight_id}/runs")
async def list_insight_runs(insight_id: str, limit: int = 100) -> Dict[str, Any]:
    item = await _run_db(store.get_insight, insight_id)
    if not item:
        raise HTTPException(404, "Insight not found")
    runs = await _run_db(lambda: store.list_insight_runs(insight_id, limit=limit))
    return {"ok": True, "insight_id": insight_id, "runs": runs}


@router.delete("/insights/{insight_id}/runs/{run_id}")
async def delete_insight_run(insight_id: str, run_id: int) -> Dict[str, Any]:
    # insight_id is in the path mostly for URL scoping; the runs table
    # is keyed by run_id alone. We don't enforce membership here.
    await _run_db(store.delete_insight_run, run_id)
    return {"ok": True}


@router.delete("/insights/{insight_id}")
async def delete_insight(insight_id: str) -> Dict[str, Any]:
    await _run_db(store.delete_insight, insight_id)
    return {"ok": True}


# --- Tool catalog (introspection for the UI) ------------------------------

@router.get("/tools")
def list_tools() -> Dict[str, Any]:
    from .tools import openai_tool_schemas
    return {"ok": True, "tools": openai_tool_schemas(allowed_only=True)}
