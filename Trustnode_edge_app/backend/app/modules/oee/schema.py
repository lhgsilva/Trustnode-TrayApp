# -*- coding: utf-8 -*-
"""OEE tables.

Sixteen tables, all prefixed `oee_`, created by `ensure_oee_schema(conn)` which
AppStore._ensure_schema calls inside its own transaction.

IMPORTANT — why there are no foreign keys to gateways/devices/tags
------------------------------------------------------------------
In this application a gateway, a device and a tag are NOT rows. Gateways and
devices are JSON documents in `config_documents` (domains
`gateway_configurations` and `devices`), and a tag is a string inside the
gateway document's `tags[]` array. There is therefore nothing for SQL to point
at, and `REFERENCES gateway(id)` would fail to create.

So every link to the collection system is a SOFT reference: a TEXT column
holding the id/name, validated by the service when the row is saved. The same
applies to the historian - OEE addresses it by (gateway_id, tag_name, time
range), never by row id, because retention prunes historian rows and a stored
row id would dangle.

Do not "fix" this by adding foreign keys. See docs/OEE_MODULE_PLAN.md.
"""
from __future__ import annotations

from typing import Any

# Every table carries tenant_id, matching the batch tables, so a multi-tenant
# edge keeps its OEE data separated the same way.
OEE_SCHEMA_SQL = """
/* ============================ 1. Machines ============================= */
CREATE TABLE IF NOT EXISTS oee_machines (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  machine_code TEXT NULL,                 -- operator-facing short id
  name TEXT NOT NULL,
  line TEXT NULL,
  area TEXT NULL,
  description TEXT NULL,
  oee_enabled INTEGER NOT NULL DEFAULT 1,
  signal_enabled INTEGER NOT NULL DEFAULT 1,     -- PLC/sensor monitoring
  power_enabled INTEGER NOT NULL DEFAULT 0,      -- power meter monitoring
  manual_enabled INTEGER NOT NULL DEFAULT 1,     -- operator input
  default_status_source TEXT NOT NULL DEFAULT 'signal',  -- signal|power|manual|combined
  ideal_cycle_time_s REAL NULL,           -- fallback when no product is running
  standby_power_kw REAL NULL,             -- above this while stopped = waste
  idle_power_kw REAL NULL,                -- above this while idle = waste
  allow_over_100 INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_utc TEXT NOT NULL,
  created_by TEXT NULL,
  updated_utc TEXT NOT NULL,
  updated_by TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_oee_machines_tenant ON oee_machines(tenant_id, enabled);
CREATE UNIQUE INDEX IF NOT EXISTS idx_oee_machines_code
  ON oee_machines(tenant_id, machine_code) WHERE machine_code IS NOT NULL;

/* ========================= 2. Signal mappings ========================= */
/* gateway_id/device_id/tag_name are SOFT references - see module docstring. */
CREATE TABLE IF NOT EXISTS oee_signal_mappings (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  machine_id TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  source_type TEXT NOT NULL DEFAULT 'plc',   -- plc|sensor|manual|energy_meter
  gateway_id TEXT NULL,
  device_id TEXT NULL,
  tag_name TEXT NULL,
  oee_function TEXT NOT NULL,                -- running_status|cycle_start|total_count|...
  condition_op TEXT NULL,                    -- eq|ne|gt|gte|lt|lte|rising|falling|changed|stale
  condition_value TEXT NULL,
  hold_seconds REAL NOT NULL DEFAULT 0,      -- condition must persist this long
  priority INTEGER NOT NULL DEFAULT 100,     -- lower wins when several match
  notes TEXT NULL,
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oee_sigmap_machine ON oee_signal_mappings(tenant_id, machine_id, enabled);
CREATE INDEX IF NOT EXISTS idx_oee_sigmap_fn ON oee_signal_mappings(machine_id, oee_function);

/* ====================== 3. Power meter mappings ======================= */
CREATE TABLE IF NOT EXISTS oee_power_meter_mappings (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  machine_id TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  gateway_id TEXT NULL,
  device_id TEXT NULL,
  power_tag TEXT NULL,                      -- kW
  energy_tag TEXT NULL,                     -- kWh
  current_tag TEXT NULL,                    -- A   (optional)
  voltage_tag TEXT NULL,                    -- V   (optional)
  power_factor_tag TEXT NULL,               --     (optional)
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_oee_pmm_machine ON oee_power_meter_mappings(tenant_id, machine_id);

/* ======================= 4. Power state rules ========================= */
CREATE TABLE IF NOT EXISTS oee_power_state_rules (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  machine_id TEXT NOT NULL,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  measurement TEXT NOT NULL DEFAULT 'power_kw',  -- power_kw|current_a
  min_value REAL NULL,                      -- NULL = unbounded
  max_value REAL NULL,
  min_duration_s REAL NOT NULL DEFAULT 0,
  generated_status TEXT NOT NULL,           -- off|stopped|idle|running|production|high_consumption|energy_waste|unknown
  requires_no_count INTEGER NOT NULL DEFAULT 0,  -- "and production is not increasing"
  priority INTEGER NOT NULL DEFAULT 100,
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oee_psr_machine ON oee_power_state_rules(tenant_id, machine_id, enabled, priority);

/* ============================ 5. Products ============================= */
CREATE TABLE IF NOT EXISTS oee_products (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  product_code TEXT NULL,
  name TEXT NOT NULL,
  sku TEXT NULL,
  ideal_cycle_time_s REAL NULL,
  standard_rate_per_hour REAL NULL,
  unit TEXT NULL DEFAULT 'pcs',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oee_products_tenant ON oee_products(tenant_id, enabled);

/* ============================= 6. Orders ============================== */
CREATE TABLE IF NOT EXISTS oee_orders (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  order_number TEXT NOT NULL,
  machine_id TEXT NULL,
  product_id TEXT NULL,
  target_quantity REAL NULL,
  planned_start_utc TEXT NULL,
  planned_end_utc TEXT NULL,
  actual_start_utc TEXT NULL,
  actual_end_utc TEXT NULL,
  status TEXT NOT NULL DEFAULT 'planned',   -- planned|running|paused|completed|cancelled
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oee_orders_machine ON oee_orders(tenant_id, machine_id, status);
CREATE INDEX IF NOT EXISTS idx_oee_orders_number ON oee_orders(tenant_id, order_number);

/* ============================= 7. Cycles ============================== */
CREATE TABLE IF NOT EXISTS oee_cycles (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  machine_id TEXT NOT NULL,
  product_id TEXT NULL,
  order_id TEXT NULL,
  start_utc TEXT NOT NULL,
  end_utc TEXT NULL,                        -- NULL while the cycle is open
  duration_s REAL NULL,
  source TEXT NOT NULL DEFAULT 'manual',    -- plc|sensor|manual
  result TEXT NOT NULL DEFAULT 'unknown',   -- good|reject|unknown
  operator TEXT NULL,
  notes TEXT NULL,
  created_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oee_cycles_machine ON oee_cycles(tenant_id, machine_id, start_utc DESC);
CREATE INDEX IF NOT EXISTS idx_oee_cycles_open ON oee_cycles(machine_id) WHERE end_utc IS NULL;

/* ============================= 8. Shifts ============================== */
CREATE TABLE IF NOT EXISTS oee_shifts (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  name TEXT NOT NULL,
  start_time TEXT NOT NULL,                 -- 'HH:MM' local
  end_time TEXT NOT NULL,                   -- may wrap past midnight
  working_days TEXT NOT NULL DEFAULT '1,2,3,4,5',  -- 1=Mon .. 7=Sun
  break_minutes REAL NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oee_shifts_tenant ON oee_shifts(tenant_id, enabled);

/* ========================== 9. Planned stops ========================== */
CREATE TABLE IF NOT EXISTS oee_planned_stops (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  name TEXT NOT NULL,
  machine_id TEXT NULL,                     -- NULL = whole line
  line TEXT NULL,
  shift_id TEXT NULL,
  start_time TEXT NULL,                     -- 'HH:MM' for a repeating stop
  end_time TEXT NULL,
  start_utc TEXT NULL,                      -- or an absolute one-off window
  end_utc TEXT NULL,
  repeat_rule TEXT NOT NULL DEFAULT 'daily', -- none|daily|weekdays|weekly
  exclude_from_oee INTEGER NOT NULL DEFAULT 1,
  show_on_dashboard INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oee_pstops_machine ON oee_planned_stops(tenant_id, machine_id, enabled);

/* ==================== 9b. Planning calendar (2026-08-29) ==============
   Concrete, dated events an administrator schedules. Distinct from
   oee_planned_stops, which models a REPEATING stop pattern: a planned event
   here has real start/end timestamps, may carry a product/order/batch, and
   states its own effect on the OEE denominator.

   `exclude_from_oee` is the one field that changes a number rather than a
   picture, so it is explicit per event and never inferred from the type. */
CREATE TABLE IF NOT EXISTS oee_planned_events (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  name TEXT NOT NULL,
  event_type TEXT NOT NULL DEFAULT 'planned_production',
  machine_id TEXT NULL,                     -- NULL = the whole line
  line TEXT NULL,
  shift_id TEXT NULL,
  start_utc TEXT NOT NULL,
  end_utc TEXT NOT NULL,
  product_id TEXT NULL,
  order_id TEXT NULL,
  batch_ref TEXT NULL,
  recipe_ref TEXT NULL,
  -- How this window is treated by the calculation. "No production planned"
  -- must NOT count as downtime; planned maintenance must not be mistaken for
  -- an unplanned stop; a changeover is one or the other by customer policy.
  exclude_from_oee INTEGER NOT NULL DEFAULT 0,
  counts_as_planned_stop INTEGER NOT NULL DEFAULT 0,
  expected_runtime_s INTEGER NULL,
  expected_quantity REAL NULL,
  expected_cycle_s REAL NULL,
  repeat_rule TEXT NOT NULL DEFAULT 'none',  -- none|daily|weekdays|weekly
  notes TEXT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NULL,
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);
-- The calendar is always read as "this machine, this window", so the index
-- leads with the tenant and machine and ends on the range column.
CREATE INDEX IF NOT EXISTS idx_oee_planned_events_win
  ON oee_planned_events(tenant_id, machine_id, start_utc);
CREATE INDEX IF NOT EXISTS idx_oee_planned_events_range
  ON oee_planned_events(tenant_id, start_utc, end_utc);

/* ======================= 10. Downtime reasons ========================= */
CREATE TABLE IF NOT EXISTS oee_downtime_reasons (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  category TEXT NOT NULL,
  reason TEXT NOT NULL,
  description TEXT NULL,
  is_planned INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 100,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oee_dtr_tenant ON oee_downtime_reasons(tenant_id, enabled, sort_order);

/* ======================== 11. Quality reasons ========================= */
CREATE TABLE IF NOT EXISTS oee_quality_reasons (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  category TEXT NOT NULL,
  reason TEXT NOT NULL,
  description TEXT NULL,
  sort_order INTEGER NOT NULL DEFAULT 100,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oee_qr_tenant ON oee_quality_reasons(tenant_id, enabled, sort_order);

/* ======================== 12. Machine events ========================== */
/* The state timeline. One row per state CHANGE, closed when the next starts.
   Runtime/downtime and every duration in the module derive from this. */
CREATE TABLE IF NOT EXISTS oee_machine_events (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  machine_id TEXT NOT NULL,
  state TEXT NOT NULL,                      -- running|idle|stopped|faulted|...
  status_source TEXT NOT NULL DEFAULT 'signal',  -- signal|power|manual|combined
  confidence TEXT NOT NULL DEFAULT 'medium',     -- high|medium|low|conflict|missing
  start_utc TEXT NOT NULL,
  end_utc TEXT NULL,                        -- NULL = currently in this state
  duration_s REAL NULL,
  is_planned INTEGER NOT NULL DEFAULT 0,
  planned_stop_id TEXT NULL,
  downtime_reason_id TEXT NULL,
  downtime_category TEXT NULL,
  operator_comment TEXT NULL,
  confirmed_by TEXT NULL,
  confirmed_utc TEXT NULL,
  detected_detail TEXT NULL,                -- which rule/tag decided this
  created_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oee_events_machine ON oee_machine_events(tenant_id, machine_id, start_utc DESC);
CREATE INDEX IF NOT EXISTS idx_oee_events_open ON oee_machine_events(machine_id) WHERE end_utc IS NULL;
CREATE INDEX IF NOT EXISTS idx_oee_events_window ON oee_machine_events(tenant_id, start_utc, end_utc);

/* ======================= 13. Production counts ======================== */
CREATE TABLE IF NOT EXISTS oee_production_counts (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  machine_id TEXT NOT NULL,
  ts_utc TEXT NOT NULL,
  total_count REAL NOT NULL DEFAULT 0,
  good_count REAL NULL,
  reject_count REAL NULL,
  source TEXT NOT NULL DEFAULT 'manual',    -- plc|sensor|manual
  order_id TEXT NULL,
  product_id TEXT NULL,
  cycle_id TEXT NULL,
  operator TEXT NULL,
  created_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oee_counts_machine ON oee_production_counts(tenant_id, machine_id, ts_utc DESC);

/* ======================== 14. Quality results ========================= */
CREATE TABLE IF NOT EXISTS oee_quality_results (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  machine_id TEXT NOT NULL,
  ts_utc TEXT NOT NULL,
  quantity REAL NOT NULL DEFAULT 0,
  result TEXT NOT NULL DEFAULT 'reject',    -- good|reject|scrap|rework
  quality_reason_id TEXT NULL,
  order_id TEXT NULL,
  product_id TEXT NULL,
  cycle_id TEXT NULL,
  operator TEXT NULL,
  comment TEXT NULL,
  created_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oee_quality_machine ON oee_quality_results(tenant_id, machine_id, ts_utc DESC);

/* ======================== 15. Energy summary ========================== */
/* Energy integrated per machine per state, per rollup bucket. */
CREATE TABLE IF NOT EXISTS oee_energy_summary (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  machine_id TEXT NOT NULL,
  bucket_start_utc TEXT NOT NULL,
  bucket_end_utc TEXT NOT NULL,
  energy_total_kwh REAL NOT NULL DEFAULT 0,
  energy_running_kwh REAL NOT NULL DEFAULT 0,
  energy_idle_kwh REAL NOT NULL DEFAULT 0,
  energy_stopped_kwh REAL NOT NULL DEFAULT 0,
  energy_planned_stop_kwh REAL NOT NULL DEFAULT 0,
  energy_wasted_kwh REAL NOT NULL DEFAULT 0,
  avg_power_kw REAL NULL,
  peak_power_kw REAL NULL,
  created_utc TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_oee_energy_bucket
  ON oee_energy_summary(tenant_id, machine_id, bucket_start_utc);

/* ====================== 16. Calculated results ======================== */
/* Cached OEE per machine per bucket, so the Overview does not recompute a
   month of history on every page load. Always reproducible from the tables
   above - safe to delete. */
CREATE TABLE IF NOT EXISTS oee_calculated_results (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  machine_id TEXT NOT NULL,
  bucket_start_utc TEXT NOT NULL,
  bucket_end_utc TEXT NOT NULL,
  shift_id TEXT NULL,
  order_id TEXT NULL,
  product_id TEXT NULL,
  planned_time_s REAL NOT NULL DEFAULT 0,
  runtime_s REAL NOT NULL DEFAULT 0,
  downtime_s REAL NOT NULL DEFAULT 0,
  planned_stop_s REAL NOT NULL DEFAULT 0,
  total_count REAL NOT NULL DEFAULT 0,
  good_count REAL NOT NULL DEFAULT 0,
  reject_count REAL NOT NULL DEFAULT 0,
  availability REAL NULL,                   -- NULL = not enough data
  performance REAL NULL,
  quality REAL NULL,
  oee REAL NULL,
  energy_kwh REAL NULL,
  energy_wasted_kwh REAL NULL,
  created_utc TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_oee_calc_bucket
  ON oee_calculated_results(tenant_id, machine_id, bucket_start_utc);
CREATE INDEX IF NOT EXISTS idx_oee_calc_window
  ON oee_calculated_results(tenant_id, bucket_start_utc);
"""


def ensure_oee_schema(conn: Any) -> None:
    """Create every OEE table. Idempotent; safe on every boot.

    Called from AppStore._ensure_schema with an open connection so the OEE
    tables are created in the SAME transaction as the rest of the schema - a
    half-migrated store is what makes a boot fail in a way nobody can debug.
    """
    conn.executescript(OEE_SCHEMA_SQL)


# Table names, for tests and for the reset/backup paths that enumerate tables.
OEE_TABLES = (
    "oee_machines",
    "oee_signal_mappings",
    "oee_power_meter_mappings",
    "oee_power_state_rules",
    "oee_products",
    "oee_orders",
    "oee_cycles",
    "oee_shifts",
    "oee_planned_stops",
    "oee_downtime_reasons",
    "oee_quality_reasons",
    "oee_machine_events",
    "oee_production_counts",
    "oee_quality_results",
    "oee_energy_summary",
    "oee_calculated_results",
)
