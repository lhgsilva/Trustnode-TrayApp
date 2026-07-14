# Batch Management Redesign — Implementation Guide (2026-07-14)

> **Purpose.** This is the durable reference for the Batch Management clean rebuild.
> It captures the spec, the locked decisions, the reuse map, the new schema/API/UI, and
> the staged plan so context is not lost across sessions. Read this before touching the
> batch module.

---

## 0. Rollback point

- **Last-working legacy module** tagged: `v0.1.0-batch-legacy-working-2026-07-14`
  (commit `e047919` — legacy `batch_types`/`batches` + spec-limits/pass-fail/KPIs/manual/charts).
- Rollback: `git checkout v0.1.0-batch-legacy-working-2026-07-14`.
- The legacy `batch_*` tables are **retained untouched** as a data backup; the new module
  is an **additive parallel** implementation.

---

## 1. Decisions (locked — do not re-litigate)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Rewrite depth | **Clean rebuild** — new tables/models/routes named exactly per spec (`batch_definition`, `batch_definition_version`, `batch_group`, `batch`, `batch_kpi_result`, `batch_excursion`, …). |
| 2 | Report path | **Seed report templates** into the EXISTING Report module (`report_templates`), rendered by the existing `report_renderer.py`; auto-gen + email via existing `report_scheduler.py` / `notifications.py`. No 2nd report engine or email scheduler. |
| 3 | Delivery | **All stages, then one build** (`npm run dist` → both EXEs at the end). |
| 4 | Old data | **Fresh start** — new tables begin empty; legacy `batch_*` tables left untouched, NOT surfaced in the new UI. A best-effort status-mapping helper exists in case legacy is later surfaced, but no migration is built. |
| 5 | Legacy status mapping (if ever surfaced) | running→RUNNING, completed→COMPLETED, failed→ABORTED, validated→COMPLETED; result pass→WITHIN_SPECIFICATION, fail→OUT_OF_SPECIFICATION, na→NOT_EVALUATED; data-quality→NOT_EVALUATED. |

---

## 2. CRITICAL GUARDRAIL (must hold at every stage)

The following MUST NOT break — all changes are additive:

- historian **write path** (`INSERT INTO historian_readings`) and gateway data collection
- gateway communication + **gateway trigger execution**
- **dashboard charts**
- the **Report module**, **email module**, **scheduled email**
- auth / user management
- navigation **outside** Batch Management

**Verification (run before every build):**
```bash
git diff --stat HEAD -- backend/app/services/plc_manager.py backend/app/services/telemetry_service.py \
  frontend/src/components/Dashboard/   # expect EMPTY
grep -n "INSERT INTO historian_readings" backend/app/services/app_store.py   # must be unchanged
```
The only shared files touched: `app_store.py` (additive DDL + new query helpers only),
`App.jsx` (nav/menu/dispatcher for the 3 new pages), `api.js` (new batch fns).
The existing gateway trigger evaluator is **consumed/reused, not duplicated or moved**.

---

## 3. Terminology → schema mapping

| Spec term | New table(s) | Legacy analog (kept, not reused) |
|-----------|--------------|----------------------------------|
| Batch Definition | `batch_definition` + `batch_definition_version` (immutable published versions) | `batch_types` |
| Batch Definition Tag | `batch_definition_tag` | `summary_tags_json` on `batch_types` |
| Batch Trigger Reference | `batch_trigger_reference` | `trigger_start_json`/`trigger_stop_json` on `batch_types` |
| Batch Limit Definition | `batch_limit_definition` | `batch_type_limits` |
| KPI Definition | `kpi_definition` | (none) |
| Batch Group | `batch_group` | `batches` where `batch_kind='multiple'` (parent) |
| Batch | `batch` | `batches` |
| Batch Data Window | `batch_data_window` | `batch_membership` |
| Batch Event | `batch_event` | `batch_events` |
| Batch KPI Result | `batch_kpi_result` | (none) |
| Batch Excursion | `batch_excursion` | (none) |
| Batch Report Reference | `batch_report_reference` | (linked `generated_reports`) |

> The window-link-to-historian architecture is preserved: `batch_data_window` stores
> `(gateway_id, window_start, window_end)`; reads JOIN `historian_readings` by
> `(tenant, gateway, ts-range)`. **No time-series rows are ever duplicated.**

---

## 4. Reuse map (call these; do NOT reimplement)

### Historian (read-only) — `app_store.py`
- `get_historian_rows_range(from_utc,to_utc,limit,offset,gateway,tag)` — window rows (oldest-first)
- `get_historian_agg_rows(bucket, …)` — minute/hour/day aggregates
- `get_historian_stats(from_utc,to_utc,gateway,tag)` — sum/avg/min/max/count/latest per tag
- `get_historian_rule_stats(rules, …)` — operator (any/eq/ne/lt/lte/gt/gte/between) + agg (count/sum/avg/min/max/latest)
- `get_live_rows(…)` — latest-per-(gateway,tag)
- `get_config_domain('gateway_configurations')` — tag/gateway registry (equipment ≈ gateway)

### Report module (reuse) — `services/`
- `reports_store.ReportsStore` — `upsert_template`, `get_template`, `list_templates`,
  `insert_generated`, `get_generated`, `update_generated_email_status`,
  `upsert_schedule`, `mark_schedule_run`
- `report_renderer.render_template_to_pdf(template) -> (Path, bytes, sha256)`
- `report_renderer.build_template_render_data(template) -> dict` (HTML preview)
- `report_renderer` **already resolves** `time_range={preset:'batch', batch_id | batch_of_type_id}`
  via `BatchService` → so batch templates "just work"
- `report_scheduler.ReportRunner.run(schedule, triggered_by, email_settings)` — render+store+email
- `notifications.send_email_request(EmailRequest)` — SMTP/PHP send with attachments (direct call)

### Trigger evaluation (reuse the logic) — `modules/batch_management/triggers.py`
- Rule shape `{operator: AND|OR, rules:[{tag, kind: rising_edge|falling_edge|threshold|equals, value, op, hysteresis}]}`
- `_evaluate_condition`, `_evaluate_rule`, `_latest_values` — reused by the new execution daemon.

### Frontend platform — `styles.css`, `App.jsx`
- Theme tokens (dark/light): `--bg --card --text --muted --stroke --teal(#14a89a) --brand --ok --danger --error-bg/text/border`
- Components/classes: `.card`, `.table/.thead/.trow/.db-cell` (CSS-grid tables), `.btn/.btn-primary/.btn-secondary/.btn-danger/.btn-sm/.icon-btn/.table-action-btn`, `.form-grid`, `.modal-backdrop/.modal-card/.modal-card-wide`, status pills `.status-online/.status-warning/.status-offline`
- Charts: **Recharts** (`LineChart/ComposedChart/ReferenceLine`, `.chart-wrap`)
- Permissions: `currentUser.{role,permissions}`, `canEditPage(page)`, `canOpenPage(page)`, `buildRolePermissions(role)`
- **No existing wizard** → build one wide-modal stepper (horizontal step indicator + inline sections).

---

## 5. New database schema (additive)

**Placement:** `app_store.py`, immediately after the legacy batch block (after the
`batch_manual_entries` CREATE, ~line 4625, before the `historian_readings` PRAGMA at ~4626).
**Idiom:** idempotent `CREATE TABLE IF NOT EXISTS` + PRAGMA-guarded `ALTER TABLE ADD COLUMN`.
Every table has `tenant_id TEXT NOT NULL DEFAULT 'default'`. IDs `{prefix}-{12hex}` via `_new_id`.

Tables (full DDL finalized in the plan file / Stage 2):
`batch_definition`, `batch_definition_version`, `batch_definition_tag`,
`batch_trigger_reference`, `batch_limit_definition`, `kpi_definition`,
`batch_group`, `batch`, `batch_data_window`, `batch_event`, `batch_kpi_result`,
`batch_excursion`, `batch_report_reference`.

Recommended indexes: batch reference, group reference, batch status, group status,
gateway/equipment id, definition_version_id, started/ended, event ts, report/email status.

---

## 6. Lifecycle (state machines)

**Batch execution status** (separate from quality + data-quality):
```
PLANNED → READY → RUNNING ⇄ HELD → COMPLETED | ABORTED
READY → ABORTED,  (any) → INVALID
```
Allowed transitions ONLY; every transition writes a `batch_event`. Unrestricted status
updates are rejected by the execution service's transition guard.

**Quality status:** `NOT_EVALUATED | WITHIN_SPECIFICATION | WITH_WARNINGS | OUT_OF_SPECIFICATION | DATA_INCOMPLETE`
**Data-quality status:** `GOOD | GOOD_WITH_WARNINGS | INCOMPLETE | INVALID | NOT_EVALUATED`

**Batch Group status:** `PLANNED → ACTIVE → COMPLETED | ABORTED`. Completing a group does
**not** modify/delete children.

---

## 7. KPIs (initial fixed set)

**Batch:** cycle time, running time, hold time, min, max, avg, first, last, total, count,
time above/below/within/outside limit, #excursions, total energy, total flow, production qty.
**Group:** total/completed/aborted children, avg/min/max cycle time, total energy,
total production qty, total excursions, completion %.
Each result: `value, unit, calculated_utc, quality_status ∈ {VALID, INCOMPLETE, INVALID, NOT_APPLICABLE}`.
**Never mark a KPI VALID when required data is missing.**

---

## 8. Report templates (4, seeded into `report_templates`)

Seeded on module init (idempotent, by stable id/name), using the existing section schema:
- **Batch Summary** — header + kpi_grid + summary table (time_range `{preset:'batch', batch_id}`)
- **Batch Detailed** — + trend line_charts (with `limit_lines`) + event table + excursions table
- **Batch Group Summary** — group header + children rollup table + group KPIs
- **Batch Group Detailed** — + per-child sections

Generate = build a concrete template (inject `batch_id`) → `render_template_to_pdf` →
`insert_generated` → link via `batch_report_reference`. Auto-generate-on-complete + auto-email
run through `ReportRunner`/`send_email_request`. Preview modal uses
`GET /api/reports/templates/{id}/preview-data` (HTML) or `generated/{id}/file?inline=true` (PDF).

---

## 9. New API surface (spec-named; existing conventions)

```
/api/batch-management/definitions            GET POST
/api/batch-management/definitions/{id}       GET PUT
/api/batch-management/definitions/{id}/validate   POST
/api/batch-management/definitions/{id}/publish    POST
/api/batch-management/definitions/{id}/versions   GET POST

/api/batch-management/batches                GET POST
/api/batch-management/batches/{id}           GET
/api/batch-management/batches/{id}/start|stop|hold|resume|abort   POST
/api/batch-management/batches/{id}/comments  POST
/api/batch-management/batches/{id}/events|trends|kpis|excursions  GET
/api/batch-management/batches/{id}/reports   GET POST
/api/batch-management/batches/{id}/reports/{reportId}/email       POST

/api/batch-management/groups                 GET POST
/api/batch-management/groups/{id}            GET
/api/batch-management/groups/{id}/complete|abort   POST
/api/batch-management/groups/{id}/batches|kpis     GET
/api/batch-management/groups/{id}/reports    GET POST

/api/batch-management/analysis/comparison    GET
/api/batch-management/analysis/excursions    GET
```
Reuse Report module preview/download/email endpoints where present. `/status` stays ungated.

---

## 10. Frontend (3 pages + modal + wizard)

New files under `frontend/src/components/BatchManagement/`:
- `BatchOverviewPage.jsx` — summary cards + filters + Batch table + Batch Group table + row actions
- `BatchDetailPage.jsx` — header, tag values, trends (Recharts), KPIs, event timeline, excursions, comments, report actions
- `BatchGroupDetailPage.jsx` — progress, child table, group KPIs, group events, report actions
- `BatchDefinitionsPage.jsx` — list + guided multi-step builder (wide-modal stepper: General → Structure → Identification → Start → Stop → Tags&Limits → KPIs → Reports&Email → Validate/Publish)
- `BatchAnalysisPage.jsx` — tabs: Reports / Batch Comparison / Batch Group Performance / Excursions
- `ReportPreviewModal.jsx` — shared modal (title, generated date, template name; close/download/regenerate/email; dark+light)

`App.jsx` menu group "Batch Management" → items **Batch Overview**, **Batch Definitions**,
**Batch Analysis** (replacing Batches/Batch Types/Batch Audit); add dispatcher cases; keep
license-gated visibility via `/status`. All new UI uses CSS vars only (dark+light), permission
gating via `canEditPage`/`canOpenPage`.

---

## 11. Backend services (new, under the module)

- **BatchDefinitionService** — draft CRUD, `validate`, `publish` (snapshots `configuration_json`, freezes version), `new_version`, `load_config`.
- **BatchExecutionService** — `create/start/stop/hold/resume/abort` with a **transition guard**; assigns references; associates `batch_data_window`; consumes gateway trigger state; **idempotency** (key = gateway+trigger+equipment+event_type+source_ts+reference); **restart recovery** (reload active batches, resume consumption, record recovery event, mark data-quality INCOMPLETE if continuity unconfirmed).
- **KpiService** — batch + group KPIs; persists `batch_kpi_result`; returns quality status.
- **ExcursionService** — evaluate limits over the window; persist `batch_excursion` (tag, limit_type, limit_value, actual min/max, start/end, duration_s, severity, acknowledged).
- **DataQualityService** — expected vs actual sample count, missing required tags, bad-quality samples, comms/query failures → status.
- **ReportIntegrationService** — builds concrete template, calls `ReportsStore`/`render_template_to_pdf`/`ReportRunner`; links `batch_report_reference`; exposes preview/download/regenerate/email.
- **BatchEventService** — lifecycle/trigger/manual/report/email events → timeline.

**One daemon only.** The new execution daemon replaces the legacy `triggers.py` daemon start
in `__init__.py` (or the legacy daemon is disabled) so two daemons never both create batches.
It reuses the trigger *evaluation* helpers, not the legacy `_fire_start/_fire_stop` paths.

---

## 12. Staged plan (execution order)

1. **Discovery** — done (this guide).
2. **Domain + DB** — add the 13 tables to `app_store.py` (additive/idempotent); new Pydantic models; new `models.py` shapes. Verify historian untouched.
3. **Execution** — state machine + transition guard; manual actions; window association; single daemon consuming triggers; idempotency + restart recovery.
4. **Calculations** — KPI service, data-quality service, excursion detection, group aggregation.
5. **Report + email** — seed 4 templates; generate/auto-generate/email via existing engine; preview.
6. **Frontend** — 3 pages + sub-pages + preview modal + definition wizard; `api.js` fns; `App.jsx` nav; dark/light verify.
7. **Testing + regression** — backend unit/integration (transitions incl. invalid, dup-trigger prevention, KPI, excursion, data-quality, report-gen, email, restart recovery) via `scripts/` + a test backend on a spare port; manual dark/light + permissions; full guardrail git-diff; then ONE `npm run dist`.

---

## 13. Risks & open questions

- **Plant/area/equipment**: no such tables exist; **equipment ≈ gateway**. v1 treats plant/area
  as optional free-text and equipment as a gateway selector (spec allows "only filters
  supported by available data"). Revisit if a real asset tree is added later.
- **Trigger reference model**: triggers currently live inline on `batch_types`, not a standalone
  registry. The new `batch_trigger_reference` stores the referenced gateway + condition; the new
  daemon evaluates it with the reused helpers. Confirm no double-fire with any legacy daemon.
- **Single daemon**: must disable/replace the legacy `triggers.py` auto-start to avoid two
  batch-creating loops.
- **Legacy coexistence**: old `batch_*` tables + old endpoints remain but are unreferenced by the
  new UI; keep them inert to avoid confusion.

---

*This guide is the source of truth for the redesign. Update it as decisions evolve.*
