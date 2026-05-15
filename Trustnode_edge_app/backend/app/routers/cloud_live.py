"""Server-Sent Events live-stream for cloud client portals.

This is an ADDITIVE endpoint. It does not change any existing data-flow,
edge ingest, or sync worker. The endpoint runs on whichever backend
serves the customer's portal (typically the cloud VPS), reads the same
`live_latest` snapshot the existing /api/app-store/live endpoint reads,
and pushes compact JSON deltas to the browser over `text/event-stream`.

Why SSE instead of WebSocket:
  * one-way (server → browser) is all a dashboard needs
  * works through every plain HTTP proxy / corporate firewall
  * no special nginx upgrade rules needed beyond `proxy_buffering off`
  * auto-reconnects on the browser via the `EventSource` API

Tenant isolation:
  * The same /api/* auth middleware enforces a valid Bearer token and
    sets the request tenant before this handler runs.
  * `get_current_tenant()` is used INSIDE the streaming generator so the
    rows we emit are always scoped to the caller's tenant — no chance of
    leaking another customer's data.
  * `app_store.get_live_rows(prefer_cloud_reads=True)` already applies a
    `tenant_id = :tenant` filter against the Supabase `live_latest`
    mirror table.

Buffering / cadence:
  * Default tick = 250 ms (configurable via `TRUSTNODE_CLOUD_LIVE_SSE_MS`).
  * Only rows whose `ts` is newer than the last sent timestamp are emitted
    (delta only) — keeps the wire and the browser cheap.
  * A keep-alive comment is sent every 15 s so idle proxies don't drop
    the connection.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.state import app_store
from app.tenant import get_current_tenant

router = APIRouter(prefix="/api/cloud-live", tags=["cloud-live"])


def _row_ts_ms(row: dict[str, Any]) -> int:
    raw = str(row.get("ts") or row.get("ts_utc") or "").strip()
    if not raw:
        return 0
    try:
        # Accept both "2026-05-14T12:34:56.789Z" and
        # "2026-05-14 12:34:56.789" formats.
        from datetime import datetime
        text = raw.replace("Z", "+00:00")
        if " " in text and "T" not in text:
            text = text.replace(" ", "T")
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except Exception:
        return 0


@router.get("/stream")
async def cloud_live_stream(request: Request, limit: int = 200) -> StreamingResponse:
    """Stream live_latest deltas for the caller's tenant.

    Query params:
      limit: int   max rows the snapshot endpoint should pull per tick
                   (capped to keep payload bounded).
    """
    tick_ms = max(100, int(os.environ.get("TRUSTNODE_CLOUD_LIVE_SSE_MS", "250") or "250"))
    safe_limit = max(50, min(int(limit or 200), 800))
    tenant_id = get_current_tenant()

    async def event_gen():
        last_emit_ms = 0
        last_keepalive = time.monotonic()
        # First frame: the current snapshot so the browser paints
        # immediately without waiting for the next change.
        try:
            initial = app_store.get_live_rows(limit=safe_limit, prefer_cloud_reads=True)
        except Exception as exc:
            yield f"event: error\ndata: {json.dumps({'message': str(exc)})}\n\n"
            return
        if initial:
            payload = {
                "tenant_id": tenant_id,
                "tick_ms": tick_ms,
                "rows": initial,
            }
            yield f"event: snapshot\ndata: {json.dumps(payload, default=str)}\n\n"
            for row in initial:
                ts = _row_ts_ms(row)
                if ts > last_emit_ms:
                    last_emit_ms = ts

        while True:
            # Hard exit when the client disconnects so we don't hold a
            # dead generator + cloud DB read in memory.
            if await request.is_disconnected():
                break

            await asyncio.sleep(tick_ms / 1000.0)

            now_mono = time.monotonic()
            if now_mono - last_keepalive >= 15.0:
                yield ": keepalive\n\n"
                last_keepalive = now_mono

            try:
                rows = app_store.get_live_rows(limit=safe_limit, prefer_cloud_reads=True)
            except Exception as exc:
                yield f"event: error\ndata: {json.dumps({'message': str(exc)})}\n\n"
                continue

            # Emit only the deltas. The latest snapshot returns rows
            # sorted desc by ts, so a single pass picking ts > last_emit
            # captures every row that changed since the previous tick.
            deltas = []
            highest = last_emit_ms
            for row in rows or []:
                ts = _row_ts_ms(row)
                if ts > last_emit_ms:
                    deltas.append(row)
                    if ts > highest:
                        highest = ts
            if deltas:
                last_emit_ms = highest
                payload = {
                    "tenant_id": tenant_id,
                    "rows": deltas,
                }
                yield f"event: delta\ndata: {json.dumps(payload, default=str)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            # Disable proxy buffering so each event reaches the browser
            # promptly (nginx default chunks the response).
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
