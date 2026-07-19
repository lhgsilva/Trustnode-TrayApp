"""Batch Management v2 service layer (clean rebuild).

All SQLite access for the spec-named tables (batch_definition / _version / _tag /
batch_trigger_reference / batch_limit_definition / kpi_definition / batch_group /
batch / batch_data_window / batch_event / batch_kpi_result / batch_excursion /
batch_report_reference).

Guarantees (identical to the legacy service):
  * NEVER writes to historian_readings, gateway_configurations, or any table
    outside the batch_* / batch / kpi_definition set. Purely additive.
  * Reads the historian by TIME WINDOW only (batch_data_window) — no duplication
    of time-series rows.
  * Every read filters by tenant_id.

The state machine + transition guard live here (BatchExecutionService). KPI /
excursion / data-quality math live in calc_v2.py; report integration in
reports_v2.py — both composed by the router, not imported here (keeps this file
free of the report/email deps).

Guide: docs/BATCH_MANAGEMENT_REDESIGN_2026-07-14.md
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

# Reuse the proven helpers from the legacy service module (same conventions:
# id format, utc format, json coding, numeric parsing, window seconds).
from .service import (
    _utc_now, _new_id, _json_or_none, _json_load, _opt_num, _window_seconds,
)


# --------------------------------------------------------------------------- #
#  Batch lifecycle state machine (spec §8)
# --------------------------------------------------------------------------- #
#   PLANNED -> READY -> RUNNING <-> HELD -> COMPLETED | ABORTED
#   READY -> ABORTED,  (any non-terminal) -> INVALID
BATCH_STATES = {
    "planned", "ready", "running", "held", "completed", "aborted", "invalid",
}
BATCH_TERMINAL = {"completed", "aborted", "invalid"}

# allowed (from -> {to}) transitions. Anything not listed is rejected.
BATCH_TRANSITIONS: dict[str, set[str]] = {
    "planned": {"ready", "aborted", "invalid"},
    "ready":   {"running", "aborted", "invalid"},
    "running": {"held", "completed", "aborted", "invalid"},
    "held":    {"running", "completed", "aborted", "invalid"},
    "completed": set(),
    "aborted":   set(),
    "invalid":   set(),
}

GROUP_STATES = {"planned", "active", "completed", "aborted"}
GROUP_TRANSITIONS: dict[str, set[str]] = {
    "planned": {"active", "aborted"},
    "active":  {"completed", "aborted"},
    "completed": set(),
    "aborted":   set(),
}


class BatchStateError(ValueError):
    """Raised when an illegal state transition is attempted (router -> 409)."""


def _can_transition(current: str, target: str) -> bool:
    return target in BATCH_TRANSITIONS.get(str(current or "").lower(), set())


# --------------------------------------------------------------------------- #
#  Shared base — connection + tenant + event/audit writers
# --------------------------------------------------------------------------- #
class _BatchV2Base:
    def __init__(self, app_store) -> None:
        self._app_store = app_store

    def _connect(self) -> sqlite3.Connection:
        return self._app_store._connect()

    def _connect_readonly(self) -> sqlite3.Connection:
        return self._app_store._connect_readonly()

    def _tenant(self) -> str:
        return self._app_store._current_tenant_id()

    # -- event writer (on the SAME connection, before commit) -----------
    def _event(
        self,
        c: sqlite3.Connection,
        *,
        batch_id: Optional[str] = None,
        batch_group_id: Optional[str] = None,
        event_type: str,
        severity: str = "info",
        source: Optional[str] = None,
        source_reference: Optional[str] = None,
        message: Optional[str] = None,
        metadata: Any = None,
        actor: Optional[str] = None,
    ) -> None:
        now = _utc_now()
        c.execute(
            """
            INSERT INTO batch_event
              (id, tenant_id, batch_id, batch_group_id, event_type, event_utc,
               severity, source, source_reference, message, metadata_json,
               created_by, created_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _new_id("bev"), self._tenant(), batch_id, batch_group_id,
                event_type, now, severity, source, source_reference, message,
                _json_or_none(metadata), actor, now,
            ),
        )


# --------------------------------------------------------------------------- #
#  Batch Definition Service (spec §17 — draft/validate/publish/version)
# --------------------------------------------------------------------------- #
class BatchDefinitionService(_BatchV2Base):
    """Draft CRUD + immutable versioning. A published version freezes its
    configuration_json snapshot and can never be edited — edits create a new
    draft version."""

    def list_definitions(self) -> list[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                """
                SELECT d.*, v.version_number AS cur_version_number
                FROM batch_definition d
                LEFT JOIN batch_definition_version v ON v.id = d.current_version_id
                WHERE d.tenant_id = ?
                ORDER BY d.name COLLATE NOCASE
                """,
                (tid,),
            ).fetchall()
        return [self._row_to_definition(r) for r in rows]

    def get_definition(self, definition_id: str, *, version_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            d = c.execute(
                "SELECT * FROM batch_definition WHERE id = ? AND tenant_id = ?",
                (definition_id, tid),
            ).fetchone()
            if not d:
                return None
            out = self._row_to_definition(d)
            vid = version_id or d["current_version_id"] or self._latest_version_id(c, definition_id, tid)
            if vid:
                out["config"] = self._load_version_config(c, vid, tid)
                vrow = c.execute(
                    "SELECT version_number FROM batch_definition_version WHERE id = ? AND tenant_id = ?",
                    (vid, tid),
                ).fetchone()
                out["version_number"] = int(vrow["version_number"]) if vrow else None
                out["current_version_id"] = vid
        return out

    def list_versions(self, definition_id: str) -> list[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                """
                SELECT id, version_number, status, effective_from, created_utc,
                       published_utc, published_by
                FROM batch_definition_version
                WHERE batch_definition_id = ? AND tenant_id = ?
                ORDER BY version_number DESC
                """,
                (definition_id, tid),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_definition(
        self, payload: dict[str, Any], *, actor: Optional[str] = None,
        definition_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a new definition (with a draft v1) OR update the DRAFT version
        of an existing definition. A published definition can't be edited in
        place — call new_version() first (or this method auto-creates a fresh
        draft when the current version is published)."""
        tid = self._tenant()
        now = _utc_now()
        cfg = payload.get("config") or {}
        with self._connect() as c:
            if not definition_id:
                definition_id = _new_id("bdef")
                version_id = _new_id("bdefv")
                c.execute(
                    """
                    INSERT INTO batch_definition
                      (id, tenant_id, code, name, description, plant, area,
                       equipment_id, product, owner, status, current_version_id,
                       created_utc, created_by, updated_utc, updated_by)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        definition_id, tid, payload.get("code"), payload.get("name"),
                        payload.get("description"), payload.get("plant"),
                        payload.get("area"), payload.get("equipment_id"),
                        payload.get("product"), payload.get("owner"),
                        "draft", version_id, now, actor, now, actor,
                    ),
                )
                self._insert_version(c, tid, definition_id, version_id, 1, cfg, actor, now)
                self._event(c, batch_id=None, event_type="definition.created",
                            source="api", message=payload.get("name"), actor=actor,
                            metadata={"definition_id": definition_id})
            else:
                d = c.execute(
                    "SELECT * FROM batch_definition WHERE id = ? AND tenant_id = ?",
                    (definition_id, tid),
                ).fetchone()
                if not d:
                    raise ValueError("definition not found")
                # update header fields
                c.execute(
                    """
                    UPDATE batch_definition SET code=?, name=?, description=?, plant=?,
                        area=?, equipment_id=?, product=?, owner=?, updated_utc=?, updated_by=?
                    WHERE id = ? AND tenant_id = ?
                    """,
                    (
                        payload.get("code"), payload.get("name"), payload.get("description"),
                        payload.get("plant"), payload.get("area"), payload.get("equipment_id"),
                        payload.get("product"), payload.get("owner"), now, actor,
                        definition_id, tid,
                    ),
                )
                version_id = d["current_version_id"]
                vrow = c.execute(
                    "SELECT id, version_number, status FROM batch_definition_version WHERE id = ? AND tenant_id = ?",
                    (version_id, tid),
                ).fetchone() if version_id else None
                if (not vrow) or str(vrow["status"]) == "published":
                    # current version is published (immutable) -> spin a new draft
                    next_num = self._next_version_number(c, definition_id, tid)
                    version_id = _new_id("bdefv")
                    self._insert_version(c, tid, definition_id, version_id, next_num, cfg, actor, now)
                    c.execute(
                        "UPDATE batch_definition SET current_version_id = ?, status='draft', updated_utc=? WHERE id = ? AND tenant_id = ?",
                        (version_id, now, definition_id, tid),
                    )
                else:
                    # rewrite the existing draft version body
                    self._replace_version_body(c, tid, version_id, cfg, now)
                self._event(c, batch_id=None, event_type="definition.updated",
                            source="api", message=payload.get("name"), actor=actor,
                            metadata={"definition_id": definition_id, "version_id": version_id})
            c.commit()
        return self.get_definition(definition_id) or {}

    def new_version(self, definition_id: str, *, actor: Optional[str] = None) -> dict[str, Any]:
        """Clone the current version into a fresh DRAFT and point the definition at it."""
        tid = self._tenant()
        now = _utc_now()
        with self._connect() as c:
            d = c.execute(
                "SELECT * FROM batch_definition WHERE id = ? AND tenant_id = ?",
                (definition_id, tid),
            ).fetchone()
            if not d:
                raise ValueError("definition not found")
            src_cfg = self._load_version_config(c, d["current_version_id"], tid) if d["current_version_id"] else {}
            next_num = self._next_version_number(c, definition_id, tid)
            version_id = _new_id("bdefv")
            self._insert_version(c, tid, definition_id, version_id, next_num, src_cfg, actor, now)
            c.execute(
                "UPDATE batch_definition SET current_version_id = ?, status='draft', updated_utc=? WHERE id = ? AND tenant_id = ?",
                (version_id, now, definition_id, tid),
            )
            self._event(c, event_type="definition.version_created", source="api",
                        actor=actor, metadata={"definition_id": definition_id, "version_id": version_id, "version_number": next_num})
            c.commit()
        return self.get_definition(definition_id) or {}

    def validate_definition(self, definition_id: str) -> dict[str, Any]:
        """Spec §9 validation. Returns {ok, errors:[...], warnings:[...]}."""
        d = self.get_definition(definition_id)
        if not d:
            return {"ok": False, "errors": ["definition not found"], "warnings": []}
        cfg = d.get("config") or {}
        errors: list[str] = []
        warnings: list[str] = []
        if not (d.get("name") or "").strip():
            errors.append("Definition name is required.")
        mode = cfg.get("batch_mode") or "individual"
        if mode not in ("individual", "group", "both"):
            errors.append("Batch mode must be individual, group or both.")
        tags = cfg.get("tags") or []
        if not tags:
            warnings.append("No historian tags selected — reports and KPIs will be empty.")
        # a tag flagged required with no name is invalid
        for t in tags:
            if not str((t or {}).get("tag_name") or "").strip():
                errors.append("A selected tag is missing its historian tag name.")
                break
        # start/stop config presence
        if not cfg.get("start_config"):
            warnings.append("No start condition configured — batches must be started manually.")
        if not cfg.get("stop_config"):
            warnings.append("No stop condition configured — batches must be stopped manually.")
        # report/email consistency
        if cfg.get("auto_email_batch_report"):
            ec = cfg.get("email_config") or {}
            if not (ec.get("recipients")):
                errors.append("Auto-email is enabled but no recipients are configured.")
            if not cfg.get("batch_report_template_id"):
                warnings.append("Auto-email enabled without a batch report template — the default will be used.")
        return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings}

    def publish_definition(self, definition_id: str, *, actor: Optional[str] = None) -> dict[str, Any]:
        """Freeze the current draft version (immutable) + mark the definition published.
        Rejects if validation fails."""
        res = self.validate_definition(definition_id)
        if not res.get("ok"):
            raise ValueError("validation failed: " + "; ".join(res.get("errors") or []))
        tid = self._tenant()
        now = _utc_now()
        with self._connect() as c:
            d = c.execute(
                "SELECT current_version_id FROM batch_definition WHERE id = ? AND tenant_id = ?",
                (definition_id, tid),
            ).fetchone()
            if not d or not d["current_version_id"]:
                raise ValueError("definition not found")
            vid = d["current_version_id"]
            c.execute(
                """
                UPDATE batch_definition_version
                SET status='published', effective_from=?, published_utc=?, published_by=?
                WHERE id = ? AND tenant_id = ?
                """,
                (now, now, actor, vid, tid),
            )
            c.execute(
                "UPDATE batch_definition SET status='published', updated_utc=? WHERE id = ? AND tenant_id = ?",
                (now, definition_id, tid),
            )
            self._event(c, event_type="definition.published", source="api", actor=actor,
                        metadata={"definition_id": definition_id, "version_id": vid})
            c.commit()
        return self.get_definition(definition_id) or {}

    def delete_definition(self, definition_id: str, *, actor: Optional[str] = None) -> bool:
        """Delete a DRAFT-only definition (never delete one with batches). Published
        definitions are retired instead."""
        tid = self._tenant()
        with self._connect() as c:
            has_batch = c.execute(
                "SELECT 1 FROM batch WHERE definition_id = ? AND tenant_id = ? LIMIT 1",
                (definition_id, tid),
            ).fetchone()
            if has_batch:
                c.execute(
                    "UPDATE batch_definition SET status='retired', updated_utc=? WHERE id = ? AND tenant_id = ?",
                    (_utc_now(), definition_id, tid),
                )
                self._event(c, event_type="definition.retired", source="api", actor=actor,
                            metadata={"definition_id": definition_id})
                c.commit()
                return True
            vids = [r["id"] for r in c.execute(
                "SELECT id FROM batch_definition_version WHERE batch_definition_id = ? AND tenant_id = ?",
                (definition_id, tid)).fetchall()]
            for vid in vids:
                c.execute("DELETE FROM batch_limit_definition WHERE definition_tag_id IN "
                          "(SELECT id FROM batch_definition_tag WHERE definition_version_id = ?)", (vid,))
                c.execute("DELETE FROM batch_definition_tag WHERE definition_version_id = ?", (vid,))
                c.execute("DELETE FROM batch_trigger_reference WHERE definition_version_id = ?", (vid,))
                c.execute("DELETE FROM kpi_definition WHERE definition_version_id = ?", (vid,))
            c.execute("DELETE FROM batch_definition_version WHERE batch_definition_id = ? AND tenant_id = ?", (definition_id, tid))
            c.execute("DELETE FROM batch_definition WHERE id = ? AND tenant_id = ?", (definition_id, tid))
            self._event(c, event_type="definition.deleted", source="api", actor=actor,
                        metadata={"definition_id": definition_id})
            c.commit()
        return True

    # -- version internals --------------------------------------------
    def _insert_version(self, c, tid, definition_id, version_id, num, cfg, actor, now) -> None:
        c.execute(
            """
            INSERT INTO batch_definition_version
              (id, tenant_id, batch_definition_id, version_number, status,
               configuration_json, batch_mode, group_config_json, identification_json,
               start_config_json, stop_config_json, report_config_json,
               batch_report_template_id, batch_group_report_template_id,
               auto_generate_batch_report, auto_generate_batch_group_report,
               auto_email_batch_report, auto_email_batch_group_report,
               email_config_json, created_utc, created_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                version_id, tid, definition_id, num, "draft",
                _json_or_none(cfg), cfg.get("batch_mode") or "individual",
                _json_or_none(cfg.get("group_config")), _json_or_none(cfg.get("identification")),
                _json_or_none(cfg.get("start_config")), _json_or_none(cfg.get("stop_config")),
                _json_or_none(cfg.get("report_config")),
                cfg.get("batch_report_template_id"), cfg.get("batch_group_report_template_id"),
                1 if cfg.get("auto_generate_batch_report") else 0,
                1 if cfg.get("auto_generate_batch_group_report") else 0,
                1 if cfg.get("auto_email_batch_report") else 0,
                1 if cfg.get("auto_email_batch_group_report") else 0,
                _json_or_none(cfg.get("email_config")), now, actor,
            ),
        )
        self._write_version_children(c, tid, version_id, cfg)

    def _replace_version_body(self, c, tid, version_id, cfg, now) -> None:
        c.execute(
            """
            UPDATE batch_definition_version SET configuration_json=?, batch_mode=?,
                group_config_json=?, identification_json=?, start_config_json=?,
                stop_config_json=?, report_config_json=?, batch_report_template_id=?,
                batch_group_report_template_id=?, auto_generate_batch_report=?,
                auto_generate_batch_group_report=?, auto_email_batch_report=?,
                auto_email_batch_group_report=?, email_config_json=?
            WHERE id = ? AND tenant_id = ?
            """,
            (
                _json_or_none(cfg), cfg.get("batch_mode") or "individual",
                _json_or_none(cfg.get("group_config")), _json_or_none(cfg.get("identification")),
                _json_or_none(cfg.get("start_config")), _json_or_none(cfg.get("stop_config")),
                _json_or_none(cfg.get("report_config")),
                cfg.get("batch_report_template_id"), cfg.get("batch_group_report_template_id"),
                1 if cfg.get("auto_generate_batch_report") else 0,
                1 if cfg.get("auto_generate_batch_group_report") else 0,
                1 if cfg.get("auto_email_batch_report") else 0,
                1 if cfg.get("auto_email_batch_group_report") else 0,
                _json_or_none(cfg.get("email_config")), version_id, tid,
            ),
        )
        # rewrite children (tags/limits/triggers/kpis) — full-set replace
        old_tag_ids = [r["id"] for r in c.execute(
            "SELECT id FROM batch_definition_tag WHERE definition_version_id = ?", (version_id,)).fetchall()]
        for tgid in old_tag_ids:
            c.execute("DELETE FROM batch_limit_definition WHERE definition_tag_id = ?", (tgid,))
        c.execute("DELETE FROM batch_definition_tag WHERE definition_version_id = ?", (version_id,))
        c.execute("DELETE FROM batch_trigger_reference WHERE definition_version_id = ?", (version_id,))
        c.execute("DELETE FROM kpi_definition WHERE definition_version_id = ?", (version_id,))
        self._write_version_children(c, tid, version_id, cfg)

    def _write_version_children(self, c, tid, version_id, cfg) -> None:
        now = _utc_now()
        for i, t in enumerate(cfg.get("tags") or []):
            tag_id = _new_id("bdtag")
            c.execute(
                """
                INSERT INTO batch_definition_tag
                  (id, tenant_id, definition_version_id, gateway_id, historian_tag_id,
                   tag_name, display_name, engineering_unit, data_type, tag_category,
                   required, report_enabled, trend_enabled, chart_group,
                   expected_sample_rate_s, sort_order)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    tag_id, tid, version_id, t.get("gateway_id"), t.get("historian_tag_id"),
                    t.get("tag_name"), t.get("display_name"), t.get("engineering_unit"),
                    t.get("data_type"), t.get("tag_category"),
                    1 if t.get("required") else 0,
                    0 if t.get("report_enabled") is False else 1,
                    0 if t.get("trend_enabled") is False else 1,
                    t.get("chart_group"), _opt_num(t.get("expected_sample_rate_s")),
                    int(t.get("sort_order") or i),
                ),
            )
            for lim in (t.get("limits") or []):
                c.execute(
                    """
                    INSERT INTO batch_limit_definition
                      (id, tenant_id, definition_tag_id, limit_type, limit_value,
                       severity, persistence_seconds, enabled)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        _new_id("blim"), tid, tag_id, lim.get("limit_type"),
                        _opt_num(lim.get("limit_value")), lim.get("severity") or "warning",
                        _opt_num(lim.get("persistence_seconds")) or 0,
                        0 if lim.get("enabled") is False else 1,
                    ),
                )
        for tr in (cfg.get("triggers") or []):
            c.execute(
                """
                INSERT INTO batch_trigger_reference
                  (id, tenant_id, definition_version_id, trigger_scope, gateway_id,
                   existing_trigger_id, condition_json, enabled)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    _new_id("btrig"), tid, version_id, tr.get("trigger_scope"),
                    tr.get("gateway_id"), tr.get("existing_trigger_id"),
                    _json_or_none(tr.get("condition")),
                    0 if tr.get("enabled") is False else 1,
                ),
            )
        for i, k in enumerate(cfg.get("kpis") or []):
            c.execute(
                """
                INSERT INTO kpi_definition
                  (id, tenant_id, definition_version_id, code, name, scope,
                   calculation_type, configuration_json, engineering_unit, enabled, sort_order)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    _new_id("kpi"), tid, version_id, k.get("code"), k.get("name"),
                    k.get("scope") or "batch", k.get("calculation_type"),
                    _json_or_none(k.get("configuration")), k.get("engineering_unit"),
                    0 if k.get("enabled") is False else 1, int(k.get("sort_order") or i),
                ),
            )

    def _load_version_config(self, c, version_id: str, tid: str) -> dict[str, Any]:
        v = c.execute(
            "SELECT * FROM batch_definition_version WHERE id = ? AND tenant_id = ?",
            (version_id, tid),
        ).fetchone()
        if not v:
            return {}
        cfg: dict[str, Any] = {
            "batch_mode": v["batch_mode"],
            "group_config": _json_load(v["group_config_json"]),
            "identification": _json_load(v["identification_json"]),
            "start_config": _json_load(v["start_config_json"]),
            "stop_config": _json_load(v["stop_config_json"]),
            "report_config": _json_load(v["report_config_json"]),
            "batch_report_template_id": v["batch_report_template_id"],
            "batch_group_report_template_id": v["batch_group_report_template_id"],
            "auto_generate_batch_report": bool(v["auto_generate_batch_report"]),
            "auto_generate_batch_group_report": bool(v["auto_generate_batch_group_report"]),
            "auto_email_batch_report": bool(v["auto_email_batch_report"]),
            "auto_email_batch_group_report": bool(v["auto_email_batch_group_report"]),
            "email_config": _json_load(v["email_config_json"]),
        }
        tags = []
        for tg in c.execute(
            "SELECT * FROM batch_definition_tag WHERE definition_version_id = ? ORDER BY sort_order",
            (version_id,),
        ).fetchall():
            limits = [dict(l) for l in c.execute(
                "SELECT limit_type, limit_value, severity, persistence_seconds, enabled "
                "FROM batch_limit_definition WHERE definition_tag_id = ?", (tg["id"],)).fetchall()]
            td = dict(tg)
            td["limits"] = limits
            tags.append(td)
        cfg["tags"] = tags
        cfg["triggers"] = [dict(r) for r in c.execute(
            "SELECT trigger_scope, gateway_id, existing_trigger_id, condition_json, enabled "
            "FROM batch_trigger_reference WHERE definition_version_id = ?", (version_id,)).fetchall()]
        for tr in cfg["triggers"]:
            tr["condition"] = _json_load(tr.pop("condition_json", None))
        cfg["kpis"] = [dict(r) for r in c.execute(
            "SELECT code, name, scope, calculation_type, configuration_json, engineering_unit, enabled, sort_order "
            "FROM kpi_definition WHERE definition_version_id = ? ORDER BY sort_order", (version_id,)).fetchall()]
        for k in cfg["kpis"]:
            k["configuration"] = _json_load(k.pop("configuration_json", None))
        # Custom properties AND charts live ONLY in the raw configuration_json
        # (neither has a child table, unlike tags/triggers/kpis). This rebuild
        # walks the child tables, so both must be re-injected from the raw blob
        # or they read back null — which is exactly why a definition's Charts
        # step looked empty on edit and the batch view rendered no chart cards.
        raw = _json_load(v["configuration_json"]) or {}
        if isinstance(raw, dict):
            for _k in ("properties", "charts"):
                if raw.get(_k):
                    cfg[_k] = raw[_k]
            # Per-tag chart axis + axis_options have no dedicated columns, so the
            # child-table rebuild above drops them. Re-inject from the raw blob,
            # matched by tag_name, so the Tags & Limits axis config round-trips.
            raw_by_name = {}
            for rt in (raw.get("tags") or []):
                nm = str((rt or {}).get("tag_name") or "").strip()
                if nm:
                    raw_by_name[nm] = rt
            for td in cfg["tags"]:
                rt = raw_by_name.get(str(td.get("tag_name") or "").strip())
                if rt:
                    if rt.get("chart_axis") is not None:
                        td["chart_axis"] = rt.get("chart_axis")
                    if rt.get("axis_options") is not None:
                        td["axis_options"] = rt.get("axis_options")
        return cfg

    def _latest_version_id(self, c, definition_id, tid) -> Optional[str]:
        r = c.execute(
            "SELECT id FROM batch_definition_version WHERE batch_definition_id = ? AND tenant_id = ? "
            "ORDER BY version_number DESC LIMIT 1", (definition_id, tid)).fetchone()
        return r["id"] if r else None

    def _next_version_number(self, c, definition_id, tid) -> int:
        r = c.execute(
            "SELECT COALESCE(MAX(version_number),0) AS n FROM batch_definition_version "
            "WHERE batch_definition_id = ? AND tenant_id = ?", (definition_id, tid)).fetchone()
        return int(r["n"]) + 1

    @staticmethod
    def _row_to_definition(r) -> dict[str, Any]:
        d = dict(r)
        d.pop("cur_version_number", None)
        return d


# --------------------------------------------------------------------------- #
#  Batch Group Service
# --------------------------------------------------------------------------- #
class BatchGroupService(_BatchV2Base):
    def list_groups(self, *, status: Optional[str] = None, limit: int = 200, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        tid = self._tenant()
        where = ["tenant_id = ?"]
        params: list[Any] = [tid]
        if status:
            where.append("status = ?"); params.append(status)
        wc = " AND ".join(where)
        with self._connect_readonly() as c:
            total = c.execute(f"SELECT COUNT(*) FROM batch_group WHERE {wc}", params).fetchone()[0]
            rows = c.execute(
                f"SELECT * FROM batch_group WHERE {wc} ORDER BY created_utc DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    def get_group(self, group_id: str) -> Optional[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            r = c.execute("SELECT * FROM batch_group WHERE id = ? AND tenant_id = ?", (group_id, tid)).fetchone()
        return dict(r) if r else None

    def create_group(self, payload: dict[str, Any], *, actor: Optional[str] = None) -> dict[str, Any]:
        tid = self._tenant()
        now = _utc_now()
        gid = _new_id("bgrp")
        defn_version_id = None
        if payload.get("definition_id"):
            with self._connect_readonly() as c:
                d = c.execute("SELECT current_version_id FROM batch_definition WHERE id = ? AND tenant_id = ?",
                              (payload["definition_id"], tid)).fetchone()
                defn_version_id = d["current_version_id"] if d else None
        ref = payload.get("reference") or self._auto_group_reference(now)
        with self._connect() as c:
            c.execute(
                """
                INSERT INTO batch_group
                  (id, tenant_id, reference, external_reference, definition_id,
                   definition_version_id, equipment_id, status, expected_child_count,
                   actual_child_count, started_utc, completed_utc, created_utc, created_by, updated_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    gid, tid, ref, payload.get("external_reference"), payload.get("definition_id"),
                    defn_version_id, payload.get("equipment_id"), "active",
                    payload.get("expected_child_count"), 0, now, None, now, actor, now,
                ),
            )
            self._event(c, batch_group_id=gid, event_type="group.created", source="api",
                        message=ref, actor=actor)
            c.commit()
        return self.get_group(gid) or {}

    def complete_group(self, group_id: str, *, actor: Optional[str] = None) -> dict[str, Any]:
        return self._transition_group(group_id, "completed", actor=actor, event="group.completed")

    def abort_group(self, group_id: str, *, actor: Optional[str] = None) -> dict[str, Any]:
        return self._transition_group(group_id, "aborted", actor=actor, event="group.aborted")

    def child_batches(self, group_id: str) -> list[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                "SELECT * FROM batch WHERE batch_group_id = ? AND tenant_id = ? ORDER BY created_utc DESC",
                (group_id, tid),
            ).fetchall()
        return [_row_to_batch(r) for r in rows]

    def delete_group(self, group_id: str, *, actor: Optional[str] = None) -> bool:
        """Delete a group and ALL its child batches (with their owned rows). A
        group with a RUNNING child must be stopped first."""
        tid = self._tenant()
        with self._connect() as c:
            g = c.execute("SELECT 1 FROM batch_group WHERE id = ? AND tenant_id = ?", (group_id, tid)).fetchone()
            if not g:
                return False
            running = c.execute(
                "SELECT 1 FROM batch WHERE batch_group_id = ? AND tenant_id = ? AND LOWER(status)='running' LIMIT 1",
                (group_id, tid)).fetchone()
            if running:
                raise BatchStateError("Stop the group's running batch(es) before deleting the group")
            child_ids = [r["id"] for r in c.execute(
                "SELECT id FROM batch WHERE batch_group_id = ? AND tenant_id = ?", (group_id, tid)).fetchall()]
            for bid in child_ids:
                for tbl in ("batch_kpi_result", "batch_excursion", "batch_event",
                            "batch_data_window", "batch_property_value", "batch_report_reference"):
                    c.execute(f"DELETE FROM {tbl} WHERE batch_id = ? AND tenant_id = ?", (bid, tid))
            c.execute("DELETE FROM batch WHERE batch_group_id = ? AND tenant_id = ?", (group_id, tid))
            # group-level owned rows
            for tbl in ("batch_kpi_result", "batch_excursion", "batch_event", "batch_report_reference"):
                c.execute(f"DELETE FROM {tbl} WHERE batch_group_id = ? AND tenant_id = ?", (group_id, tid))
            c.execute("DELETE FROM batch_group WHERE id = ? AND tenant_id = ?", (group_id, tid))
            c.commit()
        return True

    def _transition_group(self, group_id, target, *, actor, event) -> dict[str, Any]:
        tid = self._tenant()
        now = _utc_now()
        with self._connect() as c:
            r = c.execute("SELECT status FROM batch_group WHERE id = ? AND tenant_id = ?", (group_id, tid)).fetchone()
            if not r:
                raise ValueError("group not found")
            cur = str(r["status"])
            if target not in GROUP_TRANSITIONS.get(cur, set()):
                raise BatchStateError(f"Cannot move group from {cur} to {target}")
            col = "completed_utc" if target == "completed" else "completed_utc"
            c.execute(
                f"UPDATE batch_group SET status=?, {col}=?, updated_utc=? WHERE id = ? AND tenant_id = ?",
                (target, now, now, group_id, tid),
            )
            self._event(c, batch_group_id=group_id, event_type=event, source="api", actor=actor)
            c.commit()
        return self.get_group(group_id) or {}

    def refresh_child_count(self, c: sqlite3.Connection, group_id: str, tid: str) -> None:
        n = c.execute("SELECT COUNT(*) FROM batch WHERE batch_group_id = ? AND tenant_id = ?", (group_id, tid)).fetchone()[0]
        c.execute("UPDATE batch_group SET actual_child_count = ?, updated_utc = ? WHERE id = ? AND tenant_id = ?",
                  (int(n), _utc_now(), group_id, tid))

    @staticmethod
    def _auto_group_reference(now: str) -> str:
        return "GRP-" + now.replace("-", "").replace(":", "").replace(" ", "").replace(".", "")[:14]


# --------------------------------------------------------------------------- #
#  Batch Execution Service (state machine + windows + idempotency + recovery)
# --------------------------------------------------------------------------- #
class BatchExecutionService(_BatchV2Base):
    def __init__(self, app_store) -> None:
        super().__init__(app_store)
        self._groups = BatchGroupService(app_store)

    # -- reads ---------------------------------------------------------
    def list_batches(
        self, *, limit: int = 200, offset: int = 0, status: Optional[str] = None,
        batch_group_id: Optional[str] = None, definition_id: Optional[str] = None,
        equipment_id: Optional[str] = None, search: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], int]:
        tid = self._tenant()
        where = ["tenant_id = ?"]
        params: list[Any] = [tid]
        if status:
            where.append("status = ?"); params.append(status)
        if batch_group_id:
            where.append("batch_group_id = ?"); params.append(batch_group_id)
        if definition_id:
            where.append("definition_id = ?"); params.append(definition_id)
        if equipment_id:
            where.append("equipment_id = ?"); params.append(equipment_id)
        if search:
            where.append("(reference LIKE ? OR product LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        wc = " AND ".join(where)
        with self._connect_readonly() as c:
            total = c.execute(f"SELECT COUNT(*) FROM batch WHERE {wc}", params).fetchone()[0]
            rows = c.execute(
                f"SELECT * FROM batch WHERE {wc} ORDER BY COALESCE(started_utc, created_utc) DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
        return [_row_to_batch(r) for r in rows], int(total)

    def get_batch(self, batch_id: str) -> Optional[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            r = c.execute("SELECT * FROM batch WHERE id = ? AND tenant_id = ?", (batch_id, tid)).fetchone()
        return _row_to_batch(r) if r else None

    def version_config_for_batch(self, batch_id: str) -> Optional[dict[str, Any]]:
        """The batch's definition-version configuration_json (start/stop config,
        tags, charts, report config). Read-only; used for barcode rules etc."""
        tid = self._tenant()
        b = self.get_batch(batch_id)
        ver = (b or {}).get("definition_version_id")
        if not ver:
            return None
        with self._connect_readonly() as c:
            r = c.execute("SELECT configuration_json FROM batch_definition_version WHERE id = ? AND tenant_id = ?",
                          (ver, tid)).fetchone()
        return (_json_load(r["configuration_json"]) if r else None) or None

    def set_batch_metadata(self, batch_id: str, patch: dict[str, Any]) -> bool:
        """Merge `patch` into the batch's metadata_json (shallow)."""
        tid = self._tenant()
        with self._connect() as c:
            r = c.execute("SELECT metadata_json FROM batch WHERE id = ? AND tenant_id = ?",
                          (batch_id, tid)).fetchone()
            if not r:
                return False
            meta = _json_load(r["metadata_json"]) or {}
            if not isinstance(meta, dict):
                meta = {}
            meta.update(patch or {})
            c.execute("UPDATE batch SET metadata_json = ?, updated_utc = ? WHERE id = ? AND tenant_id = ?",
                      (_json_or_none(meta), _utc_now(), batch_id, tid))
            c.commit()
        return True

    def delete_batch(self, batch_id: str, *, actor: Optional[str] = None) -> bool:
        """Hard-delete a batch and ALL of its owned rows (KPIs, excursions,
        events, data windows, properties, report references). A RUNNING batch
        must be stopped/aborted first (guards accidental deletion of live data).
        If it's a group child, refresh the parent's child count afterward."""
        tid = self._tenant()
        with self._connect() as c:
            r = c.execute("SELECT status, batch_group_id FROM batch WHERE id = ? AND tenant_id = ?",
                          (batch_id, tid)).fetchone()
            if not r:
                return False
            if str(r["status"] or "").lower() == "running":
                raise BatchStateError("Stop or abort the batch before deleting it")
            gid = r["batch_group_id"]
            for tbl in ("batch_kpi_result", "batch_excursion", "batch_event",
                        "batch_data_window", "batch_property_value", "batch_report_reference"):
                c.execute(f"DELETE FROM {tbl} WHERE batch_id = ? AND tenant_id = ?", (batch_id, tid))
            c.execute("DELETE FROM batch WHERE id = ? AND tenant_id = ?", (batch_id, tid))
            if gid:
                try:
                    self._groups.refresh_child_count(c, gid, tid)
                except Exception:
                    pass
                self._event(c, batch_group_id=gid, event_type="batch.deleted", source="api",
                            actor=actor, message=batch_id)
            c.commit()
        return True

    # -- create --------------------------------------------------------
    def create_batch(
        self, payload: dict[str, Any], *, actor: Optional[str] = None,
        source: str = "api", idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a PLANNED batch. If an idempotency_key is supplied and a batch
        with that key already exists (same tenant), return the existing one
        instead of creating a duplicate (spec §21)."""
        tid = self._tenant()
        now = _utc_now()
        idem = idempotency_key or payload.get("idempotency_key")
        if idem:
            existing = self._find_by_idem(idem)
            if existing:
                return existing
        defn_id = payload.get("definition_id")
        defn_version_id = payload.get("definition_version_id")
        if defn_id and not defn_version_id:
            with self._connect_readonly() as c:
                d = c.execute("SELECT current_version_id FROM batch_definition WHERE id = ? AND tenant_id = ?",
                              (defn_id, tid)).fetchone()
                defn_version_id = d["current_version_id"] if d else None
        bid = _new_id("batch")
        ref = payload.get("reference") or self._auto_reference(now)
        seq = payload.get("sequence_number")
        gid = payload.get("batch_group_id")
        with self._connect() as c:
            c.execute(
                """
                INSERT INTO batch
                  (id, tenant_id, reference, batch_group_id, definition_id,
                   definition_version_id, equipment_id, status, quality_status,
                   data_quality_status, sequence_number, trigger_mode, started_utc,
                   ended_utc, start_reason, stop_reason, idempotency_key, product,
                   notes, metadata_json, created_utc, created_by, updated_utc, updated_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    bid, tid, ref, gid, defn_id, defn_version_id,
                    payload.get("equipment_id"), "planned", "not_evaluated",
                    "not_evaluated", seq, payload.get("trigger_mode") or source, None,
                    None, None, None, idem, payload.get("product"),
                    payload.get("notes"), _json_or_none(payload.get("metadata")),
                    now, actor, now, actor,
                ),
            )
            self._event(c, batch_id=bid, batch_group_id=gid, event_type="batch.created",
                        source=source, message=ref, actor=actor)
            # Persist any MANUAL property values supplied at create time (per-batch
            # entries like order # / barcode the operator typed). Linked/snapshot
            # properties are captured later, at start/end, in _capture_properties.
            self._store_manual_properties(c, tid, bid, gid, defn_version_id,
                                          payload.get("properties") or {}, actor)
            if gid:
                self._groups.refresh_child_count(c, gid, tid)
            c.commit()
        return self.get_batch(bid) or {}

    # -- custom properties (barcode / order # / equipment / ...) -------
    def _definition_properties(self, version_id: Optional[str]) -> list[dict[str, Any]]:
        """The property SCHEMA declared on the definition, read from the version's
        configuration_json (config.properties[]). Returns [] when none/absent."""
        if not version_id:
            return []
        tid = self._tenant()
        with self._connect_readonly() as c:
            r = c.execute(
                "SELECT configuration_json FROM batch_definition_version WHERE id = ? AND tenant_id = ?",
                (version_id, tid)).fetchone()
        if not r:
            return []
        cfg = _json_load(r["configuration_json"]) or {}
        props = cfg.get("properties") if isinstance(cfg, dict) else None
        return [p for p in (props or []) if isinstance(p, dict) and p.get("key")]

    def _latest_historian_value(self, c, tid, gateway_id, tag_name, at_utc):
        """Snapshot the single most-recent historian value for a tag AT (or just
        before) a timestamp. This is the whole point of a LINKED property: the
        gateway already collected the tag once — we just read the last value in
        the window. No PLC re-poll, no continuous trending."""
        if not tag_name:
            return None, None
        params: list[Any] = [tid, str(tag_name), str(at_utc)]
        gw_clause = ""
        if gateway_id:
            gw_clause = " AND gateway_id = ?"; params.append(str(gateway_id))
        row = c.execute(
            f"""SELECT value, value_text FROM historian_readings
                WHERE tenant_id = ? AND tag_name = ? AND ts_utc <= ?{gw_clause}
                ORDER BY ts_utc DESC LIMIT 1""",
            params).fetchone()
        if not row:
            return None, None
        num = _opt_num(row["value"])
        txt = row["value_text"]
        if txt is None and num is not None:
            txt = str(num)
        return num, txt

    def _store_manual_properties(self, c, tid, batch_id, group_id, version_id, values, actor) -> None:
        """Upsert manual property values (typed per batch). `values` is
        {prop_key: text}. Only keys declared manual on the definition are stored."""
        if not values:
            return
        schema = {p["key"]: p for p in self._definition_properties(version_id)}
        now = _utc_now()
        for key, raw in (values or {}).items():
            spec = schema.get(key)
            if not spec or spec.get("source") == "linked":
                continue  # unknown or linked -> not a manual entry
            txt = "" if raw is None else str(raw)
            c.execute(
                """
                INSERT INTO batch_property_value
                  (id, tenant_id, batch_id, batch_group_id, prop_key, label, source,
                   capture_at, gateway_id, tag_name, value_text, value_numeric,
                   captured_utc, captured_source, created_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id, batch_id, prop_key) DO UPDATE SET
                   value_text = excluded.value_text, value_numeric = excluded.value_numeric,
                   captured_utc = excluded.captured_utc, captured_source = excluded.captured_source
                """,
                (_new_id("bprop"), tid, batch_id, group_id, key, spec.get("label") or key,
                 "manual", None, None, None, txt, _opt_num(raw), now, "operator", now),
            )

    def _capture_properties(self, c, tid, batch_id, group_id, version_id, when, at_utc, actor) -> None:
        """Capture LINKED property snapshots whose capture_at == `when` (start|end).
        Reads the latest historian value for each linked tag at at_utc and upserts
        it. Runs inside the caller's transaction (the start/stop transition)."""
        props = [p for p in self._definition_properties(version_id)
                 if p.get("source") == "linked" and (p.get("capture_at") or "start") == when]
        if not props:
            return
        now = _utc_now()
        for p in props:
            gw = p.get("gateway_id"); tag = p.get("tag_name")
            num, txt = self._latest_historian_value(c, tid, gw, tag, at_utc)
            c.execute(
                """
                INSERT INTO batch_property_value
                  (id, tenant_id, batch_id, batch_group_id, prop_key, label, source,
                   capture_at, gateway_id, tag_name, value_text, value_numeric,
                   captured_utc, captured_source, created_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id, batch_id, prop_key) DO UPDATE SET
                   value_text = excluded.value_text, value_numeric = excluded.value_numeric,
                   gateway_id = excluded.gateway_id, tag_name = excluded.tag_name,
                   captured_utc = excluded.captured_utc, captured_source = excluded.captured_source
                """,
                (_new_id("bprop"), tid, batch_id, group_id, p["key"], p.get("label") or p["key"],
                 "linked", when, gw, tag, txt, num, at_utc, "system", now),
            )

    def list_properties(self, batch_id: str) -> list[dict[str, Any]]:
        """Captured property values for a batch (for the detail header + reports)."""
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                "SELECT prop_key, label, source, capture_at, gateway_id, tag_name, "
                "value_text, value_numeric, captured_utc FROM batch_property_value "
                "WHERE batch_id = ? AND tenant_id = ? ORDER BY created_utc",
                (batch_id, tid)).fetchall()
        return [dict(r) for r in rows]

    def definition_properties_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        """Property SCHEMA that applies to a batch (so the UI can prompt for the
        manual ones at start). Reads the batch's pinned definition version."""
        b = self.get_batch(batch_id)
        return self._definition_properties(b.get("definition_version_id")) if b else []

    # -- state transitions --------------------------------------------
    def _transition(
        self, batch_id: str, target: str, *, actor: Optional[str] = None,
        source: str = "api", reason: Optional[str] = None,
        set_started: bool = False, set_ended: bool = False,
        equipment_id: Optional[str] = None, extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        tid = self._tenant()
        now = _utc_now()
        with self._connect() as c:
            r = c.execute("SELECT * FROM batch WHERE id = ? AND tenant_id = ?", (batch_id, tid)).fetchone()
            if not r:
                raise ValueError("batch not found")
            cur = str(r["status"]).lower()
            if not _can_transition(cur, target):
                raise BatchStateError(f"Illegal transition {cur} -> {target}")
            sets = ["status = ?", "updated_utc = ?", "updated_by = ?"]
            params: list[Any] = [target, now, actor]
            if reason and target in ("aborted", "invalid", "held"):
                sets.append("stop_reason = ?"); params.append(reason)
            if set_started:
                sets.append("started_utc = ?"); params.append(now)
                if reason:
                    sets.append("start_reason = ?"); params.append(reason)
                if equipment_id:
                    sets.append("equipment_id = ?"); params.append(equipment_id)
            if set_ended:
                sets.append("ended_utc = ?"); params.append(now)
            for k, v in (extra or {}).items():
                sets.append(f"{k} = ?"); params.append(v)
            params.extend([batch_id, tid])
            c.execute(f"UPDATE batch SET {', '.join(sets)} WHERE id = ? AND tenant_id = ?", params)
            # window management
            eq = equipment_id or r["equipment_id"]
            if set_started:
                self._open_window(c, tid, batch_id, eq, now)
            if target == "held":
                self._close_open_window(c, tid, batch_id, now)  # pause: close current window
            if cur == "held" and target == "running":
                self._open_window(c, tid, batch_id, eq, now)     # resume: open a fresh window
            if set_ended:
                self._close_open_window(c, tid, batch_id, now)
            # Capture LINKED custom-property snapshots at the moment of start/end.
            # When start is PLC-trigger-driven, this rides the same transition —
            # the @start snapshot fires automatically with the trigger, no extra
            # wiring. Reads the latest historian value (no PLC re-poll).
            ver = r["definition_version_id"]
            if set_started:
                self._capture_properties(c, tid, batch_id, r["batch_group_id"], ver, "start", now, actor)
            if set_ended:
                self._capture_properties(c, tid, batch_id, r["batch_group_id"], ver, "end", now, actor)
            self._event(c, batch_id=batch_id, batch_group_id=r["batch_group_id"],
                        event_type=f"batch.{target}", source=source, message=reason, actor=actor)
            c.commit()
        return self.get_batch(batch_id) or {}

    def mark_ready(self, batch_id, *, actor=None, source="api") -> dict[str, Any]:
        return self._transition(batch_id, "ready", actor=actor, source=source)

    def start_batch(self, batch_id, *, actor=None, source="api", reason=None, equipment_id=None) -> dict[str, Any]:
        # allow PLANNED->...->RUNNING by auto-readying if needed
        b = self.get_batch(batch_id)
        if b and str(b.get("status")) == "planned":
            self._transition(batch_id, "ready", actor=actor, source=source)
        return self._transition(batch_id, "running", actor=actor, source=source, reason=reason,
                                set_started=True, equipment_id=equipment_id)

    def hold_batch(self, batch_id, *, actor=None, source="api", reason=None) -> dict[str, Any]:
        return self._transition(batch_id, "held", actor=actor, source=source, reason=reason)

    def resume_batch(self, batch_id, *, actor=None, source="api") -> dict[str, Any]:
        return self._transition(batch_id, "running", actor=actor, source=source)

    def stop_batch(self, batch_id, *, actor=None, source="api", reason=None, quality_status=None) -> dict[str, Any]:
        extra = {"quality_status": quality_status} if quality_status else None
        return self._transition(batch_id, "completed", actor=actor, source=source, reason=reason,
                                set_ended=True, extra=extra)

    def abort_batch(self, batch_id, *, actor=None, source="api", reason=None) -> dict[str, Any]:
        return self._transition(batch_id, "aborted", actor=actor, source=source, reason=reason, set_ended=True)

    def invalidate_batch(self, batch_id, *, actor=None, source="system", reason=None) -> dict[str, Any]:
        return self._transition(batch_id, "invalid", actor=actor, source=source, reason=reason, set_ended=True)

    def add_comment(self, batch_id, message: str, *, actor: Optional[str] = None) -> dict[str, Any]:
        tid = self._tenant()
        with self._connect() as c:
            r = c.execute("SELECT batch_group_id FROM batch WHERE id = ? AND tenant_id = ?", (batch_id, tid)).fetchone()
            if not r:
                raise ValueError("batch not found")
            self._event(c, batch_id=batch_id, batch_group_id=r["batch_group_id"],
                        event_type="batch.comment", source="api", message=message, actor=actor)
            c.commit()
        return {"ok": True}

    def set_quality(self, c: sqlite3.Connection, tid: str, batch_id: str,
                    *, quality: Optional[str] = None, data_quality: Optional[str] = None) -> None:
        """Used by the calc pass to write quality/data-quality on the same conn."""
        sets, params = [], []
        if quality:
            sets.append("quality_status = ?"); params.append(quality)
        if data_quality:
            sets.append("data_quality_status = ?"); params.append(data_quality)
        if not sets:
            return
        sets.append("updated_utc = ?"); params.append(_utc_now())
        params.extend([batch_id, tid])
        c.execute(f"UPDATE batch SET {', '.join(sets)} WHERE id = ? AND tenant_id = ?", params)

    # -- events / timeline --------------------------------------------
    def list_events(self, batch_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                "SELECT * FROM batch_event WHERE batch_id = ? AND tenant_id = ? ORDER BY event_utc DESC LIMIT ?",
                (batch_id, tid, max(1, min(limit, 2000))),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r); d["metadata"] = _json_load(d.pop("metadata_json", None)); out.append(d)
        return out

    # -- data windows (historian join) --------------------------------
    def _open_window(self, c, tid, batch_id, gateway_id, start) -> None:
        # don't double-open: if there's an open (end IS NULL) window, leave it.
        openw = c.execute(
            "SELECT id FROM batch_data_window WHERE batch_id = ? AND tenant_id = ? AND window_end IS NULL",
            (batch_id, tid)).fetchone()
        if openw:
            return
        c.execute(
            """
            INSERT INTO batch_data_window
              (id, tenant_id, batch_id, historian_source_id, gateway_id,
               window_start, window_end, pre_buffer_seconds, post_buffer_seconds,
               query_metadata_json, created_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (_new_id("bwin"), tid, batch_id, None, gateway_id, start, None, 0, 0, None, _utc_now()),
        )

    def _close_open_window(self, c, tid, batch_id, end) -> None:
        c.execute(
            "UPDATE batch_data_window SET window_end = ? WHERE batch_id = ? AND tenant_id = ? AND window_end IS NULL",
            (end, batch_id, tid),
        )

    def windows_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                "SELECT gateway_id, window_start, window_end, pre_buffer_seconds, post_buffer_seconds "
                "FROM batch_data_window WHERE batch_id = ? AND tenant_id = ?",
                (batch_id, tid),
            ).fetchall()
        return [dict(r) for r in rows]

    def historian_rows_for_batch(self, batch_id: str, *, limit: int = 5000) -> list[dict[str, Any]]:
        """Read-only historian rows within the batch's data window(s). Mirrors the
        legacy window-join; never touches the collection path."""
        tid = self._tenant()
        with self._connect_readonly() as c:
            windows = c.execute(
                "SELECT gateway_id, window_start, window_end FROM batch_data_window "
                "WHERE batch_id = ? AND tenant_id = ?", (batch_id, tid),
            ).fetchall()
            if not windows:
                return []
            all_rows: list[dict[str, Any]] = []
            for w in windows:
                gw = str(w["gateway_id"] or "")
                start = str(w["window_start"] or "")
                end = str(w["window_end"] or _utc_now())
                params: list[Any] = [tid, start, end]
                gw_clause = ""
                if gw:
                    gw_clause = " AND gateway_id = ?"; params.append(gw)
                rows = c.execute(
                    f"""
                    SELECT ts_utc, tag_name, value, value_text, quality, quality_label,
                           gateway_id, gateway_name, device_name
                    FROM historian_readings
                    WHERE tenant_id = ? AND ts_utc >= ? AND ts_utc <= ? {gw_clause}
                    ORDER BY ts_utc DESC LIMIT ?
                    """,
                    (*params, max(1, min(limit, 50000))),
                ).fetchall()
                all_rows.extend(dict(r) for r in rows)
        return all_rows[: max(1, min(limit, 50000))]

    def collected_tags_in_window(self, batch_id: str) -> list[str]:
        tid = self._tenant()
        tags: set[str] = set()
        with self._connect_readonly() as c:
            windows = c.execute(
                "SELECT gateway_id, window_start, window_end FROM batch_data_window "
                "WHERE batch_id = ? AND tenant_id = ?", (batch_id, tid)).fetchall()
            for w in windows:
                gw = str(w["gateway_id"] or "")
                start = str(w["window_start"] or "")
                end = str(w["window_end"] or _utc_now())
                params: list[Any] = [tid, start, end]
                gw_clause = ""
                if gw:
                    gw_clause = " AND gateway_id = ?"; params.append(gw)
                try:
                    for row in c.execute(
                        f"SELECT DISTINCT tag_name FROM historian_readings "
                        f"WHERE tenant_id = ? AND ts_utc >= ? AND ts_utc <= ?{gw_clause}", params):
                        if row[0]:
                            tags.add(str(row[0]))
                except Exception:
                    continue
        return sorted(tags)

    def tag_matrix(self, batch_id: str, *, tags: Optional[list[str]] = None, max_rows: int = 200) -> dict[str, Any]:
        """Aligned time-series matrix for a batch: one row per timestamp, one
        column per tag, plus a per-row in-limits verdict (OK / OUT vs the tags'
        spec limits). Read-only over the historian window (no PLC re-poll).

        Downsampled to <= max_rows evenly-spaced rows so long batches stay
        instant in the UI; `total` reports the pre-sample row count. The report
        renderer uses the same shape (with a larger cap) for its sampled table."""
        # per-tag spec limits, so we can flag each row within/outside
        from .calc_v2 import BatchCalcService, _SPEC_UPPER, _SPEC_LOWER
        calc = BatchCalcService(self._app_store)
        batch = self.get_batch(batch_id)
        limits = calc._limits_for_batch(batch) if batch else {}
        spec = {}
        for tg, lst in (limits or {}).items():
            lo = next((_opt_num(l.get("limit_value")) for l in lst if str(l.get("limit_type")) == _SPEC_LOWER), None)
            hi = next((_opt_num(l.get("limit_value")) for l in lst if str(l.get("limit_type")) == _SPEC_UPPER), None)
            if lo is not None or hi is not None:
                spec[tg] = (lo, hi)

        rows = self.historian_rows_for_batch(batch_id, limit=50000)
        want = set(tags) if tags else None
        # bucket by timestamp -> {tag: value}
        by_ts: dict[str, dict[str, Any]] = {}
        tagset: set[str] = set()
        for r in rows:
            tg = str(r.get("tag_name") or "")
            if want is not None and tg not in want:
                continue
            v = r.get("value")
            if v is None and r.get("value_text") is None:
                continue
            ts = str(r.get("ts_utc") or "")
            tagset.add(tg)
            cell = _opt_num(v)
            by_ts.setdefault(ts, {})[tg] = cell if cell is not None else r.get("value_text")
        ordered_ts = sorted(by_ts.keys())
        total = len(ordered_ts)
        # downsample evenly, always keep first + last
        if total > max_rows:
            import math
            stride = math.ceil(total / max_rows)
            keep = ordered_ts[::stride]
            if keep and keep[-1] != ordered_ts[-1]:
                keep.append(ordered_ts[-1])
            ordered_ts = keep
        cols = sorted(tagset)

        def _row_ok(vals: dict[str, Any]) -> Optional[bool]:
            checked = False
            for tg, (lo, hi) in spec.items():
                v = vals.get(tg)
                if not isinstance(v, (int, float)):
                    continue
                checked = True
                if (lo is not None and v < lo) or (hi is not None and v > hi):
                    return False
            return True if checked else None  # None = no spec tag on this row

        out_rows = []
        for ts in ordered_ts:
            vals = by_ts[ts]
            out_rows.append({"ts": ts, "values": {c: vals.get(c) for c in cols}, "in_limits": _row_ok(vals)})
        return {"tags": cols, "rows": out_rows, "total": total, "sampled": total > len(out_rows),
                "spec_tags": sorted(spec.keys())}

    # -- idempotency + helpers ----------------------------------------
    def _find_by_idem(self, idem: str) -> Optional[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            r = c.execute("SELECT * FROM batch WHERE tenant_id = ? AND idempotency_key = ? LIMIT 1",
                          (tid, idem)).fetchone()
        return _row_to_batch(r) if r else None

    @staticmethod
    def _auto_reference(now: str) -> str:
        return "B-" + now.replace("-", "").replace(":", "").replace(" ", "").replace(".", "")[:14]

    # -- restart recovery (spec §22) ----------------------------------
    def recover_active_batches(self) -> int:
        """On boot: find RUNNING/HELD batches, record a recovery event, and mark
        their data-quality INCOMPLETE (continuity across a restart can't be
        guaranteed). Does NOT create or complete anything — safe + idempotent.
        Returns the number of batches touched."""
        tid = self._tenant()
        touched = 0
        try:
            with self._connect() as c:
                rows = c.execute(
                    "SELECT id, batch_group_id, data_quality_status FROM batch "
                    "WHERE tenant_id = ? AND status IN ('running','held')", (tid,)).fetchall()
                for r in rows:
                    if str(r["data_quality_status"]) == "not_evaluated":
                        c.execute("UPDATE batch SET data_quality_status='incomplete', updated_utc=? WHERE id = ? AND tenant_id = ?",
                                  (_utc_now(), r["id"], tid))
                    self._event(c, batch_id=r["id"], batch_group_id=r["batch_group_id"],
                                event_type="batch.recovered", source="system",
                                message="Backend restart — data-quality marked incomplete; continuity unconfirmed.")
                    touched += 1
                if touched:
                    c.commit()
        except Exception:
            return 0
        return touched


# --------------------------------------------------------------------------- #
#  row decoders
# --------------------------------------------------------------------------- #
def _row_to_batch(r) -> dict[str, Any]:
    if r is None:
        return {}
    d = dict(r)
    d["metadata"] = _json_load(d.pop("metadata_json", None))
    return d
