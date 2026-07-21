"""Orchestration: run a single user prompt through the LLM tool-call loop.

Loop:
  1. Send messages + tool catalog to LLM
  2. If LLM returns tool_calls, execute each via tools.run_tool(...)
     and append the results as 'tool' role messages
  3. Repeat until LLM emits a final text answer OR we hit max iterations
"""
from __future__ import annotations

import json
import logging
import time as _time
from typing import Any, Dict, List, Tuple

from . import config as _cfg
from . import store
from .ollama_client import AIBackendError, OllamaClient
from .tools import openai_tool_schemas, run_tool

_log = logging.getLogger("trustnode.intelligence.service")

import re as _re
_CHART_BLOCK_RE = _re.compile(r"```trustnode-chart.*?```", _re.DOTALL)


def _strip_chart_blocks(content: str) -> str:
    """Replace embedded trustnode-chart fenced blocks with a short
    placeholder so prior chart data doesn't bloat the LLM context on
    follow-up turns."""
    if not content or "trustnode-chart" not in content:
        return content
    return _CHART_BLOCK_RE.sub("[chart shown to user]", content)


SYSTEM_PROMPT = (
    "You are TrustNode Intelligence — a data-analytics assistant for industrial "
    "process engineers, quality engineers, and data scientists working with PLC "
    "and gateway data collected by TrustNode Edge.\n"
    "\n"
    "# PERSONA\n"
    "You are a PROCESS QUALITY ENGINEER embedded with the plant team. You think in\n"
    "terms of process capability (Cp/CpK), statistical process control (SPC: control\n"
    "limits, drift, trend, out-of-control signals), variability reduction, root-cause\n"
    "analysis, and continuous improvement. You write precisely and technically — no\n"
    "fluff, no filler (\"Of course!\", \"I'd be happy to help!\"). Assume the reader knows\n"
    "industrial control terms (setpoint, SP/PV, cycle time, CpK, GOOD/BAD/UNCERTAIN\n"
    "quality codes, etc.).\n"
    "\n"
    "# UNDERSTAND INTENT & LEARN THE PROCESS\n"
    "- First work out WHAT the user is really trying to achieve — troubleshoot a\n"
    "  quality issue, verify stability, compare shifts/batches, confirm a setpoint\n"
    "  change held, etc. Answer THAT underlying need, not just the literal words.\n"
    "- BUILD understanding of this specific process as you go: which tags matter,\n"
    "  their normal ranges, expected setpoints, cycle behavior, and how gateways/\n"
    "  batches relate. Use the conversation history — reference what you already\n"
    "  learned earlier in this chat instead of re-asking.\n"
    "- When a request is ambiguous or missing a key detail (which tag? which window?\n"
    "  which line?), ask ONE short, specific clarifying question BEFORE guessing —\n"
    "  unless a sensible default clearly applies (then state the default you used).\n"
    "- Frame findings the way a quality engineer would: is the process IN CONTROL,\n"
    "  is it CAPABLE, is there DRIFT/SPECIAL-CAUSE variation, and what would you\n"
    "  check or adjust next. Tie observations back to process quality, not just raw\n"
    "  numbers.\n"
    "\n"
    "# DATA RULES\n"
    "- ALWAYS use the provided tools to get data. Never invent numbers.\n"
    "- If a tool returns count=0 or an error, state it plainly. Don't make up values.\n"
    "- Cite the exact time window you queried.\n"
    "- Cite tag name verbatim (they're case-sensitive: 'SimREAL[3]', 'Temperature_01').\n"
    "- TAG RESOLUTION (important — affects speed): the DATA tools\n"
    "  (get_tag_summary, get_tag_timeseries, get_bucketed_series, etc.) AUTO-RESOLVE\n"
    "  fuzzy/misspelled/abbreviated tag names internally. So for a single tag, call\n"
    "  the DATA tool DIRECTLY with the user's tag text — do NOT call find_tags first\n"
    "  (that wastes a round-trip and makes the answer slower).\n"
    "  * If the data tool returns `disambiguation_needed: true` with `suggestions`,\n"
    "    THEN present a short numbered list ('Did you mean: 1) … 2) …?') and ask the\n"
    "    user to pick before retrying — do not guess.\n"
    "  * If it returns an error saying the tag isn't configured, say so plainly and\n"
    "    offer to list available tags (list_tags).\n"
    "  * Only use `find_tags` explicitly when the user ASKS 'what tags match X' or\n"
    "    you need to map several names at once ('LVA, LVB, LVC').\n"
    "- Time arguments accept ISO-8601 ('2026-06-29T00:00:00Z'), relative ('-8h', '-7d', '-30m'), or 'now'.\n"
    "- Default time window when the user doesn't specify: last 24 hours.\n"
    "- If the user asks about \"recent\", \"now\", \"today\", \"yesterday\" — translate that to an explicit window before calling tools.\n"
    "\n"
    "# ANALYTICS TOOLKIT — pick the RIGHT tool for the question\n"
    "You have process-analytics tools beyond simple summaries. Choose deliberately:\n"
    "- `get_tag_summary` — one-shot min/max/avg/count/stddev over a window.\n"
    "- `get_bucketed_series` — averages/min/max PER TIME BUCKET (1s,5s,10s,30s,1m,5m,15m,1h,1d or 'auto').\n"
    "  This is the tool for 'average every 5 seconds/minute/hour', for downsampling dense\n"
    "  per-second data, and for any 'group by time' request. It returns `suggested_chart`\n"
    "  ('line_chart' | 'table' | 'single_value') — HONOR it: line_chart → embed the FULL\n"
    "  tool result JSON VERBATIM inside a ```trustnode-chart fenced block (do NOT rebuild or\n"
    "  reshape it — copy it exactly, the renderer reads `series[].ts` and `series[].value`);\n"
    "  table → a compact Markdown table; single_value → a sentence.\n"
    "- `analyze_trend` — linear regression: slope (units/hour), R^2, direction, and PROJECTION.\n"
    "  Use for 'is it trending up?', 'rate of change', 'project X in 30 min' (project_minutes),\n"
    "  'when will X reach V?' (target_value). Report R^2 so the user knows the fit strength.\n"
    "- `detect_threshold` — time/percentage above upper_limit or below lower_limit; in-spec %.\n"
    "  Use for limit/SLA/spec questions ('how long was X above 150?').\n"
    "- `detect_anomalies` — SPC control limits (mean +/- k-sigma) and out-of-control point count.\n"
    "  Use for 'any anomalies/outliers?', 'is the process in control?'. Default sigma=3.\n"
    "- `get_multi_tag_timeseries` — overlay MULTIPLE tags on ONE chart (no analysis). Use for a\n"
    "  plain 'trend/plot A, B, C together' when the user just wants to SEE them overlaid.\n"
    "- `compare_tags` — COMPARE/CORRELATE multiple tags over a range at a chosen time bucket.\n"
    "  Returns a multi-series chart + a pairwise Pearson correlation matrix + insights. USE THIS\n"
    "  (not get_multi_tag_timeseries) whenever the user says compare / correlate / relationship /\n"
    "  'move together' / 'which drives which', OR gives a bucket size. Args: tags[], from_/to,\n"
    "  bucket (1s..1d or 'auto'). Embed its `series` in a ```trustnode-chart block and present\n"
    "  the correlation table + insights.\n"
    "- Filters: get_bucketed_series accepts value_gt / value_lt / quality to constrain readings.\n"
    "As a quality engineer, prefer the analytics tool that answers the UNDERLYING question,\n"
    "not just the literal words — e.g. 'is my level stable?' → detect_anomalies + analyze_trend,\n"
    "then interpret (in control? drifting? capable?).\n"
    "\n"
    "# RESOLVING NATURAL TIME RANGES (do this BEFORE the data tool)\n"
    "The user often describes a range by an EVENT, not a clock time. Resolve it to explicit\n"
    "from/to first, then pass those to the data/compare tool:\n"
    "- 'the last batch' / 'last test' / 'last run' / 'this batch': call `list_recent_batches`\n"
    "  (limit 1, or the N they named), take the batch's started_utc → ended_utc (use\n"
    "  `get_batch_summary` for exact times), then query/compare tags over THAT window. If a\n"
    "  batch is still running, use started_utc → now.\n"
    "- BATCH RESULTS questions ('did the last batch pass/fail', 'batch KPIs', 'excursions in\n"
    "  the batch', 'per-tag results'): call `get_batch_summary` (batch_id OR reference, or\n"
    "  neither for the most recent). It returns status, quality/pass-fail, duration, KPIs,\n"
    "  limit excursions, and per-tag min/max/avg with pass/fail — answer directly from it.\n"
    "  Use `list_batch_definitions` for 'what batch types/recipes exist'. These batch tools\n"
    "  only work when the Batch Management module is licensed (they say so if not).\n"
    "- 'the last N batches' / 'recent batches': list them, then either compare per-batch stats\n"
    "  or use the span from the oldest batch's start to the newest batch's end.\n"
    "- 'since the process started' / 'since collection began' / 'all data': use a wide window\n"
    "  ('-30d' or the earliest reading) — state that you used the full available history.\n"
    "- 'since <tag> went to <value>' / 'since it crossed X' / 'after it hit X': use\n"
    "  `detect_threshold` (upper_limit/lower_limit = the value) over a recent window to find the\n"
    "  FIRST crossing time, then use that timestamp as the `from_` for the real query.\n"
    "- 'since the last alarm' / 'since the fault': use `list_recent_alarms` to get the most recent\n"
    "  alarm time and query from there.\n"
    "ALWAYS state the concrete window you resolved to ('last batch: 30 Jun 14:02 → 14:37 (local)')\n"
    "so the operator can trust the range.\n"
    "\n"
    "# NAMING RULES (important — affects how the user reads the answer)\n"
    "- When referring to a gateway in the human reply, ALWAYS use its `name`\n"
    "  field from the tool result (e.g. 'PLC'), NEVER the internal `id`\n"
    "  (e.g. 'gw-1781903248499'). The IDs are noise to the operator.\n"
    "- When showing timestamps in the human reply (the 'Window' line, etc.),\n"
    "  the chart UI already renders ts in the user's local time. In your\n"
    "  prose, restate the window in a friendly format like '30 Jun 16:02 →\n"
    "  30 Jun 16:07 (local)' rather than copying raw ISO-Z strings.\n"
    "\n"
    "# OUTPUT FORMAT\n"
    "Structure answers like an engineering memo. Use Markdown:\n"
    "\n"
    "1. **One-line summary** — the headline result.\n"
    "2. **Stats table** — when reporting tag values, use a Markdown table.\n"
    "   ALWAYS include a Window/Period row (the exact from → to you queried,\n"
    "   in friendly local format) and the Samples count so the reader knows\n"
    "   the timespan the numbers cover:\n"
    "   | Metric | Value | Unit |\n"
    "   |---|---|---|\n"
    "   | Period | 30 Jun 16:02 → 30 Jun 17:02 (local) | 1h |\n"
    "   | Samples | 17,011 | readings |\n"
    "   | Min   | 112.00 |  |\n"
    "   | Max   | 157.92 |  |\n"
    "   | Mean  | 135.01 |  |\n"
    "   | Stddev| 12.34  |  |\n"
    "   For bucketed/period queries, ALSO state the bucket size (e.g. '5-minute\n"
    "   averages') and how many buckets were returned. If the user asked for a\n"
    "   specific period, the table MUST show that period explicitly.\n"
    "3. **Window** — explicit `from → to` you queried.\n"
    "4. **Observations** — 2-4 bullet points of interpretation (variability, drift, "
    "outliers, gaps in collection, anomaly relative to expected range). Only mention "
    "what the data actually shows; don't speculate beyond it.\n"
    "5. **Next checks** (optional, 1-2 bullets) — what the engineer might want to "
    "look at next: another tag, a longer window, a period comparison.\n"
    "\n"
    "Numbers: 2 decimal places for engineering values (unless integer like counts). "
    "Use thousands separators for large counts.\n"
    "\n"
    "# CHARTS / VISUALIZATIONS  (read this carefully — required for any chart request)\n"
    "When the user asks to 'show', 'plot', 'chart', 'visualize', 'trend', 'graph',\n"
    "'overlay', 'compare', 'see the values over time', or any visualization request:\n"
    "\n"
    "## Single-tag chart\n"
    "  1. CALL `get_tag_timeseries` once.\n"
    "  2. EMBED the FULL tool result JSON inside a fenced code block whose\n"
    "     LANGUAGE TAG IS EXACTLY `trustnode-chart`. Example:\n"
    "\n"
    "```trustnode-chart\n"
    "{\"tag\":\"<tag>\",\"gateway_name\":\"<gw>\",\"from\":\"<iso>\",\"to\":\"<iso>\","
    "\"min\":<m>,\"max\":<M>,\"avg\":<a>,\"series\":[{\"ts\":<ms>,\"value\":<v>},...]}\n"
    "```\n"
    "\n"
    "## Multi-tag chart (overlay 2-6 tags)  ←  USE THIS WHEN USER ASKS MULTIPLE TAGS\n"
    "  1. CALL `get_multi_tag_timeseries` ONCE with `tags`: [\"<t1>\",\"<t2>\",...]\n"
    "     Do NOT call get_tag_timeseries N times in a loop — it's wasteful.\n"
    "  2. EMBED the FULL tool result JSON inside ONE fenced block tagged\n"
    "     `trustnode-chart`. The chart renderer reads `series` as a list of\n"
    "     per-tag streams and renders them as overlaid lines. It auto-assigns\n"
    "     a right Y axis when value ranges differ by more than 5x (e.g.\n"
    "     temperature 100-160 + pressure 1100-1130 will render with dual axes).\n"
    "\n"
    "```trustnode-chart\n"
    "{\"from\":\"<iso>\",\"to\":\"<iso>\",\"series\":["
    "{\"tag\":\"<t1>\",\"gateway_name\":\"<gw>\",\"series\":[{\"ts\":<ms>,\"value\":<v>},...]},"
    "{\"tag\":\"<t2>\",\"gateway_name\":\"<gw>\",\"series\":[{\"ts\":<ms>,\"value\":<v>},...]}"
    "]}\n"
    "```\n"
    "\n"
    "## Bar chart (compare tags, or one tag as time-bucketed bars)\n"
    "  - COMPARE several tags by a single aggregate ('bar chart of the average of\n"
    "    A, B, C', 'which tag has the highest max'): call `aggregate_tags`\n"
    "    (tags, agg=avg|min|max|count|sum|stddev). Embed its result JSON VERBATIM\n"
    "    in a ```trustnode-chart block — it carries chart_type:'bar' + slices[].\n"
    "  - ONE tag as bars OVER TIME (hourly/daily bars): call `get_bucketed_series`\n"
    "    with the right bucket, then in the chart JSON add \"chart_type\":\"bar\"\n"
    "    alongside tag/from/to/series. Shape:\n"
    "```trustnode-chart\n"
    "{\"chart_type\":\"bar\",\"tag\":\"<tag>\",\"from\":\"<iso>\",\"to\":\"<iso>\","
    "\"series\":[{\"ts\":<ms>,\"value\":<v>},...]}\n"
    "```\n"
    "\n"
    "## Donut / pie chart (categorical share — NOT a time series)\n"
    "For 'pie'/'donut' or 'share/breakdown/distribution' questions call\n"
    "`get_category_breakdown` and embed its result JSON VERBATIM (it carries\n"
    "chart_type:'donut' + slices:[{label,value,pct}]). Pick `by`:\n"
    "  - by_tag     → readings (or value) per tag ('pie of readings per tag')\n"
    "  - by_gateway → data volume per gateway\n"
    "  - quality    → GOOD/BAD/UNCERTAIN share for a tag ('quality breakdown')\n"
    "  - value_bands→ % of a tag's readings in ranges (bands=[100,150] → '<100',\n"
    "    '100–150','≥150'); for 'how often was X above/below/between …'.\n"
    "```trustnode-chart\n"
    "{\"chart_type\":\"donut\",\"by\":\"tag\",\"total\":<n>,"
    "\"slices\":[{\"label\":\"<cat>\",\"value\":<v>,\"pct\":<p>},...]}\n"
    "```\n"
    "\n"
    "## Choosing the chart type\n"
    "  - TREND over time, or comparing shapes → LINE (default).\n"
    "  - COMPARE a few discrete categories/tags by one number → BAR.\n"
    "  - COMPOSITION / share of a whole (parts add to 100%) → DONUT.\n"
    "  Honor an explicit request ('as a bar chart', 'pie'); otherwise pick the one\n"
    "  that best fits the question. Never invent slices/series — use tool output.\n"
    "\n"
    "## After the chart block\n"
    "Write 1-2 short sentences: state the window in local-friendly format and\n"
    "one observation (range, drift, anomaly, biggest slice). DO NOT duplicate the JSON in prose.\n"
    "\n"
    "## Hard rules — failures here mean the user sees raw JSON instead of a chart\n"
    "  - The LANGUAGE TAG MUST be exactly `trustnode-chart`. A plain ``` block,\n"
    "    a ```json block, or any other tag will NOT render as a chart.\n"
    "  - The block body MUST be a single valid JSON object. Do not put two\n"
    "    objects back-to-back — use the multi-series shape above.\n"
    "  - Copy the tool result JSON VERBATIM. Do not pretty-print across many\n"
    "    lines (parsers are stricter on multi-line JSON than single-line).\n"
    "  - Do NOT truncate the `series` array — the chart needs the full data.\n"
    "  - If the user asks for stats only (no chart words), use get_tag_summary instead.\n"
    "\n"
    "## Choosing time windows\n"
    "  - 'last N minutes/hours/days' → `from_`:`-Nm|-Nh|-Nd`, `to`:`now`\n"
    "  - 'yesterday' → compute the 24h window starting at user's local midnight - 1d\n"
    "  - 'this morning' → today 00:00 → 12:00 (local) — convert to UTC for the tool call\n"
    "  - When in doubt, default to last hour for live monitoring, last 24h for trend\n"
    "  - Always restate the window in the prose in LOCAL friendly format,\n"
    "    e.g. '30 Jun, 16:02 → 30 Jun, 17:02 (local)'.\n"
    "\n"
    "# COMPARISONS\n"
    "When comparing periods, present both periods in the SAME table and add a "
    "Delta row. State whether the delta exceeds ±2σ of the baseline period.\n"
    "\n"
    "# WHEN DATA IS MISSING\n"
    "If a tag has zero samples in the requested window, state:\n"
    "  - The window queried\n"
    "  - That the gateway either wasn't running or wasn't collecting that tag\n"
    "  - Suggest checking `list_gateways` for running state, or widening the window\n"
    "Do NOT apologize or invent data.\n"
)

# Operator 2026-06-30: lowered from 6 to 3. With dedup-cache (see
# run_chat_turn below) and temperature=0 in the OpenAI client, the LLM
# typically reaches a final answer in 1-2 iterations. The previous cap
# of 6 meant a model that got stuck calling the same tool with the same
# args would burn 6 round-trips before giving up — each round-trip
# costs tokens.
MAX_TOOL_ITERATIONS = 3


# Compact prompt used for the fast-path narration step. The data is ALREADY
# computed; the model only writes the engineering-memo prose around it. Tiny
# vs. the full SYSTEM_PROMPT (no tool schemas, no tool-selection rules), so
# the single LLM call is fast.
NARRATE_PROMPT = (
    "You are a process quality engineer. You are given the RESULT of a data "
    "query (JSON). Write a concise engineering-memo answer in Markdown:\n"
    "1. One-line summary (the headline number/finding).\n"
    "2. A stats table with a Period row (from → to, friendly local time) and a "
    "Samples row, then Min/Max/Mean/Stddev when present.\n"
    "3. 1-3 quality observations (in control? drift? variability?).\n"
    "Use the gateway `name` (not id). Do NOT invent numbers — use only what's in "
    "the JSON. Be precise, no filler.\n"
    "\n"
    "CHART: if the JSON has a `series` array (a chart request), embed the FULL "
    "JSON VERBATIM inside a fenced block tagged exactly `trustnode-chart` so the "
    "UI renders a line chart, THEN add the short summary + observations below it. "
    "Copy the JSON exactly — do not reshape it.\n"
)

# Chart narration: the CHART itself is injected by code, so the model only
# writes a SHORT prose summary (min/max/mean + 1-2 observations). No JSON to
# emit → fast, small output. Keep it to ~4 lines.
NARRATE_PROMPT_CHART = (
    "You are a process quality engineer. You are given SUMMARY STATS for a tag "
    "time-series that is being charted for the user (the chart is rendered "
    "separately — do NOT output any JSON or chart). Write a SHORT answer:\n"
    "- One line naming the tag, the window (from→to, friendly local time) and the "
    "point count.\n"
    "- Min / Max / Mean if present.\n"
    "- 1-2 quality observations (stable? drifting? variable?).\n"
    "Use the gateway `name`. Be concise (max ~4 lines). Do NOT invent numbers.\n"
)


def _narrate_result(client, user_message, tool_result, is_chart):
    """One small LLM call to narrate an already-computed result.

    For CHART results we do NOT ask the model to re-emit the (large) series
    JSON — that's slow to generate (~1000 output tokens) and error-prone.
    Instead we strip the series to just summary stats for the model, get a
    short prose summary, and inject the chart block OURSELVES in code. This
    keeps chart replies fast (small output) and guarantees a valid chart.
    """
    import json as _json
    # BAR / DONUT results carry chart_type + slices (or a bucketed series flagged
    # as bar). The line-rebuild below would drop that shape, so for these we build
    # the chart block from render_local (deterministic, correct) and only ask the
    # model for a short prose lead-in around it.
    _ct = str((tool_result or {}).get("chart_type") or "").lower() if isinstance(tool_result, dict) else ""
    _has_slices = isinstance(tool_result, dict) and isinstance(tool_result.get("slices"), list)
    if is_chart and isinstance(tool_result, dict) and (_ct in ("bar", "donut") or _has_slices):
        try:
            from . import fastpath as _fp
            # render_local already emits the correct ```trustnode-chart``` block
            # (with chart_type + slices) plus a table — return it directly. This
            # keeps bar/donut fast and always-valid, same as the line fast-path.
            # It keys off result['kind'] ('breakdown'/'aggregate') so an empty
            # tool name is fine here.
            body = _fp.render_local("", tool_result, True)
            if body:
                return body
        except Exception:
            pass  # fall through to generic handling
    if is_chart and isinstance(tool_result, dict) and tool_result.get("series"):
        # Give the model only the compact stats (not the full point array).
        summary_view = {k: v for k, v in tool_result.items() if k != "series"}
        summary_view["n_points"] = len(tool_result.get("series") or [])
        payload = _json.dumps(summary_view, default=str)
        msgs = [
            {"role": "system", "content": NARRATE_PROMPT_CHART},
            {"role": "user", "content": f"User asked: {user_message}\n\nChart stats JSON:\n{payload}"},
        ]
        resp = client.chat(msgs, tools=None)
        prose = str(((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        # Inject the real chart block (verbatim series) in code — the frontend
        # parses ```trustnode-chart``` and renders it.
        chart_json = _json.dumps({
            "tag": tool_result.get("tag"),
            "gateway_name": tool_result.get("gateway_name"),
            "from": tool_result.get("from"),
            "to": tool_result.get("to"),
            "series": tool_result.get("series"),
        }, default=str)
        return f"{prose}\n\n```trustnode-chart\n{chart_json}\n```"
    # Non-chart: narrate the full (small) result.
    payload = _json.dumps(tool_result, default=str)
    msgs = [
        {"role": "system", "content": NARRATE_PROMPT},
        {"role": "user", "content": f"User asked: {user_message}\n\nQuery result JSON:\n{payload}"},
    ]
    resp = client.chat(msgs, tools=None)  # no tools → smaller + no tool loop
    choice = (resp.get("choices") or [{}])[0]
    return str((choice.get("message") or {}).get("content") or "").strip()


def _client() -> Tuple[OllamaClient, str]:
    cfg = _cfg.get_ai_config()
    if not cfg.is_configured:
        return (None, "AI endpoint not configured. Ask your administrator to set "
                "TrustNode Intelligence's endpoint URL in the developer portal.")
    if not cfg.model:
        return (None, "AI model not configured in the license bundle.")
    return (OllamaClient(cfg.endpoint_url, cfg.model, cfg.auth_token), "")


def run_chat_turn(chat_id: str, user_message: str, data_source: str = "local",
                  mode: str = "high") -> Dict[str, Any]:
    """Append the user message, run the LLM tool-call loop, append the
    assistant reply. Returns {ok, content, tool_log, error}.

    mode (effort slider):
      "instant" — always answer from the direct data lookup (sub-second).
                  If the question actually needs interpretation, we still
                  give the instant data answer but ADVISE switching to High
                  Effort for the analysis.
      "high"    — spend AI effort WHERE IT'S WORTH IT: simple data lookups
                  ('last reading', 'average', 'list tags') still render
                  locally (no point paying 3s to restate a number), but
                  interpretive / reasoning questions get full AI analysis.
    """
    mode = (mode or "high").strip().lower()
    if mode not in ("instant", "high"):
        # tolerate legacy names
        mode = "instant" if mode == "turbo" else "high"
    # Operator 2026-07-03 (INSTANT-WORKS-WITHOUT-AI): do NOT bail here when the
    # AI endpoint is unconfigured. Pure-data queries (list tags/gateways/alarms,
    # tag value, trend, chart) are answered by the deterministic fast-path with
    # NO AI call at all — they must work even with no endpoint. We resolve the
    # AI client LAZILY and only require it on the branches that truly need AI
    # (narration, interpretation, the full tool loop). Previously this returned
    # "AI endpoint not configured" up front, which broke EVERY message —
    # including Instant data lookups that need no model.
    client, err = _client()

    # Operator 2026-07-02 (FAST-PATH): for common, clearly-shaped single-tag
    # questions (last reading / average / chart / bucketed) we skip the
    # tool-SELECTION LLM round-trip: classify in code, run the SQL directly,
    # then make ONE small LLM call (compact NARRATE_PROMPT, no tool schemas)
    # to write the memo. This turns 2 model calls into 1 and drops the
    # per-call token overhead from ~5000 to a few hundred — cutting simple
    # queries from ~6s to ~2-3s. Falls through to the full loop on any
    # mismatch, disambiguation, or error, so quality/coverage is unchanged.
    try:
        from . import fastpath as _fp
        fp = _fp.run_fastpath(user_message, data_source)
    except Exception:
        fp = None
    if fp is not None:
        store.append_message(chat_id, "user", user_message)
        _t0 = _time.monotonic()
        if fp.get("disambiguation"):
            sugg = fp["disambiguation"]
            lines = "\n".join(f"{i+1}) {s}" for i, s in enumerate(sugg))
            content = f"Did you mean:\n{lines}\n\nPlease confirm which tag you would like to use."
            store.append_message(chat_id, "assistant", content)
            _log.info("fast-path disambiguation in %.2fs", _time.monotonic() - _t0)
            return {"ok": True, "content": content, "tool_log": []}
        tool_result = fp["tool_result"]
        # If the result is an error or empty, fall through to the full loop
        # (the model may find a better tool / phrasing).
        if isinstance(tool_result, dict) and not tool_result.get("error"):
            tlog = [{"name": fp["tool_name"], "args": {}, "result": tool_result}]
            # DECISION MECHANIC (effort-aware):
            #   - A pure-data ask (no interpretation words) ALWAYS renders
            #     locally — in BOTH modes. It's never worth paying ~3s of AI
            #     to restate a number we already computed in <100ms.
            #   - An interpretive ask ('is it stable / drifting / anomalies'):
            #       * High Effort → AI analysis.
            #       * Instant     → give the instant data answer + a short
            #                       advisory to switch to High Effort for the
            #                       interpretation.
            wants_ai = fp.get("wants_ai")
            if not wants_ai:
                _use_local, _advise = True, False
            elif mode == "instant":
                _use_local, _advise = True, True   # data now + advise High Effort
            else:
                _use_local, _advise = False, False  # high → AI analysis
            if _use_local:
                try:
                    from . import fastpath as _fp2
                    content = _fp2.render_local(fp["tool_name"], tool_result, fp.get("is_chart"))
                    if _advise:
                        content += ("\n\n_Instant mode gave you the data. For the "
                                    "in-depth analysis (stability, drift, control), "
                                    "switch the effort slider to **High Effort** and ask again._")
                    store.append_message(chat_id, "assistant", content,
                                         tool_calls=None, tool_results=tlog)
                    _log.info("fast-path LOCAL (%s, mode=%s, advise=%s) in %.2fs",
                              fp["tool_name"], mode, _advise, _time.monotonic() - _t0)
                    return {"ok": True, "content": content, "tool_log": tlog,
                            "path": "local"}
                except Exception:
                    pass  # fall through to AI narration
            # AI narration needs a client. If the endpoint isn't configured,
            # don't error out on a query we CAN answer — render the data
            # locally (it's a real answer) and advise enabling High Effort/AI
            # for the interpretation. Only truly AI-only asks surface the error.
            if not client:
                try:
                    from . import fastpath as _fp3
                    content = _fp3.render_local(fp["tool_name"], tool_result, fp.get("is_chart"))
                    content += ("\n\n_Showing the data directly. In-depth AI analysis "
                                "is unavailable until the assistant endpoint is configured._")
                    store.append_message(chat_id, "assistant", content,
                                         tool_calls=None, tool_results=tlog)
                    _log.info("fast-path LOCAL fallback (no AI client) (%s) in %.2fs",
                              fp["tool_name"], _time.monotonic() - _t0)
                    return {"ok": True, "content": content, "tool_log": tlog,
                            "path": "local_no_ai"}
                except Exception:
                    store.append_message(chat_id, "assistant", err)
                    return {"ok": False, "error": err, "content": err, "tool_log": []}
            try:
                content = _narrate_result(client, user_message, tool_result, fp.get("is_chart"))
                if content:
                    store.append_message(chat_id, "assistant", content,
                                         tool_calls=None, tool_results=tlog)
                    _log.info("fast-path AI (%s) in %.2fs", fp["tool_name"], _time.monotonic() - _t0)
                    return {"ok": True, "content": content, "tool_log": tlog, "path": "fast_ai"}
            except AIBackendError as exc:
                store.append_message(chat_id, "assistant", str(exc))
                return {"ok": False, "error": str(exc), "content": str(exc), "tool_log": []}
            except Exception:
                pass  # fall through to full loop
        # Fall through: re-run via the full loop (persist user msg already done
        # above, so skip the duplicate append below via a flag).
        _fastpath_user_persisted = True
    else:
        _fastpath_user_persisted = False

    # Instant-mode guard: if we reach the (slow) full tool-loop in INSTANT
    # mode, the question needs real reasoning that instant can't deliver fast.
    # Rather than making the user wait ~10s in a mode labelled "Instant", we
    # answer briefly and advise switching to High Effort.
    if mode == "instant":
        if not _fastpath_user_persisted:
            store.append_message(chat_id, "user", user_message)
        advice = (
            "That question needs deeper analysis than Instant mode runs. "
            "Switch the effort slider to **High Effort** and ask again — "
            "I'll pull the data and give you the full engineering assessment."
        )
        store.append_message(chat_id, "assistant", advice)
        _log.info("instant-mode advisory (needs High Effort)")
        return {"ok": True, "content": advice, "tool_log": [], "path": "instant_advise"}

    # From here on we run the full AI tool-loop (High Effort, interpretive
    # question that the fast-path couldn't answer). This genuinely needs the
    # model — if the endpoint isn't configured, surface the friendly error now
    # (the user message was persisted above when _fastpath_user_persisted).
    if not client:
        if not _fastpath_user_persisted:
            store.append_message(chat_id, "user", user_message)
        store.append_message(chat_id, "assistant", err)
        return {"ok": False, "error": err, "content": err, "tool_log": []}

    # Build conversation history.
    # Operator 2026-07-02: cap to the last N messages and STRIP embedded
    # chart JSON from prior assistant turns. Without this, each successive
    # turn re-sends the full unbounded history (incl. up to ~4KB of
    # trustnode-chart JSON per prior chart) to the LLM, making every follow-up
    # slower and eventually pushing past the turn budget. A sliding window
    # keeps the context (so 'yes'/'1' disambiguation replies still resolve)
    # without the payload bloat.
    _HISTORY_WINDOW = 20
    history = store.list_messages(chat_id)
    if len(history) > _HISTORY_WINDOW:
        history = history[-_HISTORY_WINDOW:]
    messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in history:
        content = m["content"]
        # Replace any embedded ```trustnode-chart ...``` block with a short
        # placeholder — the chart already rendered for the user; the LLM
        # doesn't need the raw point data back in its context.
        content = _strip_chart_blocks(content)
        messages.append({"role": m["role"], "content": content})
        # We don't replay tool calls into the LLM context — keeps it lean.
    if not _fastpath_user_persisted:
        # Normal path: the user message isn't in history yet.
        messages.append({"role": "user", "content": user_message})
        store.append_message(chat_id, "user", user_message)
    # Fast-path fall-through already persisted the user msg AND it is now the
    # last row of `history` above, so it's already in `messages`.

    tools_schema = openai_tool_schemas(allowed_only=True)
    tool_log: List[Dict[str, Any]] = []
    final_content = ""
    # Operator 2026-06-30: per-turn cache of (name, args_canonical_json) ->
    # result. Prevents the "stuck loop" pattern where the LLM calls the
    # same tool with the same args N times in a row (each round-trip
    # burns prompt+output tokens). On cache hit we return the prior
    # result instantly without re-executing the tool.
    seen_tool_calls: Dict[str, Any] = {}

    _turn_t0 = _time.monotonic()
    # Operator 2026-07-02: hard wall-clock budget for the whole turn. If the
    # model gets into a slow tool loop, we stop after this many seconds and
    # return what we have, so the HTTP request never hangs long enough for
    # the browser to give up with "Failed to fetch".
    _TURN_BUDGET_S = 45.0
    try:
        for _iter in range(MAX_TOOL_ITERATIONS):
            if (_time.monotonic() - _turn_t0) > _TURN_BUDGET_S:
                _log.warning("chat_turn budget exceeded after %.1fs", _time.monotonic() - _turn_t0)
                final_content = final_content or (
                    "I gathered some data but the analysis took too long to complete. "
                    "Please try a narrower time window or a single tag."
                )
                break
            _llm_t0 = _time.monotonic()
            resp = client.chat(messages, tools=tools_schema)
            _log.info("chat_turn iter=%d llm_call=%.2fs", _iter, _time.monotonic() - _llm_t0)
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = str(msg.get("content") or "").strip()
            tool_calls = msg.get("tool_calls") or []

            if tool_calls:
                # Echo the assistant's call into the conversation so the
                # follow-up sees it.
                messages.append({"role": "assistant", "content": content or None,
                                 "tool_calls": tool_calls})
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or ""
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except Exception:
                        args = {}
                    # Dedup: canonical key = (name, sorted-keys JSON of args).
                    try:
                        cache_key = name + "|" + json.dumps(args, sort_keys=True, default=str)
                    except Exception:
                        cache_key = name + "|<unhashable>"
                    if cache_key in seen_tool_calls:
                        result = seen_tool_calls[cache_key]
                        # Mark this call as a cache hit in the log so the
                        # UI can distinguish from genuine executions.
                        tool_log.append({"name": name, "args": args, "result": result, "cached": True})
                    else:
                        _tool_t0 = _time.monotonic()
                        result = run_tool(name, args, {"data_source": data_source})
                        _log.info("chat_turn tool=%s took=%.2fs", name, _time.monotonic() - _tool_t0)
                        seen_tool_calls[cache_key] = result
                        tool_log.append({"name": name, "args": args, "result": result})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id") or name,
                        "name": name,
                        "content": json.dumps(result, default=str),
                    })
                continue  # next LLM iteration

            # No tool calls → final answer.
            final_content = content or "(empty response)"
            break
        else:
            final_content = "(reached tool-call iteration cap without a final answer)"
    except AIBackendError as exc:
        final_content = str(exc)
        store.append_message(chat_id, "assistant", final_content)
        return {"ok": False, "error": final_content, "content": final_content, "tool_log": tool_log}

    store.append_message(chat_id, "assistant", final_content,
                         tool_calls=None, tool_results=tool_log or None)
    _log.info("chat_turn DONE total=%.2fs tools=%d", _time.monotonic() - _turn_t0, len(tool_log))
    return {"ok": True, "content": final_content, "tool_log": tool_log}


def run_insight(prompt: str, tool_plan: List[Dict[str, Any]],
                data_source: str = "local") -> Dict[str, Any]:
    """Replay a saved insight: execute the deterministic tool plan, then
    pass the results to the LLM with the saved prompt for narration."""
    client, err = _client()
    tool_results: List[Dict[str, Any]] = []
    for step in tool_plan or []:
        name = str(step.get("name") or "")
        args = step.get("args") or {}
        result = run_tool(name, args, {"data_source": data_source})
        tool_results.append({"name": name, "args": args, "result": result})

    # Operator 2026-07-03 (TABLE/CHART PARITY): if the saved insight is a single
    # DETERMINISTIC data tool that the chat renders as a TABLE or CHART
    # (list_tags/list_gateways/list_recent_alarms/get_tag_timeseries/
    # get_multi_tag_timeseries/get_tag_summary/get_bucketed_series), render it
    # the SAME way here — so a scheduled run produces the exact table/chart the
    # user saw in chat, not an AI prose paraphrase. Needs no AI client, so this
    # also works when the endpoint isn't configured.
    _RENDERABLE = {
        "list_tags", "list_gateways", "list_recent_alarms",
        "get_tag_timeseries", "get_multi_tag_timeseries",
        "get_tag_summary", "get_bucketed_series",
        "aggregate_tags", "get_category_breakdown",
    }
    if len(tool_results) == 1 and str(tool_results[0].get("name")) in _RENDERABLE:
        tr = tool_results[0]
        res0 = tr.get("result")
        if isinstance(res0, dict) and not res0.get("error"):
            try:
                from . import fastpath as _fp
                is_chart = ("timeseries" in tr["name"]) or ("slices" in res0) \
                    or (bool(res0.get("series")) and tr["name"] != "get_tag_summary")
                content = _fp.render_local(tr["name"], res0, bool(is_chart))
                if content:
                    return {"ok": True, "content": content, "tool_results": tool_results,
                            "path": "insight_local"}
            except Exception:
                pass  # fall through to AI narration

    if not client:
        # Return raw results with the error message — caller (scheduler /
        # API) decides what to do.
        return {"ok": False, "error": err, "content": err, "tool_results": tool_results}

    # Ask LLM to narrate the results.
    user_msg = (
        f"{prompt}\n\n"
        f"Here are the tool results:\n```json\n"
        f"{json.dumps(tool_results, default=str, indent=2)[:8000]}\n```\n"
        f"Write a concise insight summary."
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = client.chat(messages, tools=None)  # narration only, no tools
        choice = (resp.get("choices") or [{}])[0]
        content = str((choice.get("message") or {}).get("content") or "").strip()
    except AIBackendError as exc:
        return {"ok": False, "error": str(exc), "content": str(exc), "tool_results": tool_results}
    return {"ok": True, "content": content or "(empty narration)", "tool_results": tool_results}
