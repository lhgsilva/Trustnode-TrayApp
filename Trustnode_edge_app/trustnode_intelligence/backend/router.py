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


class PresetsRequest(BaseModel):
    # Full palette: [{key, label, hint, queries:[str,...]}, ...]. Query strings
    # may use {t1}/{t2}/{t3}/{multi} placeholders — the UI fills them with the
    # customer's real tags. The customer edits/reorders these in the UI.
    categories: List[Dict[str, Any]] = Field(default_factory=list)


# Built-in DEFAULT palette — shipped with every install. Served when the
# customer hasn't customized their palette (fresh install). Query strings use
# {t1}/{t2}/{t3}/{multi} placeholders filled with the customer's real tags in
# the UI. The customer can edit/add/remove/reorder these; their version is
# then persisted per-tenant and survives upgrades.
DEFAULT_PRESETS: List[Dict[str, Any]] = [
    {
        "key": "data", "label": "Data & Live Status",
        "hint": "Instant · values, tables, what's running",
        "queries": [
            "What tags are live right now?",
            "Which gateways are running now?",
            "What is the current value of {t1}?",
            "Show me the latest reading for every live tag",
            "List all tags being collected",
            "Trend {t1} over the last 20 readings",
            "Give me detailed information about the current gateway running",
            "Are there any alarms in the last 24 hours?",
            "What is the min, max and average of {t1} in the last hour?",
            "How many readings has the active gateway written today?",
        ],
    },
    {
        "key": "analytics", "label": "Process Analytics",
        "hint": "High Effort · stability, drift, capability",
        "queries": [
            "Is {t1} stable and in control over the last hour?",
            "Are there any anomalies or spikes in {t1} today?",
            "Is the process drifting? Analyze {t2} over the last 4 hours.",
            "Give me a process-capability assessment for {t1}.",
            "Summarize the health of the process across all live tags.",
            "Why did {t1} change over the last hour?",
            "Detect any out-of-range excursions across the live tags today.",
            "What is the standard deviation of {t3} and is it acceptable?",
            "Assess whether the gateway is collecting reliably or has gaps.",
            "Identify the noisiest tag and explain its variability.",
        ],
    },
    {
        "key": "compare", "label": "Compare · Multi-series · Batches",
        "hint": "Overlays, correlation, period-over-period, batches",
        "queries": [
            "Compare {multi} grouped by 1 minute over the last hour and show correlation",
            "Correlate {t1} and {t2} every 5 seconds over the last 30 minutes",
            "Trend {multi} in the same chart",
            "Compare {t1} this hour to the same hour yesterday",
            "Trend {multi} for the last batch",
            "Show the last 5 batches and their durations",
            "Compare {t1} across the last 3 batches",
            "Trend {t1} since the process started",
            "Trend {t1} since it last crossed a high value",
            "Which of {multi} move together? Analyze the correlation over the last hour.",
        ],
    },
]


# --- Helpers --------------------------------------------------------------

def _edge_tenant() -> str:
    """Return THIS edge's own tenant id — the customer/tenant the edge is
    linked to (app_settings.tenant_id / edge_profile.linked_customer_id),
    read lock-free from the app_store db. This is the SAME scope the rest of
    the app uses for the customer's data. Cached briefly.

    Operator 2026-07-08 (SCOPING FIX): chats + insights were scoped by the
    LOGIN USER'S jwt tenant, which for the built-in `admin` user is 'default'.
    So every edge that logged in as a default-tenant admin shared one 'default'
    AI workspace — a customer saw chats/insights created on ANOTHER machine.
    Scoping by the edge's own tenant isolates each customer edge and makes a
    fresh install start clean. Returns '' if it can't be resolved (caller
    falls back to the user's jwt tenant, then 'default')."""
    import time as _t
    now = _t.monotonic()
    cached = _EDGE_TENANT_CACHE.get("value")
    if cached is not None and (now - _EDGE_TENANT_CACHE.get("at", 0.0)) < 30.0:
        return cached
    val = ""
    try:
        import json as _json, sqlite3 as _sqlite3
        from app.state import app_store  # type: ignore
        db_path = getattr(app_store, "_db_path", "") or ""
        if db_path:
            con = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
            try:
                row = con.execute(
                    "SELECT payload_json FROM config_documents WHERE domain='app_settings'"
                ).fetchone()
            finally:
                con.close()
            if row and row[0]:
                s = _json.loads(row[0])
                if isinstance(s, dict):
                    # Prefer the edge's tenant; then derive from linked customer.
                    ep = s.get("edge_profile") if isinstance(s.get("edge_profile"), dict) else {}
                    cand = str(s.get("tenant_id") or "").strip()
                    if not cand:
                        cust = str(ep.get("linked_customer_id") or s.get("customer_id") or "").strip()
                        if cust:
                            cand = cust if cust.startswith("tenant-") else f"tenant-{cust}"
                    # Never treat the placeholder 'default' as a real edge tenant.
                    if cand and cand.lower() != "default":
                        val = cand
    except Exception:
        val = ""
    _EDGE_TENANT_CACHE["value"] = val
    _EDGE_TENANT_CACHE["at"] = now
    return val


_EDGE_TENANT_CACHE: Dict[str, Any] = {"value": None, "at": 0.0}


def _user_ctx(request: Request) -> Dict[str, str]:
    # Operator 2026-07-02: the auth middleware sets `request.state.user_payload`
    # (see backend/app/main.py). The previous code read `current_user`, which
    # was NEVER set — so user_id was always empty.
    user = getattr(request.state, "user_payload", None)
    if not isinstance(user, dict):
        user = getattr(request.state, "current_user", None) or {}
    if not isinstance(user, dict):
        user = {}
    user_id = str(user.get("sub") or user.get("username") or user.get("id") or "")
    # Operator 2026-07-08 (SCOPING FIX): scope AI data by the EDGE's own tenant
    # (the customer this edge is linked to) — the same scope the rest of the app
    # uses — so each customer edge is isolated and a fresh install starts clean.
    # Fall back to the user's jwt tenant, then 'default', if the edge tenant
    # can't be resolved (e.g. a not-yet-linked fresh edge).
    tenant_id = _edge_tenant() or str(user.get("tenant_id") or "default")
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


@router.get("/catalog")
async def get_catalog() -> Dict[str, Any]:
    """Lightweight catalog for the UI to build REAL, customer-specific starter
    queries (predefined-query palette). Returns the tags actually being
    collected + configured gateways for THIS edge — so the suggestions use the
    customer's own tag names, never hardcoded demo tags. Runs on the DB pool.
    """
    def _build() -> Dict[str, Any]:
        from .tools.gateways import run_list_tags, run_list_gateways, _recently_active
        ctx = {"data_source": "local"}
        try:
            live_tags, live_gws, _ = _recently_active(600)  # last 10 min = "live"
        except Exception:
            live_tags, live_gws = set(), set()
        try:
            all_tags = [t.get("tag") for t in (run_list_tags({}, ctx).get("tags") or []) if t.get("tag")]
        except Exception:
            all_tags = []
        try:
            gws = run_list_gateways({}, ctx).get("gateways") or []
        except Exception:
            gws = []
        # De-dup, preserve order; prefer live tags first so starter queries use
        # tags the customer is actively collecting.
        seen = set()
        ordered = []
        for t in list(live_tags) + all_tags:
            if t and t not in seen:
                seen.add(t)
                ordered.append(t)
        return {
            "ok": True,
            "tags": ordered[:60],
            "live_tags": sorted(live_tags)[:60],
            "gateways": [{"name": g.get("name"), "id": g.get("id"), "running": g.get("running")} for g in gws][:20],
        }
    return await _run_db(_build)


# --- Presets (customer-editable starter-query palette) --------------------

@router.get("/presets")
async def get_presets(request: Request) -> Dict[str, Any]:
    """Return the customer's palette (their saved edits) or the shipped
    DEFAULT_PRESETS if they haven't customized it. `is_default` tells the UI
    whether these are the built-ins."""
    ctx = _user_ctx(request)
    saved = await _run_db(store.get_presets, ctx["tenant_id"])
    if saved and isinstance(saved.get("categories"), list) and saved["categories"]:
        return {"ok": True, "categories": saved["categories"], "is_default": False}
    return {"ok": True, "categories": DEFAULT_PRESETS, "is_default": True}


@router.put("/presets")
async def put_presets(payload: PresetsRequest, request: Request) -> Dict[str, Any]:
    """Save the customer's edited palette (per tenant)."""
    ctx = _user_ctx(request)
    cats = payload.categories or []
    await _run_db(store.save_presets, ctx["tenant_id"], {"categories": cats})
    return {"ok": True, "categories": cats, "is_default": False}


@router.delete("/presets")
async def delete_presets(request: Request) -> Dict[str, Any]:
    """Reset the palette to the shipped defaults."""
    ctx = _user_ctx(request)
    await _run_db(store.reset_presets, ctx["tenant_id"])
    return {"ok": True, "categories": DEFAULT_PRESETS, "is_default": True}
