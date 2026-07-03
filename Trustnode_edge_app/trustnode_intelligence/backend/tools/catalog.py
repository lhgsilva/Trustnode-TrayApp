"""Tool catalog — the fixed list of functions the LLM is allowed to call.

Each tool has:
  - name (used by LLM in tool_calls)
  - category (read_only | can_run_batches | ...) for license gating
  - openai_schema() returning the JSON Schema for the LLM
  - run(args, context) returning a JSON-serializable result

The LLM never writes SQL directly. All data access goes through these
tools, which call existing read endpoints / services. Same pattern as
batch_management's controlled router surface.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from . import alarms, analytics, batch_summary, gateways, tag_summary


class Tool:
    def __init__(self, name: str, category: str, description: str,
                 schema: Dict[str, Any], runner: Callable[[Dict[str, Any], Dict[str, Any]], Any]):
        self.name = name
        self.category = category
        self.description = description
        self.schema = schema
        self.runner = runner


TOOL_CATALOG: Dict[str, Tool] = {
    "list_tags": Tool(
        name="list_tags",
        category="read_only",
        description="List all tags currently being collected by any gateway, with their gateway id and last-known value.",
        schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        runner=gateways.run_list_tags,
    ),
    "list_gateways": Tool(
        name="list_gateways",
        category="read_only",
        description="List all configured gateways (id, name, type, IP, interval, running state).",
        schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        runner=gateways.run_list_gateways,
    ),
    "find_tags": Tool(
        name="find_tags",
        category="read_only",
        description="Resolve a possibly-misspelled or partial tag name to REAL configured tags. "
                    "Returns {exact: name} if there's an exact match, else {suggestions: [names]} ranked "
                    "by closeness. ALWAYS call this FIRST when the user gives a tag name you're not 100% sure "
                    "matches a real tag (typos, spacing like 'sim real 3', abbreviations like 'LVA'). If it "
                    "returns an exact match, use that tag. If it returns multiple suggestions and you're unsure "
                    "which the user means, present them as a short numbered pick-list and ask the user to choose "
                    "BEFORE running any data query.",
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The tag name to resolve (may be misspelled/partial)."},
                "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
        runner=gateways.run_find_tags,
    ),
    "get_tag_summary": Tool(
        name="get_tag_summary",
        category="read_only",
        description="Compute min/max/avg/count/stddev for a single tag over a time window. "
                    "Use this for process questions like 'what was the average X over the last 8 hours'.",
        schema={
            "type": "object",
            "properties": {
                "tag":  {"type": "string", "description": "Tag name (e.g. 'SimREAL[3]')"},
                "from_": {"type": "string", "description": "Start time. Either ISO-8601 ('2026-06-26T00:00:00Z') or relative ('-8h', '-7d')."},
                "to":   {"type": "string", "description": "End time. Same format as 'from_'. Use 'now' for current time."},
                "gateway_id": {"type": "string", "description": "Optional gateway id to scope the query.", "default": ""},
            },
            "required": ["tag", "from_", "to"],
        },
        runner=tag_summary.run_get_tag_summary,
    ),
    "get_tag_timeseries": Tool(
        name="get_tag_timeseries",
        category="read_only",
        description="Return a downsampled time series for a tag (~200 points) over a window. "
                    "USE THIS when the user asks to 'show', 'plot', 'chart', 'visualize', or 'see the trend' of a tag. "
                    "After calling this, embed the result inside a ```trustnode-chart fenced code block so the UI renders a chart. "
                    "Do NOT call get_tag_summary for visualization requests.",
        schema={
            "type": "object",
            "properties": {
                "tag":  {"type": "string", "description": "Tag name."},
                "from_": {"type": "string", "description": "Start time. ISO-8601 or relative ('-1h', '-24h', '-7d'). Defaults to '-1h'."},
                "to":   {"type": "string", "description": "End time. Same format, or 'now'. Defaults to 'now'."},
                "max_points": {"type": "integer", "description": "Max samples to return after downsampling. 20-500. Default 200.", "default": 200},
                "gateway_id": {"type": "string", "description": "Optional gateway scope.", "default": ""},
            },
            "required": ["tag"],
        },
        runner=tag_summary.run_get_tag_timeseries,
    ),
    "get_multi_tag_timeseries": Tool(
        name="get_multi_tag_timeseries",
        category="read_only",
        description="Fetch downsampled time series for MULTIPLE tags in a single call. "
                    "USE THIS when the user asks to show/plot/trend/compare more than one tag in the same chart. "
                    "Returns a multi-series payload ready to embed inside a ```trustnode-chart fenced block. "
                    "The chart renderer will auto-assign a right Y axis to any series whose value range is "
                    ">5x different from the others (so e.g. temperature 100-160 + pressure 1100-1130 render cleanly together).",
        schema={
            "type": "object",
            "properties": {
                "tags":  {"type": "array", "items": {"type": "string"}, "description": "2-6 tag names to overlay."},
                "from_": {"type": "string", "description": "Start time. ISO-8601 or relative ('-1h', '-24h', '-7d'). Defaults to '-1h'."},
                "to":   {"type": "string", "description": "End time. Same format, or 'now'. Defaults to 'now'."},
                "max_points": {"type": "integer", "description": "Max samples per tag after downsampling. 20-500. Default 200.", "default": 200},
                "gateway_id": {"type": "string", "description": "Optional gateway scope (applies to all tags).", "default": ""},
            },
            "required": ["tags"],
        },
        runner=tag_summary.run_get_multi_tag_timeseries,
    ),
    "compare_tags": Tool(
        name="compare_tags",
        category="read_only",
        description="Compare MULTIPLE tags over an explicit time range at a chosen time bucket "
                    "(1s/5s/10s/30s/1m/5m/15m/1h/1d, or 'auto'). USE THIS when the user wants to "
                    "compare/correlate several tags along time, or asks about relationships/correlation "
                    "between tags. Returns a multi-series chart shape PLUS a pairwise Pearson correlation "
                    "matrix PLUS plain-language insights. Prefer this over get_multi_tag_timeseries when "
                    "the user wants analysis/correlation (not just an overlay), or specifies a bucket size.",
        schema={
            "type": "object",
            "properties": {
                "tags":   {"type": "array", "items": {"type": "string"}, "description": "2-6 tag names to compare."},
                "from_":  {"type": "string", "description": "Start time. ISO-8601 or relative ('-1h','-24h','-7d'). Default '-1h'."},
                "to":     {"type": "string", "description": "End time. Same format or 'now'. Default 'now'."},
                "bucket": {"type": "string", "description": "Time grouping: 1s/5s/10s/30s/1m/5m/15m/1h/1d, or 'auto'.", "default": "auto"},
                "gateway_id": {"type": "string", "description": "Optional gateway scope.", "default": ""},
            },
            "required": ["tags"],
        },
        runner=analytics.run_compare_tags,
    ),
    "compare_periods": Tool(
        name="compare_periods",
        category="read_only",
        description="Compare a tag's stats across two time windows. Use for 'compare yesterday to today' style questions.",
        schema={
            "type": "object",
            "properties": {
                "tag": {"type": "string"},
                "period_a_from": {"type": "string"},
                "period_a_to":   {"type": "string"},
                "period_b_from": {"type": "string"},
                "period_b_to":   {"type": "string"},
                "gateway_id": {"type": "string", "default": ""},
            },
            "required": ["tag", "period_a_from", "period_a_to", "period_b_from", "period_b_to"],
        },
        runner=tag_summary.run_compare_periods,
    ),
    "get_batch_summary": Tool(
        name="get_batch_summary",
        category="read_only",
        description="Get the summary record for a single batch (start, stop, cycle time, tag stats).",
        schema={
            "type": "object",
            "properties": {
                "batch_id": {"type": "string"},
            },
            "required": ["batch_id"],
        },
        runner=batch_summary.run_get_batch_summary,
    ),
    "list_recent_batches": Tool(
        name="list_recent_batches",
        category="read_only",
        description="List the most recent N batches with id, state, start, stop. Useful for 'show me running batches'.",
        schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
                "state": {"type": "string", "enum": ["all", "running", "completed", "failed"], "default": "all"},
            },
            "required": [],
        },
        runner=batch_summary.run_list_recent_batches,
    ),
    "list_recent_alarms": Tool(
        name="list_recent_alarms",
        category="read_only",
        description="List recent alarm/event log entries with severity, source, message, timestamp.",
        schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
                "since": {"type": "string", "description": "Relative time like '-24h' or absolute ISO-8601.", "default": "-24h"},
            },
            "required": [],
        },
        runner=alarms.run_list_recent_alarms,
    ),
    # ---- Analytics tools (operator 2026-07-02) --------------------------
    "get_bucketed_series": Tool(
        name="get_bucketed_series",
        category="read_only",
        description="Aggregate a tag into fixed TIME BUCKETS (avg/min/max/count/last per interval). "
                    "USE THIS for 'average every 5 seconds/minute/hour', downsampling per-second data, "
                    "or any 'group by time' request. Bucket options: 1s,5s,10s,30s,1m,5m,15m,1h,1d, or 'auto' "
                    "(picks ~200 buckets for the window). Returns a `series` array [{ts,value,count}] plus a "
                    "`suggested_chart` hint. When suggested_chart is 'line_chart', embed the series in a "
                    "```trustnode-chart fenced block (shape {tag, from, to, series:[{ts,value}]}); when 'table', "
                    "render a small Markdown table instead. Supports optional value_gt/value_lt/quality filters.",
        schema={
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Tag name."},
                "from_": {"type": "string", "description": "Start time (ISO-8601 or relative '-8h'). Default '-1h'."},
                "to": {"type": "string", "description": "End time or 'now'. Default 'now'."},
                "bucket": {"type": "string", "enum": ["1s", "5s", "10s", "30s", "1m", "5m", "15m", "1h", "1d", "auto"],
                           "description": "Time-bucket size. 'auto' targets ~200 points.", "default": "auto"},
                "agg": {"type": "string", "enum": ["avg", "min", "max", "count", "last"],
                        "description": "Aggregation per bucket. Default 'avg'.", "default": "avg"},
                "gateway_id": {"type": "string", "default": ""},
                "value_gt": {"type": "number", "description": "Only include readings greater than this."},
                "value_lt": {"type": "number", "description": "Only include readings less than this."},
                "quality": {"type": "string", "enum": ["good", "bad", "uncertain"], "description": "Filter by quality label."},
            },
            "required": ["tag"],
        },
        runner=analytics.run_get_bucketed_series,
    ),
    "detect_threshold": Tool(
        name="detect_threshold",
        category="read_only",
        description="Count how many readings (and estimated time) a tag spent above an upper_limit and/or below a "
                    "lower_limit over a window, plus the in-spec percentage. USE THIS for 'how long was X above 150?', "
                    "'what % of the time was X in spec?', or SLA/limit questions. Provide at least one limit.",
        schema={
            "type": "object",
            "properties": {
                "tag": {"type": "string"},
                "from_": {"type": "string", "description": "Start time. Default '-1h'."},
                "to": {"type": "string", "description": "End time or 'now'. Default 'now'."},
                "upper_limit": {"type": "number", "description": "Breach when value > this."},
                "lower_limit": {"type": "number", "description": "Breach when value < this."},
                "gateway_id": {"type": "string", "default": ""},
            },
            "required": ["tag"],
        },
        runner=analytics.run_detect_threshold,
    ),
    "analyze_trend": Tool(
        name="analyze_trend",
        category="read_only",
        description="Fit a LINEAR TREND to a tag over a window: slope (units/hour), R^2 (fit strength), direction "
                    "(rising/falling/flat), and optionally PROJECT forward. USE THIS for 'is X trending up?', "
                    "'what's the rate of change?', 'project X in 30 minutes' (project_minutes), or 'when will X reach "
                    "value V?' (target_value). Regression runs on time-bucketed averages for noise robustness.",
        schema={
            "type": "object",
            "properties": {
                "tag": {"type": "string"},
                "from_": {"type": "string", "description": "Start time. Default '-1h'."},
                "to": {"type": "string", "description": "End time or 'now'. Default 'now'."},
                "project_minutes": {"type": "number", "description": "Optional: project the fitted trend this many minutes ahead."},
                "target_value": {"type": "number", "description": "Optional: estimate when the trend reaches this value."},
                "bucket": {"type": "string", "enum": ["1s", "5s", "10s", "30s", "1m", "5m", "15m", "1h", "1d", "auto"], "default": "auto"},
                "gateway_id": {"type": "string", "default": ""},
            },
            "required": ["tag"],
        },
        runner=analytics.run_analyze_trend,
    ),
    "detect_anomalies": Tool(
        name="detect_anomalies",
        category="read_only",
        description="Statistical Process Control check: compute mean and +/-k-sigma control limits (UCL/LCL) for a tag "
                    "over a window and report how many readings fall outside them (out-of-control points). USE THIS for "
                    "'are there any anomalies/outliers in X?', 'is the process in control?', or SPC/quality questions. "
                    "Default sigma=3.",
        schema={
            "type": "object",
            "properties": {
                "tag": {"type": "string"},
                "from_": {"type": "string", "description": "Start time. Default '-1h'."},
                "to": {"type": "string", "description": "End time or 'now'. Default 'now'."},
                "sigma": {"type": "number", "description": "Control-limit multiplier (default 3).", "default": 3},
                "gateway_id": {"type": "string", "default": ""},
            },
            "required": ["tag"],
        },
        runner=analytics.run_detect_anomalies,
    ),
}


def get_tool(name: str) -> Tool:
    tool = TOOL_CATALOG.get(name)
    if not tool:
        raise KeyError(f"Unknown tool: {name}")
    return tool


def openai_tool_schemas(allowed_only: bool = True) -> List[Dict[str, Any]]:
    """Return the tool list in OpenAI/Ollama tool-call format."""
    from ..license import is_tool_allowed
    out = []
    for tool in TOOL_CATALOG.values():
        if allowed_only and not is_tool_allowed(tool.category):
            continue
        out.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.schema,
            },
        })
    return out


def run_tool(name: str, args: Dict[str, Any], context: Dict[str, Any]) -> Any:
    """Dispatch a tool call. Context carries `data_source` (local|cloud)
    so each tool can route reads to the right backend."""
    tool = get_tool(name)
    from ..license import is_tool_allowed
    if not is_tool_allowed(tool.category):
        return {"error": f"Tool '{name}' is not allowed by license category '{tool.category}'."}
    try:
        return tool.runner(args or {}, context or {})
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
