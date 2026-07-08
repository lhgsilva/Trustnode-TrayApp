"""One-shot AI Endpoint config pull.

Called ONCE on Edge boot (from trustnode_intelligence/backend/__init__.py).
Does one HTTP GET to the VPS public endpoint
`/api/control-plane/edge-link/ai-endpoint?edge_id=X`, and writes the
returned config into `app_settings.ai_endpoint_config`. That's it.

Design principles:
  * No cache invalidation.
  * No dependency on cloud auth or Supabase JWT.
  * No license-check enrichment side effects.
  * Silent on every failure — the app still boots, chat just says
    "not configured" until the next boot.
  * Runs in a background thread so boot is never blocked.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request

_log = logging.getLogger("trustnode.intelligence.refresh")


import time as _time


def _resolve_edge_and_cloud(app_store) -> tuple[str, str]:
    """Read this edge's id + the portal cloud_url from app_settings. The edge_id
    lives at app_settings.edge_id OR app_settings.edge_profile.edge_id (the
    canonical place after activation). Ignores the placeholder 'edge-01'."""
    try:
        bs = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
    except Exception:
        return "", ""
    s = bs.get("app_settings") if isinstance(bs.get("app_settings"), dict) else {}
    if not isinstance(s, dict):
        return "", ""
    ep = s.get("edge_profile") if isinstance(s.get("edge_profile"), dict) else {}
    edge_id = str(ep.get("edge_id") or s.get("edge_id") or "").strip()
    if edge_id.lower() == "edge-01":
        edge_id = ""  # not yet linked — a pull for edge-01 is useless
    cloud_url = str(s.get("cloud_url") or "").strip().rstrip("/")
    return edge_id, cloud_url


def _try_pull_once(app_store, edge_id: str, cloud_url: str) -> bool:
    """One GET of the portal AI-endpoint config; persist it on success.
    Returns True if a valid config was fetched + stored, else False."""
    url = f"{cloud_url}/api/control-plane/edge-link/ai-endpoint?{urllib.parse.urlencode({'edge_id': edge_id})}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status >= 400:
                return False
            body = r.read().decode("utf-8", errors="replace")
    except Exception as exc:
        _log.debug("AI endpoint refresh: cloud fetch failed: %s", exc)
        return False
    try:
        data = json.loads(body)
    except Exception:
        return False
    if not (isinstance(data, dict) and data.get("ok")):
        return False
    cfg = data.get("config") if isinstance(data.get("config"), dict) else None
    if not isinstance(cfg, dict) or not str(cfg.get("endpoint_url") or "").strip():
        return False
    try:
        # Re-read the latest app_settings so we don't clobber concurrent writes.
        bs = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        s = dict(bs.get("app_settings") or {})
        s["ai_endpoint_config"] = dict(cfg)
        app_store.upsert_domain("app_settings", s, actor="intelligence_refresh")
        try:
            from .config import invalidate_config_cache
            invalidate_config_cache()
        except Exception:
            pass
        _log.info("AI endpoint config synced from portal: %s", cfg.get("endpoint_url"))
        return True
    except Exception as exc:
        _log.debug("AI endpoint refresh: persist failed: %s", exc)
        return False


def _do_pull(retries: int = 4) -> None:
    """Pull the AI endpoint config, RETRYING with backoff. Operator 2026-07-08:
    the previous version tried exactly ONCE and was silent on failure — so on a
    fresh/just-activated edge, or when the portal cold-started and timed out,
    the config was never fetched and chat showed 'Failed to fetch' until the app
    was restarted. Now we retry a few times; if the edge isn't linked yet
    (edge_id empty), we wait for it to appear (activation may finish mid-boot)."""
    try:
        from app.state import app_store  # type: ignore
    except Exception:
        return
    for attempt in range(max(1, retries)):
        edge_id, cloud_url = _resolve_edge_and_cloud(app_store)
        if edge_id and cloud_url:
            if _try_pull_once(app_store, edge_id, cloud_url):
                return
        # else: edge not linked yet (or no cloud_url) — wait and re-check.
        try:
            _time.sleep(min(2.0 + attempt * 3.0, 12.0))  # 2s, 5s, 8s, 11s…
        except Exception:
            pass


def start_boot_refresh() -> None:
    """Fire-and-forget background refresh with retry. Safe to call many times."""
    try:
        t = threading.Thread(target=_do_pull, name="trustnode-intelligence-refresh", daemon=True)
        t.start()
    except Exception:
        pass


def pull_now(retries: int = 3) -> None:
    """Trigger an AI-endpoint config pull immediately (e.g. right after the edge
    is activated/linked, so the config lands without waiting for a restart).
    Fire-and-forget."""
    try:
        threading.Thread(target=lambda: _do_pull(retries=retries),
                         name="trustnode-intelligence-refresh-now", daemon=True).start()
    except Exception:
        pass
