# OEE Module — plan and architecture notes

First version, 2026-08-27. Local-first, no cloud dependency.

## 1. What the existing app already provides (and OEE must not duplicate)

| Concern | Where it lives today | How OEE uses it |
|---|---|---|
| Gateways | `config_documents` domain `gateway_configurations` (JSON doc, **not a table**) | soft reference by `gateway_id` |
| Devices | `config_documents` domain `devices` (JSON doc) | soft reference by `device_id` |
| Tags | a `tags[]` array **on each gateway document** | soft reference by `tag_name` |
| Historian | `historian_readings` table (+ `historian_agg_*` rollups) | read via `app_store.get_historian_rows_range()` |
| Live values | `app_store._local_live_latest_cache` / `/api/app-store/live` | current machine state |
| Power meters | `power_management_config` domain + `power_manager` | power tags read from the historian like any tag |
| Permissions | `services/permission_catalog.py` + `access_policy.has_module()` | two new catalog keys |
| Licence modules | `control_plane_store.MODULE_CATALOG` | one new module key `oee` |
| Lite surface | `/api/lite-local/capabilities` | one new flag `oee` |
| UI shell, dark/light | `styles.css` CSS variables, `.card`, `.table`, `.modal-card` | reused verbatim |

### Consequence for the schema
Because gateways/devices/tags are **documents, not rows**, SQL foreign keys to
them are impossible. OEE tables store them as `TEXT` soft references and the
service validates them on write (the reference must resolve to a real gateway /
device / tag at save time). This is called out in every table comment so nobody
later "fixes" it by adding a FK to a table that does not exist.

The historian is referenced the same way — by `(gateway_id, tag_name, ts range)`
— never by row id, because historian rows are pruned by retention.

## 2. Module layout (mirrors `modules/batch_management/`)

```
backend/app/modules/oee/
  __init__.py       exports the router
  schema.py         the 16 tables + indexes, called from app_store._ensure_schema
  store.py          all DB access (CRUD + event/count writers)
  state_engine.py   machine state detection: signal rules, power rules, combined
  calc.py           OEE math, runtime/downtime rollup, energy + waste
  service.py        joins the OEE config to the collected data
  seed.py           default downtime + quality reasons, starter power rules
  router.py         REST surface + its pydantic payload models, permission-gated
```

Frontend:
```
frontend/src/components/OEE/
  OeeShared.jsx        status pill, KPI card, gateway→device→tag picker
  OeeOverview.jsx      KPI cards, machine cards, charts, filters
  OeeOperator.jsx      operator screen + downtime modal
  OeeConfiguration.jsx the 8 configuration sections
```

## 3. Menu

`NAV_SECTIONS` gains one group, placed after Power Management:

```
OEE  ->  Overview | Operator Screen | Configuration
```

Page keys: `oee_overview`, `oee_operator`, `oee_configuration`.

## 4. Status model

States: `running, idle, stopped, faulted, changeover, waiting_material,
waiting_operator, planned_stop, off, unknown`.

Sources: `plc, sensor, power, manual, combined`.

Confidence: `high, medium, low, conflict, missing`.

Combined resolution (state_engine):
1. Signal (PLC/sensor) result is primary.
2. Power rules produce a second opinion.
3. Agreement → `high`. Only one source → `medium`. Disagreement → `conflict`.
   Neither → `missing`.
4. Flags raised independently of the state: `energy_waste` (stopped/idle but
   power above threshold), `blocked` (running but count not increasing).

## 5. OEE maths (calc.py)

```
Availability = Runtime / PlannedProductionTime
Performance  = (IdealCycleTime * TotalCount) / Runtime
Quality      = GoodCount / TotalCount
OEE          = Availability * Performance * Quality
```
* `PlannedProductionTime` = shift time in the window − excluded planned stops.
* `Runtime` = time in `running`/`production` inside planned production time.
* `GoodCount` falls back to `TotalCount − RejectCount` when good is not measured.
* Divide-by-zero returns `None` (rendered "Not enough data"), never 0 %.
* Each factor is clamped to ≤ 1.0 unless `allow_over_100` is set on the machine.

## 6. Energy

`energy_running / energy_idle / energy_stopped / energy_planned_stop` are
integrated from the machine's power tag over the state timeline. Waste =
energy recorded in a state whose configured standby threshold was exceeded,
plus energy during `blocked`.

## 7. Deployment

* Full surfaces (desktop, LAN web, local web): all three pages.
* Lite: Overview only, read-only — flag `oee` from `/api/lite-local/capabilities`.
* No cloud calls anywhere in the module. All reads hit the local app store.

## 8. Deliberately out of scope for v1

Predictive maintenance, AI root cause, ERP/MES, cloud-only features, approval
workflow, maintenance tickets, scheduling engine, advanced reporting engine.


---

## 9. Worked examples (all verified by `scripts/test_oee_module.py`)

### 9.1 OEE

An 8-hour shift with a 30-minute excluded lunch break, 6 hours of runtime,
1000 parts made, 40 rejected, ideal cycle time 18 s:

```
PlannedProductionTime = 8h - 0.5h            = 7.5 h = 27 000 s
Runtime                                       =  6.0 h = 21 600 s
Availability = 21 600 / 27 000                = 0.800   (80.0 %)
Performance  = (18 x 1000) / 21 600           = 0.833   (83.3 %)
GoodCount    = 1000 - 40                      = 960     (good not measured)
Quality      = 960 / 1000                     = 0.960   (96.0 %)
OEE          = 0.800 x 0.833 x 0.960          = 0.640   (64.0 %)
```

### 9.2 "Not enough data" cases

| Situation | Result |
|---|---|
| No ideal cycle time configured | Performance `None`, OEE `None` |
| Runtime 0 | Performance `None` (nothing to have performed against) |
| Total count 0 | Quality `None` |
| No planned production time | Availability `None` |

They are **never** rendered as 0 %. A machine that was not scheduled has no
OEE; reporting 0 % would say it failed when it was simply not asked to run.

### 9.3 Power waste

A machine stopped for one hour that drew 4 kWh, with a standby allowance of
0.5 kW:

```
Allowed while stopped = 0.5 kW x 1 h = 0.5 kWh
Actually used                        = 4.0 kWh
Wasted                               = 3.5 kWh
```

Only the **excess** counts. A machine that legitimately keeps a controller and
heater alive is not wasting all of it, and calling the whole figure waste makes
the number useless. With no allowance configured the whole amount is treated as
avoidable, which is the conservative reading.

Waste is also raised when a machine is `running` but its count is not
increasing (`blocked` flag) — energy spent producing nothing.

### 9.4 Combined status resolution

| PLC / sensor | Power | Result | Confidence | Flags |
|---|---|---|---|---|
| running | 8 kW (production) | running | high | — |
| stopped | 0.2 kW | stopped | low* | — |
| stopped | 8 kW | stopped | conflict | conflict, energy_waste |
| running, count flat | 8 kW | running | high | blocked |
| fault + running | 8 kW | **faulted** | conflict | — |
| no tags | 8 kW | running | low | — (power fallback) |
| nothing | nothing | unknown | missing | — |
| stale tags only | — | **no verdict** | — | a dead gateway is not "stopped" |

\* low because no power rule covered 0.2 kW; add an "Off" rule below the idle
band and the pair agrees, giving high confidence.

## 10. Sample data seeded on first boot

* **19 downtime reasons** across the spec's categories (Equipment failure,
  Electrical/Mechanical/Sensor fault, Waiting for material/operator,
  Changeover, Cleaning, Maintenance, Quality issue, Planned stop, Energy waste,
  Unknown), each flagged planned/unplanned.
* **11 quality reasons** (Machine defect, Material defect, Operator error,
  Process issue, Startup scrap, Changeover scrap, Unknown).
* **4 starter power rules per machine**, created when power monitoring is
  switched on: Off (< 0.5 kW for 2 min), Idle (0.5–3 kW for 3 min), Production
  (> 3 kW for 1 min), and Energy waste (> 2 kW for 5 min while the count is
  not increasing).

Seeding only fills an EMPTY table. A site that deletes a reason on purpose does
not get it back on the next restart.

The starter power bands are deliberately conservative and **must** be tuned per
machine — different machines draw very different power, which is why the rules
are per-machine rows rather than global constants.
