# TrustNode Edge — Historian Retention, Tiered Rollups, Backup & Forwarding

**Research report + target architecture + rollout plan**
Date: 2026-08-21 · Status: **PHASES 0–3 IMPLEMENTED** (local engine, API, UI, gate) · Scope: edge backend (`backend/app`), frontend Backup & Retention page, remote/cloud sinks, release gate

---

## IMPLEMENTATION STATUS — 2026-08-21

**Shipped in this change (local historian):**

| Piece | Where |
|---|---|
| Tiered retention engine — policy model + validation, unified rollup table, hierarchical composition, watermark-guarded pruning, paced/adaptive batches, maintenance window, disk guard, online backups, compaction, status | `backend/app/services/retention_engine.py` (2.1k lines) |
| Admin-gated API (policies CRUD/activate, status, estimate, run, runs, compact, backups create/list/restore/cancel/delete/download) | `backend/app/routers/retention.py` |
| Server-side admin gate added to the LEGACY retention/backup/cleanup/**reset** routes (defect D8 — they were browser-gated only) | `backend/app/routers/app_store.py` |
| Legacy scheduler retired (kept for WAL hygiene only); legacy `POST /retention/run` now delegates to the engine; `create_backup` switched to SQLite's online backup API; boot-time staged restore/compaction swap; cloud forward cursor for prune safety | `backend/app/services/app_store.py` |
| Engine construction + lifecycle (starts LAST in deferred init, stops first on shutdown) | `backend/app/state.py`, `backend/app/main.py` |
| Backup & Retention UI — storage status, policy list + editor with live cost estimate, backups by class, maintenance history | `frontend/src/components/Retention/RetentionPanel.jsx` (replaces 3 legacy cards in `App.jsx`) |
| API helpers | `frontend/src/api.js` |
| Correctness suite (76 checks) | `scripts/test_retention_engine.py` |
| Release-gate probe + `[RETENTION]` section + `retention healthy` SLO | `scripts/validate_full_12h.py` |

**Verification performed:**
- **76/76** correctness checks — rollup matches a plain-Python reference exactly; `rollup(raw→1h) == rollup(rollup(raw→1m)→1h)`; idempotent; prune floors hold against rollup **and** cloud watermarks; backups valid/classified; estimator arithmetic; "no policy ⇒ nothing deleted".
- **30/30** live HTTP checks against a running backend on a **copy of the real 8 GB / 8.06M-row production database**: boot, admin gate (403 for non-admin), validation 422 with readable text, estimate, save+activate, dry-run writes nothing, backups (21.7 MB settings backup in ~1 s vs. the legacy 8 GB file copy), download through the API, staged restore + cancel, legacy delegation, autonomous level building, health unaffected.
- **End-to-end on real data**: all three levels built (135,792 × 1-min, 10,176 × 15-min, 2,832 × 1-h buckets, 8,593 text events), values cross-checked against surviving raw rows, `PRAGMA quick_check` ok, aggregated history reaches back to the original first reading, text tags (batch IDs) preserved.
- Frontend builds clean (874 modules, exit 0).

**Defect found and fixed by testing on real data:** deletes had no `ORDER BY`, so SQLite walked the `(tenant_id, ts_utc DESC)` index in its natural order and removed rows from the **middle** of the window — an interrupted catch-up left 07-27…07-30 intact while 08-18…08-20 were gone. Now `ORDER BY ts_utc ASC` (identical query plan, zero cost) keeps raw a contiguous recent window; regression test `[5b]` covers it.

**Deliberately NOT included yet (Phases 4–5):** forwarding modes to LAN/cloud targets (`raw` / `raw_and_rollups` / `rollups_only`), remote retention execution, Postgres monthly partitions, the transparent tier-serving query router for dashboards/reports, and the slim raw schema / monthly partition files. The engine is local-only today; the cloud forward cursor is already honoured so nothing can be deleted before the cloud has taken it.

**Defaults on upgrade:** no policy is active, so **nothing is ever deleted** until an admin chooses one. The legacy broken policy is not imported as active. Daily settings backups (small) run regardless.

---

> Companion to `docs/collection-engine-research-and-plan.md` (2026-07-25). Same method: measure the running system first, name every defect with a file:line, borrow the patterns the historian industry already converged on, then change the app in soak-gated phases that never put collection at risk.

---

## 0. Executive summary

**What the measurements say (live DB, 2026-08-21, 1 gateway × 48 tags @ 1 s):**

| Store (file) | What it holds | Rows | Size | Bytes / reading |
|---|---|---|---|---|
| `trustnode_app_store.db` → `historian_readings` | the local historian (system of record) | 7.95 M (since Jul 27) | 7.8 GB of 8.14 GB | **≈ 1,000** |
| `trustnode_edge.db` → `historian_readings` | the default **"Local SQLite" sink** — a second full copy of every reading, with `raw_payload` JSON | 10.5 M (since Jul 16) | 4.3 GB | ≈ 405 |
| `trustnode_telemetry.db` → `telemetry_samples_raw` | one row per *cycle* with all 48 tags as JSON + SHA-256 — for a cloud path that is **off by default** | 219 k cycles | 1.9 GB | ≈ 8,600 / cycle |

Every cycle is therefore written **three times** (≈ 76 KB per cycle ⇒ **≈ 6.6 GB/day, ≈ 2.4 TB/year** at 1 Hz continuous). The data dir is 19 GB today (incl. two stale 3.5 GB backup files), disk has 133 GB free.

**The retention policy that exists is enabled and has run 702 times — and has never worked:**
- it rolled up **0 rows** in every run (the minute-rollup window `[minute_cutoff, raw_cutoff)` is empty with the saved policy, so raw rows are deleted **without being aggregated** = silent history loss);
- it deletes at most 50 000 rows per hourly run (≈ 14 rows/s) against 48 rows/s of ingest, so it cannot keep up;
- it runs under the **global config lock** (`AppStore._lock`) 30 s after boot, scanning the 8 GB table while the app is still coming up (very likely the cause of the *"Backend service did not respond within 30 seconds"* splash you reported this morning — see §2.3);
- none of its endpoints check the caller's role on the server (admin-only is frontend-only);
- the UI "Save Policy" button silently zeroes the sub-day TTL fields.

**What we propose (one sentence):** a single **Retention Engine** that turns raw readings into a user-defined ladder of aggregate tiers (*raw → 1 min → 15 min → 1 h …*, up to 5 years), applies the same policy to the local historian and to every forwarded database (network or cloud), never deletes anything that has not been rolled up / forwarded / backed up, never blocks collection or the API, and is configured by admins in one "policy" dialog with a live storage estimate.

**What it buys:** with the example policy *7 d raw · 30 d @ 1 min · 1 y @ 15 min · 5 y @ 1 h* the local footprint for this site settles at **≈ 4.4 GB forever** (vs. ≈ 12 TB for 5 years of raw), charts and reports keep working at every horizon (they just get the right resolution for the window they ask for), and a 500-tag site lands at ≈ 46 GB.

**Rollout:** six gated phases. Phase 0 (days) stops the bleeding with zero behaviour change for readers. Phases 1–3 (weeks) deliver the engine, the transparent query router, and the new UI. Phases 4–5 add forwarding modes, remote retention, and the slimmer physical storage layout.

---

## 1. Requirements (restated from the operator, 2026-08-21)

1. Admin-only (server-enforced) **retention policy** that the user can switch on/off or load/create from the Backup & Retention page.
2. **No policy ⇒ collect everything raw** and push it raw to every configured database, accepting the storage cost (but the user must *see* that cost).
3. Policy editor (popup) in the user's mental model — **age bands with a resolution and an aggregate type**:
   - collection interval (raw) — e.g. 1 s
   - ≤ 1 week: raw / 5 s / 1 min … (average, min, max …)
   - ≤ 1 month: raw / 1 / 5 / 15 min …
   - ≤ 1 year: raw / 10 / 30 / 60 min …
   - up to **5 years** maximum.
4. One architecture for **local SQLite, LAN databases and cloud databases**, built on the existing collection engine and store-and-forward design; an explicit plan for the case where **no external DB is configured** (local SQLite is the only copy).
5. **Reports**: generated-report metadata is retained so any report can be **regenerated** later.
6. Professional, reliable, easy to set up; **do not break** collection, charts, historian, batches, reports, AI, cloud sync — all of which are currently gate-validated (`scripts/validate_release.py`).

---

## 2. How it works today (facts, with references)

All paths relative to `Trustnode_edge_app/`. Line numbers are from the working tree on 2026-08-21.

### 2.1 Write path — where a cycle becomes rows

```
PLC ──read──▶ GatewayWorker / ReaderV2
                 │
                 ├─▶ telemetry_service.record_collection_cycle()      trustnode_telemetry.db  (1 row/cycle, tags_json)       [telemetry_service.py:468-654]
                 │
                 ├─▶ app_store.append_historian_rows(rows)              trustnode_app_store.db  historian_readings (N rows)   [app_store.py:7356-7450]
                 │       V1: one txn per cycle   _flush_historian_buffer_then_write  [plc_manager.py:3918-3980]
                 │       V2: one txn / 250 ms for ALL gateways  StorageWriterV2._write_historian [collection_engine.py:285-296]
                 │
                 └─▶ GatewayWorker._persist_readings(readings)          the configured sinks     [plc_manager.py:2267-2406]
                         ├─ engine=sqlite   → trustnode_edge.db (default path)  write-through, incl. raw_payload   [plc_manager.py:3404-3480]
                         ├─ engine=postgresql (primary) → outbox trustnode_store_forward.db → flush thread  [plc_manager.py:2141-2181, 3061-3111]
                         ├─ parallel PG / csv / txt sinks → write-through                                  [plc_manager.py:2325-2383]
                         └─ customer-DB mirror (database_mode=customer_sql)                                [plc_manager.py:2407-2541]

                 (independent lane) app_store._flush_data_outbox_once: cloud mirror = "forward the local historian"
                      reads historian_readings WHERE id > data_sync_state.last_historian_id, pushes to cloud PG
                      historian_readings AND plc_readings (both!)                                           [app_store.py:3876-4090]
```

Key facts an architect must respect:

- `append_historian_rows` is the **single local writer** for both engines (V1 and V2) — any tiering hook belongs there or just behind it. It takes `_hist_lock` (not the config lock) and wakes the two cloud-sync loops on every call (`app_store.py:7448-7449`).
- The local row is **fat by design**: 15 columns, of which 7 are repeated TEXT identity columns (`gateway_id, gateway_name, device_name, plc_ip, database_name, source, quality_label`), two 23-char text timestamps, plus **5 indexes** that each copy `tenant_id` + one or two TEXT columns + `ts_utc` (`app_store.py:4200-4236, 5074-5076`). That is the 1 KB/row.
- The **cloud forwarder is coupled to `historian_readings.id`** (`data_sync_state.last_historian_id`, `app_store.py:3894, 4049-4053`). Anything that deletes raw rows the cursor has not passed drops them from the cloud forever. Today's `run_retention` deletes purely by `ts_utc < cutoff` with no cursor check.
- **No aggregation, deadband or compression exists on the write path** anywhere (grep confirmed). Every read of every tag becomes a row in three places.
- Supported sink engines that actually have writers: `postgresql`, `sqlite`, `csv_file`, `txt_file`, `legacy_http` (+ Supabase pooler fail-over inside the PG writer). `mysql`, `mssql`, `influxdb` appear in Pydantic `Literal`s only — **no writer exists** (`routers/database.py:19,40,729-732`; `customer_sql.SUPPORTED_ENGINES = ("postgresql",)`).
- The remote PG table created by the primary sink has **no index at all** (`plc_manager.py:3234-3255`) and carries `raw_payload JSONB` — every field stored twice.
- `sinks_sql.py:173-202` already creates `historian_agg_minute|hour|day` on customer databases "so a future rollup worker doesn't need a second migration" — **nothing has ever written them**.

### 2.2 Today's retention implementation

| Piece | Where | Behaviour |
|---|---|---|
| Policy storage | singleton table `retention_policy` (id = 1) | `enabled, schedule_minutes, raw/minute/hour/day_keep_days, *_keep_minutes, backup_before_cleanup, max_delete_rows_per_run` (`app_store.py:4370-4391`). **Not** a config document ⇒ not cloud-mirrored, not in workspace export. Seeded `enabled=1` (`:5099-5113`). |
| Scheduler | `_retention_scheduler_loop` (`app_store.py:5311-5341`) | sleeps 30 s after boot, then `run_retention()` every `schedule_minutes`; passive WAL checkpoint each tick; **prints nothing**. |
| The job | `run_retention` (`app_store.py:5588-5735`) | **whole body under `self._lock`** (`:5632`); optional `shutil.copy2` of the entire DB first (`:5633-5641`); 3 cascading `INSERT OR REPLACE … GROUP BY strftime(...)` rollups over windows `[minute_cutoff, raw_cutoff)`, `[hour_cutoff, minute_cutoff)`, `[day_cutoff, hour_cutoff)` (`:5645-5696`); then 4 `DELETE … LIMIT max_delete` (`:5710-5727`); sets `vacuum_recommended=True` which nothing consumes. |
| Agg tables | `historian_agg_minute/hour/day` (`app_store.py:4310-4368`) | `avg/min/max/sample_count/quality_min/max`; PK `(bucket_utc, gateway_id, tag_name, database_name)` — **no `tenant_id`**, no `value_text`, no first/last/sum. All three are **empty** on the live system. |
| Endpoints | `routers/app_store.py:683-724` | `GET/PUT /retention/policy`, `POST /retention/run`, `GET /retention/runs`, `GET/POST/DELETE /backups…`, `POST /cleanup-data`, `POST /reset/full` — **no role check** in any of them. |
| UI | `App.jsx:22646-22991` | Backup & Retention page: summary strip, "Backup Databases" (sink rows flagged `use_backup`), Snapshot Backups, Retention card (presets hour/day/week/month, 4 × keep-days, sub-day TTLs, max delete, backup-before-cleanup, Save/Dry-run/Run), runs table, Workspace export/import, Clean Data. **Save payload omits the four `*_keep_minutes` fields** (`App.jsx:16681-16690`) ⇒ every save resets sub-day TTLs to 0. |
| Backups | `create_backup/restore_backup` (`app_store.py:5483-5548`) | `shutil.copy2` of the live `.db` only (not `-wal/-shm`) under `_lock`; keeps newest 10 files of any kind; restore = `os.replace` while writers are live, no restart. `sqlite3` online backup API is not used. |

Other data classes: **nothing** prunes `telemetry_samples_raw` (acked or not), `app_logs`, alarm events (a JSON blob inside the `alarms_setup` config document), `generated_reports` files, batch tables, `power_readings`, or anything on the cloud side. The store-forward outbox prunes sent rows after 3 days, once per 10 min (`plc_manager.py:2216-2241`).

### 2.3 Defect list (what the design must fix)

| # | Defect | Evidence | Consequence |
|---|---|---|---|
| D1 | Rollup window is `[minute_cutoff, raw_cutoff)`; with `minute_keep == raw_keep` (the saved policy: 1 d / 1 d) it is empty | `app_store.py:5659`; `retention_runs.details.rollups = {0,0,0}` in all 702 runs | raw deleted without aggregation ⇒ history silently lost |
| D2 | Deletes capped at `max_delete_rows_per_run` (50 k) per hourly run, no loop-until-done | `:5604, :5716-5724`; candidates fall by exactly 50 000/hour in `retention_runs` | cannot keep up with 172 800 rows/hour ⇒ DB grows forever |
| D3 | Job runs under `AppStore._lock` for its whole duration (5–30 s+ on a multi-GB table) | `:5632`; comment `:5312-5319` | every config read / most API handlers block; `/api/health` stalls |
| D4 | First run at boot + 30 s | `:5320-5321`; `retention_runs` #705 at 07:23:22.96Z, 59 s after today's 07:22:23 bind | boot-time API stall inside the Electron 30 s health window ⇒ splash error (this morning's report) |
| D5 | No cursor safety vs. cloud forwarder (`last_historian_id`) or any target | `:5711-5714` vs `:3894` | raw rows deleted before they are forwarded are gone from the cloud |
| D6 | `backup_before_cleanup` = full-file `shutil.copy2` of an 8 GB DB, hourly, under `_lock`, `.db` only | `:5633-5641` | minutes of I/O + lock hold; inconsistent copy (WAL excluded); 10-file cap wipes safety copies |
| D7 | Agg tables lack `tenant_id`, `value_text`, `first/last/sum`; PK collides across tenants | `:4310-4368`, `:8047-8051` | cannot serve multi-tenant / text tags / exact hierarchical composition |
| D8 | No server-side role check on retention/backup/cleanup/reset routes | `routers/app_store.py:683-724` (no `Request`, no dependency) | any authenticated viewer can wipe the historian |
| D9 | UI save drops `*_keep_minutes` | `App.jsx:16681-16690` | "Last hour" preset silently lost on save |
| D10 | Triple write (app_store + local SQLite sink + telemetry.db) | §0 table | 6.6 GB/day; 3× I/O in the hot path |
| D11 | `telemetry_samples_raw` unbounded, 8.6 KB/cycle, for a disabled feature | `telemetry_service.py:45-50` (ingest off by default), no TTL anywhere | 1.9 GB and growing |
| D12 | Local row ≈ 1 KB (7 repeated text columns + 5 indexes); remote PG sink has no index and duplicates every row in `raw_payload` | §2.1 | 5–8× more disk/I/O than necessary |
| D13 | No observability: job prints nothing, no status endpoint, no disk-free anywhere (`shutil.disk_usage` unused) | agent survey | operators cannot see that retention is broken (it was broken for 2 months) |
| D14 | Reports cannot be re-rendered: `meta_json` is `{}`; no definition snapshot; relative presets re-anchor to `now()`; no regenerate endpoint | `reports_store.py:676`, `report_scheduler.py:182`, `report_renderer.py:196`, `routers/reports.py` | "regenerate any time" is impossible today |
| D15 | Alarm events live inside a config-document JSON blob with no cap | `routers/app_store.py:54` | grows unbounded and rides along every config save/mirror |

### 2.4 Read paths (what a tier-aware design must serve)

| Consumer | Endpoint / function | Today's assumption |
|---|---|---|
| Dashboard widgets (historical, table, pie, KPI) | `GET /historian/range` (`get_historian_rows_range`, `app_store.py:8125-8295`), `/historian/stats`, `POST /historian/rule-stats` | raw rows, **"newest N rows"** (`ORDER BY ts_utc DESC LIMIT n`, client `slice(-readingsCount)` in `dashboardAnalytics.js:118`); client-side bucketing with `QUERY_GROUP_OPTIONS` = none/1s/5s/10s/30s/1m/5m/15m/1h/1d (`DashboardDesigner.jsx:266-277`); `count` = raw sample count |
| Live charts | `LiveTagChart` seed + WS `/ws/stream` | raw 1 Hz; unaffected by tiers as long as the raw window ≥ seed window |
| Power Overview | `GET /historian/agg` (`get_historian_agg_rows`, `:8012-8123`) | **the only tier-aware consumer today**; falls back to raw when the tier is empty (`App.jsx:8784-8801`) |
| Historian page / export | `/historian` (600 rows), `/historian/range` (≤ 20 000), XLSX export of client rows | raw |
| Batch module | `historian_rows_for_batch` (`service_v2.py:1190-1195`, ≤ 50 000 rows), `tag_matrix` pivots on identical `ts_utc` strings, trends downsample by stride; triggers `_latest_values` full-table `ORDER BY ts_utc DESC` scan (`triggers.py:168-212`) | raw; excursion verdicts need min/max not avg |
| Reports | `report_renderer.py:209-218, 496-514` → `get_historian_rows_range(limit=readings_count)` then own bucketer (`:311-483`) | "newest N raw rows", not the full window |
| AI (Intelligence) | `get_bucketed_series` does SQL bucketing over raw (`analytics.py:102-190`), `get_tag_summary` sum/sumsq/min/max | already speaks avg/min/max/count per bucket — most tier-ready consumer |
| Alarms / limits | evaluated in the browser on the live stream (`App.jsx:7683-7790`) | **not** affected by historian tiers |
| Cloud read-only web | `prefer_cloud=true` ⇒ cloud PG `historian_readings` / `live_latest` | raw only; no cloud agg tier (`get_historian_agg_rows` never routes to PG) |

---

## 3. Industry research — how historians solve this, and what we adopt

| System | Mechanism | What we take |
|---|---|---|
| **OSIsoft/AVEVA PI** | *Exception* (ignore changes below ExcDev at the source) + *compression* (swinging-door: archive only the points needed to reconstruct the trend within CompDev). Typically 5–10× reduction with no time-bucketing. | **Optional per-tag deadband / swinging-door on the raw tier** (Phase 5, off by default). Especially valuable for BOOL/STRING tags (of 48 tags here, 23 are BOOL set-points that change rarely). |
| **Ignition Tag Historian** | Data stored in **time partitions** (tables per period), **pruning = dropping whole partitions**, **pre-processed partitions** (summary tables at coarser resolution), **Tag History Splitter** (recent data in a fast provider, old data in an archive provider). | **Partition-by-period for the raw tier** (monthly SQLite files locally, monthly `PARTITION BY RANGE` on Postgres) so pruning is O(1) and never needs VACUUM; a "splitter" is exactly our *forward rollups-only to the archive DB* mode. |
| **InfluxDB** | Retention policies per bucket + downsampling **tasks** that aggregate the high-resolution bucket into longer-retention buckets (e.g. raw 3 months → hourly 1 year → daily 5 years). | The **tier ladder** model and "aggregate task writes into a longer-retention store" semantics. |
| **TimescaleDB** | **Hierarchical continuous aggregates** (1 s → 1 min → 1 h → 1 d, coarser built from finer) + per-level retention; rule: *you may drop raw only if you never refresh the aggregate over the dropped window — make sure the aggregate exists before the raw is deleted.* Compression on both raw and aggregates. | **Hierarchical composition** (each tier computed from the next finer one using exact-composable statistics: sum/count/sumsq/min/max/first/last) and the **"materialized-before-delete" watermark invariant**. |
| **SQLite practice** | `DELETE` never shrinks the file (pages go to the freelist); `VACUUM` needs ~2× free disk and an exclusive lock; `incremental_vacuum` releases pages cheaply but needs `auto_vacuum=INCREMENTAL` set before the file grows; in WAL mode `VACUUM` must be followed by `wal_checkpoint(TRUNCATE)`. Dropping a table/file is O(1). | Short-term: bounded paced deletes + `incremental_vacuum` + checkpoint; long-term: **monthly raw partition files** so retention = delete file. |

Sources: [TimescaleDB continuous aggregates](https://www.tigerdata.com/learn/continuous-aggregates-timescaledb), [Timescale docs: data retention with continuous aggregates](https://github.com/timescale/docs/blob/latest/use-timescale/data-retention/data-retention-with-continuous-aggregates.md), [Ignition: data partitioning and pruning](https://www.docs.inductiveautomation.com/docs/7.9/historian/configuring-tag-historian/data-partitioning-and-pruning), [Ignition Tag History Splitter](https://corsosystems.com/posts/ignitions-tag-history-splitter), [InfluxDB downsample and retain](https://docs.influxdata.com/influxdb/v1/guides/downsample_and_retain/), [InfluxDB downsampling tasks](https://docs.influxdata.com/influxdb/v2/process-data/common-tasks/downsample-data/), [PI exception & compression](https://www.pisharp.com/article/359/understanding-exception-and-compression-in-pi-data-archive), [SQLite VACUUM in WAL mode](https://photostructure.com/coding/how-to-vacuum-sqlite/), [SQLite vacuum strategies benchmark](https://deepwiki.com/forwardemail/sqlite-benchmarks/4.3-vacuum-strategy-configurations).

---

## 4. Target architecture

### 4.1 Invariants (non-negotiable)

| # | Invariant | How it is enforced |
|---|---|---|
| I1 | **Never lose data that has not been rolled up, forwarded and (if configured) archived.** | Every delete is bounded by a *floor* = `min(policy cutoff, rollup watermark of every dependent tier, forward cursor of every target, last archive/backup stamp)`. |
| I2 | **Collection never blocks and never slows.** | The engine never takes `AppStore._lock`; historian DML runs in tiny paced transactions (< 50 ms) on its own connection; pacing backs off when the StorageWriter flush latency rises. |
| I3 | **Idempotent and resumable.** | Rollup rows are keyed `(target, resolution, tenant, gateway, tag, bucket)` and upserted; watermarks advance in the same transaction as the rows they cover; a crash mid-run just re-does the last chunk. |
| I4 | **One engine, one policy model, many targets.** | The same policy document drives local SQLite, LAN PG, cloud PG; per-target overrides only for *forward mode* and *maintenance window*. |
| I5 | **Admin-only, server-enforced, audited.** | `_require_admin(request)` (workspace.py pattern) on every retention/backup/cleanup/reset route + `config_audit` row per change + policy versioning. |
| I6 | **Observable.** | Status endpoint + UI storage dashboard + log lines + release-gate probe; "retention has been silently broken for 2 months" can never happen again. |
| I7 | **Readers never care which tier served them** — but they are *told*. | Query router returns `resolution` and `aggregate` with every response; UI shows a badge. |

### 4.2 Data model

#### 4.2.1 Tiers — one unified rollup table

Replace the three fixed `historian_agg_*` tables with **one** table keyed by resolution, so the policy can use any interval (5 s … 1 d) and the hierarchy composes exactly:

```sql
CREATE TABLE historian_rollup (
  resolution_s  INTEGER NOT NULL,            -- 5, 60, 300, 900, 3600, 86400 …
  tenant_id     TEXT    NOT NULL,
  gateway_id    TEXT    NOT NULL,
  tag_name      TEXT    NOT NULL,
  bucket_ms     INTEGER NOT NULL,            -- epoch ms, bucket START (UTC)
  n             INTEGER NOT NULL,            -- samples in bucket  (sum of n when composed)
  sum_v         REAL,                        -- Σ value            → avg = sum_v / n
  sumsq_v       REAL,                        -- Σ value²           → stddev exact when composed
  min_v         REAL,  max_v REAL,
  first_v       REAL,  first_ms INTEGER,     -- earliest sample in bucket
  last_v        REAL,  last_ms  INTEGER,     -- latest  sample in bucket
  last_text     TEXT,                        -- STRING tags: last value; BOOL: n/a
  changes       INTEGER NOT NULL DEFAULT 0,  -- value transitions inside the bucket (BOOL duty/edge counts, text changes)
  q_min         INTEGER, q_max INTEGER,      -- quality range
  q_bad_n       INTEGER NOT NULL DEFAULT 0,  -- samples with quality < 192
  PRIMARY KEY (resolution_s, tenant_id, gateway_id, tag_name, bucket_ms)
) WITHOUT ROWID;
CREATE INDEX ix_rollup_res_tenant_bucket ON historian_rollup(resolution_s, tenant_id, bucket_ms);
```

- **Why store all statistics** even though the UI asks for "type: average"? Because every statistic above composes exactly into the next tier (`avg` alone does not), because batch excursion checks need `min/max` not `avg`, because BOOL tags want duty cycle (`sum_v/n`) and edge counts, and because it costs ≈ 90 B/row with one index — negligible next to raw. The UI's "aggregate type" becomes the tier's **default display aggregate** (what charts plot when the widget does not say otherwise).
- **Text tags** (`value_text`, 6 % of rows here): the rollup keeps `last_text` + `changes`; additionally a compact **`historian_text_events`** table stores every *change* of a text tag (ts, tenant, gateway, tag, text) for as long as the longest tier — on-change storage is tiny and preserves the batch-ID / status strings that batch reports rely on.
- The same DDL goes into `sinks_sql._ddl_steps` (replacing `historian_agg_*`, bumping `SCHEMA_VERSION`) so LAN/cloud Postgres targets receive rollups with identical semantics (Postgres flavour: `BIGINT`, `DOUBLE PRECISION`, `PRIMARY KEY` same columns).

#### 4.2.2 Watermarks and cursors

```sql
CREATE TABLE historian_retention_state (
  target_id        TEXT NOT NULL,    -- 'local' | database_configurations.id | 'cloud-mirror'
  tier_key         TEXT NOT NULL,    -- 'raw' | 'r5' | 'r60' | 'r900' | 'text' | 'reports' | …
  tenant_id        TEXT NOT NULL DEFAULT '*',
  materialized_to_ms INTEGER,        -- rollup computed (and committed) up to here  (tiers)
  forwarded_to_ms    INTEGER,        -- rows up to here are confirmed on the target (forwarding)
  pruned_to_ms       INTEGER,        -- rows older than this are gone
  archived_to_ms     INTEGER,        -- rows older than this exist in an archive file / backup
  last_run_utc TEXT, last_status TEXT, last_error TEXT,
  PRIMARY KEY (target_id, tier_key, tenant_id)
);
```

The delete floor for tier *T* on target *X* is  
`floor(T,X) = min( cutoff(T), materialized_to_ms(next coarser tier that depends on T, X), forwarded_to_ms(raw, every target that forwards T), archived_to_ms(T,X) if "archive before prune" )`.  
The existing cloud cursor `data_sync_state.last_historian_id` is mapped to a timestamp each run and participates in the floor (until Phase 4 replaces it with a time-based cursor).

#### 4.2.3 Policy document (config domain `retention_policies`, cloud-mirrored, versioned)

```jsonc
{
  "active_policy_id": "pol-balanced-01",           // "" ⇒ NO POLICY: keep everything raw everywhere
  "policies": [
    {
      "id": "pol-balanced-01", "name": "Balanced (1 s line)", "version": 3,
      "updated_utc": "...", "updated_by": "admin-mari",
      "raw":   { "keep": "7d" },                    // raw = collection interval (1 s here); keep ≥ 1d, ≤ 5y
      "tiers": [                                    // age bands, strictly increasing keep, strictly coarser resolution
        { "keep": "30d", "resolution": "1m",  "aggregate": "avg" },
        { "keep": "1y",  "resolution": "15m", "aggregate": "avg" },
        { "keep": "5y",  "resolution": "1h",  "aggregate": "avg" }
      ],
      "text_tags":   { "keep": "5y" },              // on-change events
      "scope":       { "gateways": "all", "tag_overrides": [] },   // Phase 5: per-tag raw deadband / longer raw for critical tags
      "targets": {
        "local":         { "apply": true },
        "db-7f3a…":      { "apply": true,  "forward": "raw_and_rollups" },   // LAN PG
        "cloud-mirror":  { "apply": true,  "forward": "rollups_only", "min_resolution": "1m" }
      },
      "maintenance": { "window_local": "01:00-05:00", "catch_up_outside_window": true,
                       "max_run_minutes": 30, "pace_ms_per_batch": 20, "archive_before_prune": false },
      "other_data":  { "reports_files_keep": "2y", "reports_metadata_keep": "forever",
                       "alarm_events_keep": "2y", "app_logs_keep": "90d", "audit_keep": "5y",
                       "telemetry_cycles_keep": "7d", "outbox_sent_keep": "1d" },
      "backups":     { "config_daily_keep": 14, "historian_weekly_keep": 4, "location": "" }
    }
  ]
}
```

Validation rules (server side, Pydantic): `1d ≤ raw.keep ≤ 5y`; tiers sorted, `keep[i] > keep[i-1]`, `resolution[i] > resolution[i-1]`, each resolution an integer multiple of the previous one (exact hierarchical composition — 5 s → 1 min → 15 min → 1 h all satisfy this; 10 min → 15 min would be rejected with a clear message); `keep[last] ≤ 5y`; `raw.keep ≥ 2 × tiers[0].resolution + 1h` (late-arrival grace); resolution ∈ {5s,10s,30s,1m,5m,10m,15m,30m,1h,4h,1d}.

Backward compatibility: the existing `retention_policy` row is migrated into `pol-legacy` on first boot; its `enabled=1` maps to `active_policy_id` only if its rollup windows are sane — **the current broken row (1 d / 1 d / 1 d / 7 d) is imported as disabled** with a banner "your previous policy could not have worked (see run history); review and activate".

### 4.3 The Retention Engine (maintenance daemon)

One daemon thread `tn-retention-engine` (started where `_retention_scheduler_loop` is today, joined in `AppStore.shutdown()`), with a job scheduler and a **paced executor**:

```
every 60 s:            plan()  →  for each target X, tier T:
                          rollup jobs : materialize  [materialized_to_ms, now - resolution - grace)
                          forward jobs: push raw / rollups beyond forwarded_to_ms          (Phase 4)
                          prune jobs  : delete below floor(T,X)                             (respects window / catch-up)
                          hygiene     : incremental_vacuum, wal_checkpoint(PASSIVE), backup rotation, reports/alarm/log TTLs
```

Execution rules:

1. **Boot:** first plan no earlier than **5 min after boot** and only after `/api/health` has served successfully (`boot.first_health_served_s` exists in the health payload) — removes D4.
2. **Locks:** never `AppStore._lock`. Rollup SELECTs and DELETEs run on the engine's own SQLite connection (`busy_timeout=3000`, WAL). The writer's `_hist_lock` is taken **only** to refresh `_local_live_latest_cache` if a prune touches the newest row of a tag (it never does — raw keep ≥ 1 day).
3. **Pacing:** work is chunked — rollup by **one source-hour per statement**, deletes by **adaptive batches** (start 5 000 rows; double while the batch took < 60 ms, halve when > 150 ms, floor 1 000). Between batches the engine sleeps `pace_ms_per_batch` and checks the StorageWriter's last flush latency (`collection_engine` already tracks it; V1 exposes `persist_ms` in the cadence log) — if writer p95 > 500 ms the engine pauses 10 s. Result: a multi-million-row backlog drains in minutes-to-hours **without a measurable effect on cadence**.
4. **Windows:** prune and vacuum run inside `maintenance.window_local`; rollups and forwarding run continuously (they are cheap and keep the floor moving). `catch_up_outside_window` lets prune run any time when raw exceeds `keep + 1d` (the steady state after a long outage).
5. **Composition:** tier *k* is computed from tier *k-1* when tier *k-1* fully covers the window, else from raw. `avg = sum/n`, `stddev = sqrt(sumsq/n − avg²)`, `first/last` by `first_ms/last_ms`. Unit tests assert `rollup(raw→1h) == rollup(rollup(raw→1m)→1h)` on synthetic data.
6. **Late data:** a grace of `max(2 × collection_interval, 2 min)` before a bucket is considered closed; buckets are upserted, so a late store-and-forward replay simply re-materializes the affected buckets (the engine keeps a small "dirty bucket" queue fed by `append_historian_rows` when a row arrives with `ts < materialized_to_ms`).
7. **Disk guard:** `shutil.disk_usage(data_dir)` every plan. Thresholds: **warn** at < 15 % or projected-full < 30 d (banner + alarm event + email via the existing notifications route); **emergency** at < 5 % or < 5 GB: run prune immediately regardless of window, then shrink raw keep temporarily to `max(1d, tiers[0].resolution × 10)` for the active policy *only for the oldest raw*, and — if still critical — rotate the oldest month's raw partition to an archive file on the configured archive location. **Collection is never stopped by retention.**
8. **Logging:** `logging.getLogger("trustnode.retention")` added to the INFO allow-list in `main.py:37-42` — one line per job: `retention target=local tier=r60 materialized 2026-08-20T10:00→12:00 rows=5760 in 0.8s`, `retention prune target=local tier=raw deleted=250000 floor=... took 38s (42 batches, max 71ms)`.
9. **History:** `retention_runs` keeps one row per job with `details_json` (kept 1 y, itself pruned).

### 4.4 Local SQLite physical layout

**Phase 1 (keep the current single file, make it safe):**
- Bounded paced deletes (above) + `PRAGMA incremental_vacuum(N)` after each prune batch. Switching the existing 8 GB file to `auto_vacuum=INCREMENTAL` needs one `VACUUM`; we do it as a **"Compact database"** admin action implemented as `VACUUM INTO <tmp>` (online, no writer blocking) + atomic swap **applied by the tray at the next start** (the tray already owns pre-start DB moves in `workspace_detector.js`). Precondition: free disk ≥ 1.2 × file size. After the first rollup+prune pass of the existing data this will shrink the file from 8.1 GB to well under 1 GB.
- Index diet now: drop `idx_hist_tenant_gwname_tag_ts` and `idx_hist_tenant_tag_ts` (the router resolves `gateway_name → gateway_id` and always filters by gateway) → 5 → 3 indexes, −30 % per row, no query without an index (the `(tenant, gateway_id, tag, ts)` and `(tenant, ts)` indexes cover every read path listed in §2.4).

**Phase 5 (recommended end state): monthly raw partition files + slim rows.**

```
<data_dir>/historian/
   raw_2026-08.db        ← historian_raw(tag_id INTEGER, ts_ms INTEGER, value REAL, value_text TEXT, quality INTEGER) WITHOUT ROWID PK(tag_id, ts_ms)
   raw_2026-09.db           + tag_dim(tag_id, tenant_id, gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name, data_type, source)
   rollups.db            ← historian_rollup + historian_text_events + historian_retention_state
   archive/raw_2026-05.db ← optional, exported before prune (same format; read-only; importable)
```
- Slim raw row ≈ **40–60 B** (vs 1,000) — the identity text lives once in `tag_dim`; `ts_ms` integer; one clustered PK, no secondary index needed for per-tag range scans; `(ts_ms)` covering scans for "everything in a window" are served via the small per-month file.
- **Prune = delete the file** (O(1), no freelist, no VACUUM, no 2× disk); **archive = copy the file**; **backup granularity = month**.
- Readers attach ≤ 3 partition files for any window (the router knows the span); the legacy `historian_readings` name survives as a **compatibility VIEW** over `tag_dim ⋈ historian_raw` in the current partition, so external scripts and the Database Inspector keep working.
- Migration tool converts the existing table month-by-month into partition files while the app runs (reads are read-only snapshots; writer switches at a month boundary), validated by row counts + checksums, with the old file kept until the gate passes.

If you prefer not to take Phase 5, Phase 1's single-file design is sufficient for the numbers in Appendix A — Phase 5 is about I/O headroom (500+ tags, 100 ms intervals) and operational simplicity, not about making retention work.

### 4.5 Query router — serving tiers transparently

A new `HistorianReader` service (`services/historian_reader.py`) becomes the **only** place that decides *which table answers a read*:

```
query(tenant, gateways, tags, from, to, want = {max_points | bucket | aggregate | need_raw}) →
   1. if to-from fits in raw keep AND (need_raw OR points(raw) ≤ max_points)        → raw table (+ partitions)
   2. else pick the finest tier whose keep covers `from` and whose bucket count ≤ max_points
   3. if the picked tier is not yet materialized up to `to` (recent edge)            → stitch: tier up to materialized_to, raw after
   4. return rows + {resolution_s, aggregate, stitched: bool, tiers_used: [...]}
```

Consumer adaptation (each keeps its API shape; new optional params/fields only):

| Consumer | Change |
|---|---|
| `/historian/range`, `/historian` | new optional `resolution=auto|raw|5s|1m|…`, `aggregate=avg|min|max|last|first|count`, `max_points`. Response rows gain `resolution_s`, `n`, `value_min`, `value_max`, `value_last` (absent on raw rows). Default stays raw ⇒ **zero behaviour change** until a client opts in. |
| `/historian/agg` | becomes a thin alias of the router (`bucket=minute|hour|day` mapped to 60/3600/86400) so the Power Overview keeps working unchanged; gains `tenant_id` correctness. |
| `/historian/stats`, `/historian/rule-stats` | when the window exceeds the raw keep, computed over the tier with **sample-count-weighted** avg (`Σsum/Σn`) and `count = Σn` — semantics preserved. |
| Dashboard widgets | `fetchWidgetRows` passes `max_points = readings_count` and `resolution=auto`; `bucketAndAggregateRows` skips re-bucketing when rows already carry `resolution_s ≥ requested bucket` (avoids avg-of-avg); `mergeHistorianRowsStable` treats `resolution_s` rows as a separate series key so bucket-anchored timestamps are never dropped; gap detection uses `resolution_s` instead of `gatewayIntervalMs × 3`; a small **"1 min avg"** badge in the card footer when `resolution_s > 0`. Live charts are untouched (raw window ≥ 1 d always covers the seed). |
| Batch module | `historian_rows_for_batch`, `tag_matrix`, trends go through the router with `need_raw` when the batch window is inside raw keep (almost always for live/ recent batches); for old batches the matrix shows `min/max/avg` per bucket and the in-limits verdict uses `min/max` (so excursions are **not** masked). `triggers._latest_values` stops scanning the table and reads the live-latest cache (`/api/app-store/live` source) — a correctness fix independent of tiers. |
| Reports | `report_renderer` asks for `max_points = readings_count` over the **full** section window (fixes today's "newest N raw rows" surprise as well); section footers print "data: raw" or "data: 15-min averages". |
| AI tools | `get_bucketed_series` / `get_tag_summary` use the router (they already speak n/avg/min/max). |
| Cloud read-only | the same router on the hosted side reads `historian_rollup` on PG (Phase 4) — the cloud finally gets an agg tier. |

### 4.6 Forwarding to network and cloud databases

Every target in `database_configurations` (+ the Supabase cloud mirror) gets a **forward mode** in the policy:

| Mode | What is sent | When to use | Bandwidth @ 48 tags / 1 Hz |
|---|---|---|---|
| `raw` (today) | every reading, write-through or via outbox | LAN historian DB that is the customer's system of record | ≈ 4.1 M rows/day ≈ 600 MB/day |
| `raw_and_rollups` | raw + every tier row the engine materializes | LAN DB that should keep long history cheaply; lets the remote apply the same policy | + ≈ 10 MB/day |
| `rollups_only` (`min_resolution`) | only tiers ≥ `min_resolution` (+ text events) | **cloud archive / portal** where raw 1 s is cost without value | ≈ 69 k rows/day ≈ **10 MB/day** |

Design points:
- **Rollups are computed once, at the edge, and forwarded** (single source of truth, identical numbers everywhere). Forward cursor per `(target, tier)` in `historian_retention_state.forwarded_to_ms`; upsert `ON CONFLICT DO UPDATE` on the PK ⇒ idempotent replay after outages.
- **Remote retention is executed by the same engine** through SQLAlchemy (`customer_sql.get_engine` already enforces `statement_timeout=8000`): rollup rows are not recomputed remotely; prune runs paced `DELETE … WHERE ctid IN (SELECT ctid … LIMIT 5000)` batches inside the target's maintenance window. `apply: false` leaves a customer-owned DB untouched (they may run their own policies).
- **Postgres layout for new targets**: `historian_readings PARTITION BY RANGE (ts_utc)` with monthly partitions created 2 months ahead by the engine; prune = `DROP TABLE historian_readings_2026_05` (O(1)). Existing unpartitioned tables keep working with batched deletes. If the `timescaledb` extension is present, the engine can optionally convert to a hypertable and use native `add_retention_policy` (detected, never required).
- **Cursor safety (I1)**: raw prune floor ≤ min over all targets with `forward ∈ {raw, raw_and_rollups}` of `forwarded_to_ms(raw)`. If a target is offline longer than raw keep, the policy's `offline_behaviour` decides: `hold_raw` (default; bounded by the disk guard, which escalates to a warning) or `skip_raw_forward_rollups` (the target receives rollups for that period and a gap marker row).
- The store-and-forward outbox loses `raw_payload` (half its size) and its sent rows are pruned after `outbox_sent_keep` (1 d) instead of 3 d.
- The **"Local SQLite" sink duplicate** (`trustnode_edge.db`) is retired: when a sink is `engine=sqlite` at the default path, `_persist_readings` treats it as "already in the local historian" (no second write). An explicit other path (USB/NAS) stays supported and is treated as a target like any other.

### 4.7 When no external database is configured

The local SQLite **is** the system of record. The design makes that explicit:

- Backup & Retention page shows a **"Local only — this is the only copy of your data"** state with: current size, rows, oldest raw, projected growth, days-until-disk-full, and one-click "Apply Balanced policy" / "Configure a backup location".
- Defaults in this mode: config backup daily (tiny), historian **archive-before-prune ON by default** to a user-chosen folder (NAS/USB) if one is configured, else OFF with a warning; disk guard thresholds as in §4.3.
- **Archive export/import**: "Export month…" writes a self-contained partition file (`VACUUM INTO` in single-file mode, file copy in partition mode); "Open archive…" attaches it read-only so the Historian page / reports can query it. This is how a site without a server keeps 5 years of raw if it wants to — on its own storage — while the live DB stays small.
- If later a LAN/cloud DB is added, the engine **back-fills** rollups (and raw for the remaining raw window) to the new target from local data — the forward cursors simply start at the oldest available data.

### 4.8 Backups (replacing `shutil.copy2`)

| Class | Content | Method | Schedule / rotation (defaults) |
|---|---|---|---|
| **Config** | `config_documents*`, `retention_policies`, auth DB, batch definitions, report templates, alarms setup | `sqlite3.Connection.backup()` (online, consistent, WAL-safe) of app_store with historian tables excluded via a `VACUUM INTO`-of-a-filtered-copy, + auth DB copy; zipped; ≈ MBs | daily 02:30, keep 14 daily + 8 weekly |
| **Historian snapshot** | rollups.db + current raw partition(s) | file copy of closed partitions + `backup()` of the open one | weekly, keep 4 (optional) |
| **Archive** | a closed raw month | copy (partition mode) / `VACUUM INTO` (single-file) | on prune, if `archive_before_prune` |
| **Pre-restore safety** | whatever is about to be replaced | `backup()` | kept **outside** rotation for 30 d |

Restore is **staged**: the file lands as `*.restore_pending`, the tray applies it at next start (handles `-wal/-shm`, takes the safety copy, verifies `PRAGMA quick_check` before swapping). Backups carry a manifest (`schema_version`, app version, checksums). The Backup page lists backups by class with download through a **backend endpoint** (today's `file:///` anchor does not work from the hosted UI).

### 4.9 Reports metadata and regeneration

- `generated_reports.meta_json` gains a **generation snapshot**: `{ "definition": <template definition at render time>, "sections": [{ "id", "from_utc", "to_utc", "gateway", "tags", "resolution_s", "aggregate" }], "tenant_id", "actor", "renderer_version", "batch_id"?, "batch_group_id"? }`. Batch reports store the **concretized** template (the one `reports_v2._concretize_batch` builds in memory today), so the static KPI/pass-fail tables are reproducible.
- New `POST /api/reports/generated/{id}/regenerate` (admin or report owner): re-renders from the snapshot with the **absolute** windows (never re-anchored to `now()`), through the router; the new PDF is stamped *"Regenerated 2026-… from 15-min averages (original: raw)"* when the resolution differs; the record keeps a `regenerations[]` list (old file pruned per policy, metadata kept).
- Retention for reports: **metadata kept forever** (toggle in policy `reports_metadata_keep`), **files** pruned after `reports_files_keep`; pruned rows show a "Regenerate" button instead of "Download". Same for `batch_report_reference`.

### 4.10 Other data classes (defaults, all in the policy's `other_data`)

| Data | Today | Proposed |
|---|---|---|
| `telemetry_samples_raw` (telemetry.db) | unbounded, 8.6 KB/cycle, ingest off | **do not write cycle rows when `ingest_enabled` is False** (keep `latest_machine_state`/`gateway_runtime_state` only); otherwise TTL `telemetry_cycles_keep` (7 d) |
| Alarm events | JSON blob in `alarms_setup` config doc | move to an `alarm_events` table (tenant, ts, rule, tag, value, state, ack…) with TTL `alarm_events_keep` (2 y); config doc keeps only the setup |
| `app_logs` | unbounded | TTL 90 d |
| `config_audit`, `cp_security_audit_log`, `batch_audit_log` | unbounded (small) | TTL 5 y (compliance) |
| Store-forward outbox sent rows | 3 d | 1 d, no `raw_payload` |
| `power_readings` | outside every retention path | same tiering as historian (router + engine treat `source=power_*` rows identically) |
| Batch tables | tiny, keep | keep; batch *historian* access goes through the router (old batches read tiers) |

### 4.11 Security / RBAC / licensing

- Add `_require_admin(request)` (the `routers/workspace.py:42` helper, moved to a shared `app/deps.py`) to **every** retention, backup, cleanup, reset, compact, archive and regenerate route; viewers/operators get read-only `GET …/status`. Audit each mutation with the `control_plane._audit` pattern.
- Policy document is a **shared-edge domain** (`_SHARED_EDGE_DOMAINS`) so it mirrors to the portal with the rest of the configuration and appears in workspace export.
- Licensing (optional, decision #5): tiers beyond 1 year or forwarding modes could be a `retention_pro` module using the 404 pattern of `modules/batch_management/license.py`; the base policy (raw + 2 tiers, 1 y) should be in every license — retention is a reliability feature, not an upsell.

### 4.12 Observability and the release gate

- `GET /api/app-store/retention/status` → per target and tier: `materialized_to`, `forwarded_to`, `pruned_to`, lag seconds, rows, bytes, oldest raw, last/next run, disk free, projected days-to-full, active policy version, engine pace state. Polled by the UI storage dashboard and by the gate.
- Gate additions (`scripts/validate_full_12h.py`, new `retention_task` + `[RETENTION]` section + SLO lines): `rollup lag < 2 × resolution + 10 min` for every configured tier; `oldest raw ≤ raw.keep + 1 d`; `app_store.db growth per hour within ±20 % of the policy's forecast`; writer cadence SLOs unchanged **while a prune is running** (the gate triggers a paced prune on purpose); no `[trustnode][lock-watchdog]` dumps; `first retention job ≥ 5 min after boot`. Plus the boot assertion already planned for the splash issue (`/api/health` 200 within 10 s of spawn).
- Unit tests: composition exactness, idempotent re-run, floor computation with offline targets, policy validation, migration of the legacy row, adaptive batch sizing under injected latency.

---

## 5. UI design — Backup & Retention page (admin only)

```
┌ Backup & Retention ───────────────────────────────────────────────────────────────┐
│ ① STORAGE STATUS                                                                   │
│   Local historian  0.9 GB ▂▃▅ (−7.2 GB after compaction)   Oldest raw: 7 d 02 h      │
│   Raw 7d ● 1-min 30d ● 15-min 1y ● 1-h 5y      Rollup lag 48 s   Disk free 133 GB   │
│   Forecast: steady state 4.4 GB · days-to-full: ∞     [Run maintenance now] [Dry run]│
├───────────────────────────────────────────────────────────────────────────────────┤
│ ② RETENTION POLICY                                                                  │
│   Active policy: [ Balanced (1 s line)  ▾ ]  (None = keep everything raw)            │
│   [New policy…] [Edit] [Duplicate] [Delete]            Last change: admin-mari, v3   │
│   ⚠ No policy active: at the current rate this PC stores 6.6 GB/day (≈ 2.4 TB/year).│
├───────────────────────────────────────────────────────────────────────────────────┤
│ ③ TARGETS (where data goes, and which policy applies)                              │
│   Local SQLite           system of record      policy ✔   —                         │
│   PG "Plant historian"   raw + rollups         policy ✔   lag 0 s   window 01-05    │
│   Cloud mirror (Supabase) rollups ≥ 1 min      policy ✔   lag 12 s                  │
├───────────────────────────────────────────────────────────────────────────────────┤
│ ④ BACKUPS & ARCHIVES     config daily ✔ 14 kept · historian weekly ✔ 4 kept         │
│   location: \\nas\trustnode\backups   [Back up now] [Restore…] [Export month…]      │
├───────────────────────────────────────────────────────────────────────────────────┤
│ ⑤ REPORTS & EVENTS       report files 2 y · metadata forever ✔ · alarms 2 y · logs 90 d│
│ ⑥ MAINTENANCE HISTORY    table: time · job · target · tier · rows · duration · status │
└───────────────────────────────────────────────────────────────────────────────────┘
```

**Policy editor (modal, `.modal-card-wide`)** — the user's mental model, one row per age band:

```
Policy name  [ Balanced (1 s line) ]          Collection interval: 1 s (from gateways)

  Age band        Resolution            Aggregate shown     Estimated size (48 tags)
  ≤ [ 7  ][days ▾]  [ raw          ▾]    —                   3.9 GB
  ≤ [ 30 ][days ▾]  [ 1 minute     ▾]    [ Average ▾]        186 MB
  ≤ [ 1  ][years▾]  [ 15 minutes   ▾]    [ Average ▾]        150 MB
  ≤ [ 5  ][years▾]  [ 1 hour       ▾]    [ Average ▾]        190 MB
  [+ add band]                                         Steady-state total ≈ 4.4 GB

  Text tags: keep changes for [ 5 ][years ▾]
  Maintenance window [01:00]–[05:00] local   ☑ catch up outside window if behind
  ☑ Archive raw month to  [ \\nas\trustnode\archive ] before deleting
  Apply to:  ☑ Local  ☑ PG "Plant historian" (forward: [raw + rollups ▾])  ☑ Cloud (forward: [rollups ≥ 1 min ▾])
  Presets: [Minimal 1y] [Balanced 5y] [Keep raw 30d]                       [Cancel] [Save & activate]
```

Validation messages appear inline (e.g. *"15 minutes must be a multiple of 1 minute — OK"*, *"10 minutes is not a multiple of 15 minutes"*). The estimate uses the formula in Appendix A with the live tag count and interval. Destructive actions ("Run maintenance now" when > 1 M rows would be pruned, "Restore", "Compact") use the existing `withConfirm()` dialog. All controls `disabled={!canEditPage("backup_and_retention")}` plus the server-side gate.

---

## 6. API surface (new / changed)

| Method & path | Purpose | Role |
|---|---|---|
| `GET /api/app-store/retention/policies` · `PUT …/policies` · `POST …/policies/{id}/activate` | policy CRUD (config domain), validation errors as 422 with field paths | admin |
| `GET /api/app-store/retention/status` | watermarks, lags, sizes, disk, forecast, engine state | any authenticated |
| `POST /api/app-store/retention/run` `{dry_run, targets?, jobs?}` | plan + execute now (paced) | admin |
| `GET /api/app-store/retention/runs` | job history (exists; richer rows) | any |
| `POST /api/app-store/retention/estimate` `{policy}` | server-side storage estimate for the editor | admin |
| `POST /api/app-store/maintenance/compact` · `GET …/compact/status` | `VACUUM INTO` + staged swap | admin |
| `POST /api/app-store/archives/export` `{month, destination}` · `GET /archives` · `POST /archives/{id}/attach` | archive files | admin |
| `GET/POST/DELETE /api/app-store/backups…` (exist) + `GET /backups/{id}/download` + `POST /backups/schedule` | online backups by class, download through backend, staged restore | admin |
| `GET /api/app-store/historian/range?resolution=&aggregate=&max_points=` (exists, new params) | router-served reads | any |
| `POST /api/reports/generated/{id}/regenerate` | re-render from snapshot | admin / owner |

All existing retention/backup/cleanup/reset routes gain `_require_admin`.

---

## 7. Rollout plan (each phase: feature-flag, gate PASS, rollback = flag off)

| Phase | Scope | Acceptance (gate) | Risk / rollback |
|---|---|---|---|
| **0 — Stop the bleeding** (1–2 days) | Server-side admin gate on all routes (D8). Fix UI save (D9). Fix rollup window: materialize everything older than `now − grace` that is not yet materialized **before** any delete, using a watermark (D1). Loop-until-caught-up paced deletes off `_lock` (D2, D3). First run ≥ 5 min after boot + after first health (D4). Floor vs `last_historian_id` (D5). Replace `copy2` backup with `sqlite3.backup()` and default it OFF (D6). TTL for `telemetry_samples_raw` / skip when ingest disabled (D11). Status line in logs + `GET retention/status` v0 (D13). Retire the duplicate local SQLite sink write (D10). | gate PASS with a forced prune of the existing backlog running during the 10-min window; boot splash assertion PASS; DB size decreasing. | Pure bug fixes; rollback = previous build. |
| **1 — Engine + unified tiers** (1–2 weeks) | `historian_rollup` (+ text events, + state table), hierarchical composition, adaptive pacing, windows, disk guard, policy document + validation + migration of the legacy row, `retention/status` v1. Old `historian_agg_*` kept read-compatible until Phase 2. | composition unit tests; 12 h soak with FakeLogixDriver @ 500 tags/1 s: cadence SLOs unchanged while engine runs; lag SLO met. | Flag `TRUSTNODE_RETENTION_V2`; off ⇒ Phase 0 behaviour. |
| **2 — Query router + consumers** (1–2 weeks) | `HistorianReader`; new params on range/stats/rule-stats; dashboard/batch/report/AI adaptations; resolution badge; `/historian/agg` aliased; reports snapshot + regenerate endpoint (D14). | widget-by-widget screenshot checks raw vs tier windows; batch excursion tests on tier data; regenerate round-trip. | Default `resolution=raw` until the client opts in; per-consumer flags. |
| **3 — New Backup & Retention UI** (1–2 weeks) | Storage status, policy selector + editor modal with estimator, targets, backups by class with schedule + download + staged restore, reports & events TTLs, maintenance history. Alarm events table (D15). | UX review; admin/viewer role tests; estimator within ±20 % of measured. | UI behind the same flag; old card remains until removal. |
| **4 — Forwarding modes + remote retention** (1–2 weeks) | `forward` modes, rollup forwarding with cursors, remote prune via engine, PG monthly partitions for new targets, optional Timescale, outbox without `raw_payload`, cloud agg tier read path. | 12 h soak with a LAN PG + Supabase target: zero raw loss across a simulated 2-h WAN outage; cloud receives rollups only when configured. | Per-target `apply=false` fallback = today's behaviour. |
| **5 — Physical storage redesign** (2 weeks, optional) | Slim raw schema + `tag_dim`, monthly partition files, compatibility view, online migrator, optional per-tag deadband / swinging-door, one-time compaction of the current 8 GB. | migration checksum = 100 %; bytes/reading ≤ 80; prune of a month < 1 s; gate PASS. | Keep single-file mode as a supported layout; migrator reversible until cut-over. |

**"Do not break what works" safeguards across all phases:** no change to `append_historian_rows`' signature or transaction shape; the engine is the only new writer and it only ever *adds* rollup rows or *deletes below a floor*; every delete is preceded by a dry-run count in the same job and logged; the release gate runs a prune on purpose and asserts cadence/chart-feed/freshness SLOs during it; feature flags per phase; the legacy policy row is imported disabled.

---

## 8. Decisions I need from you

1. **Default for new installs** — per your spec, *no policy* (raw everywhere) with the forecast banner and a one-click "Balanced" preset. Alternative: ship Balanced active. *Recommendation: your spec, plus a first-run prompt.*
2. **Store all statistics per bucket** (avg/min/max/first/last/n/sum/sumsq/changes) with "aggregate type" as the display default — or only the chosen aggregate? *Recommendation: all (≈ 90 B/row, enables batch excursions, BOOL duty cycles, exact composition).*
3. **Retire the duplicate "Local SQLite" sink write** (`trustnode_edge.db`) — *Recommendation: yes, Phase 0.*
4. **Rollups computed at the edge and forwarded** (identical numbers everywhere) vs. recomputed on each target — *Recommendation: edge-computed.*
5. **Phase 5 (monthly partition files + slim rows)** — go, or stay single-file with incremental vacuum? *Recommendation: go, after Phases 0–4 are green; it is what makes 500-tag / sub-second sites comfortable.*
6. **Licensing**: base retention in every license; make "forwarding modes + > 1 y tiers" a module? *Recommendation: keep it all in the base for now.*

---

## Appendix A — Storage math (used by the UI estimator)

```
rows_per_day(raw)      = tags × 86400 / interval_s
rows_per_day(tier r)   = tags × 86400 / r
bytes(raw row)         = 1000 today  │ ≈ 330 after Phase 0/1 index diet  │ ≈ 50 after Phase 5
bytes(rollup row)      = 90 (all statistics, one index)
bytes(text event)      = 80 × changes/day (negligible)
size(policy)           = raw.keep_days × rows_per_day(raw) × bytes(raw)
                       + Σ_tiers keep_days(tier) × rows_per_day(tier) × 90
```

This site (48 tags, 1 s):

| Scenario | Raw | 1-min / 30 d | 15-min / 1 y | 1-h / 5 y | **Total** |
|---|---|---|---|---|---|
| Today, no working retention, 3 copies | 6.6 GB/**day** | — | — | — | **≈ 2.4 TB / year** |
| No policy, one copy, slim rows (Phase 5) | 0.21 GB/day | — | — | — | ≈ 380 GB for 5 y |
| Balanced, Phase 1 rows (330 B) | 7 d → 9.6 GB | 186 MB | 150 MB | 190 MB | **≈ 10 GB** |
| Balanced, Phase 5 rows (50 B) | 7 d → 1.45 GB | 186 MB | 150 MB | 190 MB | **≈ 2 GB** |
| Same, 500 tags | ×10.4 | | | | ≈ 21 GB (Phase 5) / 104 GB (Phase 1) |

(The §0 headline "≈ 4.4 GB" uses an intermediate 130 B/row for raw after the index diet plus dropping the repeated text columns — Phase 1 with the optional column-slimming pulled forward.)

## Appendix B — Reference map (files the implementation touches)

| Area | Files |
|---|---|
| Engine, tiers, state, policy | `backend/app/services/app_store.py` (`_retention_scheduler_loop` 5311, `run_retention` 5588, schema 4200-4400, `append_historian_rows` 7356, cloud forwarder 3876-4090), new `services/retention_engine.py`, `services/historian_reader.py`, `services/retention_policy.py` |
| Routes | `backend/app/routers/app_store.py` (683-724 retention/backups/cleanup; 393-651 historian reads), new `routers/retention.py`, `routers/reports.py` (regenerate), shared `app/deps.py` (`_require_admin`) |
| Collection hot path | `plc_manager.py` `_persist_readings` 2267-2406 (local-sink de-dup, outbox `raw_payload` 2166), `collection_engine.py` (writer latency signal), `telemetry_service.py:468-654` (skip cycle rows when ingest disabled) |
| Remote schema / sinks | `services/sinks_sql.py` (`_ddl_steps` 64-309 → `historian_rollup`), `customer_sql.py`, `plc_manager._persist_postgresql` 3171-3402 (partitioned DDL, drop `raw_payload`) |
| Reports | `services/reports_store.py:676` (meta), `report_scheduler.py:171-183`, `report_renderer.py:176-218` (absolute windows, router), `modules/batch_management/reports_v2.py:319-374, 753-797` |
| Batch | `modules/batch_management/service_v2.py:1190-1284`, `triggers.py:168-212` |
| AI | `trustnode_intelligence/backend/tools/analytics.py:102-190`, `tag_summary.py` |
| Frontend | `App.jsx:22646-22991` (page), `:16675-16843` (handlers), `:20662-20750` (fetch adapters), `:1098-1128` (merge guard), `api.js:1057-1250, 1382-1452`, `components/Dashboard/DashboardWidgets.jsx` (184-275 bucketing, 2617-2793 series fetch), `dashboardAnalytics.js:109-126` |
| Desktop | `desktop/workspace_detector.js` (staged restore / compaction swap), `desktop/main.js` |
| Gate | `scripts/validate_full_12h.py` (new `retention_task`, `[RETENTION]` section, SLO lines), `scripts/validate_release.py` |
