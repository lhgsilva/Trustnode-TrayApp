"""Smoke test for TrustNode Intelligence.

Runs the LLM + tool pipeline against the live edge data — no HTTP, no
auth, no UI. Validates:

  1. License is recognised (module configs read correctly).
  2. AI endpoint is reachable.
  3. Tool catalog lists the expected tools.
  4. The LLM successfully calls list_gateways and reports mari's PLC.
  5. The LLM calls get_tag_summary on a real tag and produces stats.

Run AFTER backend is built and license injected, but does not need the
backend HTTP server to be running — it imports the module directly.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

# Force UTF-8 stdout so the arrow + box-drawing chars don't crash on cp1252.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Wire path so we can import both `app.*` (existing edge) and
# `trustnode_intelligence.*` (the new module). Mirrors what
# backend/app/main.py does at boot.
ROOT = Path(__file__).resolve().parents[2]   # Trustnode_edge_app/
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

# Don't override the app_store path — let AppStore resolve through
# the same workspace detector the packaged Edge uses, so we hit the
# SAME database the running backend uses.


def banner(text):
    print()
    print("=" * 70)
    print(text)
    print("=" * 70)


def main():
    banner("1. Import sanity")
    from trustnode_intelligence.backend import config, license
    from trustnode_intelligence.backend.tools import TOOL_CATALOG, run_tool
    print(f"  tool catalog: {len(TOOL_CATALOG)} tools")
    for name in TOOL_CATALOG:
        print(f"    - {name} ({TOOL_CATALOG[name].category})")

    banner("2. License + config")
    print(f"  has_intelligence_module: {license.has_intelligence_module()}")
    print(f"  module_config: {json.dumps(license.get_module_config(), indent=2)}")
    cfg = config.get_ai_config()
    print(f"  endpoint_url: {cfg.endpoint_url!r}")
    print(f"  model: {cfg.model!r}")
    print(f"  is_configured: {cfg.is_configured}")
    if not cfg.is_configured:
        print("  FAIL: AI endpoint not configured — license injection did not take.")
        sys.exit(1)

    banner("3. AI endpoint reachable (TCP probe)")
    import socket
    from urllib.parse import urlparse
    p = urlparse(cfg.endpoint_url)
    host = p.hostname or "127.0.0.1"
    port = p.port or (443 if p.scheme == "https" else 80)
    print(f"  probing {host}:{port}...", flush=True)
    try:
        with socket.create_connection((host, port), timeout=3.0):
            print(f"  OK: TCP port {port} open on {host}")
    except Exception as exc:
        print(f"  FAIL: cannot reach AI endpoint: {exc}")
        sys.exit(1)

    banner("4. Tool: list_gateways (no LLM, direct call)")
    result = run_tool("list_gateways", {}, {"data_source": "local"})
    print(f"  gateways: {result.get('count', 0)}")
    for g in (result.get("gateways") or []):
        print(f"    - {g.get('name')} ({g.get('id')}) type={g.get('type')} ip={g.get('plc_ip')} running={g.get('running')}")
    if not result.get("gateways"):
        print("  WARN: no gateways in scope. The supervisor reads ALL scopes, but bootstrap may not.")

    banner("5. Tool: list_tags (no LLM, direct call)")
    result = run_tool("list_tags", {}, {"data_source": "local"})
    print(f"  tags: {result.get('count', 0)}")
    for t in (result.get("tags") or [])[:10]:
        print(f"    - {t.get('tag')} (gateway={t.get('gateway_name')})")
    if result.get("count", 0) > 10:
        print(f"    ... +{result['count'] - 10} more")

    banner("6. Tool: get_tag_summary (no LLM, direct call)")
    # Use SimREAL[3] which we know exists from the soak.
    result = run_tool("get_tag_summary",
                      {"tag": "SimREAL[3]", "from_": "-1h", "to": "now"},
                      {"data_source": "local"})
    print(f"  tag={result.get('tag')} count={result.get('count')}")
    print(f"  min={result.get('min')} max={result.get('max')} avg={result.get('avg')}")
    if result.get("count", 0) == 0:
        print("  WARN: no rows. Make sure the gateway has been collecting recently.")

    banner("7. Full LLM round-trip (THE BIG TEST)")
    from trustnode_intelligence.backend import store, service
    chat_id = store.create_chat(tenant_id="tenant-cust-e5916328",
                                user_id="admin-mari",
                                title="smoke test",
                                data_source="local")
    print(f"  created chat: {chat_id}", flush=True)
    print("  asking LLM: 'List my gateways.'", flush=True)
    print("  (this calls Ollama — first call typically 30-90s on CPU)", flush=True)
    import time as _t
    _t0 = _t.monotonic()
    res = service.run_chat_turn(chat_id, "List my gateways.", data_source="local")
    print(f"  finished in {_t.monotonic() - _t0:.1f}s", flush=True)
    print()
    print(f"  ok: {res.get('ok')}")
    print(f"  error: {res.get('error')}")
    print(f"  tool calls: {len(res.get('tool_log') or [])}")
    for tl in (res.get('tool_log') or []):
        print(f"    -> {tl['name']}({json.dumps(tl.get('args') or {}, default=str)})")
    print()
    print("  FINAL ANSWER:")
    print("  " + "-" * 60)
    for line in (res.get("content") or "(no content)").splitlines():
        print(f"  {line}")
    print("  " + "-" * 60)

    banner("8. Another LLM round-trip — tag stats question")
    print("  asking LLM: 'What is the average SimREAL[3] over the last hour?'", flush=True)
    _t0 = _t.monotonic()
    res = service.run_chat_turn(chat_id,
                                "What is the average value of SimREAL[3] over the last hour?",
                                data_source="local")
    print(f"  finished in {_t.monotonic() - _t0:.1f}s", flush=True)
    print(f"  ok: {res.get('ok')}, tool calls: {len(res.get('tool_log') or [])}")
    for tl in (res.get('tool_log') or []):
        print(f"    -> {tl['name']}({json.dumps(tl.get('args') or {}, default=str)})")
    print()
    print("  FINAL ANSWER:")
    print("  " + "-" * 60)
    for line in (res.get("content") or "(no content)").splitlines():
        print(f"  {line}")
    print("  " + "-" * 60)

    print()
    print("=" * 70)
    print("SMOKE TEST COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
