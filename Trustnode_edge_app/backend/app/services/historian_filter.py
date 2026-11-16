# -*- coding: utf-8 -*-
"""The data filter: rows chosen by the value of OTHER tags at the same time.

2026-11-16, from the owner: "to any dashboard widget a filter option not
only for the time but also based on a tag value ... check the timestamp of
the data row and the tag value in the same timestamp ... multiple
conditions, and / or ... any dashboard widget, any csv engine and any report
section".

One shape everywhere::

    {"logic": "all" | "any",
     "conditions": [{"gateway_id": "", "tag_name": "Run", "operator": "eq",
                     "value1": 1, "value2": null}, ...]}

A row at time T is kept when the conditions hold for the named tags:

* a condition with no gateway, or the row's own gateway, reads the tag in
  the SAME collection cycle - every tag a gateway reads in one cycle carries
  the same ts_utc text, so this is an equality probe on the composite
  (tenant, gateway, tag, ts) index;
* a condition on ANOTHER gateway reads that tag's latest value at or before
  T (sample-and-hold), the way the report trigger timeline already does.

The SQL is a correlated predicate appended to a reader's WHERE, so nothing
is joined or copied; the same conditions are evaluated in Python for rows
already in memory (an export sink's collection cycle).
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Callable, Iterable

MAX_CONDITIONS = 8
_OPS = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "between": None}
_ALIASES = {">": "gt", ">=": "gte", "<": "lt", "<=": "lte", "=": "eq", "==": "eq", "!=": "ne", "<>": "ne",
            "is": "eq", "not": "ne", "range": "between"}


def _num(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def parse_filter(raw: Any) -> dict[str, Any] | None:
    """A clean filter, or None when there is nothing usable in `raw`.

    Accepts the dict or its JSON text (a GET parameter). A condition without
    a tag or without a number where it needs one is dropped - the editor
    validates; a half-typed condition must not empty every chart.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        txt = raw.strip()
        if not txt or txt in ("null", "{}", "[]"):
            return None
        try:
            raw = json.loads(txt)
        except ValueError:
            return None
    if isinstance(raw, list):
        raw = {"conditions": raw}
    if not isinstance(raw, dict):
        return None
    if raw.get("enabled") is False:
        return None
    logic = "any" if str(raw.get("logic") or "all").strip().lower() in ("any", "or") else "all"
    out: list[dict[str, Any]] = []
    for c in list(raw.get("conditions") or [])[:MAX_CONDITIONS * 2]:
        if not isinstance(c, dict):
            continue
        tag = str(c.get("tag_name") or c.get("tag") or "").strip()
        if not tag:
            continue
        op = str(c.get("operator") or c.get("op") or "eq").strip().lower()
        op = _ALIASES.get(op, op)
        if op not in _OPS:
            continue
        v1 = _num(c.get("value1", c.get("value")))
        v2 = _num(c.get("value2"))
        if v1 is None or (op == "between" and v2 is None):
            continue
        out.append({"gateway_id": str(c.get("gateway_id") or c.get("gateway") or "").strip(),
                    "tag_name": tag, "operator": op, "value1": v1,
                    "value2": v2 if op == "between" else None})
        if len(out) >= MAX_CONDITIONS:
            break
    if not out:
        return None
    return {"logic": logic, "conditions": out}


def _pred(op: str, lhs: str, p1: str, p2: str) -> str:
    if op == "between":
        return f"{lhs} >= {p1} AND {lhs} <= {p2}"
    return f"{lhs} {_OPS[op]} {p1}"


def sql_fragment(flt: dict[str, Any] | None, query_gateway: str = "", prefix: str = "df",
                 outer: str = "historian_readings") -> tuple[str, dict[str, Any]]:
    """(" AND (...)", named params) to append to a reader's WHERE, or ("", {}).

    `outer` is how the reader names its historian_readings (the bare table
    name when it uses none). `query_gateway` is the reader's own gateway
    scope: a condition on that gateway is a same-cycle read.
    """
    if not flt:
        return "", {}
    qg = str(query_gateway or "").strip()
    parts: list[str] = []
    params: dict[str, Any] = {}
    for i, c in enumerate(flt["conditions"]):
        k = f"{prefix}{i}"
        params[f"{k}_tag"] = c["tag_name"]
        params[f"{k}_v1"] = c["value1"]
        if c["operator"] == "between":
            params[f"{k}_v2"] = c["value2"]
        cg = c["gateway_id"]
        same_cycle = (not cg) or (qg and cg == qg)
        if same_cycle:
            if cg:
                params[f"{k}_gw"] = cg
                gw_clause = f"b.gateway_id = :{k}_gw"
            else:
                gw_clause = f"b.gateway_id = {outer}.gateway_id"
            parts.append(
                f"EXISTS (SELECT 1 FROM historian_readings b WHERE b.tenant_id = {outer}.tenant_id"
                f" AND {gw_clause} AND b.tag_name = :{k}_tag AND b.ts_utc = {outer}.ts_utc"
                f" AND {_pred(c['operator'], 'b.value', ':' + k + '_v1', ':' + k + '_v2')})")
        else:
            params[f"{k}_gw"] = cg
            held = (f"(SELECT b.value FROM historian_readings b WHERE b.tenant_id = {outer}.tenant_id"
                    f" AND b.gateway_id = :{k}_gw AND b.tag_name = :{k}_tag AND b.ts_utc <= {outer}.ts_utc"
                    f" ORDER BY b.ts_utc DESC LIMIT 1)")
            parts.append("(" + _pred(c["operator"], held, ":" + k + "_v1", ":" + k + "_v2") + ")")
    joiner = " OR " if flt["logic"] == "any" else " AND "
    return " AND (" + joiner.join(parts) + ")", params


def sql_fragment_positional(flt: dict[str, Any] | None, query_gateway: str = "") -> tuple[str, list[Any]]:
    """The same predicate as one WHERE term with `?` placeholders (Data Export)."""
    sql, named = sql_fragment(flt, query_gateway=query_gateway, prefix="df")
    if not sql:
        return "", []
    order: list[Any] = []

    def _swap(m: "re.Match[str]") -> str:
        order.append(named[m.group(1)])
        return "?"
    out = re.sub(r":(df\d+_(?:tag|v1|v2|gw))", _swap, sql)
    return out[len(" AND "):], order


def _matches(op: str, value: Any, v1: float, v2: float | None) -> bool:
    n = _num(value)
    if n is None:
        return False
    if op == "eq":
        return n == v1
    if op == "ne":
        return n != v1
    if op == "lt":
        return n < v1
    if op == "lte":
        return n <= v1
    if op == "gt":
        return n > v1
    if op == "gte":
        return n >= v1
    if op == "between":
        hi = v1 if v2 is None else v2
        return min(v1, hi) <= n <= max(v1, hi)
    return False


def cycle_passes(flt: dict[str, Any] | None, cycle_values: dict[str, Any], gateway_id: str = "",
                 lookup: Callable[[str, str], Any] | None = None) -> bool:
    """Does one collection cycle pass? `cycle_values` maps tag -> value for
    the gateway's cycle; `lookup(gateway_id, tag)` answers a condition on
    another gateway (its latest value) and may be None."""
    if not flt:
        return True
    results: list[bool] = []
    for c in flt["conditions"]:
        cg = c["gateway_id"]
        if cg and gateway_id and cg != gateway_id:
            value = lookup(cg, c["tag_name"]) if lookup else None
        else:
            value = cycle_values.get(c["tag_name"])
            if value is None and cg and lookup and not gateway_id:
                value = lookup(cg, c["tag_name"])
        results.append(_matches(c["operator"], value, c["value1"], c["value2"]))
    return any(results) if flt["logic"] == "any" else all(results)


def filter_rows(flt: dict[str, Any] | None, rows: Iterable[dict[str, Any]],
                lookup: Callable[[str, str], Any] | None = None) -> list[dict[str, Any]]:
    """Rows (dicts with gateway_id / tag_name / ts_utc / value) grouped by
    (gateway, timestamp) into cycles; the rows of the cycles that pass, in order."""
    src = list(rows or [])
    if not flt:
        return src

    def _key(r: dict[str, Any]) -> tuple[str, str]:
        return (str(r.get("gateway_id") or ""), str(r.get("ts_utc") or r.get("ts") or ""))

    cycles: dict[tuple[str, str], dict[str, Any]] = {}
    for r in src:
        cycles.setdefault(_key(r), {})[str(r.get("tag_name") or r.get("tag") or "")] = r.get("value")
    ok = {key for key, vals in cycles.items() if cycle_passes(flt, vals, key[0], lookup)}
    return [r for r in src if _key(r) in ok]
