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


def _do_pull() -> None:
    try:
        from app.state import app_store  # type: ignore
    except Exception:
        return
    try:
        bs = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
    except Exception:
        return
    s = bs.get("app_settings") if isinstance(bs.get("app_settings"), dict) else {}
    if not isinstance(s, dict):
        return
    edge_id = str(s.get("edge_id") or "").strip()
    cloud_url = str(s.get("cloud_url") or "").strip().rstrip("/")
    if not edge_id or not cloud_url:
        return
    url = f"{cloud_url}/api/control-plane/edge-link/ai-endpoint?{urllib.parse.urlencode({'edge_id': edge_id})}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status >= 400:
                return
            body = r.read().decode("utf-8", errors="replace")
    except Exception as exc:
        _log.debug("AI endpoint refresh: cloud fetch failed: %s", exc)
        return
    try:
        data = json.loads(body)
    except Exception:
        return
    if not (isinstance(data, dict) and data.get("ok")):
        return
    cfg = data.get("config") if isinstance(data.get("config"), dict) else None
    if not isinstance(cfg, dict) or not str(cfg.get("endpoint_url") or "").strip():
        return
    # Write into app_settings.ai_endpoint_config. get_ai_config() reads
    # this directly — no license-bundle overlay required.
    try:
        merged = dict(s)
        merged["ai_endpoint_config"] = dict(cfg)
        app_store.upsert_domain("app_settings", merged, actor="intelligence_refresh_boot")
        # Clear the config-read cache so get_ai_config() sees the fresh
        # endpoint on the very next call instead of waiting out the TTL.
        try:
            from .config import invalidate_config_cache
            invalidate_config_cache()
        except Exception:
            pass
        _log.info("AI endpoint config synced from portal: %s", cfg.get("endpoint_url"))
    except Exception as exc:
        _log.debug("AI endpoint refresh: persist failed: %s", exc)


def start_boot_refresh() -> None:
    """Fire-and-forget background refresh. Safe to call multiple times."""
    try:
        t = threading.Thread(target=_do_pull, name="trustnode-intelligence-refresh", daemon=True)
        t.start()
    except Exception:
        pass
