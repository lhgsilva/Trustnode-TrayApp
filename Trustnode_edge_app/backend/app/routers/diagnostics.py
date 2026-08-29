# -*- coding: utf-8 -*-
"""System and data-path diagnostics.

Plain `def`, not `async def`: FastAPI then runs the handler in its threadpool
instead of on the event loop. `/api/health` was once an `async def` that called
`to_thread`, which starved the shared anyio pool and produced historian gaps -
this endpoint touches psutil and two small SQLite stores, so it must never sit
on the loop. The work itself is cached in the service for a couple of seconds.
"""
from fastapi import APIRouter

from app.services import diagnostics as diagnostics_service

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


@router.get("")
@router.get("/")
def get_diagnostics(refresh: int = 0) -> dict:
    """Machine, TrustNode's own share of it, storage and the data path.

    `refresh=1` bypasses the cache — for a manual "refresh now" button, not for
    a polling loop.
    """
    return diagnostics_service.snapshot(force=bool(refresh))


@router.get("/system")
def get_system() -> dict:
    """Just the machine + our processes — the cheap half, for a status strip."""
    snap = diagnostics_service.snapshot()
    return {
        "ok": True,
        "ts_utc": snap.get("ts_utc"),
        "machine": snap.get("machine"),
        "trustnode": snap.get("trustnode"),
    }


@router.get("/pipeline")
def get_pipeline() -> dict:
    """Just the data path: collection, storage, distribution, forwarding."""
    snap = diagnostics_service.snapshot()
    return {
        "ok": True,
        "ts_utc": snap.get("ts_utc"),
        "storage": snap.get("storage"),
        "pipeline": snap.get("pipeline"),
    }
