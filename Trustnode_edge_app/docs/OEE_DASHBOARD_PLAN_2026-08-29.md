# OEE module — dashboards, machine detail, planning calendar, reusable widgets

**Date:** 2026-08-29
**Extends:** [`OEE_MODULE_PLAN.md`](OEE_MODULE_PLAN.md) (the module built 2026-08-27: 16 `oee_` tables, state engine, calc, 16 endpoints, Overview / Operator / Configuration pages).

This is the plan for the second layer: making the module **visual and usable** for operators, supervisors, managers and customer administrators.

> **Reference images were mentioned but not received.** The written specification is detailed enough to build from, so this plan follows it. Send the images and the visual treatment can be adjusted — nothing here depends on them.

---

## 0. What exists today, and what is actually missing

Building the plan on the real code rather than on the brief:

| Layer | Today | Gap |
|---|---|---|
| Tables | 16 `oee_` tables incl. `oee_machine_events`, `oee_calculated_results`, `oee_energy_summary`, `oee_shifts`, `oee_planned_stops` | **no planning-calendar table** |
| API | 16 endpoints: `/meta`, `/config/{kind}`, `/overview`, `/trend`, `/machines/live`, `/machines/{id}/result`, `/machines/{id}/events`, 6 × `/operator/*`, `/health` | no **dashboard-ready** aggregates: status timeline, downtime pareto, energy summary, machine-detail summary, shift comparison, planning CRUD, widget endpoints |
| Pages | Overview, Operator, Configuration | no **Machine Detail**, no **Planning Calendar** |
| Navigation | `hours=` window only | no **date / shift navigation**, no state carried Overview → Detail |
| Widgets | none | **17 reusable widgets** |

The existing `overview` and `trend` endpoints already return plant aggregates and time buckets, so the Overview page is an *enrichment*, not a rewrite.

**Precedent to follow, not invent:** Batch Management already ships 5 dashboard widgets registered in `frontend/src/components/Dashboard/widgetRegistry.js` with `licenseModule: "batch_management"`, which the designer uses to hide them when unlicensed and to render an "unlicensed" placeholder on existing dashboards. OEE widgets use exactly this shape with `licenseModule: "oee"`. No second widget system.

---

## 1. Principles this plan is bound by

These are not style preferences; each is a defect this product has already paid for.

1. **The frontend must not calculate OEE.** Widgets and pages consume module outputs. Two implementations of an availability formula will disagree, and the one on screen will be the one nobody can trace.
2. **Never show a number the data does not support.** Every surface carries the maturity label (§7). "Estimated OEE assuming Quality = 100%" is honest; a bare 87% is not.
3. **No new charting library.** Reuse the existing dashboard chart components. A second charting system is a second set of dark-mode bugs.
4. **Colour never carries meaning alone.** Status text accompanies every status colour — the same rule the Diagnostics page follows.
5. **Aggregates are computed server-side and bounded.** A control-room screen polling a plant overview must not scan the historian; see §6 on cost.
6. **Every new field must reach the API.** The gateway Start payload was a hand-written allowlist that silently dropped fields for weeks. Anything added to a config model gets a test that fails when the transport forgets it.

---

## 2. Pages and navigation

```
OEE ─ Overview            plant dashboard, machine cards
    ├ Machine Detail      opened by clicking a card/row/name — STAYS in the module
    ├ Operator Screen     exists
    ├ Planning Calendar   new, admin/supervisor
    └ Configuration       exists, extended
```

Page keys: `oee_overview`, `oee_machine_detail`, `oee_operator`, `oee_planning`, `oee_configuration`. `pageId()` maps the labels; `MODULE_KEY_BY_PAGE` maps all of them to the `oee` licence module, as the three existing pages already do.

**Selection is carried, not re-picked.** Date, shift, and filters live in one `oeeSelection` state object shared by Overview and Machine Detail. Selecting Shift 2 on 2026-08-29 and clicking Machine 3 opens Machine 3 *for Shift 2 on 2026-08-29*. The selection persists while navigating inside the module, matching how the app already remembers page state.

---

## 3. Date and shift navigation (one component, both pages)

`OeePeriodBar` — the single source of the selected window:

* presets: Today · Yesterday · Current shift · Previous shift · Last 7 days · Last 30 days · Custom range
* steppers: ‹ day › · ‹ shift › · Today · Current shift
* pickers: date range, shift dropdown, machine / line / area / process-type filters
* the resolved window is always printed in the header — a dashboard whose period is ambiguous is a dashboard that gets misread

Shift boundaries come from `oee_shifts`; "current shift" is resolved server-side so the browser's clock and timezone cannot disagree with the calculation.

---

## 4. Overview page

**Header** — title, resolved period, `OeePeriodBar`, filters.

**KPI row** — Overall OEE · Availability · Performance · Quality · Runtime/Downtime · Energy waste, plus counts (production, good, reject), machine-state totals (active / idle / stopped / missing data / signal conflict) and shift progress.

Each KPI card carries: value, trend vs the previous comparable period, status colour **and** label, a tooltip stating the formula, and a confidence indicator. When a value is unavailable it says which of *Not configured · Not enough data · Estimated · Manual input · Missing signal* applies — never a zero dressed as a measurement.

**Machine cards grid** — one card per configured machine: name, line/area, status + duration, OEE (radial) with A/P/Q as compact bars, runtime, downtime, counts, current order/product/batch, current power, energy-waste flag, status confidence, maturity label. Whole card is a link to Machine Detail.

Statuses: Running · Production · Idle · Stopped · Faulted · Planned stop · Changeover · Waiting for material · Waiting for operator · Off · Unknown · Signal conflict.

**Charts** — Overall OEE trend · A/P/Q trend · machine comparison · runtime vs downtime · downtime Pareto · energy usage · energy waste · production counts · **machine status timeline across all selected machines** (bottom, full width).

---

## 5. Machine Detail page

Header: machine, status + duration, resolved period, the same `OeePeriodBar`, quick actions.

Rows: KPIs → current order/product/batch/cycle + status source & confidence + live power → OEE/APQ trend, production trend, energy trend → downtime Pareto, reject Pareto, cycle time → **status timeline, downtime events over time, downtime event table**.

**The downtime timeline is the centrepiece.** X-axis is the shift or day; blocks are machine states; clicking a block opens an editor for start, end, duration, state, category, reason, planned/unplanned, operator comment, source (PLC / sensor / power meter / manual / combined) and confidence. This is the same reason-capture workflow the Operator Screen already uses — one workflow, two entry points.

---

## 6. Backend: dashboard-ready aggregates

New endpoints, all returning the metadata block in §7:

| Endpoint | Serves |
|---|---|
| `GET /oee/dashboard/plant` | plant summary incl. machine-state totals and data-quality summary |
| `GET /oee/dashboard/machines` | one summary per machine (the cards) |
| `GET /oee/dashboard/machine/{id}` | full machine detail |
| `GET /oee/dashboard/timeline` | status blocks per machine |
| `GET /oee/dashboard/downtime-pareto` | reasons by duration, filterable |
| `GET /oee/dashboard/energy` | energy by machine / state / time |
| `GET /oee/dashboard/shifts` | OEE by shift |
| `GET /oee/planning` + POST/PUT/DELETE | planning calendar CRUD |

**Cost control, learnt the hard way.** A plant overview must never scan raw historian rows: these read `oee_calculated_results`, `oee_machine_events` and `oee_energy_summary`, all of which are already written by the state engine. Where a live value is needed, it comes from the latest-per-tag cache, not from history. The store grows ~2.85 GB/day on this install with no retention policy — a dashboard that scans it will be the thing that stops collection.

Every response carries `window`, `shift`, `timezone`, `source`, `confidence`, `stage`, `missing_factors`, `is_estimated`, `is_partial`.

---

## 7. Maturity labels

`Full OEE · Availability Only · OEE without Quality · OEE without Performance · Estimated OEE · Manual Input · Not Configured · Not Enough Data · Signal Conflict`

Shown on machine cards, Machine Detail, every widget, tooltips and reports. When a missing factor is assumed at 100%, the assumption is printed: *"Estimated OEE assuming Quality = 100%"*.

---

## 8. Planning calendar

New table `oee_planned_events`: name, type, machine/line, start, end, shift, product/order/batch, `exclude_from_oee`, expected runtime / quantity / cycle time, notes, enabled, repeat rule.

Types: planned production · planned stop · planned maintenance · cleaning · changeover · setup · no production planned · break · meeting · trial/test run · batch run · recipe run · order run.

Views: day · week · month · machine timeline, with line/machine/shift filters and create/edit/delete. Drag-and-drop **only if** an existing library supports it — a bespoke drag implementation is not worth its bugs in v1.

**Calculation rules:** planned production time comes from shifts *and* planned events; planned stops are excluded from OEE when configured; *no production planned* is not downtime; planned maintenance is separated from unplanned; changeover is planned or unplanned per customer setting.

Edit requires the planning permission; operators may view when permitted.

---

## 9. Reusable widgets (17)

Registered in `WIDGET_TYPES` with `group: "OEE"` and `licenseModule: "oee"`, exactly like the Batch widgets:

`oee_kpi` · `oee_availability_kpi` · `oee_performance_kpi` · `oee_quality_kpi` · `oee_machine_card` · `oee_machine_status` · `oee_trend` · `oee_apq_trend` · `oee_downtime_pareto` · `oee_status_timeline` · `oee_runtime_downtime` · `oee_energy_usage` · `oee_energy_waste` · `oee_production_count` · `oee_shift_performance` · `oee_machine_comparison` · `oee_data_quality`

Each: machine/line/area/date/shift/process filters; dark and light mode; permission-respecting; existing data-loading patterns; **no OEE maths in the widget**; loading, empty and error states; usable on the main dashboard, not only inside the module.

**Widget settings:** title, machine/line/area, process type, date range, shift, aggregation (plant/line/machine/shift/product/order/batch), chart type, target line, trend comparison, confidence, partial OEE, energy values, refresh interval, and colour thresholds (OEE good/warning/bad; A/P/Q good; energy-waste high) — **defaults provided, all configurable.**

---

## 10. Configuration additions

Dashboard settings (enable widgets, default period/shift/group/aggregation, all thresholds) · widget settings (expose to the main dashboard, defaults) · planning settings (enable calendar, planned stops excluded by default, planned-time source: shift schedule / calendar / manual / combined) · machine dashboard settings (show-hide energy, production, quality, cycle, planning sections; default detail range).

---

## 11. Permissions

`oee_overview_view · oee_machine_detail_view · oee_operator_view · oee_operator_edit · oee_config_view · oee_config_edit · oee_planning_view · oee_planning_edit · oee_widgets_view · oee_widgets_add · oee_widget_config_edit`

Added to `permission_catalog.py` so they appear as real checkboxes. A permission key with no checkbox is always `undefined`, which is how batch pages were accidentally admin-only for months.

---

## 12. Delivery order

Staged deliberately. The July 2026 reliability crisis came from one day of concentrated change meeting latent defects, and a config-read regression on 2026-08-28 shipped because an optimisation outran its validation. Each phase builds, gates and ships on its own.

| Phase | Contents | Gate |
|---|---|---|
| **1** | Backend aggregates (§6) + `oee_planned_events` table + permissions | unit tests on aggregate shapes; migration is additive |
| **2** | `OeePeriodBar`, Overview rebuild (KPIs, machine cards, charts) | UI walk incl. the new page; render smoke |
| **3** | Machine Detail + status timeline + downtime timeline/editor | UI walk; downtime write-path test |
| **4** | Planning Calendar + calculation rules | planning CRUD test; OEE-with-planning test |
| **5** | 17 widgets + widget config + Configuration additions | widget registry test; dashboard designer walk |

Priority within phase 2–3 follows §20 of the brief: plant overview, machine cards, machine detail, date/shift navigation, status timeline, downtime over time, downtime Pareto — before any advanced reporting.

**Not in v1:** OEE Reports/Trends page (§1 item 6), month view if the calendar library makes it costly, drag-and-drop without library support.

---

## 13. Risks

| Risk | Containment |
|---|---|
| Dashboard polling becomes a load source | aggregates read pre-computed OEE tables, never raw historian; server-side caching with a short TTL, as `/api/diagnostics` does |
| Widgets drift from module maths | widgets consume endpoints only; a test asserts no OEE formula exists in widget source |
| New config fields silently not transported | the `GatewayConfig` drift-guard pattern applied to OEE config models |
| A timeline over a long window returns enormous payloads | server-side block merging + a hard cap, with the cap reported in the response rather than silently truncating |
| More machines → more rows → the disk fills sooner | OEE consumes existing tags; it adds no collection. The retention decision remains open and is the real constraint |
