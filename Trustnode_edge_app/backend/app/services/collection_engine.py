"""TrustNode Collection Engine V2 — decoupled reader/writer pipeline.

Feature flag: TRUSTNODE_ENGINE_V2=1  (default OFF — V1 asyncio run-loop runs).

Shape (docs/collection-engine-research-and-plan.md, Phase 2):

    ReaderThread (per gateway)      StorageWriter (ONE per process)
    ─ plain thread, absolute-tick   ─ plain thread
    ─ pycomm3/OPC read (sync)       ─ drains ALL gateways' cycle batches
    ─ WS fanout IMMEDIATELY         ─ historian: ONE txn per flush window
    ─ bounded queue → writer        ─ telemetry + sinks per cycle batch
    ─ OFFLINE state w/ backoff      ─ failure → bounded re-buffer, retry

Why: in V1 the asyncio event loop chains read → telemetry → emit → persist,
so any storage stall (or a frozen event loop) stalls acquisition. Here the
reader NEVER touches a database and the charts' WebSocket feed never waits
on storage. A frozen event loop can no longer stop collection — readers and
the writer are plain threads; only the WS fanout callback hops to the loop.

Contracts kept identical to V1 (other modules see no difference):
  * historian_readings rows — same shape, written via app_store.append_historian_rows
  * telemetry — telemetry_service.record_collection_cycle per cycle
  * sinks/outbox — worker._persist_readings per cycle (WAL store-forward)
  * WS event dict — same keys as V1's `reading` event (persisted_local is
    None: durability is now asynchronous; the frontend never reads it)
  * worker.get_status() / latest_readings / progress stamps — updated the
    same way, so the existing status API and supervisor keep working.

Supervisor interplay: the reader stamps `_last_progress_mono` on every
successful read AND on every clean OFFLINE backoff tick (the thread is
demonstrably alive and managing its own reconnect — a calm "PLC offline"
must not trigger restart storms). The watchdog therefore only fires when
the reader thread itself is hung/dead — its correct backstop role.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

_LOG = logging.getLogger("trustnode.engine_v2")

_TRUTHY = ("1", "true", "on", "yes")


def engine_v2_enabled() -> bool:
    return str(os.environ.get("TRUSTNODE_ENGINE_V2", "")).strip().lower() in _TRUTHY


def _flush_interval_s() -> float:
    try:
        ms = int(os.environ.get("TRUSTNODE_V2_FLUSH_MS", "250") or "250")
    except Exception:
        ms = 250
    return max(0.05, min(5.0, ms / 1000.0))


class _CycleBatch:
    __slots__ = ("worker", "gateway_id", "readings", "wall_ts")

    def __init__(self, worker, gateway_id: str, readings: list, wall_ts: float) -> None:
        self.worker = worker
        self.gateway_id = gateway_id
        self.readings = readings
        self.wall_ts = wall_ts


class ReaderV2(threading.Thread):
    """Absolute-tick PLC reader. Never touches a database."""

    BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)

    def __init__(self, worker, manager, engine: "CollectionEngineV2") -> None:
        super().__init__(daemon=True, name=f"tn-v2-read-{worker.gateway_id[:12]}")
        self.worker = worker
        self.manager = manager
        self.engine = engine
        self._stop_evt = threading.Event()
        self._state = "CONNECTING"  # CONNECTING | ONLINE | OFFLINE
        self._backoff_idx = 0
        self.missed_ticks = 0

    def stop(self) -> None:
        self._stop_evt.set()

    # -- helpers ---------------------------------------------------------
    def _sleep_until(self, deadline_mono: float) -> None:
        """Interruptible sleep in ≤250 ms slices so stop() is prompt."""
        while not self._stop_evt.is_set():
            remain = deadline_mono - time.monotonic()
            if remain <= 0:
                return
            self._stop_evt.wait(timeout=min(0.25, remain))

    def _set_state(self, new_state: str, detail: str = "") -> None:
        if new_state != self._state:
            _LOG.info(
                "v2-reader gateway=%s %s -> %s%s",
                self.worker.gateway_id, self._state, new_state,
                f" ({detail})" if detail else "",
            )
            self._state = new_state

    # -- main loop -------------------------------------------------------
    def run(self) -> None:  # noqa: C901 — one linear loop, kept together on purpose
        w = self.worker
        interval_s = max(0.2, min(3600.0, (int(w.config.interval_ms or 1000)) / 1000.0))
        next_tick = time.monotonic()
        cycle_count = 0
        read_ms_sum = 0.0
        read_ms_max = 0.0
        while not self._stop_evt.is_set() and w.running:
            self._sleep_until(next_tick)
            if self._stop_evt.is_set() or not w.running:
                break
            now = time.monotonic()
            # Absolute schedule: never drift. If we fell behind by whole
            # intervals (slow read / backoff), skip the missed ticks and
            # COUNT them — a silent catch-up burst would fake the cadence.
            next_tick += interval_s
            if now > next_tick:
                skipped = int((now - next_tick) / interval_s) + 1
                self.missed_ticks += skipped
                next_tick = now + interval_s

            t0 = time.monotonic()
            try:
                readings = w._read_from_gateway()
            except Exception as exc:
                # OFFLINE path: calm, bounded backoff. The reader is alive and
                # managing itself — stamp progress so the supervisor doesn't
                # storm-restart a gateway whose PLC is simply unreachable.
                self._set_state("OFFLINE", f"{type(exc).__name__}")
                w.last_error = f"PLC read failed: {exc}"
                w._last_progress_mono = time.monotonic()
                backoff = self.BACKOFF_S[min(self._backoff_idx, len(self.BACKOFF_S) - 1)]
                self._backoff_idx += 1
                next_tick = time.monotonic() + backoff
                continue

            read_ms = (time.monotonic() - t0) * 1000.0
            self._backoff_idx = 0
            self._set_state("ONLINE")
            w.latest_readings = readings
            w._last_progress_mono = time.monotonic()
            partial = getattr(w, "_last_partial_error", "") or ""
            w.last_error = partial or None

            if w._collection_gate_cb:
                allowed, block_reason = w._collection_gate_cb(w.gateway_id, readings)
            else:
                allowed, block_reason = w._is_collection_allowed(readings)
            w.collection_blocked = not allowed
            w.collection_block_reason = block_reason

            # WS fanout FIRST — charts get the sample immediately; durability
            # happens in the writer. Same event shape as V1 (persisted_local
            # is None here: commit is asynchronous; frontend never reads it).
            try:
                event = {
                    "type": "reading",
                    "gateway_id": w.gateway_id,
                    "collection_allowed": allowed,
                    "persisted_local": None,
                    "edge_record_id": None,
                    "collection_block_reason": block_reason,
                    "status": w.get_status().model_dump(),
                    "readings": [r.model_dump() for r in readings],
                }
                self.manager.fanout_threadsafe(event)
            except Exception:
                pass  # fanout must never stop acquisition

            if allowed and readings:
                self.engine.enqueue(_CycleBatch(w, w.gateway_id, readings, time.time()))

            # cadence attribution every 60 cycles (mirrors V1's cadence line)
            cycle_count += 1
            read_ms_sum += read_ms
            read_ms_max = max(read_ms_max, read_ms)
            w._measured_cycle_ms = read_ms
            if cycle_count % 60 == 0:
                q_len, dropped = self.engine.queue_stats()
                _LOG.info(
                    "cadence-v2 gateway=%s read_ms=%.0f/%.0f interval_ms=%d tags=%d "
                    "missed_ticks=%d q=%d dropped=%d",
                    w.gateway_id, read_ms_sum / 60.0, read_ms_max,
                    int(interval_s * 1000), len(w.config.tags or []),
                    self.missed_ticks, q_len, dropped,
                )
                read_ms_sum = 0.0
                read_ms_max = 0.0
        _LOG.info("v2-reader gateway=%s exited (stop=%s running=%s)",
                  w.gateway_id, self._stop_evt.is_set(), w.running)


class StorageWriterV2(threading.Thread):
    """HISTORIAN committer (V2.1): local WAL-SQLite commits ONLY — never
    touches a network. Committed batches are handed to DistributionV2 on a
    second bounded queue, so a hung cloud write can lag distribution but can
    NEVER freeze the local historian again.

    V2.0 lesson (2026-07-25, live): one thread did historian + telemetry +
    sinks; a cloud-PG INSERT with no statement_timeout parked it for 59+ min.
    Charts (fed by the reader) stayed at 100%, but the historian froze and
    ~3.5k cycles piled up in RAM. Splitting the pipeline + statement_timeout
    on the PG engine removes both failure modes."""

    MAX_QUEUE_CYCLES = 4096          # bounded: ~68 min of 1 s cycles
    MAX_REBUFFER_ROWS = 50_000       # historian retry buffer bound

    def __init__(self) -> None:
        super().__init__(daemon=True, name="tn-v2-writer")
        self._queue: deque = deque(maxlen=self.MAX_QUEUE_CYCLES)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_evt = threading.Event()
        self.dropped_cycles = 0
        self._hist_rebuffer: list[dict] = []
        self._last_bootstrap_refresh = 0.0
        self._distributor = DistributionV2()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()
        self._distributor.stop()

    def enqueue(self, batch: _CycleBatch) -> None:
        with self._lock:
            if len(self._queue) == self._queue.maxlen:
                self.dropped_cycles += 1
                if self.dropped_cycles % 100 == 1:
                    _LOG.warning(
                        "v2-writer queue FULL — dropped %d cycle(s) so far "
                        "(storage persistently slower than acquisition)",
                        self.dropped_cycles,
                    )
            self._queue.append(batch)
        self._wake.set()

    def queue_stats(self) -> tuple[int, int]:
        with self._lock:
            hist_q, hist_drop = len(self._queue), self.dropped_cycles
        dist_q, dist_drop = self._distributor.stats()
        return hist_q + dist_q, hist_drop + dist_drop

    # -- row building (same shape _broadcast produced in V1) -------------
    @staticmethod
    def _rows_for(batch: _CycleBatch) -> list[dict]:
        w = batch.worker
        try:
            db_name = str((w.db_sink or {}).get("name") or "")
        except Exception:
            db_name = ""
        gateway_name = str(getattr(w.config, "name", "") or batch.gateway_id)
        device_name = str(getattr(w.config, "device_name", "") or "")
        plc_ip = str(getattr(w.config, "plc_ip", "") or "")
        rows = []
        for r in batch.readings:
            tag = str(getattr(r, "tag_name", "") or "").strip()
            if not tag:
                continue
            rows.append(
                {
                    "ts_utc": str(getattr(r, "ts_utc", "") or ""),
                    "source": str(getattr(r, "source", "") or ""),
                    "gateway_id": batch.gateway_id,
                    "gateway_name": gateway_name,
                    "device_name": device_name,
                    "plc_ip": plc_ip,
                    "database_name": db_name,
                    "tag_name": tag,
                    "value": getattr(r, "value", None),
                    "value_text": getattr(r, "value_text", None),
                    "quality": getattr(r, "quality", None),
                    "quality_label": str(getattr(r, "quality_label", "") or ""),
                }
            )
        return rows

    def _write_historian(self, batches: list[_CycleBatch]) -> None:
        """ALL cycles from ALL gateways in ONE append call (one txn)."""
        from app.state import app_store

        rows = list(self._hist_rebuffer)
        self._hist_rebuffer = []
        for b in batches:
            rows.extend(self._rows_for(b))
        if not rows:
            return
        try:
            app_store.append_historian_rows(rows)
        except Exception as exc:
            # Keep (bounded) for next flush — chronological order preserved.
            keep = rows[-self.MAX_REBUFFER_ROWS:]
            lost = len(rows) - len(keep)
            self._hist_rebuffer = keep
            _LOG.warning(
                "v2-writer historian write failed (%s: %s) — re-buffered %d rows%s",
                type(exc).__name__, exc, len(keep),
                f", DROPPED {lost} oldest" if lost > 0 else "",
            )

    def run(self) -> None:
        flush_s = _flush_interval_s()
        if not self._distributor.is_alive():
            self._distributor.start()
        _LOG.info("v2-writer started (flush=%.0fms, queue cap=%d cycles, "
                  "distribution split off)", flush_s * 1000, self.MAX_QUEUE_CYCLES)
        while not self._stop_evt.is_set():
            self._wake.wait(timeout=flush_s)
            self._wake.clear()
            with self._lock:
                batches = list(self._queue)
                self._queue.clear()
            if not batches and not self._hist_rebuffer:
                continue
            t0 = time.monotonic()
            self._write_historian(batches)
            hist_ms = (time.monotonic() - t0) * 1000.0
            # Hand the committed batches to distribution (telemetry + cloud +
            # parallel sinks). Never inline — a slow cloud write must not delay
            # the NEXT historian commit.
            for b in batches:
                self._distributor.submit(b)
            if hist_ms > 2000:
                _LOG.warning(
                    "v2-writer slow HISTORIAN flush: %d cycle(s) in %.0f ms "
                    "(local DB lagging)", len(batches), hist_ms,
                )
        _LOG.info("v2-writer exited")


class DistributionV2(threading.Thread):
    """Telemetry + sinks/outbox distribution, decoupled from the historian.

    Consumes committed cycle batches; every stage is TIMED and logged when
    slow so a blocking call names itself in the log (the V2.0 park was only
    attributable after the fact — never again). Bounded queue: if the cloud
    stays stuck past ~2h of 1s cycles, oldest DISTRIBUTION work drops (the
    local historian already holds the data; the store-forward outbox re-syncs
    cloud gaps from it on recovery)."""

    MAX_QUEUE_CYCLES = 8192

    def __init__(self) -> None:
        super().__init__(daemon=True, name="tn-v2-dist")
        self._queue: deque = deque(maxlen=self.MAX_QUEUE_CYCLES)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_evt = threading.Event()
        self.dropped = 0
        self._last_bootstrap_refresh = 0.0
        self._slow_log_mono = 0.0

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()

    def submit(self, batch: _CycleBatch) -> None:
        with self._lock:
            if len(self._queue) == self._queue.maxlen:
                self.dropped += 1
                if self.dropped % 100 == 1:
                    _LOG.warning(
                        "v2-dist queue FULL — dropped %d cycle(s) of DISTRIBUTION "
                        "work (historian already durable; outbox re-syncs cloud)",
                        self.dropped,
                    )
            self._queue.append(batch)
        self._wake.set()

    def stats(self) -> tuple[int, int]:
        with self._lock:
            return len(self._queue), self.dropped

    def _distribute_one(self, batch: _CycleBatch) -> None:
        from app.state import telemetry_service, app_store

        w = batch.worker
        boot_ms = tel_ms = sink_ms = 0.0
        t = time.monotonic()
        if t - self._last_bootstrap_refresh >= 10.0:
            self._last_bootstrap_refresh = t
            try:
                bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
                telemetry_service.configure_from_bootstrap(bootstrap)
            except Exception:
                pass
            boot_ms = (time.monotonic() - t) * 1000.0
        t = time.monotonic()
        try:
            ok, err, _rec_id = telemetry_service.record_collection_cycle(
                gateway_id=batch.gateway_id,
                config=w.config,
                readings=batch.readings,
                collection_status="ok",
            )
            if ok and not getattr(w, "_first_row_logged", False):
                w._first_row_logged = True
                boot0 = getattr(w, "_startup_started_monotonic", 0.0) or 0.0
                if boot0 > 0:
                    _LOG.info(
                        "first-row gateway=%s boot_to_first_row=%.2fs (v2)",
                        batch.gateway_id, time.monotonic() - boot0,
                    )
            if not ok:
                w.last_error = f"Cloud-record write failed: {err}"
        except Exception as exc:
            w.last_error = f"Cloud-record write error: {exc}"
        tel_ms = (time.monotonic() - t) * 1000.0
        t = time.monotonic()
        try:
            w._persist_readings(batch.readings)
        except Exception as exc:
            _LOG.warning(
                "v2-dist sink persist failed gateway=%s: %s: %s",
                batch.gateway_id, type(exc).__name__, exc,
            )
        sink_ms = (time.monotonic() - t) * 1000.0
        total = boot_ms + tel_ms + sink_ms
        # Name the culprit when distribution is slow — rate-limited to one
        # line per 30 s so a degraded cloud doesn't flood the log.
        if total > 1000.0 and (time.monotonic() - self._slow_log_mono) > 30.0:
            self._slow_log_mono = time.monotonic()
            q_len, _ = self.stats()
            _LOG.warning(
                "v2-dist slow cycle gateway=%s total=%.0fms "
                "(bootstrap=%.0f tel=%.0f sinks=%.0f) backlog=%d",
                batch.gateway_id, total, boot_ms, tel_ms, sink_ms, q_len,
            )

    def run(self) -> None:
        _LOG.info("v2-dist started (queue cap=%d cycles)", self.MAX_QUEUE_CYCLES)
        while not self._stop_evt.is_set():
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            while not self._stop_evt.is_set():
                with self._lock:
                    batch = self._queue.popleft() if self._queue else None
                if batch is None:
                    break
                try:
                    self._distribute_one(batch)
                except Exception as exc:
                    _LOG.warning("v2-dist cycle failed: %s: %s",
                                 type(exc).__name__, exc)
        _LOG.info("v2-dist exited")


class CollectionEngineV2:
    """Process-wide singleton: reader registry + the single writer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._writer: Optional[StorageWriterV2] = None
        self._readers: Dict[str, ReaderV2] = {}

    def _ensure_writer(self) -> StorageWriterV2:
        with self._lock:
            if self._writer is None or not self._writer.is_alive():
                self._writer = StorageWriterV2()
                self._writer.start()
            return self._writer

    def enqueue(self, batch: _CycleBatch) -> None:
        self._ensure_writer().enqueue(batch)

    def queue_stats(self) -> tuple[int, int]:
        w = self._writer
        return w.queue_stats() if w else (0, 0)

    def start_reader(self, worker, manager) -> None:
        self._ensure_writer()
        with self._lock:
            old = self._readers.get(worker.gateway_id)
        if old is not None:
            old.stop()
            old.join(timeout=2.0)
        reader = ReaderV2(worker, manager, self)
        with self._lock:
            self._readers[worker.gateway_id] = reader
        reader.start()
        _LOG.info("v2 engine: reader started for gateway=%s", worker.gateway_id)

    def stop_reader(self, gateway_id: str, join_timeout: float = 2.0) -> None:
        with self._lock:
            reader = self._readers.pop(gateway_id, None)
        if reader is not None:
            reader.stop()
            reader.join(timeout=join_timeout)


engine_v2 = CollectionEngineV2()
