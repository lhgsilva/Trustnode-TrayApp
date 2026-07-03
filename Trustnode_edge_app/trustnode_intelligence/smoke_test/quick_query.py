"""Quick: ask a focused tag-stats question over a wider window so we hit
the historian data from the soak runs."""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from trustnode_intelligence.backend import store, service
from trustnode_intelligence.backend.tools import run_tool

# 1. Direct call: confirm 4-day window finds rows
print("=== direct get_tag_summary(SimREAL[3], -4d, now) ===")
res = run_tool("get_tag_summary",
               {"tag": "SimREAL[3]", "from_": "-4d", "to": "now"},
               {"data_source": "local"})
print(f"count={res.get('count')} min={res.get('min')} max={res.get('max')} avg={res.get('avg'):.2f}" if res.get('count') else f"count=0")
print()

# 2. Full LLM round-trip
print("=== LLM: 'What is the average SimREAL[3] over the last 4 days?' ===")
chat_id = store.create_chat(tenant_id="tenant-cust-e5916328",
                            user_id="admin-mari",
                            title="quick query",
                            data_source="local")
import time as _t
t0 = _t.monotonic()
r = service.run_chat_turn(chat_id, "What is the average value of SimREAL[3] over the last 4 days? Include min, max, count.", data_source="local")
print(f"finished in {_t.monotonic() - t0:.1f}s, ok={r.get('ok')}, tool_calls={len(r.get('tool_log') or [])}")
for tl in r.get('tool_log') or []:
    print(f"  -> {tl['name']}({tl.get('args')})")
print()
print("FINAL ANSWER:")
print("-" * 70)
print(r.get("content") or "(none)")
print("-" * 70)
