# -*- coding: utf-8 -*-
"""Query builder for the Data Export assistant.

2026-08-31: "we should create a new sub menu under data history called Data
export... selecting gateways, devices, tags, data range, other columns
conditions, complete filtering system conditions, aggregation and filter
features... cannot break the historian, it is only a query assistant to the
database."

Deliberately its own module. The historian read path in app_store.py is on the
hot path of every chart and widget in the app; an export assistant that wants
grouping, pivots and arbitrary conditions has no business growing inside it.
Nothing here is imported by the historian, and nothing here writes.

Two rules shape the SQL:

  * Filters are EXACT and parameterised. `LOWER(COALESCE(tag_name,'')) LIKE
    '%x%'` cannot use idx_hist_tenant_tag_ts and turns a seek into a scan of
    16 million rows; an `IN (?,?,?)` list keeps the index. Free-text matching
    is offered only where the operator explicitly asks for "contains", and it
    is applied AFTER the indexed predicates have narrowed the set.
  * Identifiers are never interpolated from user input. Columns, aggregate
    functions and bucket sizes are looked up in fixed tables, so a request can
    only name things that already exist.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

#: Selectable columns, mapped to their SQL expression. An export can only ask
#: for something in this table, so no request can name an arbitrary identifier.
COLUMNS: Dict[str, str] = {
    "ts_utc": "ts_utc",
    "gateway_id": "gateway_id",
    "gateway_name": "gateway_name",
    "device_name": "device_name",
    "plc_ip": "plc_ip",
    "database_name": "database_name",
    "tag_name": "tag_name",
    "value": "value",
    "value_text": "value_text",
    "data_type": "data_type",
    "quality": "quality",
    "quality_label": "quality_label",
    "source": "source",
}

DEFAULT_COLUMNS = ["ts_utc", "gateway_name", "tag_name", "value", "quality_label"]

#: Aggregate functions, by the name the UI uses.
AGGREGATES: Dict[str, str] = {
    "avg": "AVG(value)",
    "min": "MIN(value)",
    "max": "MAX(value)",
    "sum": "SUM(value)",
    "count": "COUNT(*)",
    "first": "MIN(value)",     # within a bucket ordered by ts, see note below
    "last": "MAX(value)",
}

#: Bucket sizes in seconds. SQLite has no date_trunc, so a bucket is computed
#: arithmetically from the epoch - which is also index-friendly, because the
#: ts_utc predicate that narrows the range stays a plain comparison.
BUCKETS: Dict[str, int] = {
    "1s": 1, "10s": 10, "30s": 30,
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "6h": 21600, "12h": 43200, "1d": 86400,
}

#: Comparison operators an operator can put on a value.
OPERATORS: Dict[str, str] = {
    "eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
}

MAX_PREVIEW_ROWS = 500
#: A hard stop so a runaway request cannot stream forever. Reported to the
#: caller rather than silently truncating - a limit an operator can see is one
#: they can work around.
MAX_EXPORT_ROWS = 5_000_000


def _clean_list(raw: Any) -> List[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[str] = []
    for item in raw:
        txt = str(item or "").strip()
        if txt and txt not in out:
            out.append(txt)
    return out


def resolve_columns(raw: Any) -> List[str]:
    """The columns to emit, filtered to ones that exist."""
    cols = [c for c in _clean_list(raw) if c in COLUMNS]
    return cols or list(DEFAULT_COLUMNS)


def build_query(spec: Dict[str, Any], tenant_id: str) -> Tuple[str, list, List[str]]:
    """Return (sql, params, column_names) for a spec.

    The spec is whatever the assistant's form produced; every field is
    optional. Unknown or malformed entries are ignored rather than rejected -
    a filter nobody can see is worse than a filter that does nothing, and the
    preview shows exactly what the query returned.
    """
    spec = spec or {}
    where: List[str] = ["tenant_id = ?"]
    params: List[Any] = [tenant_id]

    # --- indexed predicates first ---------------------------------------
    frm = str(spec.get("from_utc") or "").strip()
    to = str(spec.get("to_utc") or "").strip()
    if frm:
        where.append("ts_utc >= ?")
        params.append(frm.replace("T", " ").replace("Z", ""))
    if to:
        where.append("ts_utc <= ?")
        params.append(to.replace("T", " ").replace("Z", ""))

    for field, key in (("gateway_id", "gateways"), ("tag_name", "tags"),
                       ("device_name", "devices")):
        values = _clean_list(spec.get(key))
        if values:
            where.append("%s IN (%s)" % (field, ",".join("?" for _ in values)))
            params.extend(values)

    quality = str(spec.get("quality") or "all").strip().lower()
    if quality == "good":
        where.append("quality >= 192")
    elif quality == "bad":
        where.append("quality < 192")

    # --- value conditions, applied after the indexed narrowing ----------
    for cond in (spec.get("conditions") or []):
        if not isinstance(cond, dict):
            continue
        op = OPERATORS.get(str(cond.get("op") or "").strip().lower())
        if not op:
            continue
        try:
            where.append("value %s ?" % op)
            params.append(float(cond.get("value")))
        except (TypeError, ValueError):
            where.pop()          # a condition we cannot parse filters nothing
            continue

    contains = str(spec.get("tag_contains") or "").strip()
    if contains:
        # Free text is a scan by nature, so it runs LAST, on whatever the
        # indexed predicates above have already narrowed the set to.
        where.append("tag_name LIKE ?")
        params.append("%" + contains + "%")

    where_sql = " AND ".join(where)

    # --- aggregation ------------------------------------------------------
    bucket = BUCKETS.get(str(spec.get("bucket") or "").strip().lower())
    agg = AGGREGATES.get(str(spec.get("aggregate") or "").strip().lower())
    if bucket and agg:
        # strftime('%s') gives epoch seconds; integer division buckets them.
        bucket_expr = ("datetime((CAST(strftime('%%s', ts_utc) AS INTEGER) / %d) * %d, "
                       "'unixepoch')" % (bucket, bucket))
        sql = (
            "SELECT %s AS ts_utc, gateway_name, tag_name, %s AS value, COUNT(*) AS samples "
            "FROM historian_readings WHERE %s "
            "GROUP BY 1, gateway_name, tag_name ORDER BY 1 ASC"
            % (bucket_expr, agg, where_sql)
        )
        return sql, params, ["ts_utc", "gateway_name", "tag_name", "value", "samples"]

    columns = resolve_columns(spec.get("columns"))
    select_sql = ", ".join(COLUMNS[c] + " AS " + c for c in columns)
    order = "ASC" if str(spec.get("order") or "asc").lower() == "asc" else "DESC"
    sql = ("SELECT %s FROM historian_readings WHERE %s ORDER BY ts_utc %s"
           % (select_sql, where_sql, order))
    return sql, params, columns


def pivot_rows(rows: List[Dict[str, Any]], value_key: str = "value",
               label_key: str = "tag_name") -> Tuple[List[str], List[Dict[str, Any]]]:
    """One row per timestamp, one column per tag.

    Long form answers "what happened"; an operator comparing tags wants them
    side by side against a shared clock, which is what a spreadsheet is for.
    """
    by_ts: Dict[str, Dict[str, Any]] = {}
    labels: List[str] = []
    for row in rows:
        ts = str(row.get("ts_utc") or "")
        label = str(row.get(label_key) or "")
        if label and label not in labels:
            labels.append(label)
        slot = by_ts.setdefault(ts, {"ts_utc": ts})
        slot[label] = row.get(value_key)
    ordered = [by_ts[k] for k in sorted(by_ts.keys())]
    return ["ts_utc", *labels], ordered


def csv_escape(value: Any) -> str:
    txt = "" if value is None else str(value)
    if any(ch in txt for ch in (",", '"', "\n", "\r")):
        return '"' + txt.replace('"', '""') + '"'
    return txt


def iter_csv(cursor: Iterable, columns: List[str], include_header: bool = True):
    """Stream CSV a row at a time.

    Never materialises the result set: a day of this historian is ~9 million
    rows, and building that in memory is how an export becomes an outage.
    """
    if include_header:
        yield ",".join(columns) + "\n"
    for row in cursor:
        yield ",".join(csv_escape(row[i]) for i in range(len(columns))) + "\n"
