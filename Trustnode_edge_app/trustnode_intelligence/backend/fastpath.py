"""Deterministic fast-path for common data questions.

Modern data-assistant pattern (Julius/Hex/Perplexity style): instead of
letting the LLM decide-a-tool → call-it → narrate (3 sequential model
round-trips), we CLASSIFY the intent with cheap regex, run the query
DIRECTLY in code, and then make AT MOST ONE small LLM call to narrate the
already-computed result. Pure-data asks can even skip the LLM entirely.

This does NOT change any SQL/analytics — it reuses the exact same tool
runners, so data quality is identical. It only removes model round-trips.

Returns None when the question isn't a recognized fast-path shape, in
which case the caller falls back to the full tool-calling loop.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .tools import run_tool

# Intent patterns. Each maps a user phrasing to (tool_name, arg_builder).
# We keep these deliberately conservative — only fire on clearly-shaped asks.

_LAST_RE = re.compile(r"\b(last|latest|current|most recent)\b.*\b(reading|value|reads?)\b", re.I)
_AVG_RE = re.compile(r"\b(average|avg|mean|min|max|minimum|maximum|stddev|std|summary|stats?)\b", re.I)
# Bare current-value ask: "value of X", "what is X", "reading of X",
# "how much is X", "show me X". Very common; must be instant (maps to the
# tag summary, which returns latest value + basic stats). Deliberately does
# NOT require a last/latest qualifier — that gap sent these to the slow loop.
_VALUE_RE = re.compile(r"\b(value|reading|worth)\b|\b(what|whats|how much|show( me)?|give me)\b", re.I)
_CHART_RE = re.compile(r"\b(chart|plot|graph|trend|visuali[sz]e|show me.*(over time|readings?)|line chart)\b", re.I)
_BUCKET_RE = re.compile(r"\b(every|per)\s+(\d+)\s*(s|sec|second|seconds|m|min|minute|minutes|h|hour|hours)\b", re.I)

# Time-window hints.
_WINDOW_RE = re.compile(r"\blast\s+(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hours?|d|days?|w|weeks?)\b", re.I)
_LAST_N_RE = re.compile(r"\blast\s+(\d+)\s+(reading|readings|points?|samples?)\b", re.I)

# Words that signal the user wants ANALYSIS / REASONING (only these truly
# need the AI). Instant mode can do EVERYTHING ELSE — tables, values, trends,
# charts, projections, comparisons — because those are DATA operations we
# compute directly. Only genuine interpretation ('is it stable/in control',
# 'why', 'any anomalies', 'is it capable') requires the AI's judgement.
# NOTE: 'trend', 'chart', 'plot', 'compare', 'forecast', 'project' are NOT
# here — they're visualizations/computations Instant handles instantly.
_INTERPRET_WORDS = (
    "why", "explain", "interpret", "stable", "unstable", "in control",
    "out of control", "abnormal", "healthy", "capable", "capability",
    "should i", "does it mean", "what does", "anomal", "outlier", "drift",
    "root cause", "recommend", "advise", "correlat", "cpk", "cp k",
    "spc", "assess", "evaluate", "diagnos", "is it ok", "is it good",
    "is it bad", "is it healthy", "is it normal", "insight", "analy",
    "how is the process", "process quality", "control chart",
)
_INTERPRET_RE = re.compile("|".join(re.escape(w) for w in _INTERPRET_WORDS), re.I)


# Filler/stop words that are never a tag name — skip them when scanning.
_STOP = {
    "tag", "tags", "the", "of", "for", "a", "an", "last", "first", "recent",
    "reading", "readings", "value", "values", "point", "points", "sample",
    "samples", "data", "trend", "chart", "plot", "graph", "show", "me", "give",
    "list", "average", "avg", "mean", "min", "max", "and", "in", "on", "over",
    "this", "that", "current", "latest", "collected", "being", "process",
    "hour", "hours", "minute", "minutes", "day", "days", "week", "weeks",
    "gateway", "gateways", "plc", "from", "to", "now", "is", "it", "are",
}


def _extract_tag(text: str) -> Optional[str]:
    """Pull a candidate tag token from the message. We grab the most
    tag-looking token and let the tool's auto-resolver map it to a real tag."""
    tokens = list(re.finditer(r"[A-Za-z][A-Za-z0-9_]*(?:\[\d+\])?", text))
    # Pass 1: strongly tag-shaped — bracket, underscore, or embedded digit.
    for m in tokens:
        tok = m.group(0)
        if tok.lower() in _STOP:
            continue
        if "[" in tok or "_" in tok or any(c.isdigit() for c in tok):
            return tok
    # Pass 2: an UPPERCASE-ish abbreviation (PVA, LVB) or a 4+ char word that
    # isn't a stop word — let the auto-resolver map it to the real tag.
    for m in tokens:
        tok = m.group(0)
        low = tok.lower()
        if low in _STOP:
            continue
        if tok.isupper() and 2 <= len(tok) <= 12:   # PVA, LVB, TEMP
            return tok
        if len(tok) >= 4:                            # 'level', 'temperature'
            return tok
    # Pass 3: quoted tag.
    m = re.search(r"['\"]([^'\"]{2,40})['\"]", text)
    if m:
        return m.group(1)
    return None


def _window_from(text: str) -> str:
    m = _WINDOW_RE.search(text)
    if m:
        n, unit = m.group(1), m.group(2).lower()[0]
        return f"-{n}{unit}"
    lm = _LAST_N_RE.search(text)
    if lm:
        # "last N readings" — size the window to comfortably include N points.
        # At ~1 reading/sec that's N seconds; pad generously (×3, min 5 min).
        try:
            n = int(lm.group(1))
        except Exception:
            n = 100
        secs = max(300, n * 3)
        return f"-{secs}s"
    return "-1h"


_LIST_TAGS_RE = re.compile(r"\b(list|show|what|which)\b.*\btags?\b", re.I)
_LIST_GW_RE = re.compile(r"\b(list|show|what|which)\b.*\b(gateway|gateways|plc)s?\b", re.I)
_LIST_ALARMS_RE = re.compile(r"\b(list|show|any|recent)\b.*\b(alarm|alarms|alert|alerts|events?)\b", re.I)


def classify(user_message: str) -> Optional[Dict[str, Any]]:
    """Return {tool, args, kind} for a recognized fast-path, else None."""
    text = user_message.strip()
    if len(text) > 200:
        return None  # long/complex → full loop

    # Catalog listings — pure data, no tag needed. These are common and should
    # be instant (the old full-loop made 'list tags' take several seconds).
    if _LIST_GW_RE.search(text) and not _LIST_TAGS_RE.search(text):
        return {"kind": "list", "tool": "list_gateways", "args": {}, "is_chart": False}
    if _LIST_TAGS_RE.search(text):
        return {"kind": "list", "tool": "list_tags", "args": {}, "is_chart": False}
    if _LIST_ALARMS_RE.search(text):
        return {"kind": "list", "tool": "list_recent_alarms", "args": {}, "is_chart": False}

    tag = _extract_tag(text)
    if not tag:
        return None

    is_chart = bool(_CHART_RE.search(text))
    frm = _window_from(text)

    # Bucketed ("average every 5 minutes")
    bm = _BUCKET_RE.search(text)
    if bm and (_AVG_RE.search(text) or is_chart):
        n, unit = bm.group(2), bm.group(3).lower()
        ubase = "s" if unit.startswith("s") else ("m" if unit.startswith("m") else "h")
        bucket = f"{n}{ubase}"
        return {"kind": "bucketed", "tool": "get_bucketed_series",
                "args": {"tag": tag, "from_": frm, "to": "now", "bucket": bucket, "agg": "avg"},
                "is_chart": is_chart}

    if is_chart:
        return {"kind": "chart", "tool": "get_tag_timeseries",
                "args": {"tag": tag, "from_": frm, "to": "now",
                         "max_points": 200},
                "is_chart": True}

    if _LAST_RE.search(text) or _AVG_RE.search(text) or _VALUE_RE.search(text):
        return {"kind": "summary", "tool": "get_tag_summary",
                "args": {"tag": tag, "from_": frm, "to": "now"},
                "is_chart": False}

    return None


def run_fastpath(user_message: str, data_source: str) -> Optional[Dict[str, Any]]:
    """Execute the fast-path if the message matches. Returns:
      {"tool_result": <dict>, "tool_name": str, "is_chart": bool,
       "disambiguation": <suggestions or None>}
    or None if not a fast-path.
    """
    plan = classify(user_message)
    if not plan:
        return None
    result = run_tool(plan["tool"], plan["args"], {"data_source": data_source})
    if not isinstance(result, dict):
        return None
    # Auto-resolver may signal it needs the user to choose.
    if result.get("disambiguation_needed"):
        return {"disambiguation": result.get("suggestions") or [],
                "query": result.get("query") or plan["args"].get("tag"),
                "tool_name": plan["tool"], "is_chart": plan.get("is_chart", False),
                "tool_result": None}
    # DECISION: does this question need AI interpretation, or just the data?
    # If the user asked a plain data question (no 'why/stable/anomaly/trend'
    # words), we can render a deterministic answer in CODE — instant, no AI.
    wants_ai = bool(_INTERPRET_RE.search(user_message))
    return {"tool_result": result, "tool_name": plan["tool"],
            "is_chart": plan.get("is_chart", False), "disambiguation": None,
            "wants_ai": wants_ai}


# --------------------------------------------------------------------------
# Instant local render (NO AI) — deterministic Markdown for pure-data asks.
# --------------------------------------------------------------------------

def _fmt_num(v):
    if v is None:
        return "—"
    try:
        f = float(v)
        if f == int(f) and abs(f) < 1e15:
            return f"{int(f):,}"
        return f"{f:,.2f}"
    except Exception:
        return str(v)


def render_local(tool_name: str, result: dict, is_chart: bool) -> str:
    """Build a Markdown answer directly from the tool result — no LLM.
    Sub-second, deterministic, always-correct numbers."""
    import json as _json

    # ---- Catalog listings (tags / gateways / alarms) ----
    if tool_name == "list_tags":
        tags = result.get("tags") or []
        if not tags:
            return "No tags are currently being collected."
        rows = ["**Collected tags** — {} total".format(len(tags)), "",
                "| Tag | Gateway |", "|---|---|"]
        for t in tags:
            rows.append(f"| {t.get('tag','')} | {t.get('gateway_name') or t.get('gateway_id','')} |")
        return "\n".join(rows)
    if tool_name == "list_gateways":
        gws = result.get("gateways") or []
        if not gws:
            return "No gateways are configured."
        rows = ["**Gateways** — {} total".format(len(gws)), "",
                "| Name | Type | IP | Interval (ms) | Running |", "|---|---|---|---|---|"]
        for g in gws:
            rows.append(f"| {g.get('name') or g.get('id','')} | {g.get('type','')} | "
                        f"{g.get('plc_ip','')} | {g.get('interval_ms','')} | "
                        f"{'Yes' if g.get('running') else 'No'} |")
        return "\n".join(rows)
    if tool_name == "list_recent_alarms":
        al = result.get("alarms") or []
        if not al:
            return f"No alarms since {result.get('since','the requested window')}."
        rows = [f"**Recent alarms** — {len(al)}", "", "| Time | Level | Source | Message |", "|---|---|---|---|"]
        for a in al[:50]:
            rows.append(f"| {a.get('ts_utc','')} | {a.get('level','')} | "
                        f"{a.get('gateway_name') or a.get('category','')} | {str(a.get('message','')).replace('|','/')} |")
        return "\n".join(rows)

    tag = result.get("tag") or "tag"
    gw = result.get("gateway_name")
    frm, to = result.get("from"), result.get("to")

    # No data in the requested window — say so plainly (charts + summaries).
    if (result.get("count") == 0) or (is_chart and not result.get("series")):
        gwt = f" @ {gw}" if gw else ""
        return (f"**{tag}**{gwt} — no readings in {frm} → {to}.\n\n"
                f"The gateway may not have been collecting this tag in that "
                f"window. Try a wider window, or check that the gateway is running.")

    if is_chart and result.get("series"):
        pts = result.get("series") or []
        chart_json = _json.dumps({
            "tag": tag, "gateway_name": gw, "from": frm, "to": to, "series": pts,
        }, default=str)
        head = f"**{tag}**{f' @ {gw}' if gw else ''} — {len(pts)} points"
        stats = []
        for k, lbl in (("min", "Min"), ("max", "Max"), ("avg", "Mean")):
            if result.get(k) is not None:
                stats.append(f"{lbl} {_fmt_num(result.get(k))}")
        substat = ("  ·  " + "  ·  ".join(stats)) if stats else ""
        return f"{head}{substat}\n\n```trustnode-chart\n{chart_json}\n```"

    # Summary / bucketed table.
    lines = [f"**{tag}**{f' @ {gw}' if gw else ''}", "", "| Metric | Value | Unit |", "|---|---|---|"]
    if frm and to:
        lines.append(f"| Period | {frm} → {to} |  |")
    if result.get("count") is not None:
        lines.append(f"| Samples | {_fmt_num(result.get('count'))} | readings |")
    for k, lbl in (("last", "Last"), ("min", "Min"), ("max", "Max"),
                   ("avg", "Mean"), ("stddev", "Stddev")):
        if result.get(k) is not None:
            lines.append(f"| {lbl} | {_fmt_num(result.get(k))} |  |")
    return "\n".join(lines)
