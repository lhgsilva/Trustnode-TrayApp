# Reading and storing data — what is slow, measured on the live install

**Date:** 2026-08-28
**Question asked:** *"how to optimise the data reading and logged to the database — it should be faster and reliable, in a scenario like this where we have multiple power meters, multiple PLCs and other blocks; we should see real data in the historian and tags, charts updating in real time as configured in the gateways, and database writing fast and reliable."*

This continues [`collection-engine-research-and-plan.md`](collection-engine-research-and-plan.md) (2026-07-25), which established that PLC I/O is ~1% of a cycle and the storage side is the ceiling. That still holds. Everything below is measured on the **current** install today — with a benchmark and query plans, not re-derived from that document.

> Where a measurement contradicted the hypothesis that led to it, the measurement is what is written down. Two of the four sections below say something different from what the investigation expected.

---

## 0. The install being measured

| | |
|---|---|
| Gateways collecting | 3 — Allen-Bradley PLC (49 tags), ifm block over EtherNet/IP (16 tags), EM1 power meter (87 tags) |
| Total tags | **152, all at 1 Hz** |
| Engine | **V2** (`TRUSTNODE_ENGINE_V2=1`) — reader → queue → batched writer |
| Historian store | **13.4 GB** holding **4.7 days** (23 Aug 19:45 → 28 Aug 13:16) ≈ **2.85 GB/day** |
| Indexes on `historian_readings` | **five** |
| Disk / machine | 83% full · 8 logical cores · 34 GB RAM |

Freshness right now is good: all three gateways under 1 s old, one canonical timestamp format, GOOD quality. **The system works.** What follows is why it works harder than it needs to.

---

## 1. Writing — index amplification dominates, and it is not close

### What the log had been saying, 348 times

```
WARNING trustnode.engine_v2: v2-writer slow HISTORIAN flush:
        1 cycle(s) in 2953 ms (local DB lagging)
        ... 5250 ms ... 5391 ms ... 9015 ms ...
```

Roughly once a minute. Nothing failed; the historian just wrote slowly, for as long as anyone had been looking.

### Measured

`scripts/bench_historian_write.py` builds a store with the **same schema and all five indexes**, grows it to 4 M rows (1.8 GB), then appends at the live shape — 152 rows, one commit per cycle — under each configuration. The first row reproduces what the app does today, including opening a **fresh connection per flush**.

| Configuration | median | p95 | rows/s | vs today |
|---|---|---|---|---|
| **AS SHIPPED** — new connection per flush, 2 MB cache, FULL | 85.9 ms | 176 ms | 1 769 | 1.0× |
| + `synchronous=NORMAL` (what the source already intends) | 75.4 ms | 340 ms | 2 015 | 1.1× |
| + 128 MB page cache, still reconnecting per flush | 67.0 ms | 140 ms | 2 270 | 1.3× |
| + keep the connection open between flushes | 54.7 ms | 109 ms | 2 780 | 1.6× |
| **+ drop the gateway-*name* index** | **8.6 ms** | 81 ms | 17 671 | **10.0×** |
| + keep only two indexes | 4.4 ms | 63 ms | 34 727 | 19.6× |

**One index is worth more than every other change combined.** Dropping a single index took the median from 54.7 ms to 8.6 ms — a 6.4× step, against 1.6× for all the connection and pragma work together. Each inserted row updates five B-trees; at 13.4 GB those pages are not cached, so the cost is five random page reads per row, every row, every second.

### Why there is a pragma bug underneath it anyway

`app_store.py` bootstraps the schema with `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;` inside one `executescript`. **`journal_mode` is stored in the file and persists; `synchronous` and `cache_size` belong to a connection and do not.** Read off the live store:

```
journal_mode  wal        <- persisted, as intended
synchronous   2 (FULL)   <- the source says NORMAL; it has never been NORMAL
cache_size    -2000      <- 2 MB, never set at all
```

Demonstrated directly:

```
same connection      : journal=wal synchronous=1 cache=-2000
a NEW connection     : journal=wal synchronous=2 cache=-2000
```

Real, worth fixing, and now fixed — but on the evidence it is the 1.3× lever, not the 10× one. It is written down second because that is where the measurement put it.

### The gateway-name index is used by nothing

Before recommending a drop, every historian read path was checked with `EXPLAIN QUERY PLAN` against the live store:

| Query | Index chosen |
|---|---|
| `gateway_id = 'gw-…'` | `idx_hist_tenant_gw_ts` |
| `gateway_id = ? AND tag_name = ?` | `idx_hist_tenant_gwid_tag_ts` |
| the `COALESCE(gateway_name…) OR …` form | `idx_hist_tenant_ts` |
| — | **`idx_hist_tenant_gwname_tag_ts`: never chosen** |

It exists to serve gateway-**name** lookups, but the only query that would want it is written as a `COALESCE(...)`/`OR` pair, which SQLite cannot answer from an index. It costs a random page write on every row inserted and returns nothing.

---

## 2. Distributing — a cloud sink sits inside the collection cycle

The `Cloud Supabase` connection is saved with **`use_gateway=True`**, making it a *parallel sink* of every gateway. `_persist_readings` therefore performs two synchronous WAN round-trips — `write_historian_batch` and `upsert_live_latest` — on the shared distribution thread, every cycle. The log gives the split exactly:

```
WARNING trustnode.engine_v2: v2-dist slow cycle gateway=gw-1781903248499
        total=2828ms (bootstrap=0 tel=31 sinks=2797) backlog=1
        total=2718ms (bootstrap=0 tel=15 sinks=2703) backlog=0
```

**99% of a slow distribution cycle is `sinks`** — the WAN write. This is the failure documented on 2026-08-22, which produced a 5.6-hour distribution wedge; the deadline guard added then prevents the wedge, but the latency remains in the path every second. It is also **duplicated work**: the cloud sync worker already forwards the historian (19.5 M rows pushed).

---

## 3. Reading — the SQL is not the problem

The expectation going in was that meter charts were slow because their query could not use an index. Half of that was true. Measured against the live 13.4 GB store, best of three, 5 000 rows:

| Query | Plan | Time |
|---|---|---|
| meter chart, `COALESCE`/`OR` form | `idx_hist_tenant_ts` (tenant only) | 15.4 ms |
| meter chart, `gateway_id =` | `idx_hist_tenant_gw_ts` | **9.5 ms** |
| meter series, `OR` + tag | `idx_hist_tenant_tag_ts` | 54.2 ms |
| meter series, id + tag | `idx_hist_tenant_gwid_tag_ts` | **43.3 ms** |

The plan really was wrong — the filter branched on a `"gw-"` prefix, assuming only those are ids, and a power meter's id is its name (`EM1`), so every meter chart took the fallback branch and walked the tenant timeline. That is fixed (§4). **But the gain is ~1.6×, not the order of magnitude expected**, because EM1 is 87 of 152 tags: even a scan hits its rows 57% of the time.

**So slow charts are not caused by slow SQL.** At 10–50 ms per query the database is not what a user waits for. The cost is the **volume of rows shipped and parsed** — a widget requests up to 8 000 raw rows, and a dashboard has many widgets. The fix for that is §5.4, not more indexes.

---

## 4. What was changed today

| Change | Measured effect | Risk |
|---|---|---|
| `cache_size` applied per connection (128 MB, `TRUSTNODE_SQLITE_CACHE_KB`) | part of the 1.3× | none — cache has no durability meaning |
| `synchronous` applied per connection, **default left at FULL** | makes the setting real | none by default; see §5.2 |
| Gateway filter always tries the indexed `gateway_id =` first, with a name fallback | 1.6× on meter reads, correct plan for every gateway | fallback preserves name lookups |
| Per-register collection ticks for power meters | fewer rows, fewer and shorter Modbus block reads | absent flag = enabled; existing meters unchanged |
| Per-signal collection ticks for EtherNet/IP | same, for ifm blocks and any EDS device | absent flag = enabled |
| **Dropped `idx_hist_tenant_gwname_tag_ts`** — no longer created, and removed on boot (idempotent, alongside the four dropped in 2026-05-18) | **6.4×** on the write path | no plan selects it; DROP measured at 0.2 s on a 3M-row index with a concurrent writer seeing a 239 ms worst commit and zero errors |
| **`get_bootstrap_scoped` reads without the global write mutex** | removes a **6 s p95** from `/api/plc/gateways/status` (p50 was 59 ms) | every statement in it is a SELECT; the new test asserts a saved document reads back field-for-field |
| Diagnostics page, including a banner when no retention policy is active | makes all of this visible without reading a log | read-only |

Guarded by `scripts/test_sqlite_write_pragmas.py` (what a *fresh* connection reports) and `scripts/test_collect_selection.py` (the selection rules on both paths).

---

## 5. What to do next, in measured value order

> Before any of these: **§9 — no retention policy is configured and the disk
> has about 30 days of headroom.** These make the system faster; §9 is what
> keeps it running at all.

### 5.1 ~~Drop `idx_hist_tenant_gwname_tag_ts`~~ — **DONE**
No query plan selects it (§1); removing it cut the median flush from 54.7 ms to 8.6 ms. Now removed from schema creation and dropped on boot, idempotently, next to the four retired in 2026-05-18. Re-run the `EXPLAIN QUERY PLAN` table above after upgrading to confirm nothing moved to a worse plan.

### 5.2 Decide on `synchronous` — operator's call, one line
`NORMAL` in WAL mode **cannot corrupt the database**; a power cut can lose only the most recent commits, which the store-and-forward outbox re-sends. `FULL` fsyncs on every commit. The standing rule here is that data cannot be lost, so the default was **left at FULL** rather than changed as a side effect of a performance fix. To take it: `TRUSTNODE_SQLITE_SYNCHRONOUS=NORMAL` in `%LOCALAPPDATA%\TrustNode\.env`. Worth only ~1.1× on its own — do §5.1 first.

### 5.3 Clear "feed from gateways" on the Supabase connection — no code
Removes 2.2–2.8 s of WAN latency from every collection cycle (§2). The cloud still receives everything through the sync worker. A configuration change on the Database Connections page.

### 5.4 Populate `historian_agg_minute` — the real fix for chart speed
It is **empty** on this install, so long-range charts fall back to live SQL bucketing over raw rows. A populated rollup turns a 24-hour chart into 1 440 rows instead of ~130 000 — which attacks the volume that §3 identifies as the actual cost. Tracked in `historian-retention-and-forwarding-architecture-2026-08-21.md`.

### 5.5 Keep the historian write connection open — 1.6×, but its own build
`append_historian_rows` opens and closes a connection per flush, discarding its page cache each time. A single long-lived handle under the existing `_hist_lock` keeps the hot index pages resident. The lock already serialises these appends so the change is contained — but it is the busiest path in the product and deserves its own build and its own gate run rather than riding along with everything else.

---

## 6. Volume is the lever nothing else substitutes for

152 tags at 1 Hz is **13.1 million rows a day**. The EM1 meter alone is 87 of those 152 — its entire register map — because until today nothing offered a way to say *"poll these twelve, not all eighty-seven"*.

At ~1 KB/row across the three copies:

| Tags @ 1 Hz | Rows/day | Growth/day | 30-day store |
|---|---|---|---|
| 152 (today) | 13.1 M | 2.85 GB | 85 GB |
| 152, half unticked | 6.6 M | 1.4 GB | 42 GB |
| 500 | 43 M | 9.4 GB | 280 GB |
| 1 000 | 86 M | 18.8 GB | 560 GB |

The disk is already 83% full. The cheapest row to write is the one never collected: a register nobody charts costs a poll, a row, five index updates, a WAL page, an outbox entry and a cloud push — every second, for ever. Per-tag selection (§4) plus rollups and retention (§5.4) are what make the 500–1 000 tag rows survivable; the write-path fixes are what make them *fast*. Neither substitutes for the other.

---

## 7. Two things that are *not* the problem

* **The ifm block.** All 32 106 `ifm gateway … value(s) failed` warnings fall between 27 Aug 23:49 and 28 Aug 08:45 and belong to the **old IoT-Core gateway** (`gw-1787852825837`). That block's IoT Core genuinely cannot serve 19 values at 1 Hz, and the driver said so, naming the fix. The gateway in use now reads the same pins over EtherNet/IP in **one** CIP request. The advice worked; the warnings are historic.
* **The PLC read itself.** Unchanged from July: ~10 ms of a cycle. Network and devices have enormous headroom.

---

## 8. How to re-measure

```
python scripts/bench_historian_write.py --rows 4000000 --cycles 100
```
Builds its own store in a temp directory — never touches the live database — and times the live write shape under each configuration. Run it on a quiet machine: the first run of the day was taken while the app was collecting to the same disk and the `max` column was dominated by that contention.

```
python scripts/test_sqlite_write_pragmas.py
python scripts/test_collect_selection.py
python scripts/smoke_ifm_end_to_end.py
```

Live and read-only: **Settings → Diagnostics** now shows machine CPU / memory / network, TrustNode's share of each, and per-gateway historian-commit stamps with distribution stage — the same figures this document was written from.
---

## 9. The finding that outranks everything above: nothing is ever deleted

Checked on the running install after the improvements went in:

```
GET /api/app-store/retention/v2/status  ->  "policy": null
GET /api/app-store/retention/v2/policies ->  0 policies saved, none active
levels: raw only.  keep: (empty)
```

**No retention policy exists**, so the retention engine — which is running, and
idle — deletes nothing and rolls nothing up. Two consequences:

1. `historian_agg_minute` is empty because rollup only produces buckets for data
   past a raw cutoff, and there is no cutoff. Charts still work: live SQL
   bucketing (added 2026-08-26) computes buckets per request, which is why
   `/historian/agg` returns correct data with `source: historian_agg_live`.
2. **The store only grows.** Measured on the live disk:

| | |
|---|---|
| Store today | 13.4 GB |
| Growth | ~2.85 GB/day |
| Disk free | 86.6 GB of 511.4 GB (83% used) |
| **Headroom** | **~30 days** |

When the disk fills, writes stop and collection ends. Nothing in the product
said so, which is why the Diagnostics page now leads with a banner when no
policy is active, and derives days-to-full from the store's own size over the
age of its oldest row rather than a number typed into the source.

**This was NOT fixed here, deliberately.** A retention policy decides what gets
deleted, and the standing rule on this install is that data cannot be lost.
Choosing the raw / minute / hour / day windows is the operator's decision, made
under **Database and Backup → Backup and Retention**. It is the single most
urgent item in this document; everything above only changes how fast the disk
fills.
