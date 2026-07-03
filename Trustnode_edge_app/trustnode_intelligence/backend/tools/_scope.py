"""Cross-scope readers for gateway / database / tags.

The standard `app_store.get_bootstrap()` resolves a SINGLE scope based on
the current tenant context. From a background process (smoke test, the
scheduler, etc.) or from a tenant whose docs live in a different scope
than the bootstrap resolver picks, that returns empty.

These helpers scan EVERY `config_documents_scoped` row directly so the
LLM tools always see every configured gateway / DB / tag regardless of
which scope they live in. Same trick the watchdog supervisor uses.

Read-only; never writes. De-duplicates by `id` (first scope wins).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List


def _candidate_db_paths() -> List[str]:
    """The Edge's active app_store path plus known historical workspace
    locations on Windows. Earlier deployments wrote to user-data; the
    current packaged build writes to ProgramData. Reading from BOTH
    lets the tools surface data the user can see in the UI regardless
    of which workspace the running Edge ended up choosing.

    Order matters: first hit wins on duplicate ids."""
    paths: List[str] = []
    try:
        from app.state import app_store as _store  # type: ignore
        live = getattr(_store, "_db_path", "")
        if live:
            paths.append(live)
    except Exception:
        pass
    # Add the well-known Windows locations if they exist + aren't already in.
    import os as _os
    for cand in (
        r"C:\ProgramData\TrustNode\edge\trustnode_app_store.db",
        _os.path.expanduser(r"~\.trustnode_edge\data\trustnode_app_store.db"),
    ):
        if _os.path.exists(cand) and cand not in paths:
            paths.append(cand)
    return paths


def _db_path() -> str:
    """Kept for callers that just want the live one."""
    paths = _candidate_db_paths()
    return paths[0] if paths else ""


def _read_all(domain: str) -> List[Dict[str, Any]]:
    """Return a list of items across every scope AND every candidate DB
    for the given domain. De-dup by 'id' (first hit wins)."""
    out: List[Dict[str, Any]] = []
    seen_ids: set = set()

    def _ingest(items):
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = str(item.get('id') or '').strip()
            if not iid or iid in seen_ids:
                continue
            seen_ids.add(iid)
            out.append(item)

    for path in _candidate_db_paths():
        try:
            # WAL-resilient: short busy_timeout + read_uncommitted so config
            # reads never stall on a checkpoint of the actively-written DB.
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
            try:
                con.execute("PRAGMA busy_timeout=400")
                con.execute("PRAGMA read_uncommitted=1")
            except Exception:
                pass
        except Exception:
            continue
        try:
            for sql in (
                "SELECT payload_json FROM config_documents_scoped WHERE domain=?",
                "SELECT payload_json FROM config_documents WHERE domain=?",
            ):
                try:
                    cur = con.execute(sql, (domain,))
                except sqlite3.OperationalError:
                    continue
                for (payload,) in cur:
                    try:
                        data = json.loads(payload) if isinstance(payload, str) else payload
                    except Exception:
                        continue
                    items = data if isinstance(data, list) else (
                        data.get('gateways') or data.get('databases') or
                        data.get('items') or []
                    )
                    _ingest(items)
        finally:
            try: con.close()
            except Exception: pass
    return out


def all_db_paths() -> List[str]:
    """Exposed for other tools that want to query multiple DBs (e.g.
    historian, app_logs)."""
    return _candidate_db_paths()


def all_gateways() -> List[Dict[str, Any]]:
    return _read_all('gateway_configurations')


def all_tag_names() -> List[str]:
    """Every configured tag name across all gateways (deduped, sorted)."""
    names = set()
    for g in all_gateways():
        for t in (g.get("tags") or []):
            if isinstance(t, str):
                nm = t.strip()
            elif isinstance(t, dict):
                nm = str(t.get("tag_name") or t.get("name") or "").strip()
            else:
                nm = ""
            if nm:
                names.add(nm)
    return sorted(names)


def resolve_tag(query: str, limit: int = 5) -> Dict[str, Any]:
    """Fuzzy-match a (possibly misspelled) tag name against configured tags.

    Returns:
      {exact: "<name>"}                      when there's an exact (case-insensitive) hit
      {suggestions: ["<name>", ...]}         when close matches exist (user should pick)
      {suggestions: []}                      when nothing is close

    Used by the AI so a typo like 'sim real[3]' or 'LVA' resolves to the real
    tag, or the assistant can offer a short pick-list instead of failing.
    """
    import difflib
    q = str(query or "").strip()
    tags = all_tag_names()
    if not q or not tags:
        return {"exact": None, "suggestions": []}
    # Exact (case-insensitive).
    for t in tags:
        if t.lower() == q.lower():
            return {"exact": t, "suggestions": []}
    ql = q.lower()

    def _norm(s):
        return "".join(ch for ch in s.lower() if ch.isalnum())
    qn = _norm(q)
    # NORMALIZED-exact: 'simreal3' == norm('SimREAL[3]'). If EXACTLY ONE tag
    # normalizes to the query, that's an unambiguous match — return it as
    # exact so the fast-path proceeds without a disambiguation round-trip.
    norm_exact = [t for t in tags if _norm(t) == qn]
    if len(norm_exact) == 1:
        return {"exact": norm_exact[0], "suggestions": []}
    if len(norm_exact) > 1:
        return {"exact": None, "suggestions": norm_exact[:limit]}

    scored = []
    for t in tags:
        tn = _norm(t)
        if not tn:
            continue
        if qn and (qn in tn or tn in qn):
            score = 0.95
        else:
            score = difflib.SequenceMatcher(None, qn, tn).ratio()
        # Also consider substring on the raw (case-insensitive) form.
        if ql and ql in t.lower():
            score = max(score, 0.9)
        scored.append((score, t))
    scored.sort(key=lambda x: (-x[0], x[1]))
    suggestions = [t for s, t in scored if s >= 0.45][:limit]
    return {"exact": None, "suggestions": suggestions}


def gateway_name_for(gateway_id: str) -> str:
    """Return the human-friendly gateway name for an id (or the id
    itself when no name is configured). Used by tag tools so the LLM
    can render names instead of opaque IDs in the chat reply."""
    gid = str(gateway_id or "").strip()
    if not gid:
        return ""
    for g in all_gateways():
        if str(g.get("id") or "").strip() == gid:
            return str(g.get("name") or gid)
    return gid


def gateway_name_for_tag(tag: str) -> str:
    """Best-effort: find the gateway that owns a tag and return its name.
    If multiple gateways carry the same tag name we return the first
    match. Returns empty string when no gateway is configured to read it."""
    t = str(tag or "").strip()
    if not t:
        return ""
    for g in all_gateways():
        for entry in (g.get("tags") or []):
            name = ""
            if isinstance(entry, str):
                name = entry
            elif isinstance(entry, dict):
                name = str(entry.get("tag_name") or entry.get("name") or "")
            if name == t:
                return str(g.get("name") or g.get("id") or "")
    return ""


def all_databases() -> List[Dict[str, Any]]:
    return _read_all('database_configurations')
