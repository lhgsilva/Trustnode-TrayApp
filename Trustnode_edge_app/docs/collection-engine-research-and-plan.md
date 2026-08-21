# Collection Engine — Deep Research & Reliability Plan

**Date:** 2026-07-25 · **Status:** Phases 0–2 IMPLEMENTED (commit 9b89781). Phase 0 baseline + 2× stress measured live; Phase 1 per-stage timings shipped (log-only); Phase 2 V2 engine shipped behind `TRUSTNODE_ENGINE_V2=1` (default OFF, V1 untouched). See "Implementation status" at the end.
**Goal:** A collection engine that is fast, light, reliable, and scalable to 1,000 values/second, running 24/7 with automatic recovery and zero (or minimal, accounted-for) data loss — without breaking every time we improve something else.

---

## 1. What actually happened over the last week (evidence, not memory)

Reconstructed from `git log`, `backend.log`, and the historian database.

| Period | What the record shows |
|---|---|
| **Jul 15–16** | Collection isolated onto a dedicated thread pool (08d2fd8); historian dual-DB split fixed (5ca0989). Engine reached a stable configuration. |
| **Jul 16–23** | **Quiet week — the engine was working.** Zero collection-engine commits. All work was batch features, reports, dashboards (b8468be, e047919, c98fc31, 558f8cb…). One recovery fix Jul 19 (5c26733). This is the "it was working before" period, confirmed by git. |
| **Jul 24 (one single day)** | Program-tags fix (91b73aa, 16:56) + STRING/value_text feature (05fb611, 17:58), then a same-evening cascade of **six emergency fixes**: death-spiral (fd2a168), KPI/event-loop (6594416), cadence/loss (63da332), persist bound (b5201a1), socket leak (1bf1bf5, 309d837), buffer de-escalation (04474f0), persist-wedge/WAL (50b140f). Historian shows only 11% daily coverage. Log: **51 stalls, 8 process kills, 7 cooldowns in one day.** |
| **Jul 25 morning** | 11:58–12:01: every read fails with `TimeoutError: timed out … Failed to open socket to 192.168.10.240:44818` — a **genuine TCP/network outage at boot** (PLC or network not yet up). The engine churned (3 stall-restarts, ~20 orphans in 3 min) but **auto-recovered within ~60s of the network returning**. |
| **Jul 25 now** | **Steady state, best measured health ever:** 98% on-target cadence across all 48 tags (incl. all 13 program tags + `Batch_Status_String`), median gap 1.01s vs 1.00s target, p95 1.03s, freshness median 0.78s, `cycle_ms` 687–797. |

**Conclusion 1:** The engine did not "become unreliable" gradually. It was stable for a week, then **one day of concentrated change** (program tags + STRING storage) collided with **latent, pre-existing defects** (shared executor starvation, an outbox DB created without WAL, an uncloseable-socket leak) that had been dormant because nothing had stressed the reconnect path. The Jul 24 fixes were real fixes to real latent bugs — but they were applied under fire, one on top of another, which is what produced the "over-complicated" feeling.

**Conclusion 2:** The Jul 25 morning failure was **not software** — it was the network/PLC being unreachable at boot (proven by the socket-level `TimeoutError` in the log and by the engine resuming by itself). The current build recovered without human help. The remaining criticism is that recovery was *noisy* (orphan/restart churn every 9s) instead of a calm "PLC offline — retrying with backoff" state.

---

## 2. Measured facts (from a direct PLC probe + live metrics, Jul 25)

A standalone pycomm3 probe against the real PLC (1769-L33ERMS CompactLogix, <1ms LAN):

| Operation | Measured |
|---|---|
| `open()` + controller tag DB upload | **105 ms** |
| `open()` + controller **and program** tag DB upload | **137 ms** (program tags add ~32 ms, 238 tags cached) |
| Read 7 tags, one batched call | **5–6 ms** |
| Read 10 mixed controller+program tags, one call | **6 ms** |

Live app, same PLC, same moment: full 48-tag cycle = **687–797 ms**.

**Conclusion 3 — the most important number in this document:** Raw PLC I/O is ~10 ms of a ~750 ms cycle. **~99% of cycle time is app machinery** (telemetry transaction, historian write, outbox transaction, WebSocket broadcast, status serialization, gate callback). The PLC layer has enormous headroom — 500 tags would read in ~30–60 ms. Scaling to 1,000 values/s is entirely a question of **restructuring the storage/fan-out side**, not the PLC side.

**Conclusion 4 — program tags are innocent.** +32 ms on connect, zero cost per read. They must stay (13 of the 48 production tags are program-scoped). The Jul-24 buffer-exhaustion incident was caused by leaked CIP connections from hung threads (now fixed) — the heavy template re-read merely amplified it during restart storms. The de-escalation valve (04474f0) is a correct safety net and `_needs_program_tags()` already skips the cost for controller-only gateways.

---

## 3. Why it keeps breaking when we improve things (architectural diagnosis)

The problem is not any single bug. It is the **shape** of the engine:

1. **One 4,630-line class does everything.** `plc_manager.py` contains: driver management, connect locking, tag caching, reading, coercion, telemetry, historian buffering, outbox store-and-forward, cloud flush, parallel sinks, CSV/TXT/SQLite/Postgres writers, customer-DB mirror, OPC-UA/MQTT publish, circuit breakers, three watchdog layers, orphaning, socket surgery, and restart choreography. Any change anywhere risks everything.
2. **The hot loop chains seven concerns in sequence** (read → stamp → gate → telemetry → emit → persist → sleep). Each was individually wrapped in timeouts/executors over time, producing five thread pools & locks whose interactions no one can fully reason about. Every Jul-24 incident was an *interaction* failure between these layers, never a failure of one layer alone.
3. **Supervision is layered, not unified.** Read-timeout orphaning (8s), stall watchdog (30s), burst cooldown (300s), wedge-watchdog process kill (60s). During the Jul-25 outage they all fired over each other — ~20 orphans + 3 restarts + cooldowns for what was simply "the network is down."
4. **Abandon-on-timeout leaks.** `asyncio.wait_for` cannot cancel a running thread. Every timeout leaves a live thread holding slots/locks/sockets. Both Jul-24 death spirals (executor saturation, connection leak) were this one pattern expressed twice.
5. **Health is measured by thread liveness, not by data.** The watchdogs watch progress stamps and event-loop latency. The only question an operator cares about — "is fresh data reaching the historian?" — is exactly what `metrics_collection.py` measures, and nothing in the engine itself supervises on it.

---

## 4. How professional historians do this (industry survey)

Patterns common to OSIsoft PI (interface nodes + bufserv/PIBufss), Inductive Automation Ignition (tag historian store-and-forward), Canary (sender/receiver caching), and Telegraf/InfluxDB (metric buffers):

1. **Acquisition is decoupled from storage by a bounded buffer.** The scan/reader *never* waits on any database. It appends to an in-memory queue and immediately schedules the next scan. Storage failure can never stall acquisition (Ignition: memory buffer → local disk cache → remote DB; PI: interface buffers on the collector node).
2. **One writer per storage file, batched commits.** Samples are committed in *one transaction per flush interval* (250–1000 ms), not one per sample or per scan. SQLite in WAL mode with a single writer and batched transactions sustains **tens of thousands of rows/second**; the commit count stays constant (~2–4/s) no matter how many tags exist. This is the entire scalability trick.
3. **Store-and-forward is layered and idempotent.** RAM → local durable journal → remote, with sequence numbers so replays after crash/outage are harmless. Remote slowness consumes local disk, never scan cadence. (Already half-built in TrustNode: the outbox is exactly this journal.)
4. **Scans run on an absolute schedule** ("scan classes"): next tick = start + N×interval, so processing time doesn't drift the cadence, and one slow cycle skips (and *counts*) missed ticks instead of back-pressuring.
5. **Health = data freshness; reconnect = exponential backoff.** A collector that can't reach its PLC enters a visible OFFLINE state and retries at 1s→2s→5s→…→30s. No restart storms, no thread surgery. Recovery is a *state transition*, not an emergency.
6. **Optional deadbands** (PI "exception/compression"): only store a sample when it moved more than a per-tag deadband or a heartbeat interval elapsed. Cuts storage volume 5–20× on slow-moving industrial signals. Worth having as an opt-in per-tag setting later — not required to hit 1,000/s.

---

## 5. Target architecture (V2 collection pipeline)

Same DB schemas, same APIs, same events — other modules (charts, batch, reports, intelligence, cloud sync) see **zero difference**.

```
per gateway                          per process
┌─────────────────────┐   bounded    ┌──────────────────────────┐
│ ReaderThread        │   queue      │ StorageWriter (ONE thread)│
│ absolute-tick sched │ ──────────▶ │ drains ALL gateway queues │
│ pycomm3 batched read│  (maxlen,    │ every 250–500 ms:         │
│ socket-level t/o    │   drop-      │  • historian: 1 txn/flush │
│ updates RAM "latest"│   oldest +   │  • outbox:    1 txn/flush │
│ fires WS event      │   counter)   │  • telemetry: 1 txn/flush │
└─────────────────────┘              └──────────┬───────────────┘
        │ state machine:                        │ durable local commit
        │ CONNECTING/ONLINE/                    ▼
        │ OFFLINE(backoff)             ┌──────────────────────────┐
┌───────┴─────────────┐               │ Distribution (existing)   │
│ Supervisor (ONE)    │               │ outbox→cloud PG flusher   │
│ health = row        │               │ parallel sinks, mirror,   │
│ freshness per gw    │               │ OPC-UA/MQTT publish       │
└─────────────────────┘               └──────────────────────────┘
```

Key properties:

- **Charts stay live even if every DB is slow**: the WS event and the in-memory "latest" snapshot are emitted straight from the reader, before any storage.
- **The reader is a plain thread with socket-level timeouts** (pycomm3 `socket_timeout` already enforces this — proven by the morning outage raising `TimeoutError` in 2 s). No `wait_for`-abandonment, no orphaning, no executor swapping — those exist only because reads currently run under asyncio.
- **One writer thread total** (not per gateway) batches all gateways' samples. 10 gateways × 100 tags @1 s = 1,000 rows/s = ~3 commits/s. Constant.
- **Bounded queue with drop-oldest + a visible counter** — if storage is down for hours the app degrades predictably (RAM capped, drops counted and surfaced in UI) instead of wedging. The outbox journal already covers cloud outages losslessly; the bound only matters for catastrophic local-disk failure.
- **Supervisor watches one number per gateway** — seconds since last durable row vs interval — and drives the reader's state machine. The wedge-watchdog (process kill) remains as the final backstop, unchanged.

### Scalability math against the 1,000 values/s requirement

| Load | PLC read (measured basis) | Historian writes | Verdict |
|---|---|---|---|
| 100 tags @ 1 s (today ×2) | ~10–20 ms/cycle | 100 rows/s → 2–4 batched commits/s | Trivial |
| 500 tags @ 1 s | ~50–80 ms/cycle (packed multi-service CIP) | 500 rows/s, same commit count | Comfortable |
| 1,000 values/s (e.g. 500 tags @ 500 ms, or multiple PLCs) | Parallel readers, one per PLC | 1,000 rows/s = 86.4M rows/day → **needs day-partitioned historian files + retention** (drop old partition files instead of DELETE) | Achievable; partitioning is the one new storage feature required |

---

## 6. Migration plan — small, reversible, soak-gated steps

**The prime directive: the engine only changes behind a feature flag, and only after a soak test passes.** No more fixing under fire.

- **Phase 0 — Freeze & baseline (now, zero code).** Leave the current build alone — it is measuring 98% on-target. Run it 24–48 h and keep `metrics_collection.py --window 60` snapshots. Adopt SLOs: cadence ≥ 95% on-target · freshness p50 < 2 s · stalls = 0 per day · loss < 0.5%/day · recovery after PLC outage < 60 s.
- **Phase 1 — Instrument, don't restructure (1 small change).** Extend the existing `cadence` log line with per-stage timings (`read_ms / telemetry_ms / emit_ms / persist_ms / queue_depth`) so the ~700 ms cycle cost is attributed precisely before anything is redesigned. Log-only; zero behavioral risk.
- **Phase 2 — Build the V2 pipeline behind `TRUSTNODE_ENGINE_V2=1`.** New module (`collection_engine.py`), *not* another edit to plc_manager: ReaderThread + bounded queue + single StorageWriter, writing the **identical** schemas. V1 remains the default and untouched. A/B: run V2 24 h against the same SLOs before it ever becomes default.
- **Phase 3 — Unify supervision.** Replace read-timeout-orphan + stall-restart + cooldown with the reader state machine (CONNECTING / ONLINE / OFFLINE-backoff) + freshness supervisor. Calm, visible "PLC offline, retrying in Ns" in the gateway UI instead of restart churn. Keep the wedge-watchdog.
- **Phase 4 — Prove scale with a simulator.** A `FakeLogixDriver` (deterministic values, injectable faults: connect refusal, mid-read hang, slow reads) + soak harness at 100/500/1000 tags. Becomes a CI regression gate: **every future release must pass a 10-min simulated soak + fault-injection recovery before EXEs are built.** This is the structural answer to "it breaks every time we improve something."
- **Phase 5 — 24/7 durability features.** Day-partitioned historian with partition-drop retention; outbox sequence numbers + idempotent cloud upsert (safe replay after crash); startup crash-recovery check that resumes the outbox from the last acked sequence. Then optional per-tag deadbands for storage economy.

**Explicitly out of scope / do-not-do:** no rewrite of app_store, no schema changes, no changes to batch/reports/intelligence modules, no new frameworks or brokers (no Redis/MQTT-internal-bus — SQLite WAL + threads are sufficient at this scale and keep the app light), and V1 is never deleted until V2 has ≥ 1 week of green SLOs in production.

---

## 7. Answers to the specific questions asked

- **"How was it working in the last week?"** Genuinely well from Jul 16–23 (zero engine commits — all feature work). The crisis was one day (Jul 24) of concentrated change exposing latent concurrency defects, plus a real network outage on Jul 25 that the engine survived on its own. **Right now it meets spec: 98% cadence, <1 s freshness, program tags and STRING tags all collecting.**
- **"Did the program-tags change affect it?"** Measured: +32 ms per connect, 0 ms per read. Keep them; already conditional; de-escalation stays as the safety valve. The real Jul-24 damage came from restart storms re-triggering heavy reconnects — solved by making reconnects rare and calm (Phase 3).
- **"How do historian companies do it?"** Section 4: decouple with buffers, single batched writer, layered store-and-forward, absolute-tick scans, freshness-based health with backoff — all of which map directly onto what TrustNode already half-has (outbox, WAL stores, watchdog) and Section 5 completes.
- **"1,000 values/s with no delay or loss?"** Yes — Section 5 math. The PLC side already has 50× headroom; the work is batching the storage side and partitioning the historian. Charts stay real-feeling because the UI feed comes from memory, ahead of storage.

---

## 8. Implementation status (2026-07-25, commit 9b89781)

- **Phase 0 (baseline)** — 30-min live monitor @1000 ms: **99.2% delivery (1786/1800), zero gaps > 3 s**, inter-arrival p50 0.985 s / p95 1.14 s, **chart-feed latency p50 122 ms / p95 233 ms**, DB freshness p50 0.74 s, all 48 tags in every event under ONE shared timestamp (100% full stamps), zero warnings. Earlier the same day the monitor's log-watch captured the failure signature this plan exists to kill: a 7-min hole (12:56→13:04 UTC) ending in an event-loop-stale process kill.
- **Phase 0b (2× stress)** — 10-min live window @500 ms (interval switched via API, then restored): V1 saturates at cycle_ms ≈ 780 → **61.5% of ticks delivered** — the serial per-cycle storage pipeline (~750 ms) is the rate ceiling, not the PLC (~10 ms). Degradation was GRACEFUL: zero stalls, zero warnings, zero partial writes, chart-feed latency held at 124 ms, clean restore. Live confirmation of the Section 2/5 analysis and of exactly what V2's batched-writer design removes.
- **Phase 1 (instrumentation)** — the `cadence` log line now carries `read_ms / tel_ms / emit_ms / persist_ms` (avg/max per 60-cycle window) + `persist_q` depth. Log-only.
- **Phase 2 (V2 engine)** — `backend/app/services/collection_engine.py`: `ReaderV2` (plain thread, absolute ticks, WS fanout before storage, OFFLINE state with 1/2/5/10/30 s backoff that keeps progress stamps fresh so the watchdog stays quiet) + `StorageWriterV2` (one thread, all gateways, one historian txn per flush, bounded queue + re-buffer). Integration: flag-gated branches in `GatewayWorker.start/stop`, the watchdog spawn step, and `PLCManager.fanout_threadsafe`. **Verified:** exact cadence with 450 ms-slow storage, zero row loss with batched flushes; calm OFFLINE recovery (max progress staleness 4.5 s vs 30 s threshold); V1 regression suites green with the flag off.
- **Enable for A/B:** add `TRUSTNODE_ENGINE_V2=1` to `C:\Users\User\AppData\Local\TrustNode\.env` (the desktop app loads it at boot) after installing a build that contains 9b89781; remove the line to fall back to V1 instantly.
- **Next:** Phase 3 (unified supervision as default), Phase 4 (simulator + fault-injection CI gate), Phase 5 (day-partitioned historian + idempotent cloud replay).

## 9. Table / Query-Builder widget — Phase 2 roadmap (planned 2026-07-27)

Phase 1 (shipped): searchable tag picker (chips), reorganized builder
sections, sortable output (any column, asc/desc), and "Tag limit" columns
sourced from Tags Limits & Triggers — enabling last | min | max | limit
layouts for the same tag.

Phase 2 (next): a data-source selector on the table widget adding
  * Batches — latest batches with configurable columns (batch id, start,
    end, status, type, duration) via the existing batch APIs;
  * Power meters — current power / current / energy columns from the
    power_manager tables;
  * full pivot transpose (tags as rows × calculations as columns) and
    query-result CSV export from the widget.
Each source keeps the same filter/sort/limit chrome; historian remains the
default source so existing widgets are untouched.

---

## 2026-08-21 — the distribution wedge, and why the UI lied about it

**What happened on a live build.** Six minutes after boot the `tn-v2-dist` thread stopped making progress. For the next 5.6 hours:

- the reader kept reading (WS chart feed 98.8% delivery, cadence p95 1.018 s),
- `StorageWriterV2` kept committing to the historian (`historian_readings` +48 rows/s the whole time),
- **nothing** was distributed: no telemetry/cloud record, no extra sinks, `sync_outbox` frozen at the same minute,
- `running` stayed `true`, `last_error` and `db_last_error` stayed `None`,
- the only symptom was `v2-dist queue FULL — dropped N cycle(s)` once per 100 drops, and the drop count tracked elapsed seconds exactly (the bounded deque had filled ~2.3 h after the wedge).

**Why the UI reported "no writes".** `db_write_count` / `db_last_write_utc` were stamped *only* by `PLCWorker._mark_db_write_success`, reachable only from `_persist_readings`, called only from `DistributionV2._distribute_one`. So the two fields the gateway footer, `getGatewayHealth` and every freshness rule depend on measured the *lossy* path, not the durable one. The footer showed `Local SQLite | —` on a gateway that was storing 48 rows/s.

**Changes.**

1. `StorageWriterV2._write_historian` stamps `PLCWorker._mark_historian_commit(n)` for each contributing batch after a successful `append_historian_rows`, and returns early (no stamp) when the commit fails. The legacy historian flush in `plc_manager` stamps the same way. New status fields: `historian_write_count`, `historian_last_write_utc` (durable truth — what the UI shows), `sink_write_count` / `sink_last_write_utc` (the old `db_*` numbers under an honest name; `db_*` is unchanged for existing consumers).
2. `DistributionV2` tracks its current stage (`bootstrap` / `telemetry` / `sinks` / `idle`), the time it entered it, and its last progress. `health()` reports `stalled_s` **only when work is waiting** — an idle distributor with an empty queue is not stalled. `abandon()` retires a wedged instance; it exits at the next stage boundary (its blocked call cannot be interrupted, so at most one already-started cycle is completed by it — a possible duplicate telemetry record is the deliberate trade).
3. `StorageWriterV2._supervise_distribution()` runs on every flush tick: an ERROR in `backend.log` **and the customer log** after `TRUSTNODE_V2_DIST_WARN_S` (120 s) naming the blocked stage, automatic replacement after `TRUSTNODE_V2_DIST_RESTART_S` (300 s) or immediately if the thread is dead, capped at `MAX_DIST_RESTARTS_PER_HOUR = 5` — beyond that it keeps collecting and keeps shouting rather than leaking one blocked thread per attempt.
4. `distribution_stalled_s` / `distribution_stage` ride along in `/api/plc/gateways/status` so the UI can distinguish "nothing is being distributed" from "nothing is being collected".

**Still open:** the first-principles cause of *that* wedge is unattributed — the packaged service runs elevated, so `py-spy dump --pid <service>` needs an elevated shell. The instrumentation now names the stage the next time it happens.

**Rule for diagnosing "is it collecting":** row growth in `historian_readings` (tenant + gateway predicate) or the `/ws/stream` feed. Frozen write counters + a growing historian = wedged distribution, not a collection stall.
