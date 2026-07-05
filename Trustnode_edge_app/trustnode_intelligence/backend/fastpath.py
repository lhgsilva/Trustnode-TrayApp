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

# Chart-TYPE intent. The user can ask for a specific visualization; default is
# a line trend. 'bar'/'column'/'histogram' -> bar; 'pie'/'donut'/'doughnut' ->
# donut. We only switch the type — the data query is chosen by the question.
_BAR_RE = re.compile(r"\b(bar|column|histogram)\s*(chart|graph|plot)?\b|\bas\s+bars?\b", re.I)
_DONUT_RE = re.compile(r"\b(pie|donut|doughnut)\s*(chart|graph)?\b", re.I)
# Category / breakdown intent (the natural source for a donut, but also usable
# as a bar). We detect WHAT to break down by.
_SHARE_RE = re.compile(r"\b(share|breakdown|proportion|percentage|percent|distribution|split|how much of|what (?:part|fraction)|makeup|composition)\b", re.I)
_PER_TAG_RE = re.compile(r"\b(per|by|each|for every|across)\s+tags?\b|\btags?\b.*\b(share|breakdown|proportion|distribution)\b|\breadings?\s+per\s+tag\b", re.I)
_PER_GW_RE = re.compile(r"\b(per|by|each|for every|across)\s+(gateway|gateways|plc|plcs|device|devices)\b|\b(gateway|gateways|plc|device)\b.*\b(share|breakdown|proportion|distribution|volume)\b", re.I)
_QUALITY_RE = re.compile(r"\b(quality|good|bad|uncertain)\b.*\b(share|breakdown|proportion|distribution|split|percentage|percent|donut|pie|how much)\b|\b(share|breakdown|proportion|distribution|how much)\b.*\bquality\b|\bgood\s*/\s*bad\b|\bquality\s+(breakdown|distribution|split)\b", re.I)
# value-band intent: "how often was X above 100", "time in band", "between A and B"
_BAND_RE = re.compile(r"\b(how (?:often|much|long)|time (?:in|spent)|percentage of (?:time|readings)|distribution of values?)\b", re.I)

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
# "last N readings" AND "N last readings" AND "last N points/samples" — the
# count may come BEFORE or AFTER 'last' (operators phrase it both ways). Also
# accept a bare "last N" with no unit-word right before the tag list.
_LAST_N_RE = re.compile(
    r"\b(?:last\s+(\d+)\s+(?:reading|readings|points?|samples?)"
    r"|(\d+)\s+last\s+(?:reading|readings|points?|samples?)"
    r"|(?:the\s+)?last\s+(\d+)\s+(?:reading|readings|points?|samples?))\b",
    re.I)


def _last_n_readings(text: str):
    """If the user asked for the last N readings/points/samples, return N,
    else None. Handles 'last 20 readings' and '20 last readings'."""
    m = _LAST_N_RE.search(text)
    if not m:
        return None
    for g in m.groups():
        if g:
            try:
                return max(1, min(5000, int(g)))
            except Exception:
                return None
    return None

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


# Cached set of real tag names (normalized) so the fast-path can VALIDATE that
# an extracted token is actually a tag before running a query on it — otherwise
# plain English words ('each','tank','detailed') get mis-read as tags (audit
# 1g). Refreshed every 60s. On any failure, returns None and _extract_tag
# falls back to shape-only heuristics (never worse than before).
_TAGSET_CACHE = {"at": 0.0, "norm": None, "raw": None}
_TAGSET_TTL = 60.0


def _norm_tag(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _known_tags():
    """(normalized_set, raw_list) of real tag names, cached; or (None, None)."""
    import time as _t
    now = _t.monotonic()
    if _TAGSET_CACHE["norm"] is not None and (now - _TAGSET_CACHE["at"]) < _TAGSET_TTL:
        return _TAGSET_CACHE["norm"], _TAGSET_CACHE["raw"]
    try:
        from .tools._scope import all_tag_names
        raw = list(all_tag_names() or [])
        norm = {_norm_tag(t) for t in raw}
        _TAGSET_CACHE.update(at=now, norm=norm, raw=raw)
        return norm, raw
    except Exception:
        return None, None


def _looks_like_real_tag(tok: str) -> bool:
    """True if tok exactly/normalized-matches a real tag, or is a substring of
    one (abbreviations like 'PVA' -> 'BT_PVA_Level'). If the tag set can't be
    read, fall back to True for strongly-shaped tokens only (handled by caller)."""
    norm, _raw = _known_tags()
    if not norm:
        return True  # can't validate — don't block; caller uses shape heuristic
    tn = _norm_tag(tok)
    if tn in norm:
        return True
    # substring match against any real tag (>=3 chars to avoid noise)
    if len(tn) >= 3 and any(tn in n for n in norm):
        return True
    return False


def _extract_tag(text: str) -> Optional[str]:
    """Pull a candidate tag token from the message — but only if it plausibly
    corresponds to a REAL tag, so plain English words aren't mis-read as tags.
    Returns None when nothing tag-like is found (→ caller defers to the AI)."""
    tokens = list(re.finditer(r"[A-Za-z][A-Za-z0-9_]*(?:\[\d+\])?", text))
    norm, _raw = _known_tags()
    # Pass 1: strongly tag-shaped — bracket, underscore, or embedded digit.
    # These are almost always real tags; accept without validation.
    for m in tokens:
        tok = m.group(0)
        if tok.lower() in _STOP:
            continue
        if "[" in tok or "_" in tok or any(c.isdigit() for c in tok):
            return tok
    # Pass 2: a word/abbreviation — accept ONLY if it matches a real tag name
    # (exact, normalized, or as an abbreviation substring). This stops 'each',
    # 'tank', 'detailed', 'information' from being treated as tags.
    for m in tokens:
        tok = m.group(0)
        low = tok.lower()
        if low in _STOP:
            continue
        if len(tok) < 2:
            continue
        if _looks_like_real_tag(tok):
            return tok
    # Pass 3: quoted tag (user explicitly quoted it → trust it).
    m = re.search(r"['\"]([^'\"]{2,40})['\"]", text)
    if m:
        return m.group(1)
    return None


def _extract_tags(text: str, limit: int = 6) -> list:
    """Pull MULTIPLE candidate tag tokens, in order, de-duplicated. Used for
    multi-series charts ('trend BT_PVA_Level, BT_PVB_Level, BT_PVC_Level').
    Only collects strongly tag-shaped tokens (bracket / underscore / embedded
    digit) so plain English words don't get mistaken for tags — for a single
    plain word we still fall back to _extract_tag. Returns [] if none."""
    out = []
    seen = set()
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_]*(?:\[\d+\])?", text):
        tok = m.group(0)
        if tok.lower() in _STOP:
            continue
        # Strongly tag-shaped only (avoid grabbing 'chart', 'readings', etc.).
        if "[" in tok or "_" in tok or any(c.isdigit() for c in tok):
            key = re.sub(r"[^a-z0-9]", "", tok.lower())
            if key not in seen:
                seen.add(key)
                out.append(tok)
        if len(out) >= limit:
            break
    return out


def _window_from(text: str) -> str:
    m = _WINDOW_RE.search(text)
    if m:
        n, unit = m.group(1), m.group(2).lower()[0]
        return f"-{n}{unit}"
    n = _last_n_readings(text)
    if n is not None:
        # "last N readings" — size the window to comfortably include N points.
        # At ~1 reading/sec that's N seconds; pad generously (×3, min 60s). The
        # tool then keeps the most-recent N via max_points, so the chart shows
        # exactly N points even if the window caught more.
        secs = max(60, n * 3)
        return f"-{secs}s"
    # "last minute / hour / day / week" (no number) + "today" / "past hour".
    bare = re.search(r"\b(?:last|past|previous|this)\s+(minute|hour|day|week|month)\b", text, re.I)
    if bare:
        u = bare.group(1).lower()
        return {"minute": "-1m", "hour": "-1h", "day": "-1d", "week": "-7d", "month": "-30d"}[u]
    if re.search(r"\btoday\b", text, re.I):
        return "-1d"
    if re.search(r"\byesterday\b", text, re.I):
        return "-2d"
    return "-1h"


def _window_from_or(text: str, default: str) -> str:
    """Like _window_from but use `default` when no explicit window is present
    (breakdowns default to a wider window than trends)."""
    # Reuse _window_from, but only override its "-1h" fallback.
    has_explicit = bool(
        _WINDOW_RE.search(text)
        or _last_n_readings(text) is not None
        or re.search(r"\b(?:last|past|previous|this)\s+(minute|hour|day|week|month)\b", text, re.I)
        or re.search(r"\b(today|yesterday)\b", text, re.I)
    )
    w = _window_from(text)
    if not has_explicit and w == "-1h":
        return default
    return w


# Operator 2026-07-03: broadened these so natural phrasings route to the
# instant catalog tools instead of being misread as a tag lookup. Previously
# "give me detailed information about the current gateway" started with
# "give me" (not in the old list|show|what|which set), so it fell through,
# grabbed "detailed" as a tag, found nothing, and punted to High Effort. Now
# ANY request-ish verb OR a bare mention of the entity triggers the listing —
# gateways/tags/alarms are pure data and must ALWAYS be instant, no AI.
# "Live intent" — the user wants what is ACTIVE NOW, not the whole catalog.
_LIVE_RE = re.compile(r"\b(live|running|active|now|currently|being collected|collecting|online|real[\- ]?time)\b", re.I)
# CONDITION / complex-filter intent — a data question the deterministic
# fast-path can't answer precisely ("show X WHEN Y was above 50", "trend X
# WHILE the machine was running", "only when quality was bad"). These need the
# AI tool-loop (it has detect_threshold + can reason about the condition), so
# classify() returns None and lets the full AI handle it instead of giving a
# wrong plain answer.
_CONDITION_RE = re.compile(
    r"\bwhen\b|\bwhile\b|\bduring\b|\bwhenever\b|\bif\b|\bas long as\b|"
    r"\b(above|below|over|under|exceed|greater than|less than|between)\b.*\b\d|"
    r"\bwas\s+(above|below|over|under|at|equal)\b",
    re.I)
# Time-window phrases that use 'over'/'under' innocently ("over the last 6
# hours", "over time", "under 5 minutes ago"). We strip these BEFORE the
# condition test so a plain trend with a window isn't mistaken for a threshold
# condition (which would wrongly defer it to the AI).
_TIMEWORD = r"(?:second|seconds|sec|secs|minute|minutes|min|mins|hour|hours|hr|hrs|day|days|week|weeks|month|months)"
_TIME_PHRASE_RE = re.compile(
    rf"\bover\s+time\b"
    rf"|\b(?:over|under|above|below|within|in|for)\s+the\s+(?:last|past|previous|next)\s+\d+\s*{_TIMEWORD}\b"
    rf"|\b(?:over|for|within|in)\s+(?:the\s+)?(?:last|past|previous|next)?\s*\d+\s*{_TIMEWORD}\b",
    re.I)


def _has_condition(text: str) -> bool:
    """True if the text carries a real threshold/condition filter — after
    removing innocent time-window phrases ('over the last 6 hours')."""
    cleaned = _TIME_PHRASE_RE.sub(" ", text)
    return bool(_CONDITION_RE.search(cleaned))
# "Compare intent" — wants correlation/comparison analysis, not just an overlay.
_COMPARE_RE = re.compile(r"\b(compare|comparison|correlat|relationship|relate|versus|vs\.?|against|side by side|analyz?e|analys)\b", re.I)
_UNIT = r"(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)"
# EXPLICIT bucket phrase — a grouping keyword immediately before the number.
# This must win over a bare time that belongs to the RANGE ("last 30 minutes").
_BUCKET_EXPLICIT_RE = re.compile(
    rf"\b(?:group(?:ed)?\s+(?:by|in|into)|every|per|each|bucket(?:ed|s)?(?:\s+of)?|interval(?:\s+of)?|resolution(?:\s+of)?)\s+(\d+)\s*{_UNIT}\b",
    re.I)
# A number+unit that appears right before/after the word 'bucket' (e.g. "10s buckets").
_BUCKET_TRAILING_RE = re.compile(rf"\b(\d+)\s*{_UNIT}\s+buckets?\b", re.I)
_BUCKET_NAMED = {
    "secondly": "1s", "minutely": "1m", "hourly": "1h", "daily": "1d",
}


# Valid bucket sizes the tool actually honors (must match analytics.BUCKET_SECONDS).
_VALID_BUCKET_SECONDS = {"1s":1,"5s":5,"10s":10,"30s":30,"1m":60,"5m":300,"15m":900,"1h":3600,"1d":86400}


def _snap_bucket(label: str) -> str:
    """Snap any 'Nu' bucket to the nearest VALID enum bucket, so the tool never
    silently substitutes a different size (audit 1c). '45s'->'30s', '20m'->'15m',
    '3h'->'1h', etc. Already-valid labels pass through."""
    if label in _VALID_BUCKET_SECONDS:
        return label
    m = re.match(r"(\d+)\s*([smhd])", str(label).lower())
    if not m:
        return "auto"
    n = int(m.group(1)); u = m.group(2)
    secs = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]
    # nearest by absolute difference
    best = min(_VALID_BUCKET_SECONDS.items(), key=lambda kv: abs(kv[1] - secs))
    return best[0]


def _agg_from(text: str) -> str:
    """Pick the aggregation the user asked for (min/max/avg/count), default avg."""
    t = text.lower()
    if re.search(r"\bmax(imum)?\b", t): return "max"
    if re.search(r"\bmin(imum)?\b", t): return "min"
    if re.search(r"\bsum|total\b", t): return "sum"
    if re.search(r"\bcount\b|how many\b|number of\b", t): return "count"
    return "avg"


def _chart_type_from(text: str) -> str:
    """Explicit chart type the user asked for: 'bar' | 'donut' | 'line' (default)."""
    if _DONUT_RE.search(text):
        return "donut"
    if _BAR_RE.search(text):
        return "bar"
    return "line"


def _bands_from(text: str):
    """Extract numeric band edges from phrases like 'above 100', 'between 100
    and 150', 'over 50 under 80', '100, 150'. Returns a sorted list or None."""
    nums = []
    for m in re.finditer(r"(?:above|over|below|under|between|and|greater than|less than|>=?|<=?|,)\s*(-?\d+(?:\.\d+)?)", text, re.I):
        try:
            nums.append(float(m.group(1)))
        except Exception:
            pass
    edges = sorted(set(nums))
    return edges or None


def _bucket_label_from(text: str):
    """Extract the time-BUCKET the user asked for ('5s','1m','1h'...), or None.
    Prefers an explicit grouping phrase ('group by 1 minute', 'every 5s',
    '10s buckets') so a range like 'last 30 minutes' is NOT mistaken for it."""
    for rx in (_BUCKET_EXPLICIT_RE, _BUCKET_TRAILING_RE):
        m = rx.search(text)
        if m:
            n, u = m.group(1), m.group(2).lower()
            ub = "s" if u.startswith("s") else ("m" if u.startswith("m") else ("h" if u.startswith("h") else "d"))
            return f"{n}{ub}"
    for word, lab in _BUCKET_NAMED.items():
        if re.search(rf"\b{word}\b", text, re.I):
            return lab
    return None

# "Latest VALUES per tag" — wants values, not a name list. Matches "latest/
# current reading(s)/value(s) ... for/of every/all/each tag(s)", "current
# values of all tags", "what is everything reading now".
_LIVE_VALUES_RE = re.compile(
    r"(?:\b(latest|current|last|live|most recent)\b.*\b(reading|readings|value|values)\b.*\b(every|all|each|the)\b.*\btags?\b)"
    r"|(?:\b(reading|readings|value|values)\b.*\bfor (every|all|each)\b.*\btags?\b)"
    r"|(?:\b(current|latest|live)\s+values?\b)"
    r"|(?:\bwhat\b.*\b(everything|all tags?)\b.*\b(reading|reads?|value)\b)",
    re.I)
_REQ = r"(list|show|what|which|give|tell|display|info|information|details?|describe|current|running|status|all|my)"
_LIST_TAGS_RE = re.compile(rf"\b{_REQ}\b.*\btags?\b", re.I)
_LIST_GW_RE = re.compile(rf"\b{_REQ}\b.*\b(gateway|gateways|plc|plcs|device|devices)\b", re.I)
_LIST_ALARMS_RE = re.compile(rf"\b{_REQ}\b.*\b(alarm|alarms|alert|alerts|events?)\b", re.I)


def classify(user_message: str) -> Optional[Dict[str, Any]]:
    """Return {tool, args, kind} for a recognized fast-path, else None.

    Returns None (→ full AI tool-loop) whenever the request carries a CONDITION
    or complex filter the deterministic path can't answer precisely, so the AI
    (which can reason + use detect_threshold/get_bucketed_series) handles it
    instead of the fast-path forcing a wrong plain answer.
    """
    text = user_message.strip()
    if len(text) > 200:
        return None  # long/complex → full loop
    # Conditional / filtered data question → let the AI reason about it. Uses
    # _has_condition so an innocent time window ("over the last 6 hours") is NOT
    # mistaken for a threshold condition.
    if _has_condition(text):
        return None

    # Operator 2026-07-03 (LIVE INTENT): if the question is about what is LIVE /
    # RUNNING / ACTIVE / being collected right NOW, pass live_only so the tool
    # proves recency from the historian instead of dumping the whole configured
    # catalog. "list all tags" (no live word) still returns the full catalog.
    live_only = bool(_LIVE_RE.search(text))
    _largs = {"live_only": True} if live_only else {}

    # Operator 2026-07-05 (LATEST VALUES): "latest reading/value for every/all
    # tag(s)", "current values", "what is everything reading now" wants the
    # actual VALUES per tag, not a name list. Must fire BEFORE list_tags (the
    # query contains 'tags'). get_live_values returns value+timestamp per tag.
    if _LIVE_VALUES_RE.search(text):
        return {"kind": "live_values", "tool": "get_live_values", "args": {}, "is_chart": False}

    # Operator 2026-07-06 (DONUT / CATEGORY BREAKDOWN): a pie/donut request, or a
    # "share/breakdown/distribution … by tag/gateway/quality" question, maps to
    # get_category_breakdown (categorical slices), NOT a time-series. Must run
    # BEFORE list_tags/list_gateways (those phrasings also contain 'tags'/
    # 'gateways'). We infer the `by` dimension from the wording.
    _wants_donut = bool(_DONUT_RE.search(text))
    _multi = _extract_tags(text)
    if _wants_donut or _QUALITY_RE.search(text) or (_SHARE_RE.search(text) and (_PER_TAG_RE.search(text) or _PER_GW_RE.search(text))):
        frm = _window_from_or(text, "-24h")
        # quality breakdown
        if _QUALITY_RE.search(text) or re.search(r"\bquality\b", text, re.I):
            _t = _extract_tag(text)
            args = {"by": "quality", "from_": frm, "to": "now"}
            if _t:
                args["tag"] = _t
            return {"kind": "breakdown", "tool": "get_category_breakdown", "args": args, "is_chart": True}
        # value bands ("how often was X above/between …") for a single tag
        _bands = _bands_from(text)
        if _bands and _extract_tag(text) and (_BAND_RE.search(text) or _wants_donut):
            return {"kind": "breakdown", "tool": "get_category_breakdown",
                    "args": {"by": "value_bands", "tag": _extract_tag(text), "bands": _bands,
                             "from_": frm, "to": "now"}, "is_chart": True}
        # by gateway
        if _PER_GW_RE.search(text):
            return {"kind": "breakdown", "tool": "get_category_breakdown",
                    "args": {"by": "by_gateway", "from_": frm, "to": "now"}, "is_chart": True}
        # by tag (default donut dimension) — optionally limited to named tags
        args = {"by": "by_tag", "from_": frm, "to": "now"}
        if re.search(r"\bby value|sum|total value|amount\b", text, re.I):
            args["measure"] = "value"
        if len(_multi) >= 2:
            args["tags"] = _multi
        return {"kind": "breakdown", "tool": "get_category_breakdown", "args": args, "is_chart": True}

    # Operator 2026-07-06 (BAR COMPARE): "bar chart of the avg/min/max of A, B, C"
    # — one bar per tag. Needs >=2 tags AND an explicit bar request (else a
    # multi-tag ask is a line overlay, handled below).
    if _BAR_RE.search(text) and len(_multi) >= 2 and not _bucket_label_from(text):
        return {"kind": "aggregate", "tool": "aggregate_tags",
                "args": {"tags": _multi, "from_": _window_from(text), "to": "now",
                         "agg": _agg_from(text)}, "is_chart": True}

    # Catalog listings — pure data, no tag needed. These are common and should
    # be instant (the old full-loop made 'list tags' take several seconds).
    if _LIST_GW_RE.search(text) and not _LIST_TAGS_RE.search(text):
        return {"kind": "list", "tool": "list_gateways", "args": dict(_largs), "is_chart": False}
    if _LIST_TAGS_RE.search(text):
        return {"kind": "list", "tool": "list_tags", "args": dict(_largs), "is_chart": False}
    if _LIST_ALARMS_RE.search(text):
        return {"kind": "list", "tool": "list_recent_alarms", "args": {}, "is_chart": False}

    tag = _extract_tag(text)
    if not tag:
        return None

    is_chart = bool(_CHART_RE.search(text))
    frm = _window_from(text)
    # If the user asked for "last N readings", cap the chart to N points so it
    # shows exactly what they asked (not the default 200). Else default 200.
    _n_read = _last_n_readings(text)
    _max_pts = _n_read if _n_read else 200

    # Operator 2026-07-03 (MULTI-SERIES CHART): if the user named MORE THAN ONE
    # tag and wants a chart ("trend BT_PVA_Level, BT_PVB_Level, BT_PVC_Level in
    # the same chart"), route to the multi-tag tool so all series overlay on one
    # chart. The renderer already accepts the multi-series shape natively and
    # auto-splits axes when ranges differ. Previously only the FIRST tag was
    # extracted, so only one series was plotted.
    multi_tags = _extract_tags(text)
    if len(multi_tags) >= 2:
        # Operator 2026-07-03 (COMPARE + CORRELATE): if the user wants to
        # COMPARE / CORRELATE the tags, or specifies a time BUCKET, route to
        # compare_tags (same-grid bucketing + Pearson correlation + insights).
        # This produces its OWN chart, so it doesn't require a 'chart'/'trend'
        # word. A plain overlay (chart word, no compare intent) still uses the
        # lightweight multi-tag timeseries.
        wants_compare = bool(_COMPARE_RE.search(text))
        bucket = _bucket_label_from(text)
        if wants_compare or bucket:
            return {"kind": "comparison", "tool": "compare_tags",
                    "args": {"tags": multi_tags, "from_": frm, "to": "now",
                             "bucket": bucket or "auto"},
                    "is_chart": True}
        if is_chart:
            return {"kind": "multichart", "tool": "get_multi_tag_timeseries",
                    "args": {"tags": multi_tags, "from_": frm, "to": "now",
                             "max_points": _max_pts},
                    "is_chart": True}

    # Bucketed ("average every 5 minutes", "grouped by 1 hour", "hourly avg").
    # Operator 2026-07-05: also use _bucket_label_from so single-tag bucketing
    # catches "grouped by / interval / resolution / hourly" (was multi-tag only,
    # audit 1b). Snap non-standard buckets (45s, 20m, 3h) to the nearest valid
    # one so the tool doesn't SILENTLY substitute (audit 1c).
    bucket = _bucket_label_from(text)
    bm = _BUCKET_RE.search(text)
    if not bucket and bm:
        n, unit = bm.group(2), bm.group(3).lower()
        ubase = "s" if unit.startswith("s") else ("m" if unit.startswith("m") else "h")
        bucket = f"{n}{ubase}"
    # The user may want this drawn as BARS ('bar chart of hourly average').
    _ctype = _chart_type_from(text)
    # If an explicit bucket/grain was requested, that IS a bucketed request —
    # fire it regardless of whether the word 'average' or 'chart' appears
    # ("show X grouped by 1 hour" is bucketed even without those words).
    # A bar request over a single tag ALSO implies bucketing (bars need discrete
    # buckets, not ~200 raw points) — default to 'auto' when bars asked w/o grain.
    if bucket or _ctype == "bar":
        snapped = _snap_bucket(bucket) if bucket else "auto"
        return {"kind": "bucketed", "tool": "get_bucketed_series",
                "args": {"tag": tag, "from_": frm, "to": "now", "bucket": snapped, "agg": _agg_from(text)},
                "is_chart": True, "chart_type": ("bar" if _ctype == "bar" else "line")}

    if is_chart:
        return {"kind": "chart", "tool": "get_tag_timeseries",
                "args": {"tag": tag, "from_": frm, "to": "now",
                         "max_points": _max_pts},
                "is_chart": True, "chart_type": _ctype}

    if _LAST_RE.search(text) or _AVG_RE.search(text) or _VALUE_RE.search(text):
        return {"kind": "summary", "tool": "get_tag_summary",
                "args": {"tag": tag, "from_": frm, "to": "now"},
                "is_chart": False}

    return None


# Windows we treat as "narrow" for auto-widen (seconds). If the user asked for
# a short recent window and it's empty, collection may have paused moments ago —
# a smart assistant widens to find the freshest data instead of dead-ending.
_NARROW_WINDOW_S = 15 * 60  # 15 minutes


def _window_seconds(frm: str) -> Optional[int]:
    """Parse a '-Nu' relative window into seconds, else None (absolute/ISO)."""
    m = re.match(r"-\s*(\d+)\s*([smhdw])\s*$", str(frm or "").strip(), re.I)
    if not m:
        return None
    n = int(m.group(1))
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2).lower()]


def _result_is_empty(result: dict, is_chart: bool) -> bool:
    """True when a tag query came back with no data for the window."""
    if not isinstance(result, dict):
        return True
    if result.get("count") == 0:
        return True
    if is_chart and not (result.get("series")):
        return True
    return False


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

    # SMART AUTO-WIDEN (understand intent): the user asked for a SHORT recent
    # window ("last minute", "last 20 readings") but it came back empty — usually
    # because collection paused/stopped minutes ago, so the newest reading sits
    # just outside their window. A person wouldn't say "no data" and stop; they'd
    # show the most recent data and note it's a bit stale. We retry ONCE at a
    # wider window (1h → 1d), and if that has data, return it flagged with
    # `widened_from`/`widened_to` so render_local tells the user we widened.
    # Only applies to single-tag data tools with a narrow relative window; never
    # touches a query that already returned data, and never widens explicit long
    # windows (if you ask for "last minute" and mean it, an empty 1-min result is
    # only widened when the tag genuinely has NO recent data at that grain).
    if (not result.get("error")
            and plan["tool"] in ("get_tag_timeseries", "get_tag_summary", "get_bucketed_series")
            and "tag" in (plan.get("args") or {})
            and _result_is_empty(result, plan.get("is_chart", False))):
        orig_frm = plan["args"].get("from_")
        win_s = _window_seconds(orig_frm)
        if win_s is not None and win_s <= _NARROW_WINDOW_S:
            for wider in ("-1h", "-1d", "-7d"):
                if (_window_seconds(wider) or 0) <= win_s:
                    continue
                wargs = dict(plan["args"])
                wargs["from_"] = wider
                # For bucketed queries, a 1-min bucket over 1d is absurd — let the
                # tool pick a sensible grain by switching to 'auto' when widening.
                if plan["tool"] == "get_bucketed_series":
                    wargs["bucket"] = "auto"
                wresult = run_tool(plan["tool"], wargs, {"data_source": data_source})
                if isinstance(wresult, dict) and not _result_is_empty(wresult, plan.get("is_chart", False)):
                    wresult["widened_from"] = orig_frm
                    wresult["widened_to"] = wider
                    result = wresult
                    break

    # Stamp the requested chart_type onto the result so render_local (and the
    # AI narrator) can emit the right shape. Tools that already set chart_type
    # (breakdown='donut', aggregate='bar') keep theirs; time-series get the
    # type the classifier picked ('line' default, or 'bar' when asked).
    if isinstance(result, dict) and not result.get("chart_type"):
        result["chart_type"] = plan.get("chart_type", "line")

    # DECISION: does this question need AI interpretation, or just the data?
    # If the user asked a plain data question (no 'why/stable/anomaly/trend'
    # words), we can render a deterministic answer in CODE — instant, no AI.
    wants_ai = bool(_INTERPRET_RE.search(user_message))
    return {"tool_result": result, "tool_name": plan["tool"],
            "is_chart": plan.get("is_chart", False), "disambiguation": None,
            "wants_ai": wants_ai, "chart_type": plan.get("chart_type", "line")}


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

    # ---- Category breakdown → DONUT/PIE (slices) ----
    if tool_name == "get_category_breakdown" or result.get("kind") == "breakdown":
        slices = [s for s in (result.get("slices") or []) if s.get("value")]
        by = result.get("by")
        total = result.get("total")
        frm, to = result.get("from"), result.get("to")
        _by_lbl = {"tag": "by tag", "gateway": "by gateway", "quality": "quality",
                   "value_bands": "value bands"}.get(by, by or "")
        title_tag = f" — {result.get('tag')}" if result.get("tag") and by in ("quality", "value_bands") else ""
        if not slices:
            return (f"**Breakdown {_by_lbl}{title_tag}** — no data in {frm} → {to}.\n\n"
                    f"Nothing was collected in that window to break down.")
        chart_obj = {"chart_type": "donut", "by": by, "from": frm, "to": to,
                     "total": total,
                     "slices": [{"label": s.get("label"), "value": s.get("value"),
                                 "pct": s.get("pct")} for s in slices]}
        head = f"**Breakdown {_by_lbl}{title_tag}** — {len(slices)} categor{'y' if len(slices)==1 else 'ies'}"
        if total:
            head += f"  ·  {_fmt_num(total)} readings total"
        lines = [head, "", "```trustnode-chart", _json.dumps(chart_obj, default=str), "```", "",
                 "| Category | Value | Share |", "|---|---|---|"]
        for s in slices:
            pct = s.get("pct")
            lines.append(f"| {s.get('label','')} | {_fmt_num(s.get('value'))} | "
                         f"{(str(pct)+'%') if pct is not None else '—'} |")
        return "\n".join(lines)

    # ---- Per-tag aggregate → BAR (one bar per tag) ----
    if tool_name == "aggregate_tags" or result.get("kind") == "aggregate":
        slices = [s for s in (result.get("slices") or []) if s.get("value") is not None]
        agg = result.get("agg") or "avg"
        frm, to = result.get("from"), result.get("to")
        if not slices:
            return (f"**{agg} per tag** — no data in {frm} → {to}.\n\n"
                    f"None of the requested tags had readings in that window.")
        chart_obj = {"chart_type": "bar", "agg": agg, "from": frm, "to": to,
                     "slices": [{"label": s.get("label"), "value": s.get("value")} for s in slices]}
        head = f"**{agg.capitalize()} per tag** — {len(slices)} tag{'s' if len(slices)!=1 else ''}"
        lines = [head, "", "```trustnode-chart", _json.dumps(chart_obj, default=str), "```", "",
                 f"| Tag | {agg.capitalize()} | Samples |", "|---|---|---|"]
        for s in slices:
            lines.append(f"| {s.get('label','')} | {_fmt_num(s.get('value'))} | {_fmt_num(s.get('count'))} |")
        return "\n".join(lines)

    # ---- Latest value per tag ----
    if tool_name == "get_live_values":
        vals = result.get("values") or []
        since = result.get("since")
        if not vals:
            return (f"No tags have reported since {since} — nothing is collecting right now.")
        rows = [f"**Latest values** — {len(vals)} live tag{'s' if len(vals)!=1 else ''}"
                + (f" (as of the most recent reading)" if since else ""), "",
                "| Tag | Value | Time | Gateway |", "|---|---|---|---|"]
        for v in vals:
            rows.append(f"| {v.get('tag','')} | {_fmt_num(v.get('value'))} | "
                        f"{v.get('ts_utc','')} | {v.get('gateway_name') or v.get('gateway_id','')} |")
        return "\n".join(rows)

    # ---- Catalog listings (tags / gateways / alarms) ----
    live = bool(result.get("live_only"))
    since = result.get("since")
    if tool_name == "list_tags":
        tags = result.get("tags") or []
        if not tags:
            if live:
                return (f"No tags have reported since {since} — nothing is actively "
                        f"collecting right now.")
            return "No tags are currently being collected."
        title = (f"**Live tags** — {len(tags)} collecting now" + (f" (since {since} UTC)" if since else "")) if live \
                else "**Collected tags** — {} total".format(len(tags))
        rows = [title, "", "| Tag | Gateway |", "|---|---|"]
        for t in tags:
            rows.append(f"| {t.get('tag','')} | {t.get('gateway_name') or t.get('gateway_id','')} |")
        return "\n".join(rows)
    if tool_name == "list_gateways":
        gws = result.get("gateways") or []
        if not gws:
            if live:
                return (f"No gateway has written data since {since} — none are actively "
                        f"running right now.")
            return "No gateways are configured."
        title = (f"**Live gateways** — {len(gws)} running now" + (f" (since {since} UTC)" if since else "")) if live \
                else "**Gateways** — {} total".format(len(gws))
        rows = [title, "",
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

    # ---- Comparison (compare_tags): chart + correlation matrix + insights ----
    if result.get("kind") == "comparison":
        import json as _jc
        frm2, to2 = result.get("from"), result.get("to")
        bkt = result.get("bucket")
        good = [s for s in (result.get("series") or []) if isinstance(s, dict) and (s.get("series") or [])]
        out = []
        tags_lbl = ", ".join(f"**{s.get('tag')}**" for s in good) or "the selected tags"
        out.append(f"**Comparison** — {tags_lbl}")
        out.append(f"_Window {frm2} → {to2}  ·  bucket {bkt}  ·  {result.get('buckets',0)} points per tag_")
        # Chart (multi-series overlay).
        if good:
            out.append("")
            out.append("```trustnode-chart\n" + _jc.dumps({"from": frm2, "to": to2, "series": good}, default=str) + "\n```")
        # Correlation matrix table.
        cors = result.get("correlations") or []
        if cors:
            out.append("")
            out.append("**Correlation (Pearson r)**")
            out.append("")
            out.append("| Tag A | Tag B | r | Strength | Direction | Buckets |")
            out.append("|---|---|---|---|---|---|")
            for c in cors:
                rr = c.get("r")
                out.append(f"| {c.get('tag_a')} | {c.get('tag_b')} | "
                           f"{('%.3f' % rr) if isinstance(rr,(int,float)) else '—'} | "
                           f"{c.get('strength','')} | {c.get('direction') or '—'} | {c.get('n',0)} |")
        # Plain-language insights.
        ins = result.get("insights") or []
        if ins:
            out.append("")
            out.append("**Insights**")
            for line in ins:
                out.append(f"- {line}")
        return "\n".join(out)

    # ---- Multi-series chart (multiple tags overlaid on one chart) ----
    # The multi-tag tool returns {from, to, series:[{tag, gateway_name,
    # series:[{ts,value}]}, ...]}. Detect it by series[0] being a dict that
    # itself carries a nested 'series' list, and emit the multi-shape the
    # chart component renders natively (one line per tag, auto dual-axis).
    _ms = result.get("series")
    if is_chart and isinstance(_ms, list) and _ms and isinstance(_ms[0], dict) and "series" in _ms[0]:
        import json as _json2
        frm2, to2 = result.get("from"), result.get("to")
        # Keep only series that actually have points; note any empties.
        good = [s for s in _ms if isinstance(s, dict) and (s.get("series") or [])]
        empty = [str(s.get("tag") or "?") for s in _ms if not (isinstance(s, dict) and (s.get("series") or []))]
        if not good:
            names = ", ".join(str(s.get("tag") or "?") for s in _ms)
            return (f"**{names}** — no readings in {frm2} → {to2}.\n\n"
                    f"The gateway may not have been collecting these tags in that window.")
        chart_json = _json2.dumps({"from": frm2, "to": to2, "series": good}, default=str)
        tags_lbl = ", ".join(f"**{s.get('tag')}**" for s in good)
        note = f"  ·  (no data: {', '.join(empty)})" if empty else ""
        head = f"{tags_lbl} — {len(good)} series{note}"
        return f"{head}\n\n```trustnode-chart\n{chart_json}\n```"

    tag = result.get("tag") or "tag"
    gw = result.get("gateway_name")
    frm, to = result.get("from"), result.get("to")

    # No data in the requested window — say so plainly (charts + summaries).
    if (result.get("count") == 0) or (is_chart and not result.get("series")):
        gwt = f" @ {gw}" if gw else ""
        return (f"**{tag}**{gwt} — no readings in {frm} → {to}.\n\n"
                f"The gateway may not have been collecting this tag in that "
                f"window. Try a wider window, or check that the gateway is running.")

    # If we auto-widened because the requested short window was empty, prepend a
    # one-line note so the user knows we adjusted the window for them.
    _widen_note = ""
    if result.get("widened_to"):
        _widen_note = (f"_No data in the requested window ({result.get('widened_from')}); "
                       f"showing {result.get('widened_to')} instead — the most recent "
                       f"readings._\n\n")

    if is_chart and result.get("series"):
        pts = result.get("series") or []
        _ct = result.get("chart_type") or "line"
        _chart_payload = {"tag": tag, "gateway_name": gw, "from": frm, "to": to, "series": pts}
        if _ct and _ct != "line":
            _chart_payload["chart_type"] = _ct   # 'bar' → renderer draws bars
        chart_json = _json.dumps(_chart_payload, default=str)
        _kindword = "bars" if _ct == "bar" else "points"
        head = f"{_widen_note}**{tag}**{f' @ {gw}' if gw else ''} — {len(pts)} {_kindword}"
        stats = []
        for k, lbl in (("min", "Min"), ("max", "Max"), ("avg", "Mean")):
            if result.get(k) is not None:
                stats.append(f"{lbl} {_fmt_num(result.get(k))}")
        substat = ("  ·  " + "  ·  ".join(stats)) if stats else ""
        return f"{head}{substat}\n\n```trustnode-chart\n{chart_json}\n```"

    # Summary / bucketed table.
    lines = []
    if _widen_note:
        lines.append(_widen_note.rstrip())
        lines.append("")
    lines += [f"**{tag}**{f' @ {gw}' if gw else ''}", "", "| Metric | Value | Unit |", "|---|---|---|"]
    if frm and to:
        lines.append(f"| Period | {frm} → {to} |  |")
    if result.get("count") is not None:
        lines.append(f"| Samples | {_fmt_num(result.get('count'))} | readings |")
    for k, lbl in (("last", "Last"), ("min", "Min"), ("max", "Max"),
                   ("avg", "Mean"), ("stddev", "Stddev")):
        if result.get(k) is not None:
            lines.append(f"| {lbl} | {_fmt_num(result.get(k))} |  |")
    return "\n".join(lines)
