"""Batch Management v2 — Report + Email integration (reuses the Report module).

There is NO second report engine or email scheduler here. This service:
  * seeds 4 report_templates into the EXISTING Report module (idempotent, by
    stable id): Batch Summary / Batch Detailed / Batch Group Summary / Group Detailed;
  * builds a CONCRETE template for a specific batch/group by injecting the
    batch window (time_range {preset:'batch', batch_id}) + the definition's tags
    into the seeded template's sections;
  * renders it via report_renderer.render_template_to_pdf (the same call the
    dashboard uses), stores it via ReportsStore.insert_generated, and links it
    with a batch_report_reference row;
  * emails it via notifications.send_email_request (the same path the scheduler
    uses) and records email_status.

Auto-generate-on-complete + auto-email are invoked from the router when a batch/
group reaches a terminal state, honouring the definition version's flags.

Guide: docs/BATCH_MANAGEMENT_REDESIGN_2026-07-14.md
"""
from __future__ import annotations

import base64
from typing import Any, Optional

from .service import _utc_now, _new_id, _json_load, _opt_num
from .service_v2 import _BatchV2Base, BatchExecutionService, BatchDefinitionService, BatchGroupService
from .calc_v2 import BatchCalcService


# Stable template ids so seeding is idempotent + definitions can reference them.
TPL_BATCH_SUMMARY = "tpl-batch-summary"
TPL_BATCH_DETAILED = "tpl-batch-detailed"
TPL_GROUP_SUMMARY = "tpl-batch-group-summary"
TPL_GROUP_DETAILED = "tpl-batch-group-detailed"

REPORT_KINDS = {
    TPL_BATCH_SUMMARY: "batch_summary",
    TPL_BATCH_DETAILED: "batch_detailed",
    TPL_GROUP_SUMMARY: "group_summary",
    TPL_GROUP_DETAILED: "group_detailed",
}


def _reports_store():
    # The app composes a single ReportsStore on state (app/state.py). Reuse it.
    try:
        from app.state import reports_store as rs  # type: ignore
        if rs is not None:
            return rs
    except Exception:
        pass
    from app.services.reports_store import ReportsStore
    return ReportsStore()


# --------------------------------------------------------------------------- #
#  Template seeding (idempotent)
# --------------------------------------------------------------------------- #
def _batch_summary_definition() -> dict[str, Any]:
    # `batch_scope` tags this template as a BATCH template so the definition
    # wizard's "Batch report template" dropdown can list it (and any custom
    # customer template tagged the same way). Reports the module renders keep
    # this in definition_json harmlessly.
    return {"batch_scope": "batch", "sections": [
        {"type": "header", "title": "Batch Report", "subtitle": "Summary", "show_generated_at": True},
        {"type": "text", "title": "Batch", "text": "Batch summary — KPIs and per-tag results for the batch window."},
        {"type": "kpi_grid", "title": "Key Results", "columns": 4, "items": []},
        {"type": "table", "title": "Tag Summary", "time_range": {"preset": "batch"}, "row_limit": 50},
    ]}


def _batch_detailed_definition() -> dict[str, Any]:
    d = _batch_summary_definition()
    d["sections"][1]["text"] = "Detailed batch report — KPIs, trends, events and excursions."
    # trend chart section is filled per-batch in _concretize (one series per trend tag)
    d["sections"].append({"type": "line_chart", "title": "Process Trends",
                           "time_range": {"preset": "batch"}, "series": [], "show_legend": True,
                           "readings_count": 500})
    return d


def _group_summary_definition() -> dict[str, Any]:
    return {"batch_scope": "group", "sections": [
        {"type": "header", "title": "Batch Group Report", "subtitle": "Summary", "show_generated_at": True},
        {"type": "text", "title": "Batch Group", "text": "Aggregated results across the group's child batches."},
        {"type": "kpi_grid", "title": "Group KPIs", "columns": 4, "items": []},
    ]}


def _group_detailed_definition() -> dict[str, Any]:
    d = _group_summary_definition()
    d["sections"][1]["text"] = "Detailed batch group report — group KPIs plus a per-child summary."
    return d


def list_batch_templates() -> dict[str, list[dict[str, Any]]]:
    """Report templates the definition wizard can offer, split by scope. A
    template is a BATCH template if its definition_json has batch_scope=='batch'
    (or its id starts with tpl-batch- and isn't a group one); GROUP likewise.
    This is what lets CUSTOM customer templates created in the Reports module
    appear in the batch-definition dropdowns — not just the 4 seeded ones."""
    rs = _reports_store()
    try:
        tpls = rs.list_templates()
    except Exception:
        tpls = []
    batch, group = [], []
    for t in tpls:
        tid = str(t.get("id") or "")
        defn = t.get("definition") or {}
        scope = str((defn or {}).get("batch_scope") or "").strip().lower()
        if not scope:
            # infer from the stable seed ids so pre-scope installs still classify
            if tid in (TPL_GROUP_SUMMARY, TPL_GROUP_DETAILED):
                scope = "group"
            elif tid in (TPL_BATCH_SUMMARY, TPL_BATCH_DETAILED):
                scope = "batch"
        row = {"id": tid, "name": t.get("name") or tid, "description": t.get("description") or "", "scope": scope}
        if scope == "group":
            group.append(row)
        elif scope == "batch":
            batch.append(row)
        else:
            # untyped custom template -> offer in BOTH lists so it's usable
            batch.append(row); group.append(row)
    return {"batch": batch, "group": group}


_SEED_SPECS = [
    (TPL_BATCH_SUMMARY, "Batch Summary Report", "Auto batch summary (KPIs + tag table).", _batch_summary_definition),
    (TPL_BATCH_DETAILED, "Batch Detailed Report", "Auto batch detail (KPIs + trends + events + excursions).", _batch_detailed_definition),
    (TPL_GROUP_SUMMARY, "Batch Group Summary Report", "Auto group summary (aggregated KPIs).", _group_summary_definition),
    (TPL_GROUP_DETAILED, "Batch Group Detailed Report", "Auto group detail (KPIs + per-child).", _group_detailed_definition),
]


def seed_report_templates() -> int:
    """Create the 4 batch report templates if missing. Idempotent — only inserts
    a template whose stable id isn't already present. Returns count created."""
    rs = _reports_store()
    created = 0
    try:
        existing = {t.get("id"): t for t in rs.list_templates()}
    except Exception:
        existing = {}
    for tpl_id, name, desc, builder in _SEED_SPECS:
        cur = existing.get(tpl_id)
        if cur is None:
            try:
                rs.upsert_template({"id": tpl_id, "name": name, "description": desc, "definition": builder()})
                created += 1
            except Exception:
                continue
        elif not str(((cur.get("definition") or {}) or {}).get("batch_scope") or "").strip():
            # backfill the batch_scope tag onto a template seeded before scoping
            # so it classifies in the wizard dropdown (idempotent re-upsert).
            try:
                rs.upsert_template({"id": tpl_id, "name": cur.get("name") or name,
                                    "description": cur.get("description") or desc, "definition": builder()})
            except Exception:
                continue
    return created


# --------------------------------------------------------------------------- #
#  Report Integration Service
# --------------------------------------------------------------------------- #
class ReportIntegrationService(_BatchV2Base):
    def __init__(self, app_store) -> None:
        super().__init__(app_store)
        self._exe = BatchExecutionService(app_store)
        self._defs = BatchDefinitionService(app_store)
        self._groups = BatchGroupService(app_store)
        self._calc = BatchCalcService(app_store)

    # -- generate a batch report --------------------------------------
    def generate_batch_report(
        self, batch_id: str, *, template_id: Optional[str] = None,
        triggered_by: str = "manual", actor: Optional[str] = None,
    ) -> dict[str, Any]:
        batch = self._exe.get_batch(batch_id)
        if not batch:
            raise ValueError("batch not found")
        tpl_id = template_id or self._definition_template(batch, group=False) or TPL_BATCH_SUMMARY
        template = self._concretize_batch(batch, tpl_id)
        return self._render_and_link(template, tpl_id, batch_id=batch_id,
                                     group_id=None, triggered_by=triggered_by)

    def generate_group_report(
        self, group_id: str, *, template_id: Optional[str] = None,
        triggered_by: str = "manual", actor: Optional[str] = None,
    ) -> dict[str, Any]:
        group = self._groups.get_group(group_id)
        if not group:
            raise ValueError("group not found")
        tpl_id = template_id or self._group_template(group) or TPL_GROUP_SUMMARY
        template = self._concretize_group(group, tpl_id)
        return self._render_and_link(template, tpl_id, batch_id=None,
                                     group_id=group_id, triggered_by=triggered_by)

    # -- list references ----------------------------------------------
    def list_batch_reports(self, batch_id: str) -> list[dict[str, Any]]:
        return self._list_refs(batch_id=batch_id)

    def list_group_reports(self, group_id: str) -> list[dict[str, Any]]:
        return self._list_refs(group_id=group_id)

    def delete_report(self, reference_id: str, *, actor: Optional[str] = None) -> bool:
        """Delete a batch/group report: the batch_report_reference row AND the
        underlying generated report in the Reports module (so it also disappears
        from Generated Reports). Best-effort on the generated-report side."""
        tid = self._tenant()
        ref = self._get_ref(reference_id)
        if not ref:
            return False
        gen_id = ref.get("generated_report_id")
        if gen_id:
            try:
                _reports_store().delete_generated(gen_id)
            except Exception:
                pass
        with self._connect() as c:
            c.execute("DELETE FROM batch_report_reference WHERE id = ? AND tenant_id = ?", (reference_id, tid))
            self._event(c, batch_id=ref.get("batch_id"), batch_group_id=ref.get("batch_group_id"),
                        event_type="report.deleted", source="api", actor=actor, message=reference_id)
            c.commit()
        return True

    # -- email a generated report -------------------------------------
    def email_report(
        self, reference_id: str, *, recipients: Optional[list[str]] = None,
        subject: Optional[str] = None, body: Optional[str] = None,
        email_settings: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        tid = self._tenant()
        ref = self._get_ref(reference_id)
        if not ref or not ref.get("generated_report_id"):
            raise ValueError("report reference not found")
        rs = _reports_store()
        gen = rs.get_generated(ref["generated_report_id"])
        if not gen:
            raise ValueError("generated report not found")
        outcome = self._send(gen, recipients or [], subject, body, email_settings)
        now = _utc_now()
        with self._connect() as c:
            c.execute(
                "UPDATE batch_report_reference SET email_status=?, email_error=?, emailed_utc=? WHERE id = ? AND tenant_id = ?",
                ("sent" if outcome["ok"] else "failed", None if outcome["ok"] else outcome.get("message"),
                 now if outcome["ok"] else None, reference_id, tid))
            c.commit()
        try:
            rs.update_generated_email_status(
                ref["generated_report_id"],
                status="sent" if outcome["ok"] else "failed",
                message=str(outcome.get("message") or ""),
                recipients=list(outcome.get("recipients") or []))
        except Exception:
            pass
        return outcome

    # -- auto-on-terminal (called by router after stop/complete/abort) -
    def on_batch_terminal(self, batch_id: str, *, actor: Optional[str] = None) -> None:
        """If the batch's definition version enables auto-generate, compute + generate,
        then auto-email if enabled. Failures are recorded, NEVER raised (spec: email
        failure must not invalidate the batch)."""
        try:
            batch = self._exe.get_batch(batch_id)
            if not batch:
                return
            cfg = self._version_cfg(batch)
            if not cfg or not cfg.get("auto_generate_batch_report"):
                return
            # ensure KPIs/excursions/quality are fresh before rendering
            try:
                self._calc.compute_batch(batch_id)
            except Exception:
                pass
            gen_ref = self.generate_batch_report(
                batch_id, template_id=cfg.get("batch_report_template_id"),
                triggered_by="batch-terminal", actor=actor)
            if cfg.get("auto_email_batch_report"):
                ec = cfg.get("email_config") or {}
                recips = ec.get("recipients") or []
                if recips:
                    self.email_report(gen_ref["reference"]["id"], recipients=recips,
                                      subject=ec.get("subject"), body=ec.get("body"),
                                      email_settings=self._active_email_settings())
        except Exception:
            return

    def on_group_terminal(self, group_id: str, *, actor: Optional[str] = None) -> None:
        try:
            group = self._groups.get_group(group_id)
            if not group:
                return
            cfg = self._group_version_cfg(group)
            if not cfg or not cfg.get("auto_generate_batch_group_report"):
                return
            try:
                self._calc.compute_group(group_id)
            except Exception:
                pass
            gen_ref = self.generate_group_report(
                group_id, template_id=cfg.get("batch_group_report_template_id"),
                triggered_by="group-terminal", actor=actor)
            if cfg.get("auto_email_batch_group_report"):
                ec = cfg.get("email_config") or {}
                recips = ec.get("recipients") or []
                if recips:
                    self.email_report(gen_ref["reference"]["id"], recipients=recips,
                                      subject=ec.get("subject"), body=ec.get("body"),
                                      email_settings=self._active_email_settings())
        except Exception:
            return

    # ------------------------------------------------------------------ #
    #  internals — concretize templates
    # ------------------------------------------------------------------ #
    def _concretize_batch(self, batch: dict[str, Any], tpl_id: str) -> dict[str, Any]:
        rs = _reports_store()
        base = rs.get_template(tpl_id) or {"definition": _batch_summary_definition(), "name": "Batch Report"}
        template = {"id": f"batch-{batch['id']}", "name": base.get("name") or "Batch Report",
                    "definition": _clone(base.get("definition") or {})}
        gw = batch.get("equipment_id")
        tags = self._trend_tags(batch)
        # tag KPI cells use the renderer's own aggregation over the BATCH window
        # (avg per tag) — these values compute live from the historian window.
        kpi_items = [
            {"id": f"k{i}", "label": f"{tg} avg", "gateway_id": gw, "tag_name": tg,
             "aggregation": "avg", "time_range": {"preset": "batch", "batch_id": batch["id"]}}
            for i, tg in enumerate(tags[:8])
        ]
        # If the definition has configured/synthesized (axis-aware) charts, we
        # append those below and must NOT also fill the template's generic
        # placeholder chart, or the report shows the same trend twice — once
        # without axis config. Drop placeholder chart sections in that case.
        configured = self._configured_chart_sections(batch, gw)
        drop_placeholder_charts = bool(configured)
        kept = []
        for sec in template["definition"].get("sections", []):
            t = sec.get("type")
            if t in ("line_chart", "area_chart", "bar_chart"):
                if drop_placeholder_charts:
                    continue  # the axis-aware configured chart(s) below replace it
                sec["time_range"] = {"preset": "batch", "batch_id": batch["id"]}
                sec["series"] = [
                    {"id": f"s{i}", "label": tg, "gateway_id": gw, "tag_name": tg,
                     "axis": "left", "aggregation": "avg"}
                    for i, tg in enumerate(tags[:6])
                ]
            elif t == "table":
                sec["time_range"] = {"preset": "batch", "batch_id": batch["id"]}
                if tags:
                    sec["gateway_id"] = gw; sec["tag_name"] = tags[0]
            elif t == "kpi_grid":
                sec["items"] = kpi_items
            elif t == "header":
                sec["subtitle"] = f"{batch.get('reference') or batch['id']}"
            kept.append(sec)
        template["definition"]["sections"] = kept
        secs = template["definition"].setdefault("sections", [])
        insert_at = 1 if secs and secs[0].get("type") == "header" else 0
        # Batch pass/fail verdict + a computed-KPI table (cycle/hold/quality) —
        # both as real tables, placed right after the header.
        secs.insert(insert_at, self._kpi_table_section(batch))
        secs.insert(insert_at, self._passfail_section(batch))
        # Append the definition's CONFIGURED charts, a per-tag summary TABLE with
        # pass/fail, a limits TABLE, and the collected time-series TABLE — so the
        # report mirrors the batch view (all real tables, not plain text).
        secs.extend(configured)
        secs.append(self._tag_summary_table_section(batch))
        secs.append(self._limits_table_section(batch))
        secs.append(self._matrix_table_section(batch, newest_first=self._report_newest_first(batch)))
        return template

    _CHART_TYPE_MAP = {"line": "line_chart", "area": "area_chart", "bar": "bar_chart", "scatter": "line_chart"}

    def _definition_tag_names(self, batch: dict[str, Any]) -> Optional[list[str]]:
        """The tag names configured on the batch's definition — the report table
        + summary must show ONLY these, not every tag collected in the window.
        Returns None when the definition has no tags (show whatever collected)."""
        ver = batch.get("definition_version_id")
        if not ver:
            return None
        with self._connect_readonly() as c:
            r = c.execute("SELECT configuration_json FROM batch_definition_version WHERE id = ? AND tenant_id = ?",
                          (ver, self._tenant())).fetchone()
        cfg = _json_load(r["configuration_json"]) if r else {}
        names = [str(t.get("tag_name")).strip() for t in ((cfg or {}).get("tags") or [])
                 if isinstance(t, dict) and str(t.get("tag_name") or "").strip()]
        return names or None

    def _report_newest_first(self, batch: dict[str, Any]) -> bool:
        """Whether the report's collected-data table sorts newest-first — from
        the definition's report_config.collected_data_newest_first flag."""
        ver = batch.get("definition_version_id")
        if not ver:
            return False
        with self._connect_readonly() as c:
            r = c.execute("SELECT configuration_json FROM batch_definition_version WHERE id = ? AND tenant_id = ?",
                          (ver, self._tenant())).fetchone()
        cfg = _json_load(r["configuration_json"]) if r else {}
        rc = (cfg or {}).get("report_config") or {}
        return bool(rc.get("collected_data_newest_first"))

    def _configured_charts(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        """The charts[] declared on the batch's definition version config
        (charts live only in configuration_json, like properties). When no
        explicit chart is declared, synthesize ONE chart from the tags' per-tag
        axis assignment + axis_options (set on the Tags & Limits tab) so the
        report chart matches the batch view exactly."""
        ver = batch.get("definition_version_id")
        if not ver:
            return []
        with self._connect_readonly() as c:
            r = c.execute("SELECT configuration_json FROM batch_definition_version WHERE id = ? AND tenant_id = ?",
                          (ver, self._tenant())).fetchone()
        cfg = _json_load(r["configuration_json"]) if r else {}
        # Definition tags are the source of truth: a chart configured earlier may
        # still list tags since removed from the definition — filter them out so
        # only defined tags appear on the report chart (matches the batch view).
        def_names = {str(t.get("tag_name")).strip() for t in ((cfg or {}).get("tags") or [])
                     if isinstance(t, dict) and str(t.get("tag_name") or "").strip()}
        charts = []
        for ch in ((cfg or {}).get("charts") or []):
            if not (isinstance(ch, dict) and ch.get("tags")):
                continue
            kept = [t for t in ch["tags"] if not def_names or t in def_names]
            if not kept:
                continue
            sa = {t: (ch.get("series_axis") or {}).get(t) for t in kept if (ch.get("series_axis") or {}).get(t)}
            charts.append({**ch, "tags": kept, "series_axis": sa})
        if charts:
            return charts
        # Synthesize from per-tag axis config (mirrors the frontend's effectiveCharts).
        def _norm_axis(a):
            s = str(a or "left1").lower()
            return s if s in ("left1", "left2", "right1", "right2") else "left1"
        trend_tags = [t for t in ((cfg or {}).get("tags") or [])
                      if isinstance(t, dict) and str(t.get("tag_name") or "").strip()
                      and t.get("trend_enabled") is not False]
        if not trend_tags:
            return []
        series_axis: dict[str, str] = {}
        axis_config: dict[str, dict] = {}
        for t in trend_tags:
            nm = str(t["tag_name"]).strip()
            ax = _norm_axis(t.get("chart_axis"))
            series_axis[nm] = ax
            o = t.get("axis_options") or {}
            prev = axis_config.get(ax, {})
            axis_config[ax] = {
                "label": o.get("label") if o.get("label") not in (None, "") else prev.get("label", ""),
                "unit": o.get("unit") if o.get("unit") not in (None, "") else prev.get("unit", ""),
                "min": o.get("min") if o.get("min") not in (None, "") else prev.get("min", ""),
                "max": o.get("max") if o.get("max") not in (None, "") else prev.get("max", ""),
                "decimals": o.get("decimals") if o.get("decimals") not in (None, "") else prev.get("decimals", ""),
            }
        return [{"id": "auto", "title": "Process Trends", "type": "line",
                 "tags": [str(t["tag_name"]).strip() for t in trend_tags],
                 "series_axis": series_axis, "axis_config": axis_config}]

    def _configured_chart_sections(self, batch: dict[str, Any], gw) -> list[dict[str, Any]]:
        out = []
        for ch in self._configured_charts(batch):
            sec_type = self._CHART_TYPE_MAP.get(str(ch.get("type") or "line"), "line_chart")
            series_axis = ch.get("series_axis") or {}
            axis_cfg = ch.get("axis_config") or {}
            # The batch view supports up to 2 left + 2 right axes; the PDF
            # renderer supports a left + right axis, so we fold the operator's
            # per-tag axis choice down to left/right (left1|left2 -> left,
            # right1|right2 -> right) and carry the axis labels/units so the
            # report chart matches the on-screen chart's intent.
            def _side(tag):
                a = str(series_axis.get(tag) or "left1").lower()
                return "right" if a.startswith("right") else "left"
            sec = {
                "type": sec_type, "title": ch.get("title") or "Chart",
                "time_range": {"preset": "batch", "batch_id": batch["id"]},
                "series": [{"id": f"s{i}", "label": tg, "gateway_id": gw, "tag_name": tg,
                            "axis": _side(tg), "aggregation": "avg"}
                           for i, tg in enumerate(ch.get("tags") or [])],
                "show_legend": True, "readings_count": 500,
            }
            # carry axis labels/units + explicit ranges (first left/right axis)
            la = axis_cfg.get("left1") or axis_cfg.get("left2") or {}
            ra = axis_cfg.get("right1") or axis_cfg.get("right2") or {}
            if la.get("label"): sec["y_axis_label"] = str(la["label"])
            if la.get("unit"): sec["y_axis_unit"] = str(la["unit"])
            if la.get("min") not in (None, ""): sec["y_min"] = _opt_num(la.get("min"))
            if la.get("max") not in (None, ""): sec["y_max"] = _opt_num(la.get("max"))
            if ra.get("label"): sec["y_axis_right_label"] = str(ra["label"])
            if ra.get("unit"): sec["y_axis_right_unit"] = str(ra["unit"])
            if ra.get("min") not in (None, ""): sec["y_right_min"] = _opt_num(ra.get("min"))
            if ra.get("max") not in (None, ""): sec["y_right_max"] = _opt_num(ra.get("max"))
            out.append(sec)
        return out

    def _tag_summary_text(self, batch: dict[str, Any]) -> str:
        """Per-tag min/max/avg + within-limits, computed from the batch window."""
        try:
            mx = self._exe.tag_matrix(batch["id"], tags=self._definition_tag_names(batch), max_rows=5000)
        except Exception:
            return "No collected data."
        cols = mx.get("tags") or []
        agg = {c: [] for c in cols}
        for row in mx.get("rows") or []:
            for c in cols:
                v = (row.get("values") or {}).get(c)
                if isinstance(v, (int, float)):
                    agg[c].append(v)
        spec = set(mx.get("spec_tags") or [])
        lines = []
        for c in cols:
            vals = agg[c]
            if not vals:
                lines.append(f"{c}: (no numeric samples)"); continue
            lo, hi, av = min(vals), max(vals), sum(vals) / len(vals)
            flag = ""
            if c in spec:
                out_rows = sum(1 for r in (mx.get("rows") or [])
                               if isinstance((r.get("values") or {}).get(c), (int, float)) and r.get("in_limits") is False)
                flag = "  [PASS]" if out_rows == 0 else f"  [FAIL: {out_rows} out-of-limit]"
            lines.append(f"{c}: min {lo:g}, max {hi:g}, avg {av:g}{flag}")
        return "\n".join(lines) or "No collected data."

    def _matrix_text(self, batch: dict[str, Any], max_rows: int = 300, newest_first: bool = False) -> str:
        """A readings table as text. Shows every collected row at the gateway's
        real interval up to max_rows; only very long batches get sampled (the
        note reflects which). max_rows keeps PDFs a sane size. newest_first
        reverses the timestamp order to match the batch view's sort toggle."""
        try:
            mx = self._exe.tag_matrix(batch["id"], tags=self._definition_tag_names(batch), max_rows=max_rows)
        except Exception:
            return "No collected data."
        cols = mx.get("tags") or []
        if not cols:
            return "No collected data in the batch window."
        order = "newest first" if newest_first else "oldest first"
        header = "Timestamp".ljust(20) + "".join(str(c)[:12].rjust(13) for c in cols) + "   In-limits"
        lines = [f"(sorted {order})", header]
        rows = list(mx.get("rows") or [])
        if newest_first:
            rows = list(reversed(rows))
        for r in rows:
            ts = str(r.get("ts") or "")[:19].ljust(20)
            cells = ""
            for c in cols:
                v = (r.get("values") or {}).get(c)
                cells += (f"{v:g}" if isinstance(v, (int, float)) else (str(v) if v is not None else "—"))[:12].rjust(13)
            verdict = "—" if r.get("in_limits") is None else ("OK" if r.get("in_limits") else "OUT")
            lines.append(ts + cells + "   " + verdict)
        note = f"\n({mx.get('total')} total samples" + (f", showing {len(mx.get('rows') or [])} sampled)" if mx.get("sampled") else ")")
        return "\n".join(lines) + note

    def _computed_kpi_text(self, batch: dict[str, Any]) -> str:
        """A plain-text block of the batch's computed KPIs + quality (values the
        historian can't re-derive: cycle/hold time, excursion count, quality)."""
        kpis = {k.get("kpi_code"): k for k in self._calc.list_kpis(batch["id"])}
        def _fmt(code, label, unit="s"):
            k = kpis.get(code)
            if not k or k.get("numeric_value") is None:
                return None
            v = k["numeric_value"]
            return f"{label}: {v:g}{(' ' + unit) if unit else ''}"
        lines = [x for x in (
            f"Reference: {batch.get('reference') or batch['id']}",
            f"Status: {str(batch.get('status') or '').upper()}",
            f"Quality: {str(batch.get('quality_status') or 'not_evaluated').replace('_',' ')}",
            f"Data quality: {str(batch.get('data_quality_status') or 'not_evaluated').replace('_',' ')}",
            _fmt("cycle_time", "Cycle time"),
            _fmt("running_time", "Running time"),
            _fmt("hold_time", "Hold time"),
            _fmt("excursion_count", "Excursions", ""),
        ) if x]
        return "\n".join(lines)

    # --- table sections (real tables, not plain text) -----------------------
    def _passfail_section(self, batch: dict[str, Any]) -> dict[str, Any]:
        """A compact pass/fail verdict table for the batch."""
        q = str(batch.get("quality_status") or "not_evaluated")
        dq = str(batch.get("data_quality_status") or "not_evaluated")
        exc = self._calc.list_excursions(batch_id=batch["id"]) or []
        n_fail = sum(1 for e in exc if str(e.get("severity") or "") in ("error", "critical"))
        verdict = "PASS" if q in ("pass", "within_spec", "valid") else ("FAIL" if q in ("fail", "out_of_spec") else q.replace("_", " ").upper())
        rows = [
            ["Result", verdict],
            ["Quality", q.replace("_", " ")],
            ["Data quality", dq.replace("_", " ")],
            ["Limit excursions", str(len(exc))],
            ["Failing excursions (error/critical)", str(n_fail)],
        ]
        return {"type": "static_table", "title": "Batch Result (Pass / Fail)",
                "header": ["Check", "Value"], "rows": rows}

    def _kpi_table_section(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Computed KPIs as a real table (code, name, value, unit)."""
        kpis = self._calc.list_kpis(batch["id"]) or []
        rows = []
        for k in kpis:
            v = k.get("numeric_value")
            if v is None and not k.get("text_value"):
                continue
            val = k.get("text_value") if v is None else (f"{v:g}")
            rows.append([k.get("label") or k.get("kpi_code"), val, k.get("unit") or ""])
        if not rows:
            rows = [["No KPIs computed yet", "", ""]]
        return {"type": "static_table", "title": "Key Results (KPIs)",
                "header": ["KPI", "Value", "Unit"], "rows": rows}

    def _tag_summary_table_section(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Per-tag min/max/avg + pass/fail as a real table."""
        try:
            mx = self._exe.tag_matrix(batch["id"], tags=self._definition_tag_names(batch), max_rows=5000)
        except Exception:
            mx = {}
        cols = mx.get("tags") or []
        spec = set(mx.get("spec_tags") or [])
        rows = []
        for c in cols:
            vals = [(r.get("values") or {}).get(c) for r in (mx.get("rows") or [])]
            vals = [v for v in vals if isinstance(v, (int, float))]
            if not vals:
                rows.append([c, "—", "—", "—", "n/a"]); continue
            lo, hi, av = min(vals), max(vals), sum(vals) / len(vals)
            verdict = "—"
            if c in spec:
                out_n = sum(1 for r in (mx.get("rows") or [])
                            if isinstance((r.get("values") or {}).get(c), (int, float)) and r.get("in_limits") is False)
                verdict = "PASS" if out_n == 0 else f"FAIL ({out_n} out)"
            rows.append([c, f"{lo:g}", f"{hi:g}", f"{av:g}", verdict])
        if not rows:
            rows = [["No collected data", "", "", "", ""]]
        return {"type": "static_table", "title": "Tag Summary (Pass / Fail)",
                "header": ["Tag", "Min", "Max", "Avg", "Result"], "rows": rows}

    def _limits_table_section(self, batch: dict[str, Any]) -> dict[str, Any]:
        """The configured limits + any recorded excursions as a real table."""
        rows = []
        # configured limits from the definition
        cfg = self._exe.version_config_for_batch(batch["id"]) or {}
        for t in (cfg.get("tags") or []):
            for l in (t.get("limits") or []):
                if l.get("enabled") is False:
                    continue
                rows.append([t.get("tag_name"), str(l.get("limit_type") or ""),
                             str(l.get("limit_value") if l.get("limit_value") is not None else ""),
                             str(l.get("severity") or ""), ""])
        # mark tags that actually breached
        exc = self._calc.list_excursions(batch_id=batch["id"]) or []
        breached = {}
        for e in exc:
            key = (e.get("tag_name"), str(e.get("limit_type") or ""))
            breached[key] = breached.get(key, 0) + 1
        for r in rows:
            n = breached.get((r[0], r[1]), 0)
            r[4] = "OK" if n == 0 else f"BREACHED ({n})"
        if not rows:
            rows = [["No limits configured", "", "", "", ""]]
        return {"type": "static_table", "title": "Limits & Excursions",
                "header": ["Tag", "Limit type", "Value", "Severity", "Status"], "rows": rows}

    def _matrix_table_section(self, batch: dict[str, Any], newest_first: bool = False) -> dict[str, Any]:
        """The collected time-series as a real table (timestamp + tag columns +
        in-limits), definition tags only, at the gateway cadence up to a cap."""
        try:
            mx = self._exe.tag_matrix(batch["id"], tags=self._definition_tag_names(batch), max_rows=300)
        except Exception:
            mx = {}
        cols = mx.get("tags") or []
        header = ["Timestamp", *[str(c) for c in cols], "In limits"]
        rows = []
        data = list(mx.get("rows") or [])
        if newest_first:
            data = list(reversed(data))
        for r in data:
            cells = [str(r.get("ts") or "")[:19]]
            for c in cols:
                v = (r.get("values") or {}).get(c)
                cells.append(f"{v:g}" if isinstance(v, (int, float)) else ("" if v is None else str(v)))
            cells.append("—" if r.get("in_limits") is None else ("OK" if r.get("in_limits") else "OUT"))
            rows.append(cells)
        if not rows:
            rows = [["No collected data in the batch window", *["" for _ in cols], ""]]
        title = "Collected Data (time series)" + (" — newest first" if newest_first else "")
        return {"type": "static_table", "title": title, "header": header, "rows": rows}

    def _concretize_group(self, group: dict[str, Any], tpl_id: str) -> dict[str, Any]:
        rs = _reports_store()
        base = rs.get_template(tpl_id) or {"definition": _group_summary_definition(), "name": "Batch Group Report"}
        template = {"id": f"group-{group['id']}", "name": base.get("name") or "Batch Group Report",
                    "definition": _clone(base.get("definition") or {})}
        # group KPIs are computed (not historian-derivable) -> render as a text block
        kpis = self._calc.list_group_kpis(group["id"])
        lines = [f"Reference: {group.get('reference') or group['id']}",
                 f"Status: {str(group.get('status') or '').upper()}"]
        for k in kpis:
            v = k.get("numeric_value")
            if v is None:
                continue
            unit = k.get("unit") or ""
            lines.append(f"{k.get('label') or k.get('kpi_code')}: {v:g}{(' ' + unit) if unit and unit != 'count' else ''}")
        group_text = "\n".join(lines)
        secs = template["definition"].setdefault("sections", [])
        for sec in secs:
            if sec.get("type") == "kpi_grid":
                sec["type"] = "text"; sec["text"] = group_text
                sec.pop("items", None); sec.pop("columns", None)
            elif sec.get("type") == "header":
                sec["subtitle"] = f"{group.get('reference') or group['id']}"
        # Per-child mini-summary: reference / status / result / pass% / per-tag
        # min-max-avg, so the group report shows each child's collected results.
        try:
            children = self._groups.child_batches(group["id"])
        except Exception:
            children = []
        child_lines = []
        for ch in children:
            md = ch.get("metadata") or {}
            if isinstance(md, str):
                md = _json_load(md) or {}
            res = str(md.get("result") or "").upper() or "—"
            passc = md.get("pass_tag_count"); failc = md.get("fail_tag_count")
            pct = ""
            if isinstance(passc, int) and isinstance(failc, int) and (passc + failc) > 0:
                pct = f"  pass {round(100 * passc / (passc + failc))}%"
            child_lines.append(f"• {ch.get('reference') or ch['id']} — {str(ch.get('status') or '').upper()} — {res}{pct}")
            summ = self._tag_summary_text(ch)
            for ln in summ.splitlines():
                child_lines.append(f"    {ln}")
        if child_lines:
            secs.append({"type": "text", "title": "Child Batches", "text": "\n".join(child_lines)})
        return template

    def _trend_tags(self, batch: dict[str, Any]) -> list[str]:
        """Tags flagged trend_enabled in the definition version, else whatever was
        collected in the window."""
        ver = batch.get("definition_version_id")
        tags: list[str] = []
        if ver:
            with self._connect_readonly() as c:
                rows = c.execute(
                    "SELECT tag_name FROM batch_definition_tag WHERE definition_version_id = ? "
                    "AND trend_enabled = 1 ORDER BY sort_order", (ver,)).fetchall()
                tags = [str(r["tag_name"]) for r in rows]
        if not tags:
            tags = self._exe.collected_tags_in_window(batch["id"])
        return tags

    # ------------------------------------------------------------------ #
    #  internals — render + persist + email
    # ------------------------------------------------------------------ #
    def _render_and_link(self, template, tpl_id, *, batch_id, group_id, triggered_by) -> dict[str, Any]:
        tid = self._tenant()
        rs = _reports_store()
        ref_id = _new_id("brep")
        now = _utc_now()
        # create a pending reference first
        with self._connect() as c:
            c.execute(
                """
                INSERT INTO batch_report_reference
                  (id, tenant_id, batch_id, batch_group_id, generated_report_id,
                   report_template_id, report_kind, report_status, email_status,
                   report_error, generated_utc, created_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (ref_id, tid, batch_id, group_id, None, tpl_id,
                 REPORT_KINDS.get(tpl_id, "batch_summary"), "generating", "not_configured",
                 None, None, now))
            c.commit()
        try:
            from app.services.report_renderer import render_template_to_pdf
            path, byte_count, sha = render_template_to_pdf(template)
            record = rs.insert_generated({
                "template_id": tpl_id, "template_name": template.get("name"),
                "triggered_by": triggered_by, "file_path": str(path), "file_name": path.name,
                "file_bytes": byte_count, "file_sha256": sha,
                "meta": {"batch_id": batch_id, "batch_group_id": group_id, "report_kind": REPORT_KINDS.get(tpl_id)},
            })
            with self._connect() as c:
                c.execute(
                    "UPDATE batch_report_reference SET generated_report_id=?, report_status='generated', generated_utc=? "
                    "WHERE id = ? AND tenant_id = ?", (record.get("id"), _utc_now(), ref_id, tid))
                self._event(c, batch_id=batch_id, batch_group_id=group_id,
                            event_type="report.generated", source="system",
                            message=template.get("name"), metadata={"generated_report_id": record.get("id")})
                c.commit()
            return {"ok": True, "reference": self._get_ref(ref_id), "generated": record}
        except Exception as exc:
            with self._connect() as c:
                c.execute("UPDATE batch_report_reference SET report_status='failed', report_error=? WHERE id = ? AND tenant_id = ?",
                          (str(exc)[:500], ref_id, tid))
                self._event(c, batch_id=batch_id, batch_group_id=group_id,
                            event_type="report.failed", severity="error", source="system", message=str(exc)[:200])
                c.commit()
            return {"ok": False, "reference": self._get_ref(ref_id), "error": str(exc)}

    def _send(self, gen, recipients, subject, body, email_settings) -> dict[str, Any]:
        from app.routers.notifications import (
            EmailRequest, SMTPConfig, PHPMailConfig, EmailAttachment, send_email_request,
        )
        from pathlib import Path
        es = email_settings or {}
        attachments = []
        try:
            p = Path(gen.get("file_path") or "")
            if p.exists():
                attachments.append(EmailAttachment(
                    filename=gen.get("file_name") or "report.pdf",
                    content_b64=base64.b64encode(p.read_bytes()).decode("ascii"),
                    content_type="application/pdf"))
        except Exception:
            pass
        transport = str(es.get("transport") or ("php_http" if es.get("php_mail") else "smtp"))
        # Mirror routers/reports.py: always pass a (possibly empty) SMTPConfig — the
        # model rejects None. With no real settings the send fails softly and we
        # record email_status='failed' (spec: email failure must not stop the batch).
        req = EmailRequest(
            transport="php_http" if transport == "php_http" else "smtp",
            smtp=SMTPConfig(**(es.get("smtp") or {})),
            php_mail=PHPMailConfig(**es["php_mail"]) if es.get("php_mail") else None,
            to=list(recipients or []),
            cc=list(es.get("cc") or []),
            subject=subject or f"Batch report: {gen.get('template_name') or ''}",
            html_body=body or "<p>Please find the attached batch report.</p>",
            attachments=attachments)
        try:
            outcome = send_email_request(req)
            return {"ok": bool(outcome.ok), "message": outcome.message, "recipients": list(outcome.recipients or [])}
        except Exception as exc:
            # Any transport/validation error -> soft failure, recorded by the caller.
            return {"ok": False, "message": f"email send error: {exc}", "recipients": []}

    def _active_email_settings(self) -> Optional[dict[str, Any]]:
        """Reuse whatever email settings the report scheduler holds (frontend pushes
        them via /api/reports/scheduler/email-settings, stored in the shared holder
        on app.state). None -> email will fail softly and be recorded as failed."""
        try:
            from app.state import scheduler_email_settings_holder
            return scheduler_email_settings_holder.get()
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    #  reference + config helpers
    # ------------------------------------------------------------------ #
    def _list_refs(self, *, batch_id=None, group_id=None) -> list[dict[str, Any]]:
        tid = self._tenant()
        where = ["tenant_id = ?"]; params: list[Any] = [tid]
        if batch_id:
            where.append("batch_id = ?"); params.append(batch_id)
        if group_id:
            where.append("batch_group_id = ?"); params.append(group_id)
        with self._connect_readonly() as c:
            rows = c.execute(
                f"SELECT * FROM batch_report_reference WHERE {' AND '.join(where)} ORDER BY created_utc DESC",
                params).fetchall()
        return [dict(r) for r in rows]

    def _get_ref(self, ref_id: str) -> Optional[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            r = c.execute("SELECT * FROM batch_report_reference WHERE id = ? AND tenant_id = ?", (ref_id, tid)).fetchone()
        return dict(r) if r else None

    def _definition_template(self, batch, *, group: bool) -> Optional[str]:
        cfg = self._version_cfg(batch)
        if not cfg:
            return None
        return cfg.get("batch_group_report_template_id" if group else "batch_report_template_id")

    def _group_template(self, group) -> Optional[str]:
        cfg = self._group_version_cfg(group)
        return cfg.get("batch_group_report_template_id") if cfg else None

    def _version_cfg(self, batch) -> Optional[dict[str, Any]]:
        ver = batch.get("definition_version_id")
        defn = batch.get("definition_id")
        if not defn:
            return None
        try:
            full = self._defs.get_definition(defn, version_id=ver)
            return (full or {}).get("config")
        except Exception:
            return None

    def _group_version_cfg(self, group) -> Optional[dict[str, Any]]:
        defn = group.get("definition_id")
        ver = group.get("definition_version_id")
        if not defn:
            return None
        try:
            full = self._defs.get_definition(defn, version_id=ver)
            return (full or {}).get("config")
        except Exception:
            return None


def _clone(obj: Any) -> Any:
    import copy
    return copy.deepcopy(obj)
