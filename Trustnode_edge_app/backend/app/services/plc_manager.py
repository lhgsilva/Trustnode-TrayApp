import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote_plus

# Operator 2026-06-23: dedicated logger for the gateway worker so every
# operationally-relevant transition lands in backend.log with a stable
# tag. The user invariant is "a healthy gateway never stops without a
# logged reason" — the only way to honor that is to write a log line
# for EVERY stop / error / restart from inside the worker.
_GW_LOG = logging.getLogger("trustnode.gateway")

from app.models import GatewayConfig, GatewayReading, GatewayStatus
from app.opcua_utils import resolve_requested_nodes, split_requested_identifiers
from app.tenant import normalize_tenant_id


class _SafeDict(dict):
    """str.format_map() default: an unknown placeholder renders as the
    literal "{name}" rather than raising KeyError. Lets the custom CSV
    format string survive operator typos without crashing the poll
    cycle — they just see the literal token in their file and fix it."""
    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"


def _utc_str_to_local_iso(ts_utc: str) -> str:
    """Convert a UTC timestamp string into the operator's local ISO string.

    Accepts the formats this codebase emits ("YYYY-MM-DD HH:MM:SS.fff" and
    ISO 8601 with optional "Z"/+00:00). Returns the original string if it
    cannot be parsed so callers always get *something* in the column —
    silently dropping the cell would be worse than printing the UTC value.
    """
    raw = str(ts_utc or "").strip()
    if not raw:
        return ""
    cand = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    parsed: datetime | None = None
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(cand.split("+", 1)[0], fmt)
            break
        except Exception:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(cand)
        except Exception:
            return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except Exception:
        return raw


class GatewayWorker:
    def __init__(
        self,
        gateway_id: str,
        config: GatewayConfig,
        db_sink: Dict[str, Any] | None,
        db_sinks: List[Dict[str, Any]] | None = None,
        collection_gate_cb=None,
    ) -> None:
        self.gateway_id = gateway_id
        self.config = config
        self.db_sink = db_sink or None
        self.db_sinks = self._normalize_db_sinks(db_sink, db_sinks)
        self._collection_gate_cb = collection_gate_cb
        self.running = False
        self.last_error: str | None = None
        self.latest_readings: List[GatewayReading] = []
        self._task: asyncio.Task | None = None

        # 2026-07-15 (COLLECTION ISOLATION): a DEDICATED thread pool for this
        # gateway's blocking collection I/O (PLC reads + historian writes). These
        # used to run on asyncio's DEFAULT threadpool, which is SHARED with every
        # other sync route — including control-plane/Supabase calls. When those
        # cloud calls exhausted the shared pool (observed: ~223 threads blocked,
        # health/re-check hung), the PLC read + historian write couldn't get a
        # worker either, so collection STALLED even while the gateway said
        # "Running" (real symptom: historian gaps). Giving collection its own
        # small executor guarantees it can NEVER be starved by unrelated cloud
        # work. Created lazily; a few workers per gateway is plenty (read + write
        # + buffer flush overlap at most).
        self._collection_executor: ThreadPoolExecutor | None = None
        self._collection_executor_lock = threading.Lock()

        # SEPARATE pool for persistence (sink writes / outbox enqueue). Persist
        # must NEVER share slots with the PLC read: if a sink write parks (locked
        # DB, slow remote), a shared pool would starve the read and stall the
        # whole loop — the "running but no data" failure. Keeping persist on its
        # own 2-slot pool means the worst a wedged persist can do is delay the
        # NEXT persist; the read cycle keeps advancing and charts keep updating.
        self._persist_executor: ThreadPoolExecutor | None = None

        self._db_engine = None
        self._db_engine_key = ""
        self._db_schema_ready_key = ""
        self._buffer_engine = None
        self._buffer_engine_key = ""

        self.db_write_count = 0
        self.db_last_write_utc: str | None = None
        self.db_last_error: str | None = None
        self.db_pending_count = 0
        self.collection_blocked = False
        self.collection_block_reason: str | None = None
        # Monotonic timestamp captured each time the worker is (re)started.
        # During the first few seconds after start, transient PLC-handshake
        # errors are common and self-recover on the next cycle; we suppress
        # them so the dashboard doesn't paint "Device Fails" for a healthy
        # gateway during normal warm-up.
        self._startup_started_monotonic = 0.0
        self._startup_grace_seconds = float(
            os.environ.get("TRUSTNODE_STARTUP_GRACE_SECONDS", "8.0") or "8.0"
        )
        self._remote_flush_inflight = False
        self._remote_flush_lock = threading.Lock()
        self._remote_last_flush_started_monotonic = 0.0
        self._remote_last_pending_probe_monotonic = 0.0
        self._remote_flush_min_interval_seconds = max(
            0.05, float(os.environ.get("TRUSTNODE_REMOTE_FLUSH_MIN_SECONDS", "0.15") or "0.15")
        )
        self._remote_pending_probe_seconds = max(
            0.25, float(os.environ.get("TRUSTNODE_REMOTE_PENDING_PROBE_SECONDS", "2.0") or "2.0")
        )
        self._ab_preferred_path: str | None = None
        self._ab_pycomm3_client = None
        self._ab_pycomm3_path: str | None = None
        # Guards LogixDriver creation. The startup prewarm thread and the run
        # loop's first cycle both call _ensure_ab_pycomm3_client concurrently;
        # without this they each open a connection and fetch the tag database
        # twice.
        self._ab_connect_lock = threading.Lock()
        # One-shot flag so we log boot->first-row latency exactly once.
        self._first_row_logged = False
        # Wall-clock cap on a single read cycle. Above a healthy ~0.15s cycle,
        # below the 30s stall watchdog, so a hung read self-heals in seconds
        # rather than tripping a restart. Env-tunable; 0 disables the cap.
        try:
            self._read_timeout_s = float(
                os.environ.get("TRUSTNODE_READ_TIMEOUT_SECONDS", "8.0") or "8.0"
            )
        except Exception:
            self._read_timeout_s = 8.0
        self._ab_pylogix_client = None
        self._ab_pylogix_ip: str | None = None
        self._ab_pylogix_slot: int | None = None
        # Summary of per-tag failures from the last successful read cycle
        # (some tags BAD, others GOOD). Surfaced in get_status().last_error
        # so the operator can see WHICH tags are unhappy without losing
        # the rest of the cycle.
        self._last_partial_error: str = ""
        # Operator 2026-06-19 (L3b): bounded read-timeout. If a driver
        # call hangs indefinitely (e.g. half-open TCP after a NIC flap),
        # the worker would silently stop collecting forever. We wrap the
        # read in asyncio.wait_for and force a driver-session reset on
        # timeout so the next cycle reconnects. The counter is for
        # observability — surfaced via get_status() in last_error.
        self._stalled_read_cycles: int = 0
        # Operator 2026-06-20: cadence observability. _measured_cycle_ms
        # is the wall-clock duration of the most recent cycle; the streak
        # counts consecutive cycles where measured > 1.5× configured. A
        # streak of 3 triggers a cadence warning in last_error.
        self._measured_cycle_ms: float = 0.0
        self._cycle_overrun_streak: int = 0
        # Operator 2026-06-19 (L3c): per-sink circuit breaker. After N
        # consecutive write errors against the SAME sink (by id) we
        # stop attempting writes to that sink for COOLDOWN_S seconds.
        # Local collection continues unchanged — only the failing sink
        # is paused, preventing a broken Postgres / CSV path from
        # eating CPU on every cycle and drowning the audit log. After
        # cooldown one probe write is allowed; success closes the
        # breaker, failure resets the cooldown.
        self._sink_breaker_fails: Dict[str, int] = {}
        self._sink_breaker_open_until_mono: Dict[str, float] = {}
        self._SINK_BREAKER_THRESHOLD = max(2, int(
            os.environ.get("TRUSTNODE_SINK_BREAKER_THRESHOLD", "5") or "5"
        ))
        self._SINK_BREAKER_COOLDOWN_S = max(5.0, float(
            os.environ.get("TRUSTNODE_SINK_BREAKER_COOLDOWN_SECONDS", "60") or "60"
        ))
        self._opc_resolve_cache_key = ""
        self._opc_resolved_targets: list[tuple[str, str]] = []
        # Persistent OPC-UA client. The previous behaviour was to
        # Client.connect()+disconnect() on every poll, costing 6 round-
        # trips per cycle. Now we cache the session like the AB driver.
        self._opc_client = None
        self._opc_endpoint: str = ""
        self._telemetry_runtime_refresh_monotonic = 0.0
        # Operator 2026-06-23: liveness counter. Bumped on every
        # successful read cycle. get_status().running is computed from
        # this rather than from the bare self.running flag, so a worker
        # whose coroutine is wedged (e.g. blocked in a driver socket
        # read with no exception) reports running=False to the UI and
        # to the supervisor watchdog. Previously `self.running` stayed
        # True forever in that case and the UI showed "RUNNING but no
        # data" — the failure mode observed 2026-06-22.
        self._last_progress_mono: float = 0.0
        # Stall threshold: max(3 × interval_ms, 30 s). Computed per
        # cycle from the live config.
        self._stall_threshold_s: float = 30.0
        # Watchdog restart count for the current PlcManager lifetime,
        # exposed via get_status().restart_count for the UI.
        self.restart_count: int = 0
        self._last_restart_utc: str | None = None

    def set_config(self, config: GatewayConfig) -> None:
        if (
            str(getattr(self.config, "plc_ip", "") or "").strip() != str(getattr(config, "plc_ip", "") or "").strip()
            or str(getattr(self.config, "gateway_type", "") or "").strip().lower()
            != str(getattr(config, "gateway_type", "") or "").strip().lower()
        ):
            self._dispose_gateway_clients()
        if (
            str(getattr(self.config, "opc_url", "") or "").strip() != str(getattr(config, "opc_url", "") or "").strip()
            or list(getattr(self.config, "tags", []) or []) != list(getattr(config, "tags", []) or [])
        ):
            self._opc_resolve_cache_key = ""
            self._opc_resolved_targets = []
        self.config = config

    def _normalize_db_sinks(
        self, db_sink: Dict[str, Any] | None, db_sinks: List[Dict[str, Any]] | None
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for sink in [db_sink, *((db_sinks or []))]:
            if not isinstance(sink, dict):
                continue
            engine = str(sink.get("engine") or "").strip().lower()
            if not engine:
                continue
            key = (
                f"{engine}|{str(sink.get('id') or '').strip()}|"
                f"{str(sink.get('host') or '').strip().lower()}|{int(sink.get('port') or 0)}|"
                f"{str(sink.get('database') or '').strip().lower()}|"
                f"{str(sink.get('file_path') or '').strip().lower()}|"
                f"{str(sink.get('sqlite_path') or '').strip().lower()}"
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(sink))
        return out

    def set_db_sink(
        self, db_sink: Dict[str, Any] | None, db_sinks: List[Dict[str, Any]] | None = None
    ) -> None:
        self.db_sink = db_sink or None
        self.db_sinks = self._normalize_db_sinks(db_sink, db_sinks)
        self.db_write_count = 0
        self.db_last_write_utc = None
        self.db_last_error = None
        self.db_pending_count = 0
        with self._remote_flush_lock:
            self._remote_flush_inflight = False
            self._remote_last_flush_started_monotonic = 0.0
            self._remote_last_pending_probe_monotonic = 0.0
        self._dispose_db_engine()

    def set_collection_gate_cb(self, cb) -> None:
        self._collection_gate_cb = cb

    async def start(self, emit_event) -> None:
        if self.running:
            return
        self.running = True
        # Clear errors and counters from the previous run so the UI doesn't
        # paint a stale "Device Fails" / "DB Fails" badge between this start
        # and the first successful read cycle. The startup_started_monotonic
        # marker lets `_run_loop` suppress transient first-cycle errors for a
        # short grace window (slow PLC handshakes / first OPC session setup).
        self.last_error = None
        self.db_last_error = None
        self.collection_blocked = False
        self.collection_block_reason = None
        self._startup_started_monotonic = time.monotonic()
        # Prime the liveness counter so the supervisor doesn't flag a
        # freshly-started worker as stalled before its first cycle
        # completes.
        self._last_progress_mono = time.monotonic()
        _GW_LOG.info(
            "start gateway=%s type=%s ip=%s opc=%s interval=%dms tags=%d",
            self.gateway_id,
            self.config.gateway_type,
            self.config.plc_ip or "",
            self.config.opc_url or "",
            int(self.config.interval_ms or 0),
            len(self.config.tags or []),
        )
        # Operator 2026-06-25: run prewarm in BACKGROUND (was awaited).
        # The user reported the Start button hanging 1-3s, leading to
        # double-clicks that produced AbortError. The prewarm is an
        # optimization (skips the first-cycle handshake cost); it is
        # NOT required for correctness — the run loop's reconnect
        # handles a cold first cycle just fine. Fire-and-forget here
        # so the HTTP /api/plc/gateways/start returns instantly.
        asyncio.create_task(asyncio.to_thread(self._prewarm_client))
        # V2 engine (feature flag): decoupled reader-thread pipeline instead of
        # the asyncio run loop. Requires the manager (for thread-safe WS fanout)
        # — resolved from the bound emit_event callback. Falls back to V1 when
        # the flag is off or the manager can't be resolved.
        try:
            from app.services.collection_engine import engine_v2, engine_v2_enabled
            _mgr = getattr(emit_event, "__self__", None)
            if engine_v2_enabled() and _mgr is not None and hasattr(_mgr, "fanout_threadsafe"):
                _mgr._loop = asyncio.get_running_loop()
                self._task = None
                engine_v2.start_reader(self, _mgr)
                return
        except Exception as exc:
            _GW_LOG.warning("V2 engine start failed (%s) — falling back to V1 loop", exc)
        self._task = asyncio.create_task(self._run_loop(emit_event))

        def _on_run_loop_done(t: "asyncio.Task[Any]", gid: str = self.gateway_id) -> None:
            # Invariant A: a run-loop coroutine that exits — for ANY
            # reason — must leave a log line. Previously a silently-
            # returning loop produced 18 h of zero rows with nothing in
            # the log. With this callback the supervisor, the operator,
            # and the postmortem reader all see the exit cause.
            try:
                if t.cancelled():
                    _GW_LOG.info("run-loop-exit gateway=%s reason=cancelled", gid)
                    return
                exc = t.exception()
                if exc is not None:
                    _GW_LOG.error(
                        "run-loop-exit gateway=%s reason=exception exc=%s: %s",
                        gid, type(exc).__name__, exc,
                    )
                    return
                _GW_LOG.info("run-loop-exit gateway=%s reason=clean-return", gid)
            except Exception:
                # Defensive only — never let the callback itself die
                # silently and rob us of the postmortem.
                pass

        self._task.add_done_callback(_on_run_loop_done)

    async def stop(self) -> None:
        # Operator 2026-06-18: reverted to the original simple stop. The
        # short-lived bounded-join optimization shipped earlier today
        # appeared to interact badly with the AB driver and broke active
        # customer setups. Keeping the original "await self._task" until
        # the start/stop latency issue can be diagnosed against a live
        # machine — correctness wins over speed here.
        if not self.running:
            return
        self.running = False
        # Invariant A: every stop must leave a logged reason. The
        # explicit "operator/manager stop" reason is set here; watchdog
        # restarts log their own reason via the supervisor before they
        # call stop().
        _GW_LOG.info(
            "stop gateway=%s reason=requested last_error=%r writes=%d",
            self.gateway_id, self.last_error, int(self.db_write_count or 0),
        )
        with self._remote_flush_lock:
            self._remote_flush_inflight = False
        # V2 engine: stop the reader thread (no-op when V2 never started).
        try:
            from app.services.collection_engine import engine_v2
            engine_v2.stop_reader(self.gateway_id)
        except Exception:
            pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._dispose_gateway_clients()
        # 2026-07-15: tear down the dedicated collection executor so a stopped/
        # restarted gateway doesn't leak its thread pool. Non-blocking: don't wait
        # on in-flight writes (they finish on their own daemon threads).
        try:
            ex = self._collection_executor
            self._collection_executor = None
            if ex is not None:
                ex.shutdown(wait=False, cancel_futures=False)
        except Exception:
            pass
        try:
            pex = self._persist_executor
            self._persist_executor = None
            if pex is not None:
                pex.shutdown(wait=False, cancel_futures=False)
        except Exception:
            pass

    def is_stalled(self) -> tuple[bool, float]:
        """True iff the worker is supposed to be running but hasn't
        made progress within the configured stall window. Returns
        (stalled, idle_seconds).

        A worker that never completed its first cycle (and is still
        inside the startup grace window) is NOT considered stalled —
        the watchdog must not restart a worker that is simply slow to
        connect for the first time.
        """
        if not self.running:
            return (False, 0.0)
        last = self._last_progress_mono or 0.0
        if last <= 0.0:
            return (False, 0.0)
        idle = time.monotonic() - last
        threshold = max(5.0, float(self._stall_threshold_s or 30.0))
        return (idle > threshold, idle)

    def get_status(self) -> GatewayStatus:
        # Compute a "healthy-running" flag from the liveness counter so
        # the UI never sees the contradiction "running=True but no fresh
        # rows" that the customer hit on 2026-06-22. The bare
        # self.running flag is now an *intent* flag (the worker is
        # supposed to be running); the supervisor watchdog converts
        # intent + liveness into an honest report.
        stalled, _idle = self.is_stalled()
        effective_running = bool(self.running and not stalled)
        last_error = self.last_error
        if stalled and (not last_error or "stalled" not in last_error.lower()):
            last_error = (
                f"Worker stalled — no read cycle completed for "
                f"{_idle:.0f}s (threshold {self._stall_threshold_s:.0f}s). "
                f"Supervisor will restart it."
            )
        return GatewayStatus(
            running=effective_running,
            gateway_type=self.config.gateway_type,
            plc_ip=self.config.plc_ip,
            interval_ms=self.config.interval_ms,
            tags=self.config.tags,
            last_error=last_error,
            db_sink_engine=(self.db_sink or {}).get("engine"),
            db_write_count=self.db_write_count,
            db_last_write_utc=self.db_last_write_utc,
            db_last_error=self.db_last_error,
            db_pending_count=self.db_pending_count,
            collection_blocked=self.collection_blocked,
            collection_block_reason=self.collection_block_reason,
        )

    def _get_collection_executor(self) -> ThreadPoolExecutor:
        """Lazily create this gateway's DEDICATED collection thread pool.
        max_workers=3 covers the overlap of read + write + buffer-flush; kept
        small on purpose so it isn't a resource hog per gateway."""
        if self._collection_executor is None:
            with self._collection_executor_lock:
                if self._collection_executor is None:
                    self._collection_executor = ThreadPoolExecutor(
                        max_workers=3,
                        thread_name_prefix=f"tn-collect-{self.gateway_id[:8]}",
                    )
        return self._collection_executor

    def _record_stage_ms(self, read_ms: float, tel_ms: float, emit_ms: float, per_ms: float) -> None:
        """Phase-1 instrumentation: accumulate per-stage wall times over the
        60-cycle cadence window (sum + max per stage). Read by the cadence
        log line, then reset. Log-only — never raises."""
        try:
            win = getattr(self, "_stage_win", None)
            if win is None:
                win = {"n": 0, "sum": [0.0, 0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0, 0.0]}
            vals = (read_ms, tel_ms, emit_ms, per_ms)
            win["n"] += 1
            for i, v in enumerate(vals):
                win["sum"][i] += v
                if v > win["max"][i]:
                    win["max"][i] = v
            self._stage_win = win
        except Exception:
            pass

    def _get_persist_executor(self) -> ThreadPoolExecutor:
        """Lazily create this gateway's DEDICATED persistence thread pool,
        SEPARATE from the read pool. A stalled sink write parks a persist
        worker here and NOT a read worker, so the collection cycle keeps
        advancing (and charts keep updating) even while a sink is slow.
        2 workers so one in-flight slow write still leaves room for the next
        batch to enqueue locally without waiting."""
        if self._persist_executor is None:
            with self._collection_executor_lock:
                if self._persist_executor is None:
                    self._persist_executor = ThreadPoolExecutor(
                        max_workers=2,
                        thread_name_prefix=f"tn-persist-{self.gateway_id[:8]}",
                    )
        return self._persist_executor

    async def _run_persist_io(self, fn, *args):
        """Run a blocking persistence call on the DEDICATED persist pool
        (never the read pool). Drop-in async wrapper like `_run_collection_io`."""
        loop = asyncio.get_running_loop()
        executor = self._get_persist_executor()
        if args:
            import functools
            return await loop.run_in_executor(executor, functools.partial(fn, *args))
        return await loop.run_in_executor(executor, fn)

    def _orphan_collection_runtime(self) -> None:
        """Swap in a FRESH executor + connect lock and drop the driver client.

        A hung read/connect thread cannot be cancelled (Python threads), and if
        it hung inside plc.open() it still holds the connect lock. Reusing either
        lets one zombie thread wedge the next cycle — and after 3 they saturate
        the 3-slot pool and the loop can never start again. This retires the
        current pool (the zombie drains into it and dies when its socket finally
        times out) and hands the next cycle a clean slate. Called on read-timeout
        and by the watchdog restart."""
        # FORCE-CLOSE the hung client's socket FIRST. A read stuck in recv()
        # blocks even past the socket timeout in some half-open TCP states, so
        # the zombie thread lives forever — we saw the process reach 237 threads,
        # whose GIL churn starved the event loop so the read-timeout timer and
        # the restarted loop could barely run. shutdown(SHUT_RDWR) makes the
        # blocked recv raise immediately, so the thread exits instead of leaking.
        stale_client = self._ab_pycomm3_client
        stale_opc = self._opc_client
        for label, cli in (("ab", stale_client), ("opc", stale_opc)):
            try:
                raw = getattr(getattr(cli, "_sock", None), "sock", None)
                if raw is not None:
                    import socket as _sk
                    try:
                        raw.shutdown(_sk.SHUT_RDWR)
                    except Exception:
                        pass
                    try:
                        raw.close()
                    except Exception:
                        pass
            except Exception:
                pass
        try:
            with self._collection_executor_lock:
                old_ex = self._collection_executor
                self._collection_executor = None
            if old_ex is not None:
                old_ex.shutdown(wait=False, cancel_futures=False)
        except Exception:
            pass
        # Fresh lock so a zombie still inside plc.open() can't block the next
        # connect; drop the client refs so the next cycle reconnects clean.
        self._ab_connect_lock = threading.Lock()
        try:
            self._ab_pycomm3_client = None
            self._ab_pycomm3_path = None
            self._opc_client = None
        except Exception:
            pass

    async def _run_collection_io(self, fn, *args):
        """Run a BLOCKING collection call (PLC read / historian write) on the
        gateway's DEDICATED executor instead of asyncio's shared default pool, so
        control-plane / cloud slowness can never starve collection. Drop-in
        replacement for `await asyncio.to_thread(fn, *args)`."""
        loop = asyncio.get_running_loop()
        executor = self._get_collection_executor()
        if args:
            import functools
            return await loop.run_in_executor(executor, functools.partial(fn, *args))
        return await loop.run_in_executor(executor, fn)

    async def _run_loop(self, emit_event) -> None:
        # Operator 2026-06-18: reverted the per-cycle asyncio.wait_for
        # wrapper. It was intended as a zombie-defense but the customer
        # reported it broke a previously-working setup (PLCs going
        # offline mid-collection, gateways not advancing
        # last_check_utc). Until we can reproduce and bound the timeout
        # correctly without disturbing healthy reads, we keep the
        # original direct asyncio.to_thread call.
        while self.running:
            cycle_started = time.monotonic()
            try:
                # Operator 2026-06-20: matches the 0.0.0.1000 reference exactly.
                # The earlier wait_for wrapper added during L3b had two effects
                # that broke working customer setups: (a) a 30-second floor that
                # was way too long for a sub-second collection cycle to recover
                # from, (b) eager driver-session disposal on timeout that fed
                # back into the next cycle as another timeout. Reference's
                # plain to_thread call is the right shape — the bare `except`
                # at the bottom of the cycle catches any read exception and
                # the next cycle reconnects through `_ensure_ab_pycomm3_client`
                # / `_ensure_opc_client`, which already handle stale sessions.
                # Bound the READ with a wall-clock timeout well above a healthy
                # cycle (~0.15 s) but below the 30 s watchdog, so one hung read
                # self-heals in seconds instead of stalling for 30 s and tripping
                # the restart death-spiral. A prior naive wait_for was reverted
                # because it (a) used a 30 s floor and (b) eagerly disposed the
                # session on EVERY timeout. This version times out fast AND, on
                # timeout, ORPHANS the executor + connect lock so the hung thread
                # can't wedge the next cycle (the same clean-slate the watchdog
                # restart now does), then reconnects. Configurable; 0 disables.
                read_to = self._read_timeout_s
                # Phase-1 instrumentation: wall-clock per pipeline stage, so the
                # cadence log attributes cycle time (read vs telemetry vs emit vs
                # persist) instead of one opaque cycle_ms. Log-only; no behavior.
                _t_read0 = time.monotonic()
                try:
                    if read_to and read_to > 0:
                        readings = await asyncio.wait_for(
                            self._run_collection_io(self._read_from_gateway), timeout=read_to
                        )
                    else:
                        readings = await self._run_collection_io(self._read_from_gateway)
                except asyncio.TimeoutError:
                    # The read thread is hung (half-open socket / slow CIP). Swap
                    # in a fresh executor + connect lock so the zombie thread is
                    # orphaned and drains on its own; drop the client so the next
                    # cycle reconnects. Do NOT stamp progress — this cycle failed.
                    self._orphan_collection_runtime()
                    self.last_error = (
                        f"PLC read exceeded {read_to:.0f}s — session reset, retrying."
                    )
                    _GW_LOG.warning(
                        "read-timeout gateway=%s after=%.0fs — orphaned executor, reconnecting",
                        self.gateway_id, read_to,
                    )
                    # Brief recovery pause scaled to the configured cadence (not a
                    # hard 1s), so a fast gateway retries promptly and a slow one
                    # doesn't spin.
                    _iv = max(0.2, min(2.0, (int(self.config.interval_ms or 1000) / 1000.0)))
                    await asyncio.sleep(_iv)
                    continue
                _read_ms = (time.monotonic() - _t_read0) * 1000.0
                self.latest_readings = readings
                # Liveness stamp — successful read = worker is alive.
                # The supervisor watchdog reads this to decide whether
                # to hard-restart a stalled coroutine. We stamp BEFORE
                # persistence so a slow/dead sink doesn't make a healthy
                # PLC look dead.
                self._last_progress_mono = time.monotonic()
                # Surface a non-fatal "some tags are bad" summary from the
                # AB readers, OR clear it on a clean cycle. The UI shows
                # this in the gateway status footer; the historian still
                # records all readings (GOOD + BAD quality).
                partial = getattr(self, "_last_partial_error", "") or ""
                self.last_error = partial or None
                persisted_local = False
                persisted_edge_record_id: str | None = None
                if self._collection_gate_cb:
                    collection_allowed, block_reason = self._collection_gate_cb(self.gateway_id, readings)
                else:
                    collection_allowed, block_reason = self._is_collection_allowed(readings)
                self.collection_blocked = not collection_allowed
                self.collection_block_reason = block_reason
                _t_tel0 = time.monotonic()
                if collection_allowed:
                    # Durable local commit is the definition of successful collection.
                    # If this fails, we keep runtime alive but we do not mark this cycle as collected.
                    try:
                        from app.state import telemetry_service, app_store  # local import avoids circular import timing

                        now_mono = time.monotonic()
                        if now_mono - self._telemetry_runtime_refresh_monotonic >= 10.0:
                            try:
                                # get_bootstrap does a full DB read + JSON decode
                                # and acquires app_store._lock, which a cloud-sync
                                # thread can hold for seconds. Running it INLINE on
                                # the event loop stalled the loop long enough to
                                # trip the wedge-watchdog's "event loop stale" kill
                                # (observed 26 s stalls -> whole process respawned
                                # -> "running but no data"). Offload to a thread.
                                # On the gateway's DEDICATED executor, not the
                                # shared anyio pool — the shared pool is also used
                                # by API handlers + cloud sync and can starve,
                                # queuing this call for 100s of ms and dragging
                                # the cycle over its interval.
                                bootstrap = await self._run_collection_io(
                                    lambda: app_store.get_bootstrap(prefer_cloud_reads=False) or {}
                                )
                                telemetry_service.configure_from_bootstrap(bootstrap)
                            except Exception:
                                pass
                            self._telemetry_runtime_refresh_monotonic = now_mono

                        # DEDICATED executor (not shared anyio pool). This write
                        # gates the chart write below, so shared-pool queuing here
                        # directly delayed every chart update; isolating it keeps
                        # the cycle on-cadence.
                        ok, err, edge_record_id = await self._run_collection_io(
                            lambda: telemetry_service.record_collection_cycle(
                                gateway_id=self.gateway_id,
                                config=self.config,
                                readings=readings,
                                collection_status="ok",
                            )
                        )
                        persisted_local = bool(ok)
                        persisted_edge_record_id = edge_record_id
                        # Metric: time from start() to the FIRST durable row. This
                        # is the true "boot to data" latency — status shows
                        # running well before this, so it's the number that
                        # matters for the "running but no data" complaint.
                        if ok and not self._first_row_logged:
                            self._first_row_logged = True
                            boot0 = self._startup_started_monotonic or 0.0
                            if boot0 > 0:
                                _GW_LOG.info(
                                    "first-row gateway=%s boot_to_first_row=%.2fs",
                                    self.gateway_id, time.monotonic() - boot0,
                                )
                        if not ok:
                            # Telemetry/cloud-outbox write failed. Surface it, but
                            # DO NOT block the cycle: the historian (chart) DB is
                            # independent and healthy, so charts must still get
                            # this sample. Only the cloud outbox missed it (it
                            # re-syncs from the historian). Leaving collection_allowed
                            # True keeps _broadcast writing the chart row.
                            self.last_error = f"Cloud-record write failed: {err}"
                            _GW_LOG.warning(
                                "telemetry-write-fail gateway=%s reason=%s (charts unaffected)",
                                self.gateway_id, err,
                            )
                    except Exception as exc:
                        # Same policy: a telemetry-write exception must not blank
                        # the charts. Log and continue with collection_allowed True.
                        self.last_error = f"Cloud-record write error: {exc}"
                        _GW_LOG.warning(
                            "telemetry-write-exception gateway=%s exc=%s (charts unaffected)",
                            self.gateway_id, exc,
                        )
                _tel_ms = (time.monotonic() - _t_tel0) * 1000.0
                _t_emit0 = time.monotonic()
                await emit_event(
                    {
                        "type": "reading",
                        "gateway_id": self.gateway_id,
                        "collection_allowed": collection_allowed,
                        "persisted_local": persisted_local,
                        "edge_record_id": persisted_edge_record_id,
                        "collection_block_reason": self.collection_block_reason,
                        "status": self.get_status().model_dump(),
                        "readings": [r.model_dump() for r in readings],
                    }
                )
                _emit_ms = (time.monotonic() - _t_emit0) * 1000.0
                _per_ms = 0.0
                if collection_allowed:
                    # DB sink writes can be blocking (sqlite/file/remote enqueue).
                    # Execute off-loop AND bound it — a hung sink (locked DB, dead
                    # remote) must not stall the cycle. On timeout we log and move
                    # on; the historian buffer + outbox already retry the data.
                    _t_per0 = time.monotonic()
                    try:
                        # DEDICATED persist pool — a slow/locked sink parks a
                        # persist worker, never a read worker, so the next read
                        # cycle still runs on cadence. On timeout we log and move
                        # on; the persist keeps draining on its own pool and the
                        # historian buffer + outbox already hold the data.
                        await asyncio.wait_for(
                            self._run_persist_io(self._persist_readings, readings),
                            timeout=max(5.0, self._read_timeout_s),
                        )
                    except asyncio.TimeoutError:
                        _GW_LOG.warning(
                            "persist-timeout gateway=%s — sink slow, cycle continues "
                            "(read loop unaffected; persist draining on its own pool)",
                            self.gateway_id,
                        )
                    _per_ms = (time.monotonic() - _t_per0) * 1000.0
                self._record_stage_ms(_read_ms, _tel_ms, _emit_ms, _per_ms)
            except Exception as exc:
                err_text = str(exc)
                # Suppress transient handshake errors during the post-start
                # grace window: many PLC drivers fail the very first read while
                # the session is being negotiated, then recover on the next
                # cycle. Surfacing the error immediately produces a misleading
                # "Device Fails" label even though the gateway is healthy.
                started_at = self._startup_started_monotonic or 0.0
                in_grace = started_at > 0 and (time.monotonic() - started_at) < self._startup_grace_seconds
                if not in_grace:
                    self.last_error = err_text
                    # Invariant A: every non-grace cycle failure goes into
                    # backend.log so the operator can reconstruct what
                    # happened without needing the worker's in-memory
                    # last_error string. We log at warning because the
                    # next cycle will likely recover (driver is
                    # self-healing); a sustained outage manifests as the
                    # watchdog firing, which logs at warning too.
                    _GW_LOG.warning(
                        "cycle-error gateway=%s exc=%s: %s",
                        self.gateway_id, type(exc).__name__, err_text,
                    )
                # Do not keep stale values visible when a read cycle fails.
                self.latest_readings = []
                await emit_event({"type": "error", "gateway_id": self.gateway_id, "message": err_text})
            # Operator 2026-06-20: interval safe-guards.
            #   - Floor at 200 ms. The 100 ms minimum was theoretical — actual
            #     cycles with 4 tags take 500-1000 ms because the per-cycle
            #     work (PLC read + 2-3 SQLite writes + WebSocket emit + 10s
            #     bootstrap refresh) saturates a single worker thread. Cycles
            #     below this floor will sleep ~10 ms and run as fast as the
            #     workload allows; clamping here matches the frontend.
            #   - Ceiling at 60 s. Above 60 s the operator probably meant
            #     "off" — surface as an error rather than silently allow
            #     a 10-minute cycle.
            #   - Track measured cycle duration. When the measured cycle is
            #     consistently > 1.5× the configured interval (3 consecutive
            #     cycles), surface a clear cadence-warning in last_error so
            #     the UI status footer shows "configured 100ms, actual 1010ms"
            #     without silently lying about throughput.
            # Operator 2026-06-25: cap raised to 1 hour (was 60s).
            # Users can legitimately configure 5-min / 30-min / 1-hour
            # cadences (slow process, audit-trail dumps, hourly totals).
            # Silently clamping to 60 s caused those workers to wake up
            # way too often and surface "no readings in 30s" stalls.
            configured_ms = max(200, min(3_600_000, int(self.config.interval_ms or 1000)))
            target_s = configured_ms / 1000.0
            # Stall threshold = max(30 s, 3 × cycle interval). 30 s
            # floor protects sub-second pollers from spurious restarts
            # on a single missed cycle; the multiplier covers slow
            # cadences — a 5-min interval is "stalled" only after 15 min
            # of silence, a 1-hour interval after 3 hours.
            self._stall_threshold_s = max(30.0, 3.0 * target_s)
            elapsed_s = max(0.0, time.monotonic() - cycle_started)
            self._measured_cycle_ms = elapsed_s * 1000.0
            # Structured cadence metric every ~60 cycles: measured cycle vs
            # configured interval, so drift is visible in the log without
            # waiting for an overrun streak.
            self._cycle_metric_count = (getattr(self, "_cycle_metric_count", 0) or 0) + 1
            if self._cycle_metric_count % 60 == 0:
                # Per-stage attribution (avg/max ms over the window) so a slow
                # cycle names its culprit: read (PLC), tel (telemetry+outbox
                # gate), emit (historian write + WS fanout), persist (sinks).
                win = getattr(self, "_stage_win", None) or {}
                n = max(1, int(win.get("n") or 0))
                s = win.get("sum") or [0.0] * 4
                m = win.get("max") or [0.0] * 4
                try:
                    pq = self._persist_executor._work_queue.qsize() if self._persist_executor else 0
                except Exception:
                    pq = -1
                _GW_LOG.info(
                    "cadence gateway=%s cycle_ms=%.0f interval_ms=%d tags=%d "
                    "read_ms=%.0f/%.0f tel_ms=%.0f/%.0f emit_ms=%.0f/%.0f "
                    "persist_ms=%.0f/%.0f persist_q=%d",
                    self.gateway_id, self._measured_cycle_ms, configured_ms,
                    len(self.config.tags or []),
                    s[0] / n, m[0], s[1] / n, m[1], s[2] / n, m[2], s[3] / n, m[3], pq,
                )
                self._stage_win = None  # reset the window
            if elapsed_s * 1000.0 > configured_ms * 1.5:
                self._cycle_overrun_streak = (getattr(self, "_cycle_overrun_streak", 0) or 0) + 1
            else:
                self._cycle_overrun_streak = 0
            if self._cycle_overrun_streak >= 3:
                # Surface a cadence-warning. Doesn't override a real error;
                # the run loop's `last_error` may already carry a partial
                # tag failure, in which case we leave it alone.
                if not self.last_error or "cadence" not in self.last_error.lower():
                    self.last_error = (
                        f"Cadence warning: configured {configured_ms} ms, actual "
                        f"{elapsed_s*1000:.0f} ms — collection is running as fast as "
                        f"the workload allows. Raise the interval or reduce tag count "
                        f"to hit the target."
                    )
            await asyncio.sleep(max(0.01, target_s - elapsed_s))

    def _is_collection_allowed(self, readings: List[GatewayReading]) -> tuple[bool, str | None]:
        triggers = [t for t in (self.config.collection_triggers or []) if bool(t.get("enabled", True))]
        if not triggers:
            return True, None

        latest_by_tag = {str(r.tag_name or "").strip().lower(): r for r in readings}

        def _cmp(value: float, operator: str, threshold: float) -> bool:
            if operator == "<":
                return value < threshold
            if operator == "<=":
                return value <= threshold
            if operator == ">":
                return value > threshold
            if operator == ">=":
                return value >= threshold
            return False

        hit = False
        for tr in triggers:
            tag = str(tr.get("tag_name") or "").strip().lower()
            if not tag:
                continue
            reading = latest_by_tag.get(tag)
            if not reading:
                continue
            # A failed read (value None / BAD quality) must never satisfy a
            # trigger — absence of data is not a threshold crossing.
            if reading.value is None:
                continue
            try:
                threshold = float(tr.get("value"))
                op = str(tr.get("operator") or ">=").strip()
                if _cmp(float(reading.value), op, threshold):
                    hit = True
                    break
            except Exception:
                continue

        if hit:
            return True, None
        return False, "Trigger condition is FALSE (collection/write paused)."

    def _get_read_tags(self) -> List[str]:
        tags: List[str] = []
        seen: Set[str] = set()

        def _add(tag_raw: str) -> None:
            tag = str(tag_raw or "").strip()
            if not tag:
                return
            key = tag.lower()
            if key in seen:
                return
            seen.add(key)
            tags.append(tag)

        for t in (self.config.tags or []):
            _add(t)

        # Always monitor local trigger source tags in real-time, even if user
        # did not include them in the main gateway tag list.
        for tr in (self.config.collection_triggers or []):
            if not bool(tr.get("enabled", True)):
                continue
            trig_gid = str(tr.get("gateway_id") or "").strip()
            if trig_gid and trig_gid != self.gateway_id:
                continue
            _add(str(tr.get("tag_name") or ""))

        return tags

    def _read_from_gateway(self) -> List[GatewayReading]:
        gateway_type = (self.config.gateway_type or "").strip().lower()
        if gateway_type == "siemens_opcua":
            readings = self._read_from_opcua()
        elif gateway_type == "allen_bradley":
            readings = self._read_from_allen_bradley()
        elif gateway_type == "siemens_snap7":
            readings = self._read_from_snap7()
        else:
            raise RuntimeError(f"Gateway type '{self.config.gateway_type}' is not implemented for real-time reads.")
        # AUTOMATED string identification (2026-07-26): when a driver didn't
        # declare a type but the reading is text-only (value None, value_text
        # set, quality GOOD), stamp it STRING so every consumer — historian
        # Type column, dashboards, batches, reports — branches text-vs-numeric
        # from ONE canonical field instead of guessing per page.
        for r in readings or []:
            try:
                if not r.data_type and r.value is None and r.value_text is not None and r.quality >= 192:
                    r.data_type = "STRING"
            except Exception:
                pass
        return readings

    def _prewarm_client(self) -> None:
        """Open the PLC driver session before the run loop starts.

        Called from start() via asyncio.to_thread so the handshake +
        init_tags cost is paid during the Start button click, not on
        the first chart tick. The next `_ensure_*_client()` inside the
        run loop returns the cached instance and the first read cycle
        completes in milliseconds. Failure is non-fatal: the run loop
        will reconnect on its own — we just don't get the speedup.
        """
        gateway_type = (self.config.gateway_type or "").strip().lower()
        if gateway_type == "allen_bradley":
            ip = (self.config.plc_ip or "").strip()
            if not ip:
                return
            try:
                from pycomm3 import LogixDriver  # type: ignore
            except Exception:
                return
            path = ip if "/" in ip else ip
            self._ensure_ab_pycomm3_client(path, LogixDriver)
            return
        if gateway_type == "siemens_opcua":
            url = (self.config.opc_url or "").strip()
            if not url:
                return
            try:
                self._ensure_opc_client(url)
            except Exception:
                return
            return
        # snap7 + others: skip — their driver state machines may not
        # tolerate an out-of-band open here.

    def _coerce_value(self, raw: Any, tag_name: str) -> tuple[float | None, str | None]:
        """Return (numeric_value, text_value).

        Numeric tags return (float, None).

        TEXT-typed values (PLC STRING, OPC-UA String/ByteString, smart-meter
        strings) ALWAYS return their text, and return a numeric ONLY when the
        text genuinely parses as a number:
            'BT-RC2026-002'  -> (None, 'BT-RC2026-002')
            '77'             -> (77.0, '77')
        Two rules matter here:
          * the text is NEVER discarded. Previously a numeric-looking string
            such as '77' was stored as 77.0 with value_text=NULL, so the
            moment it became '77A' the tag silently flipped to 0.0 — a fake
            discontinuity in history.
          * a non-numeric string NEVER fabricates 0.0. It used to, which made
            a status string chart as a flat zero line and dragged AVG/MIN/MAX
            aggregates to meaningless zeros. NULL = "not a number", so charts
            show a gap and aggregates correctly skip it.
        """
        if raw is None:
            raise RuntimeError(f"Tag '{tag_name}' returned null value.")
        if isinstance(raw, bool):
            return (1.0 if raw else 0.0, None)
        if isinstance(raw, (int, float)):
            return (float(raw), None)
        if isinstance(raw, (bytes, bytearray)):
            try:
                txt = bytes(raw).decode("utf-8", errors="replace")
            except Exception:
                txt = repr(raw)
            return (self._opt_float(txt), txt)
        text = str(raw).strip()
        return (self._opt_float(text), text)

    @staticmethod
    def _opt_float(text: str) -> float | None:
        """float(text) when it parses, else None. Never fabricates a zero."""
        try:
            val = float(str(text).strip())
        except Exception:
            return None
        return val if val == val and val not in (float("inf"), float("-inf")) else None

    def _coerce_value_to_float(self, raw: Any, tag_name: str) -> float:
        if raw is None:
            raise RuntimeError(f"Tag '{tag_name}' returned null value.")
        if isinstance(raw, bool):
            return 1.0 if raw else 0.0
        if isinstance(raw, (int, float)):
            return float(raw)
        text = str(raw).strip()
        try:
            return float(text)
        except Exception as exc:
            raise RuntimeError(f"Tag '{tag_name}' is non-numeric: {raw!r}") from exc

    def _read_from_allen_bradley(self) -> List[GatewayReading]:
        ip = (self.config.plc_ip or "").strip()
        if not ip:
            raise RuntimeError("Allen-Bradley read failed: PLC IP is empty.")
        tags = self._get_read_tags()
        if not tags:
            raise RuntimeError("Allen-Bradley read failed: no tags configured.")
        candidate_paths = [ip] if "/" in ip else [ip, f"{ip}/1"]
        if self._ab_preferred_path:
            candidate_paths = [self._ab_preferred_path] + [p for p in candidate_paths if p != self._ab_preferred_path]

        # Primary: pycomm3 LogixDriver (ControlLogix/CompactLogix family).
        try:
            return self._read_from_allen_bradley_pycomm3(candidate_paths, tags)
        except Exception as pycomm3_exc:
            # Fallback: pylogix works with some AB targets where pycomm3 fails
            # during PLC-info handshake ("Failed to get PLC info").
            try:
                return self._read_from_allen_bradley_pylogix(candidate_paths, tags)
            except Exception as pylogix_exc:
                raise RuntimeError(
                    f"Allen-Bradley read failed. pycomm3='{pycomm3_exc}'; pylogix='{pylogix_exc}'"
                ) from pylogix_exc

    def _read_from_allen_bradley_pycomm3(self, candidate_paths: List[str], tags: List[str]) -> List[GatewayReading]:
        try:
            from pycomm3 import LogixDriver  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"pycomm3 unavailable: {exc}") from exc

        per_route_errors: list[str] = []

        def _norm_tag(t: str) -> str:
            return str(t or "").strip().replace(" ", "").lower()

        # Reduce a configured tag name to its controller-tag base so we
        # can validate it against pycomm3's `plc.tags` dict, which only
        # carries base names. A user-configured `SimREAL[3]` needs to
        # match the controller's `SimREAL` (array tag), and
        # `MyStruct.field[2]` should match `MyStruct`. We strip array
        # subscripts from every segment and take the first one.
        import re as _re
        def _tag_base_for_validation(t: str) -> str:
            cleaned = _re.sub(r"\[[^\]]*\]", "", str(t or "").strip())
            # PROGRAM-scoped tags are keyed by pycomm3 as
            # "Program:<ProgName>.<TagName>", so the root is the FIRST TWO
            # segments — splitting on the first '.' would yield the useless
            # "Program:MainProgram". Everything after that is struct members.
            if cleaned.startswith("Program:"):
                parts = cleaned.split(".")
                return ".".join(parts[:2]) if len(parts) >= 2 else cleaned
            # Controller-scoped: the root is everything before the first '.'.
            return cleaned.split(".", 1)[0]

        for path in candidate_paths:
            try:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                out: List[GatewayReading] = []
                # Keep AB cycle lightweight: reuse session between polls.
                plc = self._ensure_ab_pycomm3_client(path, LogixDriver)
                known_tags = set()
                if isinstance(getattr(plc, "tags", None), dict):
                    known_tags = {str(k).strip() for k in plc.tags.keys() if str(k).strip()}
                if known_tags:
                    # Compare BASE names (sans [N] subscripts and .members)
                    # against pycomm3's tag dict. The actual read further
                    # below uses the full configured string, so out-of-
                    # range subscripts (e.g. SimDINT[99] when the array
                    # is size 10) still surface as a per-tag read error
                    # — they just don't fail this presence check.
                    # Program-scoped tags ARE validated now that we connect
                    # with init_program_tags=True, so a mistyped program tag
                    # surfaces immediately instead of silently logging 0.0.
                    # Guard: if the controller returned no program tags at all
                    # (older firmware / permission), skip validating them
                    # rather than failing every poll.
                    has_program_tags = any(k.startswith("Program:") for k in known_tags)
                    def _validatable(t: str) -> bool:
                        if str(t or "").strip().startswith("Program:"):
                            return has_program_tags
                        return True
                    missing = [
                        t for t in tags
                        if _validatable(t) and _tag_base_for_validation(t) not in known_tags
                    ]
                    if missing:
                        raise RuntimeError(
                            f"Configured AB tags not found in controller ({len(missing)}): {', '.join(missing[:8])}"
                        )

                # Operator 2026-06-24: benchmark proved single plc.read(
                # *tags) is fastest (~11 ms avg vs 21 ms batched) and
                # never stalls when run in isolation. The stalls we
                # saw in the edge app come from the watchdog restart
                # sequence, not from the driver itself. Keep this
                # simple.
                try:
                    results = plc.read(*tags)
                except Exception:
                    # One reconnect attempt on broken/stale session.
                    self._close_ab_pycomm3_client()
                    plc = self._ensure_ab_pycomm3_client(path, LogixDriver)
                    results = plc.read(*tags)
                if not isinstance(results, list):
                    results = [results]
                if not results:
                    raise RuntimeError("no responses")
                if len(results) != len(tags):
                    raise RuntimeError(f"requested {len(tags)} tags but got {len(results)} results")
                # Per-tag handling. The previous behaviour was to raise on
                # the FIRST tag with an error, which threw away every good
                # reading in the same batch. Real PLCs often have one or
                # two bad tags in a list (out-of-range subscript, renamed
                # member, etc.) — losing the other 10 readings means the
                # historian shows zero data even though most of the poll
                # succeeded. Now we emit the good readings with quality
                # GOOD and the bad ones with quality BAD + a value_text
                # carrying the error. Operators see the failure in the
                # historian without losing the rest of the cycle.
                tag_errors: list[str] = []
                for idx, res in enumerate(results):
                    requested_tag = tags[idx]
                    reported_tag = str(getattr(res, "tag", "") or "")
                    if reported_tag and _norm_tag(reported_tag) != _norm_tag(requested_tag):
                        # Mismatch is a protocol-level issue, not a per-tag
                        # one — bail out so the legacy diagnostics surface.
                        raise RuntimeError(
                            f"read mismatch on route {path}: requested '{requested_tag}' but got '{reported_tag}'"
                        )
                    status = str(getattr(res, "error", None) or getattr(res, "status", "") or "")
                    if status:
                        tag_errors.append(f"{requested_tag}: {status}")
                        quality, quality_label = self._normalize_quality(self.config.gateway_type, raw_quality=0)
                        out.append(
                            GatewayReading(
                                ts_utc=ts,
                                tag_name=requested_tag,
                                # NULL, never 0.0 — a failed read is ABSENCE of
                                # data, not a real zero. Storing 0.0 made a
                                # broken tag look like a legitimate flat-zero
                                # trend on charts and in reports. The error
                                # text + BAD quality carry the diagnosis.
                                value=None,
                                value_text=status,
                                quality=quality,
                                quality_label=quality_label,
                                source=self.config.gateway_type,
                                site=self.config.site,
                                area=self.config.area,
                                equipment=self.config.equipment,
                            )
                        )
                        continue
                    value, value_text = self._coerce_value(getattr(res, "value", None), requested_tag or "<unknown>")
                    quality, quality_label = self._normalize_quality(self.config.gateway_type, raw_quality=192)
                    out.append(
                        GatewayReading(
                            ts_utc=ts,
                            tag_name=requested_tag,
                            value=value,
                            value_text=value_text,
                            # PLC-declared type from pycomm3 (DINT/REAL/STRING/
                            # BOOL/UDT name). Feeds the historian Type column
                            # and text-vs-numeric branching everywhere.
                            data_type=str(getattr(res, "type", "") or ""),
                            quality=quality,
                            quality_label=quality_label,
                            source=self.config.gateway_type,
                            site=self.config.site,
                            area=self.config.area,
                            equipment=self.config.equipment,
                        )
                    )
                # Surface per-tag errors via the last_error field but keep
                # the GOOD readings. The route is still considered
                # successful as long as at least one tag came back clean.
                good_count = sum(1 for r in out if r.quality >= 192)
                if good_count == 0:
                    raise RuntimeError(
                        f"every tag failed on route {path}: " + "; ".join(tag_errors[:6])
                    )
                if tag_errors:
                    # Stash a one-line summary so it's visible in the
                    # gateway status block without preventing collection.
                    self._last_partial_error = (
                        f"{len(tag_errors)} bad tag(s): " + "; ".join(tag_errors[:4])
                    )
                else:
                    self._last_partial_error = ""
                self._ab_preferred_path = path
                return out
            except Exception as exc:
                if self._ab_pycomm3_path == path:
                    self._close_ab_pycomm3_client()
                # Accumulate per-route errors so the operator sees WHY
                # each route failed instead of just the last one (a
                # later route's "Failed to get PLC info" used to hide
                # the real "Tag doesn't exist" error from route #1).
                per_route_errors.append(f"{path}: {exc}")
                continue
        self._ab_preferred_path = None
        raise RuntimeError("all route attempts failed -> " + " | ".join(per_route_errors))

    def _read_from_allen_bradley_pylogix(self, candidate_paths: List[str], tags: List[str]) -> List[GatewayReading]:
        try:
            from pylogix import PLC  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"pylogix unavailable: {exc}") from exc

        last_error = ""
        base_ip = (self.config.plc_ip or "").strip().split("/", 1)[0]
        slots: List[int] = []
        for p in candidate_paths:
            slot = 0
            if "/" in p:
                try:
                    slot = int(str(p).split("/", 1)[1].strip())
                except Exception:
                    slot = 0
            if slot not in slots:
                slots.append(slot)
        if 0 not in slots:
            slots.append(0)

        for slot in slots:
            comm = None
            try:
                comm = self._ensure_ab_pylogix_client(base_ip, slot, PLC)
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                out: List[GatewayReading] = []
                results = comm.Read(tags)
                if not isinstance(results, list):
                    results = [results]
                if len(results) != len(tags):
                    raise RuntimeError(f"requested {len(tags)} tags but got {len(results)} results")
                # Same skip-bad-tag treatment as the pycomm3 path: one
                # out-of-range subscript shouldn't poison every other tag
                # in the batch. Bad tags are emitted with quality=0 (BAD)
                # so the historian still records the cycle.
                tag_errors: list[str] = []
                for idx, res in enumerate(results):
                    tag = tags[idx]
                    status = str(getattr(res, "Status", "") or "")
                    status_ok = status.strip().lower() in ("success", "ok", "0")
                    if not status_ok:
                        tag_errors.append(f"{tag}: {status}")
                        quality, quality_label = self._normalize_quality(self.config.gateway_type, raw_quality=0)
                        out.append(
                            GatewayReading(
                                ts_utc=ts,
                                tag_name=tag,
                                # NULL, not 0.0 — a failed read is absence of data.
                                value=None,
                                value_text=status,
                                quality=quality,
                                quality_label=quality_label,
                                source=self.config.gateway_type,
                                site=self.config.site,
                                area=self.config.area,
                                equipment=self.config.equipment,
                            )
                        )
                        continue
                    value, value_text = self._coerce_value(getattr(res, "Value", None), tag)
                    quality, quality_label = self._normalize_quality(self.config.gateway_type, raw_quality=192)
                    out.append(
                        GatewayReading(
                            ts_utc=ts,
                            tag_name=tag,
                            value=value,
                            value_text=value_text,
                            data_type=str(getattr(res, "Type", "") or getattr(res, "type", "") or ""),
                            quality=quality,
                            quality_label=quality_label,
                            source=self.config.gateway_type,
                            site=self.config.site,
                            area=self.config.area,
                            equipment=self.config.equipment,
                        )
                    )
                good_count = sum(1 for r in out if r.quality >= 192)
                if good_count == 0:
                    raise RuntimeError(
                        f"every tag failed on slot {slot}: " + "; ".join(tag_errors[:6])
                    )
                if tag_errors:
                    self._last_partial_error = (
                        f"{len(tag_errors)} bad tag(s): " + "; ".join(tag_errors[:4])
                    )
                else:
                    self._last_partial_error = ""
                return out
            except Exception as exc:
                if self._ab_pylogix_ip == base_ip and self._ab_pylogix_slot == slot:
                    self._close_ab_pylogix_client()
                last_error = str(exc)
                continue
        raise RuntimeError(f"all slot attempts failed ({', '.join(str(s) for s in slots)}): {last_error}")

    def _parse_snap7_tag(self, raw_tag: str) -> tuple[str, int, int, str, int]:
        # Supported forms:
        # DB1,REAL0 | DB1,DINT4 | DB1,INT2 | DB1,WORD8 | DB1,BIT10.3 | DB1,BYTE12
        # M10.0 / M10.1, I0.0, Q0.0, MB10, MW12, MD20, IB0, IW2, QB4
        tag = str(raw_tag or "").strip().upper().replace(" ", "")
        if not tag:
            raise ValueError("Empty tag")

        if tag.startswith("DB"):
            left_right = tag.split(",", 1)
            if len(left_right) != 2:
                raise ValueError("DB tag format must be DB<number>,<type><offset>")
            db_no_txt = left_right[0][2:]
            spec = left_right[1]
            db_no = int(db_no_txt)
            if spec.startswith("BIT"):
                rest = spec[3:]
                byte_txt, bit_txt = rest.split(".", 1)
                byte_idx = int(byte_txt)
                bit_idx = int(bit_txt)
                if bit_idx < 0 or bit_idx > 7:
                    raise ValueError("BIT index must be 0..7")
                return ("DB", db_no, byte_idx, "BIT", bit_idx)
            if spec.startswith("REAL"):
                return ("DB", db_no, int(spec[4:]), "REAL", 0)
            if spec.startswith("DINT"):
                return ("DB", db_no, int(spec[4:]), "DINT", 0)
            if spec.startswith("DWORD"):
                return ("DB", db_no, int(spec[5:]), "DWORD", 0)
            if spec.startswith("INT"):
                return ("DB", db_no, int(spec[3:]), "INT", 0)
            if spec.startswith("WORD"):
                return ("DB", db_no, int(spec[4:]), "WORD", 0)
            if spec.startswith("BYTE"):
                return ("DB", db_no, int(spec[4:]), "BYTE", 0)
            raise ValueError("Unsupported DB type; use BIT/REAL/DINT/DWORD/INT/WORD/BYTE")

        area_prefix = tag[0]
        if area_prefix not in ("M", "I", "Q"):
            raise ValueError("Snap7 tag must start with DB, M, I, or Q")
        if "." in tag and len(tag) >= 4:
            byte_txt, bit_txt = tag[1:].split(".", 1)
            return (area_prefix, 0, int(byte_txt), "BIT", int(bit_txt))
        if len(tag) >= 3 and tag[1] in ("B", "W", "D"):
            width = tag[1]
            byte_idx = int(tag[2:])
            if width == "B":
                return (area_prefix, 0, byte_idx, "BYTE", 0)
            if width == "W":
                return (area_prefix, 0, byte_idx, "WORD", 0)
            return (area_prefix, 0, byte_idx, "DWORD", 0)
        # Default M10/I0/Q4 as BYTE.
        return (area_prefix, 0, int(tag[1:]), "BYTE", 0)

    def _read_from_snap7(self) -> List[GatewayReading]:
        try:
            import snap7  # type: ignore
            from snap7.util import get_bool, get_real, get_dint, get_int, get_word, get_dword  # type: ignore
            from snap7.type import Areas  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"Siemens Snap7 reader unavailable (python-snap7 missing): {exc}") from exc

        ip = (self.config.plc_ip or "").strip()
        if not ip:
            raise RuntimeError("Siemens Snap7 read failed: PLC IP is empty.")
        tags = self._get_read_tags()
        if not tags:
            raise RuntimeError("Siemens Snap7 read failed: no tags configured.")

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        client = snap7.client.Client()
        # Operator 2026-06-23 (Item 10): bound Snap7 connect+send+recv so
        # a half-open S7 session cannot hang the cycle for >8s. The
        # snap7 ParamNumber.PingTimeout / SendTimeout / RecvTimeout
        # values are in milliseconds.
        try:
            from snap7.type import Parameter  # type: ignore  # newer python-snap7
            client.set_param(Parameter.PingTimeout, 2000)
            client.set_param(Parameter.SendTimeout, 4000)
            client.set_param(Parameter.RecvTimeout, 8000)
        except Exception:
            # older python-snap7 layouts: ParamNumber lives elsewhere or
            # the param ids are exposed as ints. Best-effort only — the
            # watchdog still catches anything that escapes this.
            try:
                client.set_param(3, 2000)   # PingTimeout
                client.set_param(7, 4000)   # SendTimeout
                client.set_param(8, 8000)   # RecvTimeout
            except Exception:
                pass
        out: List[GatewayReading] = []
        try:
            # Standard rack/slot for S7-1200/1500; make env-configurable.
            rack = int(os.environ.get("TRUSTNODE_S7_RACK", "0"))
            slot = int(os.environ.get("TRUSTNODE_S7_SLOT", "1"))
            client.connect(ip, rack, slot)
            if not client.get_connected():
                raise RuntimeError(f"Unable to establish Snap7 session to {ip} (rack={rack}, slot={slot}).")

            for raw_tag in tags:
                try:
                    area, db_no, byte_idx, dtype, bit_idx = self._parse_snap7_tag(raw_tag)
                    if area == "DB":
                        size = 4 if dtype in ("REAL", "DINT", "DWORD") else 2 if dtype in ("INT", "WORD") else 1
                        data = client.db_read(db_no, byte_idx, size)
                    else:
                        area_code = Areas.MK if area == "M" else Areas.PE if area == "I" else Areas.PA
                        size = 4 if dtype == "DWORD" else 2 if dtype == "WORD" else 1
                        data = client.read_area(area_code, 0, byte_idx, size)

                    if dtype == "BIT":
                        val = 1.0 if get_bool(data, 0, bit_idx) else 0.0
                    elif dtype == "REAL":
                        val = float(get_real(data, 0))
                    elif dtype == "DINT":
                        val = float(get_dint(data, 0))
                    elif dtype == "DWORD":
                        val = float(get_dword(data, 0))
                    elif dtype == "INT":
                        val = float(get_int(data, 0))
                    elif dtype == "WORD":
                        val = float(get_word(data, 0))
                    else:
                        val = float(data[0])

                    quality, quality_label = self._normalize_quality(self.config.gateway_type, raw_status=0)
                    out.append(
                        GatewayReading(
                            ts_utc=ts,
                            tag_name=raw_tag,
                            value=val,
                            quality=quality,
                            quality_label=quality_label,
                            source=self.config.gateway_type,
                            site=self.config.site,
                            area=self.config.area,
                            equipment=self.config.equipment,
                        )
                    )
                except Exception as tag_exc:
                    raise RuntimeError(f"Snap7 read failed for '{raw_tag}': {tag_exc}") from tag_exc
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
        if not out:
            raise RuntimeError("Siemens Snap7 read failed: no values were read.")
        return out

    def _read_from_opcua(self) -> List[GatewayReading]:
        try:
            from opcua import Client  # noqa: F401  imported for clear error message  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"OPC-UA client not installed: {exc}") from exc

        endpoint = (self.config.opc_url or "").strip() or f"opc.tcp://{self.config.plc_ip.strip()}:4840"
        requested_ids = split_requested_identifiers(self._get_read_tags())
        if not requested_ids:
            raise RuntimeError("OPC-UA read failed: no node ids/tags configured.")

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        out: List[GatewayReading] = []

        # Persistent session: connect once, reuse across polls. Reconnect
        # only on failure. Mirrors the AB pycomm3 driver pattern.
        try:
            client = self._ensure_opc_client(endpoint)
        except Exception as exc:
            self._close_opc_client()
            raise RuntimeError(f"OPC-UA connect failed for {endpoint}: {exc}") from exc

        try:
            # Resolve once per (endpoint, tag-set). Cached across polls so
            # the per-tag node lookup doesn't happen every second.
            cache_key = f"{endpoint}|{'|'.join(requested_ids)}"
            resolved_targets: list[tuple[str, str]]
            unresolved_items: list[str] = []
            if self._opc_resolve_cache_key == cache_key and self._opc_resolved_targets:
                resolved_targets = self._opc_resolved_targets
            else:
                resolved, unresolved_items = resolve_requested_nodes(client, requested_ids)
                resolved_targets = [(t.requested, t.resolved_node_id) for t in resolved]
                self._opc_resolved_targets = resolved_targets
                self._opc_resolve_cache_key = cache_key
            for unresolved in unresolved_items:
                out.append(
                    GatewayReading(
                        ts_utc=ts,
                        tag_name=unresolved,
                        # NULL, not 0.0 — a failed read is absence of data.
                        value=None,
                        quality=0,
                        quality_label="BAD",
                        source=self.config.gateway_type,
                        site=self.config.site,
                        area=self.config.area,
                        equipment=self.config.equipment,
                    )
                )
            if not resolved_targets:
                raise RuntimeError("OPC-UA read failed: no variable nodes resolved from configured tags.")

            # Batch read: one OPC-UA Read request for ALL nodes instead of
            # one round-trip per tag. With 2 tags this saves ~20ms; with
            # 50 tags it saves ~1 second per cycle.
            nodes = [client.get_node(node_id) for _, node_id in resolved_targets]
            data_values: list = []
            tag_errors: list[str] = []
            try:
                # The python-opcua API:
                #   Client.get_values(nodes)        -> list[Any]  (values only)
                #   ua_client.get_attributes(...)   -> data_values with StatusCode
                # We need StatusCode for quality, so prefer per-node
                # get_data_value() but in a single batched server call.
                # python-opcua's Node.get_data_value() under the hood
                # round-trips one Read; for true batching we use the
                # internal read helper.
                from opcua import ua  # type: ignore
                params = ua.ReadParameters()
                for node in nodes:
                    rv = ua.ReadValueId()
                    rv.NodeId = node.nodeid
                    rv.AttributeId = ua.AttributeIds.Value
                    params.NodesToRead.append(rv)
                result = client.uaclient.read(params)
                data_values = list(result) if result else []
            except Exception as exc:
                # Server doesn't accept batch read, or we hit a transient
                # protocol error. Fall back to per-node reads — slower
                # but never worse than the old code path. If THIS fails
                # too we'll catch it below and trigger a reconnect.
                data_values = []
                for node in nodes:
                    try:
                        data_values.append(node.get_data_value())
                    except Exception as inner:
                        tag_errors.append(f"{node.nodeid}: {inner}")
                        data_values.append(None)
                if tag_errors and not any(dv for dv in data_values):
                    # Wholesale failure → reconnect on next cycle.
                    self._close_opc_client()
                    raise RuntimeError(
                        f"OPC-UA batch read failed and per-node fallback also failed: {exc}; "
                        + "; ".join(tag_errors[:3])
                    ) from exc

            for (requested_tag, _node_id), data_value in zip(resolved_targets, data_values):
                if data_value is None:
                    out.append(
                        GatewayReading(
                            ts_utc=ts,
                            tag_name=requested_tag,
                            # NULL, not 0.0 — a failed read is absence of data.
                        value=None,
                            quality=0,
                            quality_label="BAD",
                            source=self.config.gateway_type,
                            site=self.config.site,
                            area=self.config.area,
                            equipment=self.config.equipment,
                        )
                    )
                    continue
                try:
                    value = data_value.Value.Value
                    status_name = str(data_value.StatusCode.name) if data_value.StatusCode else ""
                    quality, quality_label = self._normalize_quality(
                        self.config.gateway_type,
                        raw_status=status_name,
                    )
                    if value is None:
                        # No value from the server: absence, not zero.
                        num_val, txt_val = None, None
                    else:
                        try:
                            num_val, txt_val = self._coerce_value(value, requested_tag)
                        except Exception:
                            # Unparseable payload — keep the text, no fake zero.
                            num_val, txt_val = None, str(value)
                    out.append(
                        GatewayReading(
                            ts_utc=ts,
                            tag_name=requested_tag,
                            value=num_val,
                            value_text=txt_val,
                            quality=quality,
                            quality_label=quality_label,
                            source=self.config.gateway_type,
                            site=self.config.site,
                            area=self.config.area,
                            equipment=self.config.equipment,
                        )
                    )
                except Exception as exc:
                    tag_errors.append(f"{requested_tag}: {exc}")
                    out.append(
                        GatewayReading(
                            ts_utc=ts,
                            tag_name=requested_tag,
                            # NULL, not 0.0 — a failed read is absence of data.
                        value=None,
                            value_text=str(exc),
                            quality=0,
                            quality_label="BAD",
                            source=self.config.gateway_type,
                            site=self.config.site,
                            area=self.config.area,
                            equipment=self.config.equipment,
                        )
                    )
            # Surface per-tag failures the same way the AB path does. Do
            # NOT raise — keep the GOOD readings flowing.
            good_count = sum(1 for r in out if r.quality >= 192)
            if good_count == 0:
                # Every tag failed → close the session so next cycle
                # reconnects cleanly.
                self._close_opc_client()
                raise RuntimeError(
                    "Every OPC-UA tag failed: " + "; ".join(tag_errors[:6])
                )
            self._last_partial_error = (
                f"{len(tag_errors)} bad OPC-UA tag(s): " + "; ".join(tag_errors[:4])
            ) if tag_errors else ""
            return out
        except Exception:
            # Any exception that escapes here means the session might be
            # in a bad state. Drop it; next cycle will reconnect.
            self._close_opc_client()
            raise

    def _normalize_quality(self, gateway_type: str, raw_quality: Any = None, raw_status: Any = None) -> tuple[int, str]:
        gt = (gateway_type or "").strip().lower()
        if isinstance(raw_quality, int):
            return self._quality_pair(max(0, min(255, raw_quality)))
        if isinstance(raw_quality, bool):
            return self._quality_pair(192 if raw_quality else 0)
        if gt == "siemens_opcua" and isinstance(raw_status, str):
            s = raw_status.strip().lower()
            if "good" in s:
                return 192, "GOOD"
            if "uncertain" in s:
                return 64, "UNCERTAIN"
            if s:
                return 0, "BAD"
        if isinstance(raw_status, int):
            return self._quality_pair(192 if raw_status == 0 else 0)
        return 192, "GOOD"

    def _quality_pair(self, q: int) -> tuple[int, str]:
        if q >= 192:
            return q, "GOOD"
        if q >= 64:
            return q, "UNCERTAIN"
        return q, "BAD"

    def _mark_db_write_success(self, count: int) -> None:
        self.db_write_count += max(0, int(count))
        self.db_last_write_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.db_last_error = None
        self.last_error = None

    def _mark_db_write_error(self, msg: str) -> None:
        text = str(msg or "")
        self.db_last_error = text
        # Keep transient remote timeout errors visible for diagnostics, but do not
        # downgrade runtime state to hard "DB fails" while store-forward is active.
        s = text.lower()
        transient = (
            "connectiontimeout" in s
            or "connection timeout" in s
            or "timed out" in s
            or "read timeout" in s
            or "could not connect" in s
            or "network is unreachable" in s
        )
        self.last_error = None if transient else text

    # ----------------------------------------------------------------
    # L3c: sink circuit breaker
    # ----------------------------------------------------------------
    def _sink_breaker_skip(self, sink_id: str) -> bool:
        """Return True if writes to this sink should be skipped right now."""
        sid = str(sink_id or "").strip()
        if not sid:
            return False
        open_until = self._sink_breaker_open_until_mono.get(sid, 0.0)
        if open_until and time.monotonic() < open_until:
            return True
        # Cooldown elapsed (or breaker never opened) — let one probe through.
        if open_until and time.monotonic() >= open_until:
            self._sink_breaker_open_until_mono.pop(sid, None)
            self._sink_breaker_fails[sid] = 0
        return False

    def _sink_breaker_record_success(self, sink_id: str) -> None:
        sid = str(sink_id or "").strip()
        if not sid:
            return
        if sid in self._sink_breaker_fails:
            self._sink_breaker_fails[sid] = 0
        self._sink_breaker_open_until_mono.pop(sid, None)

    def _sink_breaker_record_failure(self, sink_id: str, label: str = "") -> None:
        sid = str(sink_id or "").strip()
        if not sid:
            return
        n = self._sink_breaker_fails.get(sid, 0) + 1
        self._sink_breaker_fails[sid] = n
        if n >= self._SINK_BREAKER_THRESHOLD:
            self._sink_breaker_open_until_mono[sid] = time.monotonic() + self._SINK_BREAKER_COOLDOWN_S
            # Surface so the UI banner picks it up.
            self._mark_db_write_error(
                f"Sink '{label or sid}' circuit-breaker tripped after {n} consecutive errors; "
                f"writes paused for {int(self._SINK_BREAKER_COOLDOWN_S)}s. "
                f"Local collection continues."
            )

    def _dispose_db_engine(self) -> None:
        if self._db_engine is not None:
            try:
                self._db_engine.dispose()
            except Exception:
                pass
        self._db_engine = None
        self._db_engine_key = ""
        self._db_schema_ready_key = ""

    def _needs_program_tags(self) -> bool:
        """True when this gateway actually polls PROGRAM-scoped tags.

        Fetching the program-tag database at connect is expensive (a separate
        CIP request sequence per program), so we only pay it when at least one
        configured tag is "Program:...". Controller-only gateways connect as
        fast as they always did.
        """
        for t in (self.config.tags or []):
            if str(t or "").strip().startswith("Program:"):
                return True
        return False

    def _ensure_ab_pycomm3_client(self, path: str, logix_driver_cls):
        if self._ab_pycomm3_client is not None and self._ab_pycomm3_path == path:
            return self._ab_pycomm3_client
        # Serialise connects. start() fires _prewarm_client in a thread AND the
        # run loop's first cycle calls this from another thread; without a lock
        # they race and BOTH open a LogixDriver, doubling the tag-database
        # fetch against the PLC (and sometimes getting one connection
        # rejected). The loser of the race now waits and reuses the winner's
        # cached client.
        with self._ab_connect_lock:
            if self._ab_pycomm3_client is not None and self._ab_pycomm3_path == path:
                return self._ab_pycomm3_client
            self._close_ab_pycomm3_client()
            # init_tags MUST be True so pycomm3 fetches the controller's tag
            # database at connect. Without it, indexed-tag reads such as
            # `SimREAL[1]` fail with "Tag doesn't exist - SimREAL" because
            # pycomm3 strips the [1] suffix and looks up the bare name in
            # its empty cache.
            #
            # init_program_tags is required to read PROGRAM-scoped tags
            # ("Program:MainProgram.Foo"): pycomm3 resolves every read against
            # the cache built at connect, so without it those reads fail with
            # "Tag doesn't exist" and used to be stored as 0.0/BAD — a tag the
            # operator could watch changing in Studio 5000 showed a flat 0.
            # It is enabled ONLY when this gateway polls such tags, because the
            # extra CIP round-trips measurably slow every connect/reconnect.
            want_program_tags = self._needs_program_tags()
            # Sticky de-escalation: once init_program_tags has caused a buffer/
            # packet-space failure on this controller, stop requesting it. The
            # heavy program-tag TEMPLATE read (pycomm3 _read_template) can exceed
            # a busy ControlLogix's packet buffer ("Insufficient Packet Space" /
            # "No buffer memory"), and retrying it every reconnect turns a
            # transient buffer shortage into a permanent connect-fail spiral.
            # Auto-recover: retry program tags again after a cooldown, in case
            # the buffer shortage was transient (a busy batch run, etc.).
            if getattr(self, "_ab_skip_program_tags", False):
                since = time.monotonic() - getattr(self, "_ab_skip_program_tags_mono", 0.0)
                if since < 300.0:
                    want_program_tags = False
                else:
                    self._ab_skip_program_tags = False  # give the full init another chance
            plc = logix_driver_cls(path, init_tags=True, init_program_tags=want_program_tags)
            # Operator 2026-06-24: write directly to _cfg["socket_timeout"]
            # BEFORE plc.open(). pycomm3 1.2.14 reads this key when it
            # constructs the underlying Socket(timeout=…). Note the typo
            # in pycomm3's property setter (`socket_timout`) means
            # `plc.socket_timeout = X` doesn't take effect; we have to
            # set the dict key directly.
            # Value 2.0s: each per-fragment recv() will time out in 2s,
            # so a maximally-fragmented response can complete in <30s
            # before the watchdog 30s threshold fires. Combined with the
            # read-batching at the read site, a healthy cycle now
            # completes in ~150ms total.
            try:
                sock_timeout = float(
                    os.environ.get("TRUSTNODE_AB_SOCKET_TIMEOUT_SECONDS", "2.0") or "2.0"
                )
                plc._cfg["socket_timeout"] = sock_timeout
            except Exception:
                pass
            try:
                plc.open()
            except Exception as open_exc:
                msg = str(open_exc).lower()
                buffer_err = (
                    "buffer" in msg or "packet space" in msg
                    or "insufficient" in msg or "no memory" in msg
                )
                if want_program_tags and buffer_err:
                    # The controller couldn't serve the program-tag templates.
                    # Latch it off and reconnect controller-only so collection
                    # recovers instead of spiralling on forward_open failures.
                    self._ab_skip_program_tags = True
                    self._ab_skip_program_tags_mono = time.monotonic()
                    _GW_LOG.warning(
                        "ab-open program-tags too heavy for controller (%s) — "
                        "reconnecting controller-only", type(open_exc).__name__,
                    )
                    try:
                        plc.close()
                    except Exception:
                        pass
                    plc = logix_driver_cls(path, init_tags=True, init_program_tags=False)
                    try:
                        plc._cfg["socket_timeout"] = float(
                            os.environ.get("TRUSTNODE_AB_SOCKET_TIMEOUT_SECONDS", "2.0") or "2.0"
                        )
                    except Exception:
                        pass
                    plc.open()
                else:
                    raise
            # Also force-apply the timeout to the live socket after open(),
            # in case the open path cached a socket created with a default
            # value before our _cfg update propagated.
            try:
                sock = getattr(getattr(plc, "_sock", None), "sock", None)
                if sock is not None:
                    sock.settimeout(float(
                        os.environ.get("TRUSTNODE_AB_SOCKET_TIMEOUT_SECONDS", "2.0") or "2.0"
                    ))
            except Exception:
                pass
            self._ab_pycomm3_client = plc
            self._ab_pycomm3_path = path
            return plc

    def _close_ab_pycomm3_client(self) -> None:
        if self._ab_pycomm3_client is not None:
            try:
                self._ab_pycomm3_client.close()
            except Exception:
                pass
        self._ab_pycomm3_client = None
        self._ab_pycomm3_path = None

    def _ensure_ab_pylogix_client(self, ip: str, slot: int, plc_cls):
        if self._ab_pylogix_client is not None and self._ab_pylogix_ip == ip and self._ab_pylogix_slot == slot:
            return self._ab_pylogix_client
        self._close_ab_pylogix_client()
        comm = plc_cls()
        comm.IPAddress = ip
        comm.ProcessorSlot = int(slot)
        self._ab_pylogix_client = comm
        self._ab_pylogix_ip = ip
        self._ab_pylogix_slot = int(slot)
        return comm

    def _close_ab_pylogix_client(self) -> None:
        if self._ab_pylogix_client is not None:
            try:
                self._ab_pylogix_client.Close()
            except Exception:
                pass
        self._ab_pylogix_client = None
        self._ab_pylogix_ip = None
        self._ab_pylogix_slot = None

    def _dispose_gateway_clients(self) -> None:
        self._close_ab_pycomm3_client()
        self._close_ab_pylogix_client()
        self._close_opc_client()

    def _ensure_opc_client(self, endpoint: str):
        """Connect (or return the cached session) for an OPC-UA endpoint.
        Mirrors `_ensure_ab_pycomm3_client`: the worker keeps the session
        alive across polls so each cycle pays one round-trip (the read),
        not six (connect + Hello + CreateSession + ActivateSession +
        Close + TCP teardown). Reconnect on any cycle that throws."""
        from opcua import Client  # type: ignore
        if self._opc_client is not None and self._opc_endpoint == endpoint:
            return self._opc_client
        self._close_opc_client()
        # Operator 2026-06-23 (Item 10): client-level timeout bounds the
        # OPC-UA protocol response time. We also apply a socket-level
        # timeout AFTER connect so a half-open TCP can't hang a CIP
        # read indefinitely — same defense-in-depth as pycomm3.
        opc_timeout = float(os.environ.get("TRUSTNODE_OPC_TIMEOUT_SECONDS", "4.0") or "4.0")
        client = Client(endpoint, timeout=opc_timeout)
        client.connect()
        try:
            sock = getattr(getattr(client, "uaclient", None), "_uasocket", None)
            real_sock = getattr(sock, "_socket", None) if sock is not None else None
            if real_sock is not None:
                real_sock.settimeout(max(opc_timeout, 8.0))
        except Exception:
            # python-opcua's internals are version-specific; if we
            # can't reach the socket the protocol-level timeout still
            # applies and the watchdog catches the rest.
            pass
        self._opc_client = client
        self._opc_endpoint = endpoint
        return client

    def _close_opc_client(self) -> None:
        if self._opc_client is not None:
            try:
                self._opc_client.disconnect()
            except Exception:
                pass
        self._opc_client = None
        self._opc_endpoint = ""

    def _sqlite_url_from_path(self, sqlite_path: str) -> str:
        path_norm = (sqlite_path or "").strip()
        if not path_norm:
            path_norm = self._default_data_file("trustnode_edge.db")
        if path_norm == ":memory:":
            return "sqlite+pysqlite:///:memory:"

        # Resolve relative paths under a writable user directory.
        #
        # DATA-DIR NESTING BUG (2026-07-16): the stock sink config ships
        # sqlite_path="./data/trustnode_edge.db", but _default_data_dir()
        # ALREADY ends in ".../.trustnode_edge/data". Naively joining them
        # produced ".../.trustnode_edge/data/data/trustnode_edge.db" — a
        # SECOND, nested store. The gateway happily wrote 3M rows there while
        # every reader (batch module, trigger daemon, charts) looked at the
        # canonical store and saw an empty historian: no triggers fired, no
        # series/limits/KPIs rendered. Strip a leading "./data/" (or "data/")
        # when the base dir already ends in "data" so the configured default
        # resolves to the SAME file readers use. Absolute paths are untouched —
        # an operator who deliberately points elsewhere still gets that path.
        if not os.path.isabs(path_norm):
            base = self._default_data_dir()
            rel = path_norm.replace("\\", "/").lstrip("./")
            if os.path.basename(os.path.normpath(base)).lower() == "data":
                low = rel.lower()
                if low.startswith("data/"):
                    rel = rel[len("data/"):]
            path_norm = os.path.join(base, rel)
        path_norm = os.path.abspath(path_norm)
        parent = os.path.dirname(path_norm)
        if parent:
            os.makedirs(parent, exist_ok=True)
        path_norm = path_norm.replace("\\", "/")

        if path_norm == ":memory:":
            return "sqlite+pysqlite:///:memory:"
        if len(path_norm) > 2 and path_norm[1] == ":":
            return f"sqlite+pysqlite:///{path_norm}"
        if path_norm.startswith("/"):
            return f"sqlite+pysqlite:///{path_norm}"
        return f"sqlite+pysqlite:///{path_norm}"

    def _default_data_dir(self) -> str:
        env = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
        if env:
            os.makedirs(env, exist_ok=True)
            return env
        base = os.path.join(os.path.expanduser("~"), ".trustnode_edge", "data")
        os.makedirs(base, exist_ok=True)
        return base

    def _default_data_file(self, filename: str) -> str:
        return os.path.join(self._default_data_dir(), filename)

    def _is_builtin_local_sqlite_sink(self, sink: Optional[Dict[str, Any]]) -> bool:
        """True when `sink` is the stock "Local SQLite" connection pointing at the
        data-dir default file (./data/trustnode_edge.db).

        Operator 2026-08-21 (retention research, design item D10): every reading
        is ALREADY persisted once by app_store.append_historian_rows — the store
        that feeds charts, batches, reports and the AI tools. Writing it a second
        time into trustnode_edge.db (with a raw_payload JSON copy, in rollback-
        journal mode) cost 1.4–6 s per 1 s cycle once that file reached 4.3 GB and
        starved the V2 distribution queue (backlog 6 -> 314 in the 10-min release
        gate of 2026-08-21). The built-in local sink is therefore an ALIAS of the
        historian: db_write_count / db_last_write_utc still advance, but no
        duplicate file write happens. A sqlite sink pointed at ANY other path
        (USB / NAS export) still gets the physical write. Escape hatch for a
        rollback without rebuild: TRUSTNODE_LOCAL_SQLITE_SINK_DEDUP=0.
        """
        try:
            if not isinstance(sink, dict):
                return False
            if str(sink.get("engine") or "").strip().lower() != "sqlite":
                return False
            if os.environ.get("TRUSTNODE_LOCAL_SQLITE_SINK_DEDUP", "1").strip() == "0":
                return False
            configured = str(sink.get("sqlite_path") or "").strip() or "./data/trustnode_edge.db"
            if configured == ":memory:":
                return False
            resolved = self._sqlite_url_from_path(configured).split(":///", 1)[-1]
            default = self._default_data_file("trustnode_edge.db").replace("\\", "/")
            return os.path.normcase(os.path.abspath(resolved)) == os.path.normcase(os.path.abspath(default))
        except Exception:
            return False

    def _note_builtin_sink_dedup_once(self, label: str) -> None:
        if getattr(self, "_builtin_sink_dedup_noted", False):
            return
        self._builtin_sink_dedup_noted = True
        try:
            _GW_LOG.info(
                "gateway=%s sink '%s' is the built-in Local SQLite (data-dir trustnode_edge.db): "
                "rows are already in the historian, duplicate file write skipped "
                "(set TRUSTNODE_LOCAL_SQLITE_SINK_DEDUP=0 to restore the copy)",
                getattr(self, "gateway_id", "?"), label,
            )
        except Exception:
            pass

    def _ensure_buffer_engine(self):
        from sqlalchemy import create_engine, event, text

        buffer_path = (self.db_sink or {}).get("store_forward_path") or self._default_data_file("trustnode_store_forward.db")
        key = self._sqlite_url_from_path(str(buffer_path))
        if self._buffer_engine is None or self._buffer_engine_key != key:
            if self._buffer_engine is not None:
                try:
                    self._buffer_engine.dispose()
                except Exception:
                    pass
            # WAL + a BOUNDED busy_timeout are the whole ballgame here. The
            # store-forward outbox is written from TWO threads at once: the
            # collection executor (via `_enqueue_outbox`, on the hot cycle
            # path) and the background flush daemon (`_flush_remote_outbox_once`
            # → `_mark_sent`/`_mark_failed`). In SQLite's DEFAULT rollback-journal
            # mode a writer takes an exclusive DB lock, so while the flush thread
            # holds it the enqueue blocks — and with NO busy_timeout set, the
            # default handler could keep the enqueue thread parked long enough to
            # exceed the 5 s persist-timeout in `_run_loop`. That abandons the
            # executor thread (it keeps running, holding 1 of only 3 slots); after
            # a few cycles all 3 slots are wedged and the read loop can't run at
            # all → the exact "first row then stall forever until the 300 s
            # cooldown" signature we saw in the field. WAL lets the reader/flush
            # and the writer proceed concurrently, and busy_timeout caps any
            # residual contention at a few hundred ms (well under the 5 s persist
            # budget) so a lock is a fast retriable error, never an unbounded park.
            self._buffer_engine = create_engine(
                key,
                pool_pre_ping=True,
                connect_args={"check_same_thread": False, "timeout": 3.0},
            )

            @event.listens_for(self._buffer_engine, "connect")
            def _set_buffer_pragmas(dbapi_conn, _rec):  # noqa: ANN001
                try:
                    cur = dbapi_conn.cursor()
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA synchronous=NORMAL")
                    # 3000 ms: a locked DB retries internally for up to 3 s, still
                    # under the 5 s persist-timeout, then raises OperationalError
                    # (caught by the persist path) instead of parking forever.
                    cur.execute("PRAGMA busy_timeout=3000")
                    cur.close()
                except Exception:
                    pass

            self._buffer_engine_key = key
            with self._buffer_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS outbox_readings (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          gateway_id TEXT NOT NULL,
                          ts_utc TEXT NOT NULL,
                          tag_name TEXT NOT NULL,
                          value REAL NULL,
                          value_text TEXT NULL,
                          quality INTEGER NULL,
                          quality_label TEXT NULL,
                          source TEXT NULL,
                          site TEXT NULL,
                          area TEXT NULL,
                          equipment TEXT NULL,
                          raw_payload TEXT NULL,
                          sent_remote INTEGER NOT NULL DEFAULT 0,
                          retries INTEGER NOT NULL DEFAULT 0,
                          last_error TEXT NULL,
                          created_utc TEXT NOT NULL DEFAULT (datetime('now')),
                          sent_utc TEXT NULL,
                          sink_id TEXT NOT NULL DEFAULT ''
                        )
                        """
                    )
                )
                # Operator 2026-06-17 (M10): legacy installs miss the
                # sink_id column. ADD COLUMN IF NOT EXISTS isn't valid
                # in older SQLite; check the schema directly and ALTER
                # only when the column is missing.
                try:
                    cols = [r[1] for r in conn.execute(text("PRAGMA table_info(outbox_readings)")).fetchall()]
                    if "sink_id" not in cols:
                        conn.execute(text("ALTER TABLE outbox_readings ADD COLUMN sink_id TEXT NOT NULL DEFAULT ''"))
                except Exception:
                    pass
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_outbox_unsent ON outbox_readings(sent_remote, id)"))
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_outbox_gateway_unsent "
                        "ON outbox_readings(gateway_id, sent_remote, id)"
                    )
                )
                # Per-sink backlog index — keeps the per-sink drain
                # cheap when multiple parallel Postgres sinks coexist.
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_outbox_sink_unsent "
                        "ON outbox_readings(sink_id, sent_remote, id)"
                    )
                )
        return self._buffer_engine

    def _enqueue_outbox(self, readings: List[GatewayReading], sink_id: str = "") -> int:
        """Append a batch to the local outbox buffer.

        Operator 2026-06-17 (M10): `sink_id` lets multiple parallel
        Postgres sinks each keep their own backlog. Default "" means
        "primary sink" — the legacy single-target outbox path.
        """
        from sqlalchemy import text

        engine = self._ensure_buffer_engine()
        rows = []
        for r in readings:
            rows.append(
                {
                    "gateway_id": self.gateway_id,
                    "ts_utc": r.ts_utc,
                    "tag_name": r.tag_name,
                    "value": r.value,
                    "value_text": r.value_text,
                    "quality": r.quality,
                    "quality_label": r.quality_label,
                    "source": r.source,
                    "site": r.site,
                    "area": r.area,
                    "equipment": r.equipment,
                    "raw_payload": json.dumps(r.model_dump()),
                    "sink_id": str(sink_id or ""),
                }
            )
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO outbox_readings
                    (gateway_id, ts_utc, tag_name, value, value_text, quality, quality_label, source, site, area, equipment, raw_payload, sink_id)
                    VALUES (:gateway_id, :ts_utc, :tag_name, :value, :value_text, :quality, :quality_label, :source, :site, :area, :equipment, :raw_payload, :sink_id)
                    """
                ),
                rows,
            )
        return len(rows)

    def _load_pending(self, limit: int = 300) -> List[Dict[str, Any]]:
        from sqlalchemy import text

        engine = self._ensure_buffer_engine()
        with engine.begin() as conn:
            rs = conn.execute(
                text(
                    """
                    SELECT id, ts_utc, tag_name, value, value_text, quality, quality_label, source, site, area, equipment
                    FROM outbox_readings
                    WHERE sent_remote = 0 AND gateway_id = :gid
                    ORDER BY id ASC
                    LIMIT :lim
                    """
                ),
                {"gid": self.gateway_id, "lim": max(1, int(limit))},
            )
            return [dict(r._mapping) for r in rs]

    def _mark_sent(self, ids: List[int]) -> None:
        from sqlalchemy import text

        if not ids:
            return
        engine = self._ensure_buffer_engine()
        sent_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE outbox_readings SET sent_remote = 1, sent_utc = :su, last_error = NULL WHERE id = :id"),
                [{"id": int(i), "su": sent_utc} for i in ids],
            )
        self._prune_sent_outbox()

    def _prune_sent_outbox(self) -> None:
        """Delete acknowledged (sent_remote=1) outbox rows older than the
        retention horizon. Without this the store-and-forward DB grows
        unbounded — it reached 2.37M rows / 1.1 GB of already-sent data, which
        slowed every INSERT/UPDATE. Throttled to at most once per 10 min and
        bounded per run so it never holds a long write lock."""
        now = time.monotonic()
        last = getattr(self, "_outbox_prune_mono", 0.0)
        if now - last < 600.0:
            return
        self._outbox_prune_mono = now
        try:
            from sqlalchemy import text
            days = int(os.environ.get("TRUSTNODE_OUTBOX_KEEP_DAYS", "3") or "3")
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).strftime("%Y-%m-%d %H:%M:%S")
            engine = self._ensure_buffer_engine()
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM outbox_readings WHERE sent_remote = 1 "
                         "AND (sent_utc IS NULL OR sent_utc < :cut) "
                         "AND id IN (SELECT id FROM outbox_readings WHERE sent_remote = 1 "
                         "AND (sent_utc IS NULL OR sent_utc < :cut) LIMIT 100000)"),
                    {"cut": cutoff},
                )
        except Exception as exc:
            _GW_LOG.debug("outbox-prune skipped gateway=%s: %s", self.gateway_id, exc)

    def _mark_failed(self, ids: List[int], err: str) -> None:
        from sqlalchemy import text

        if not ids:
            return
        engine = self._ensure_buffer_engine()
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE outbox_readings SET retries = retries + 1, last_error = :err WHERE id = :id"),
                [{"id": int(i), "err": (err or '')[:1000]} for i in ids],
            )

    def _count_pending(self) -> int:
        from sqlalchemy import text

        engine = self._ensure_buffer_engine()
        with engine.begin() as conn:
            rs = conn.execute(
                text("SELECT COUNT(*) AS c FROM outbox_readings WHERE sent_remote = 0 AND gateway_id = :gid"),
                {"gid": self.gateway_id},
            )
            row = rs.first()
            return int(row[0] if row else 0)

    def _persist_readings(self, readings: List[GatewayReading]) -> None:
        if not readings or not self.db_sink:
            return
        engine = (self.db_sink.get("engine") or "").strip().lower()
        if engine == "postgresql":
            try:
                # Never block real-time collection loop on remote/cloud roundtrips.
                # Enqueue locally and flush in background thread.
                queued = self._enqueue_outbox(readings)
                if queued:
                    self.db_pending_count = max(0, int(self.db_pending_count or 0)) + int(queued)
                self._mark_db_write_success(len(readings))
                self._schedule_remote_flush(engine)
            except Exception as exc:
                self._mark_db_write_error(f"Store-forward pipeline error: {exc}")
        elif engine == "legacy_http":
            try:
                queued = self._enqueue_outbox(readings)
                if queued:
                    self.db_pending_count = max(0, int(self.db_pending_count or 0)) + int(queued)
                self._schedule_remote_flush(engine)
            except Exception as exc:
                self._mark_db_write_error(f"Store-forward pipeline error: {exc}")
        elif engine == "sqlite":
            # History: an earlier no-op shortcut broke `db_write_count` accounting,
            # so the sqlite sink was made a real write-through to the configured
            # file. Operator 2026-08-21: the STOCK "Local SQLite" sink points at the
            # data-dir trustnode_edge.db — a byte-for-byte duplicate of what
            # _broadcast / StorageWriterV2 already commit to
            # app_store.historian_readings. That second write became the slowest
            # stage of every cycle (see _is_builtin_local_sqlite_sink), so the
            # built-in sink now only ACCOUNTS the write (counters, footer, status)
            # and skips the copy. Any other sqlite path is still written for real.
            if self._is_builtin_local_sqlite_sink(self.db_sink):
                self._mark_db_write_success(len(readings))
                self._note_builtin_sink_dedup_once(str(self.db_sink.get("name") or self.db_sink.get("id") or "sqlite"))
            else:
                self._persist_sqlite(readings)
            self.db_pending_count = 0
        elif engine == "csv_file":
            self._persist_csv_file(readings)
            self.db_pending_count = 0
        elif engine == "txt_file":
            self._persist_txt_file(readings)
            self.db_pending_count = 0
        else:
            self.db_pending_count = 0

        # Fan-out write to ALL configured parallel sinks. The previous
        # implementation skipped any sink whose engine matched the
        # primary (line `if sink_engine == engine: continue`), which
        # broke the common case "primary historian PG + extra CSV mirror
        # + extra PG mirror for daily reports". The new rule:
        #   - Skip by *sink identity* (id), not engine, so multiple
        #     csv_file sinks coexist when they point at different files.
        #   - Skip the primary by id, not by reference equality — the
        #     dict instance comparison hid edge cases where the same
        #     row got passed twice.
        #   - Cover every engine, including postgresql / legacy_http:
        #     queue to the outbox the same way the primary path does,
        #     so the cloud / remote sink receives identical rows.
        #   - "MUST be in the csv file the same data the historian got"
        #     — operator's stated guarantee. Each fan-out failure now
        #     surfaces via _mark_db_write_error so the operator can see
        #     when a mirror is silently lagging.
        primary_id = str((self.db_sink or {}).get("id") or "")
        seen_sink_ids: Set[str] = set()
        if primary_id:
            seen_sink_ids.add(primary_id)
        for sink in (self.db_sinks or []):
            if not isinstance(sink, dict):
                continue
            sink_id = str(sink.get("id") or "")
            if sink_id and sink_id in seen_sink_ids:
                continue
            if sink_id:
                seen_sink_ids.add(sink_id)
            if sink.get("enabled") is False:
                continue
            if sink.get("use_gateway") is False:
                # Operator turned off "feed from gateways" for this
                # connection — same gate the primary sink uses.
                continue
            sink_engine = str(sink.get("engine") or "").strip().lower()
            sink_label = str(sink.get("name") or sink.get("id") or sink_engine)
            # L3c: skip this sink while its breaker is open. Local
            # collection has already succeeded via the primary path —
            # the parallel sinks are best-effort mirrors.
            if self._sink_breaker_skip(sink_id):
                continue
            try:
                if sink_engine == "csv_file":
                    self._persist_csv_file_for_sink(sink, readings)
                elif sink_engine == "txt_file":
                    self._persist_txt_file_for_sink(sink, readings)
                elif sink_engine == "sqlite":
                    if self._is_builtin_local_sqlite_sink(sink):
                        # Same rule as the primary path: the built-in Local SQLite
                        # sink is an alias of the historian — no duplicate write.
                        self._note_builtin_sink_dedup_once(sink_label)
                    else:
                        self._persist_sqlite_for_sink(sink, readings)
                elif sink_engine == "postgresql":
                    # Operator 2026-06-17 (M4): parallel Postgres sinks
                    # are now writable via the shared sinks_sql helper.
                    # Schema is bootstrapped on first contact and the
                    # writer uses the same row shape as the primary
                    # historian sink. legacy_http is still unsupported.
                    self._persist_postgres_parallel(sink, sink_label, readings)
                elif sink_engine == "legacy_http":
                    self._mark_db_write_error(
                        f"Parallel sink '{sink_label}' (engine legacy_http) "
                        "is not yet supported."
                    )
                else:
                    # Unknown engine on a parallel sink — log so the
                    # operator can spot a typo in the config doc.
                    self._mark_db_write_error(
                        f"Parallel sink '{sink_label}' has unsupported engine '{sink_engine}'"
                    )
                self._sink_breaker_record_success(sink_id)
            except Exception as exc:
                # Parallel sinks are best-effort and should not block
                # primary flow — but they must NOT be silent. Surface
                # the failure exactly like the primary path does.
                self._mark_db_write_error(
                    f"Parallel sink '{sink_label}' write failed: {type(exc).__name__}: {exc}"
                )
                self._sink_breaker_record_failure(sink_id, sink_label)

        # Operator 2026-06-17 (M11b): mirror PLC rows into the
        # Customer DB when `database_mode = customer_sql`, regardless
        # of whether the operator has explicitly added it as a sink.
        # This matches the symmetric behaviour in power_manager so a
        # single Customer DB activation step in Settings → Database
        # picks up both meter AND PLC data automatically.
        try:
            self._mirror_to_customer_db_if_active(readings)
        except Exception:
            # Mirror is never allowed to block / break the primary
            # collection path — the local sink stays canonical.
            pass

        # Operator 2026-06-17 (Phase 3): outbound publish to OPC UA
        # server / MQTT broker for tags that have opted in. Best-effort,
        # never blocks the primary collection path. Both functions are
        # no-ops when the respective service isn't running.
        try:
            self._publish_to_outbound_connections(readings)
        except Exception:
            pass

    def _mirror_to_customer_db_if_active(self, readings: List[GatewayReading]) -> None:
        """Best-effort PLC → Customer-DB mirror.

        Reads `database_mode` + `customer_sql_target` from the edge's
        local app_settings. When mode is `customer_sql` and a target
        is configured, write the batch to that Postgres using the
        shared sinks_sql helpers. Idempotent across reboots because
        sinks_sql.bootstrap_customer_db short-circuits when the schema
        is already in place.

        2026-07-25: failure backoff — after 3 consecutive write failures the
        mirror pauses for 60 s (see the except block below) so a degraded
        cloud can't tax every distribution cycle with a full timeout wait.

        Important: this path does NOT use the outbox (operator could
        still add the customer DB as an explicit parallel sink to
        get the M10 outbox guarantees). We chose best-effort here
        because every PLC poll already enters the primary outbox
        when the primary sink is Postgres — adding a second outbox
        per parallel sink that the operator didn't ask for would
        double the disk write rate.
        """
        if not readings:
            return
        # Failure backoff active? Skip the whole mirror attempt.
        if time.monotonic() < getattr(self, "_mirror_skip_until_mono", 0.0):
            return
        # Skip mirroring if the customer DB is ALREADY one of the
        # configured parallel sinks — that path uses the M10 outbox
        # and would otherwise produce duplicate rows.
        try:
            settings = None
            # get_bootstrap acquires app_store._lock (a cloud-sync thread can
            # hold it). Calling it EVERY cycle from this hot path added lock
            # contention for a value that changes rarely — cache the mirror
            # settings for 10s.
            now_mono = time.monotonic()
            cached = getattr(self, "_mirror_settings_cache", None)
            cached_at = getattr(self, "_mirror_settings_cache_mono", 0.0)
            if cached is not None and (now_mono - cached_at) < 10.0:
                settings = cached
            else:
                from app.state import app_store as _app_store
                bootstrap = _app_store.get_bootstrap(prefer_cloud_reads=False) or {}
                settings = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
                self._mirror_settings_cache = settings
                self._mirror_settings_cache_mono = now_mono
        except Exception:
            return
        if not settings:
            return
        if str(settings.get("database_mode") or "local_sqlite").lower() != "customer_sql":
            return
        target = settings.get("customer_sql_target")
        if not isinstance(target, dict) or not target.get("host"):
            return
        # If any configured sink (primary or parallel) already points
        # at the same Postgres (host + database + schema), let that
        # outbox-backed path own the write to avoid duplicates.
        def _sink_matches_target(sink: Dict[str, Any]) -> bool:
            try:
                return (
                    str(sink.get("engine") or "").strip().lower() == "postgresql"
                    and str(sink.get("host") or "").strip() == str(target.get("host") or "").strip()
                    and int(sink.get("port") or 5432) == int(target.get("port") or 5432)
                    and str(sink.get("database") or "").strip() == str(target.get("database") or "").strip()
                    and str(sink.get("schema") or "public").strip() == str(target.get("schema") or "public").strip()
                )
            except Exception:
                return False
        if self.db_sink and _sink_matches_target(self.db_sink):
            return
        for sink in (self.db_sinks or []):
            if _sink_matches_target(sink):
                return

        try:
            from app.services import customer_sql as _cs
            from app.services import sinks_sql as _ss
        except Exception:
            return
        engine, _err = _cs.get_engine(target)
        if engine is None:
            return
        try:
            boot = _ss.bootstrap_customer_db(
                engine,
                schema=str(target.get("schema") or "public"),
                note=f"plc_manager:{self.gateway_id}",
            )
            if not boot.get("ok"):
                return
        except Exception:
            return
        rows = []
        for r in readings:
            rows.append({
                "tenant_id": "default",
                "ts_utc": str(getattr(r, "ts_utc", "") or ""),
                "gateway_id": self.gateway_id,
                "gateway_name": "",
                "device_name": "",
                "plc_ip": "",
                "database_name": "",
                "tag_name": str(getattr(r, "tag_name", "") or ""),
                "value": getattr(r, "value", None),
                "value_text": getattr(r, "value_text", None),
                "quality": getattr(r, "quality", None),
                "quality_label": str(getattr(r, "quality_label", "") or ""),
                "source": str(getattr(r, "source", "") or ""),
            })
        try:
            _ss.write_historian_batch(engine, rows, schema=str(target.get("schema") or "public"))
            _ss.upsert_live_latest(engine, rows, schema=str(target.get("schema") or "public"))
            self._mirror_fail_streak = 0
        except Exception as exc:
            # Drop — local primary sink already has the rows. The
            # operator can promote the customer DB to an explicit
            # parallel sink for outbox-backed durability.
            #
            # 2026-07-25 backoff: this mirror is a synchronous WAN write on
            # the distribution path (measured ~2.3 s/cycle; with the new
            # statement_timeout a dead cloud costs up to 8 s per attempt).
            # After 3 consecutive failures, skip mirroring for 60 s so a
            # cloud outage degrades distribution by seconds, not by
            # 8 s x every cycle.
            self._mirror_fail_streak = int(getattr(self, "_mirror_fail_streak", 0)) + 1
            if self._mirror_fail_streak >= 3:
                self._mirror_skip_until_mono = time.monotonic() + 60.0
                _GW_LOG.warning(
                    "customer-db mirror failing (%d consecutive: %s) — pausing mirror 60s "
                    "(local historian unaffected)",
                    self._mirror_fail_streak, type(exc).__name__,
                )
            return

    def _publish_to_outbound_connections(self, readings: List[GatewayReading]) -> None:
        """Push each reading to whichever OPC UA / MQTT runtime is active
        via the connections_publish dispatcher. No-op when the chosen
        service isn't running or the tag isn't flagged for that protocol.
        """
        if not readings:
            return
        try:
            from app.services import connections_publish as cp
        except Exception:
            return
        gw_name = ""
        device_name = ""
        try:
            cfg = self.config
            gw_name = str(getattr(cfg, "name", "") or self.gateway_id)
            device_name = str(getattr(cfg, "device_name", "") or "device")
        except Exception:
            gw_name = self.gateway_id
            device_name = "device"
        for r in readings:
            cp.publish_opcua(
                gateway_id=self.gateway_id,
                device_name=device_name,
                tag_name=r.tag_name,
                value=r.value,
                ts_utc=r.ts_utc,
                quality=r.quality_label,
            )
            cp.publish_mqtt(
                gateway_id=self.gateway_id,
                gateway_name=gw_name,
                device_name=device_name,
                tag_name=r.tag_name,
                value=r.value if r.value_text is None else r.value_text,
                ts_utc=r.ts_utc,
                quality=r.quality_label,
            )

    def _filter_readings_for_sink(
        self,
        sink: Dict[str, Any],
        readings: List[GatewayReading],
    ) -> List[GatewayReading]:
        """Apply per-sink tag filtering and per-sink gateway filtering.

        Operators told us "the csv file is writing every tag", which is
        what the previous fan-out path did. Now a csv_file / txt_file
        sink can carry:

        * `tag_filters`: list of tag names (case-sensitive). When set
          and non-empty, only readings for those tags get written.
        * `gateway_filters`: list of gateway ids. When set and the
          current gateway's id isn't in the list, the sink writes
          NOTHING for this fan-out call. Lets one CSV sink be reused
          for tags from any gateway while excluding others.

        Backwards compatible: an empty / missing list means "accept
        everything", so existing sinks keep their current behaviour.
        """
        tag_filters = sink.get("tag_filters")
        gateway_filters = sink.get("gateway_filters")
        gateway_id = getattr(self.config, "gateway_id", None) or getattr(self.config, "id", "")
        if gateway_filters:
            allowed_gws = {str(g or "").strip() for g in gateway_filters if g}
            if allowed_gws and str(gateway_id or "") not in allowed_gws:
                return []
        if tag_filters:
            allowed_tags = {str(t or "").strip() for t in tag_filters if t}
            if allowed_tags:
                return [r for r in readings if str(r.tag_name or "") in allowed_tags]
        return list(readings)

    def _reading_placeholders(self, r: "GatewayReading") -> Dict[str, str]:
        """Map of placeholder names → string values for one reading.
        Used by the custom CSV/TXT format string so the operator can
        compose column layouts without backend changes. Every value is
        already stringified for safe insertion."""
        return {
            "ts_local": _utc_str_to_local_iso(r.ts_utc),
            "ts_utc": str(r.ts_utc or ""),
            "tag_name": str(r.tag_name or ""),
            "value": str(r.value if r.value is not None else ""),
            "value_text": str(getattr(r, "value_text", "") or ""),
            "quality": str(r.quality if r.quality is not None else ""),
            "quality_label": str(r.quality_label or ""),
            "source": str(r.source or ""),
            "site": str(r.site or ""),
            "area": str(r.area or ""),
            "equipment": str(r.equipment or ""),
        }

    def _format_csv_row(self, fmt: str, r: "GatewayReading") -> str:
        """Apply a user-supplied format string with {placeholder} tokens.
        Unknown placeholders are left as literal text rather than blowing
        up the entire write loop. Returns the rendered line WITHOUT a
        trailing newline."""
        try:
            return fmt.format_map(_SafeDict(self._reading_placeholders(r)))
        except Exception:
            # Defensive: never let a malformed format string crash the
            # poll cycle. Fall back to a CSV-safe canonical row.
            ph = self._reading_placeholders(r)
            return ",".join(ph[k] for k in (
                "ts_local", "ts_utc", "tag_name", "value", "value_text",
                "quality", "quality_label", "source", "site", "area", "equipment",
            ))

    def _persist_csv_file_for_sink(self, sink: Dict[str, Any], readings: List[GatewayReading]) -> bool:
        import csv
        filtered = self._filter_readings_for_sink(sink, readings)
        if not filtered:
            return True  # nothing to write but the sink itself is fine
        sink_label = str((sink or {}).get("name") or (sink or {}).get("id") or "csv_file")
        custom_format = str((sink or {}).get("csv_format") or "").strip()
        custom_header = str((sink or {}).get("csv_header") or "").strip()
        try:
            file_path = self._resolve_output_file_path((sink or {}).get("file_path") or "", "trustnode_log.csv")
            write_header = (not os.path.exists(file_path)) or os.path.getsize(file_path) == 0
            with open(file_path, "a", encoding="utf-8", newline="") as f:
                if custom_format:
                    # Custom row template: write the optional custom
                    # header as a single literal line, then format each
                    # reading via _format_csv_row.
                    if write_header and custom_header:
                        f.write(custom_header.rstrip("\r\n") + "\n")
                    for r in filtered:
                        f.write(self._format_csv_row(custom_format, r) + "\n")
                else:
                    writer = csv.writer(f)
                    if write_header:
                        writer.writerow(["ts_local", "ts_utc", "tag_name", "value", "value_text", "quality", "quality_label", "source", "site", "area", "equipment"])
                    for r in filtered:
                        writer.writerow([
                            _utc_str_to_local_iso(r.ts_utc),
                            r.ts_utc,
                            r.tag_name,
                            r.value,
                            getattr(r, "value_text", "") or "",
                            r.quality,
                            r.quality_label,
                            r.source,
                            r.site,
                            r.area,
                            r.equipment,
                        ])
            return True
        except Exception as exc:
            # Bare `except: return False` here was hiding the actual
            # cause from the operator for months. Surface it via the
            # data-sync state so the UI tile shows what's wrong.
            self._mark_db_write_error(
                f"CSV sink '{sink_label}' write failed: {type(exc).__name__}: {exc}"
            )
            try:
                import logging
                logging.getLogger("trustnode.csv-sink").warning(
                    "csv_file sink '%s' (file=%r) write failed: %s",
                    sink_label, (sink or {}).get("file_path"), exc,
                )
            except Exception:
                pass
            return False

    def _persist_txt_file_for_sink(self, sink: Dict[str, Any], readings: List[GatewayReading]) -> bool:
        filtered = self._filter_readings_for_sink(sink, readings)
        if not filtered:
            return True
        sink_label = str((sink or {}).get("name") or (sink or {}).get("id") or "txt_file")
        try:
            file_path = self._resolve_output_file_path((sink or {}).get("file_path") or "", "trustnode_log.txt")
            with open(file_path, "a", encoding="utf-8") as f:
                for r in filtered:
                    txt = getattr(r, "value_text", "") or ""
                    f.write(
                        f"{_utc_str_to_local_iso(r.ts_utc)}|{r.ts_utc}|{r.tag_name}|{r.value}|{txt}|"
                        f"{r.quality}|{r.quality_label}|{r.source}|{r.site}|{r.area}|{r.equipment}\n"
                    )
            return True
        except Exception as exc:
            self._mark_db_write_error(
                f"TXT sink '{sink_label}' write failed: {type(exc).__name__}: {exc}"
            )
            try:
                import logging
                logging.getLogger("trustnode.txt-sink").warning(
                    "txt_file sink '%s' (file=%r) write failed: %s",
                    sink_label, (sink or {}).get("file_path"), exc,
                )
            except Exception:
                pass
            return False

    def _persist_sqlite_for_sink(self, sink: Dict[str, Any], readings: List[GatewayReading]) -> bool:
        try:
            from sqlalchemy import create_engine, text
        except Exception:
            return False
        sqlite_path = (sink.get("sqlite_path") or "./data/trustnode_edge.db").strip()
        table = (sink.get("table") or "plc_readings").strip() or "plc_readings"
        url = self._sqlite_url_from_path(sqlite_path)
        engine = None
        try:
            engine = create_engine(url, pool_pre_ping=True)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{table}" (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
                          tag_name TEXT NOT NULL,
                          value REAL NULL,
                          value_text TEXT NULL,
                          quality INTEGER NULL,
                          source TEXT NULL,
                          site TEXT NULL,
                          area TEXT NULL,
                          equipment TEXT NULL,
                          seq INTEGER NULL,
                          raw_payload TEXT NULL
                        )
                        """
                    )
                )
                # Back-fill value_text on tables provisioned before STRING tags
                # were supported (CREATE TABLE IF NOT EXISTS won't add a column).
                try:
                    _cols = [r[1] for r in conn.execute(text(f'PRAGMA table_info("{table}")')).fetchall()]
                    if "value_text" not in _cols:
                        conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN value_text TEXT NULL'))
                except Exception:
                    pass
                conn.execute(
                    text(
                        f"""
                        INSERT INTO "{table}"
                        (ts_utc, tag_name, value, value_text, quality, source, site, area, equipment, raw_payload)
                        VALUES (:ts_utc, :tag_name, :value, :value_text, :quality, :source, :site, :area, :equipment, :raw_payload)
                        """
                    ),
                    [
                        {
                            "ts_utc": r.ts_utc,
                            "tag_name": r.tag_name,
                            "value": r.value,
                            "value_text": r.value_text,
                            "quality": r.quality,
                            "source": r.source,
                            "site": r.site,
                            "area": r.area,
                            "equipment": r.equipment,
                            "raw_payload": json.dumps(r.model_dump()),
                        }
                        for r in readings
                    ],
                )
            return True
        except Exception:
            return False
        finally:
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:
                    pass

    def _mark_outbox_drained(self, *, sink_id: str, count: int) -> int:
        """Mark the oldest `count` unsent outbox rows for this gateway/sink
        as sent. Returns the number of rows actually updated.

        Operator 2026-06-17 (M10): used by the parallel-PG writer after
        a successful sink write so the outbox tail follows the in-flight
        batch.

        Operator 2026-06-18 (data integrity): the SELECT MUST use ORDER BY
        id ASC. The writer enqueues then flushes FIFO, so the rows just
        written to Postgres are the OLDEST unsent rows. DESC marks the
        newest (not-yet-flushed) rows as sent and leaves the
        just-written rows pending — they'd then be re-sent on the next
        flush cycle, producing duplicate rows in the customer DB and
        leaking unsent rows that never drain. The race is exposed at any
        sustained poll rate where the next batch enqueues before the
        previous flush returns (i.e. the normal 1 Hz case).
        """
        if count <= 0:
            return 0
        from sqlalchemy import text
        engine = self._ensure_buffer_engine()
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT id FROM outbox_readings "
                    "WHERE gateway_id = :gid AND sink_id = :sid AND sent_remote = 0 "
                    "ORDER BY id ASC LIMIT :n"
                ),
                {"gid": self.gateway_id, "sid": str(sink_id or ""), "n": int(count)},
            ).fetchall()
            ids = [r[0] for r in rows]
            if not ids:
                return 0
            conn.execute(
                text(
                    "UPDATE outbox_readings "
                    "SET sent_remote = 1, sent_utc = :su, last_error = NULL "
                    "WHERE id IN (" + ",".join(str(i) for i in ids) + ")"
                ),
                {"su": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            )
            return len(ids)

    def drain_parallel_sink_outbox(self, sink: Dict[str, Any], sink_label: str, max_batch: int = 500) -> int:
        """Drain pending rows for a parallel Postgres sink that has
        recovered. Called from the existing remote-flush scheduler.

        Returns the number of rows successfully written; 0 on failure.
        """
        from sqlalchemy import text
        sink_id = str(sink.get("id") or sink_label)
        engine_buf = self._ensure_buffer_engine()
        with engine_buf.begin() as conn:
            pending = conn.execute(
                text(
                    "SELECT id, ts_utc, tag_name, value, quality, quality_label, source "
                    "FROM outbox_readings "
                    "WHERE sink_id = :sid AND sent_remote = 0 "
                    "ORDER BY id ASC LIMIT :lim"
                ),
                {"sid": sink_id, "lim": int(max_batch)},
            ).mappings().all()
        if not pending:
            return 0
        try:
            from app.services import customer_sql as _cs
            from app.services import sinks_sql as _ss
        except Exception:
            return 0
        target = {
            "engine": "postgresql",
            "host": str(sink.get("host") or "").strip(),
            "port": int(sink.get("port") or 5432),
            "database": str(sink.get("database") or "").strip(),
            "schema": str(sink.get("schema") or "public").strip(),
            "username": str(sink.get("username") or "").strip(),
            "password": str(sink.get("password") or ""),
            "tls": bool(sink.get("tls") or str(sink.get("ssl") or "").lower() in ("1", "true", "require")),
        }
        engine_pg, err = _cs.get_engine(target)
        if engine_pg is None:
            return 0
        rows = []
        for r in pending:
            rows.append({
                "tenant_id": "default",
                "ts_utc": r["ts_utc"],
                "gateway_id": self.gateway_id,
                "gateway_name": "",
                "device_name": "",
                "plc_ip": "",
                "database_name": str(sink.get("name") or sink_id),
                "tag_name": r["tag_name"],
                "value": r["value"],
                "value_text": None,
                "quality": r["quality"],
                "quality_label": r["quality_label"] or "",
                "source": r["source"] or "",
            })
        try:
            _ss.write_historian_batch(engine_pg, rows, schema=target["schema"])
            _ss.upsert_live_latest(engine_pg, rows, schema=target["schema"])
        except Exception:
            return 0
        # All written — mark sent.
        ids = [r["id"] for r in pending]
        with engine_buf.begin() as conn:
            conn.execute(
                text(
                    "UPDATE outbox_readings "
                    "SET sent_remote = 1, sent_utc = :su, last_error = NULL "
                    "WHERE id IN (" + ",".join(str(i) for i in ids) + ")"
                ),
                {"su": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            )
        return len(ids)

    def _persist_postgres_parallel(
        self,
        sink: Dict[str, Any],
        sink_label: str,
        readings: List[GatewayReading],
    ) -> None:
        """Write a batch to a parallel Postgres sink via sinks_sql.

        Operator 2026-06-17 (M4): the legacy single-target outbox is
        kept for the primary sink. For parallel Postgres sinks this
        helper is used directly — best-effort, no store-and-forward
        yet (M4 ships the writer; the per-sink outbox is M4b if/when
        an operator complains about row loss during sink downtime).
        """
        try:
            from app.services import customer_sql as _cs
            from app.services import sinks_sql as _ss
        except Exception as exc:
            self._mark_db_write_error(
                f"Parallel sink '{sink_label}' import failed: {exc}"
            )
            return

        # Convert the sink config into the (engine,host,port,...) shape
        # customer_sql expects. sinks may carry `tls` either as bool or
        # as the legacy "ssl" string — normalise both.
        target = {
            "engine": "postgresql",
            "host": str(sink.get("host") or "").strip(),
            "port": int(sink.get("port") or 5432),
            "database": str(sink.get("database") or "").strip(),
            "schema": str(sink.get("schema") or "public").strip(),
            "username": str(sink.get("username") or "").strip(),
            "password": str(sink.get("password") or ""),
            "tls": bool(sink.get("tls") or str(sink.get("ssl") or "").lower() in ("1", "true", "require")),
        }
        engine, err = _cs.get_engine(target)
        if engine is None:
            self._mark_db_write_error(
                f"Parallel sink '{sink_label}' connect failed: {err}"
            )
            return

        # Best-effort schema bootstrap. After the first success the
        # cache short-circuits subsequent calls so this stays cheap.
        try:
            boot = _ss.bootstrap_customer_db(engine, schema=target["schema"], note=f"plc_manager:{sink_label}")
            if not boot.get("ok"):
                self._mark_db_write_error(
                    f"Parallel sink '{sink_label}' schema bootstrap failed: {boot.get('error') or 'unknown'}"
                )
                return
        except Exception as exc:
            self._mark_db_write_error(
                f"Parallel sink '{sink_label}' bootstrap raised: {type(exc).__name__}: {exc}"
            )
            return

        # Convert GatewayReading instances → dict rows matching the
        # historian schema in sinks_sql. The same row shape feeds both
        # historian and live_latest.
        rows: List[Dict[str, Any]] = []
        for r in readings:
            rows.append({
                "tenant_id": str(getattr(r, "tenant_id", "") or "default"),
                "ts_utc": str(getattr(r, "ts_utc", "") or ""),
                "gateway_id": str(getattr(r, "gateway_id", "") or ""),
                "gateway_name": str(getattr(r, "gateway_name", "") or ""),
                "device_name": str(getattr(r, "device_name", "") or ""),
                "plc_ip": str(getattr(r, "plc_ip", "") or ""),
                "database_name": str(sink.get("name") or sink.get("id") or ""),
                "tag_name": str(getattr(r, "tag_name", "") or ""),
                "value": getattr(r, "value", None),
                "value_text": getattr(r, "value_text", None),
                "quality": getattr(r, "quality", None),
                "quality_label": str(getattr(r, "quality_label", "") or ""),
                "source": str(getattr(r, "source", "") or ""),
            })

        # Operator 2026-06-17 (M10): outbox-first semantics. We enqueue
        # to the per-sink backlog before attempting the network write
        # so a transient PG outage doesn't drop rows. On a successful
        # write the matching outbox rows are marked sent.
        sink_id = str(sink.get("id") or sink_label)
        try:
            self._enqueue_outbox(readings, sink_id=sink_id)
        except Exception as exc:
            # If we can't even buffer locally we're in serious trouble —
            # log and bail; the desktop SQLite is still authoritative.
            self._mark_db_write_error(
                f"Parallel sink '{sink_label}' enqueue failed: {type(exc).__name__}: {exc}"
            )
            return

        try:
            written = _ss.write_historian_batch(engine, rows, schema=target["schema"])
            up = _ss.upsert_live_latest(engine, rows, schema=target["schema"])
            # Mark these outbox rows as sent. We can't pinpoint the
            # exact ids without re-querying, so we mark the most recent
            # `len(readings)` unsent rows for this (gateway, sink) —
            # safe because the writer queues + drains FIFO.
            try:
                self._mark_outbox_drained(sink_id=sink_id, count=len(rows))
            except Exception:
                pass
            logger.debug(
                "parallel pg sink '%s' wrote %d historian + %d live_latest",
                sink_label, written, up,
            )
        except Exception as exc:
            # Don't mark the outbox rows as drained; the next flush
            # tick will retry them.
            self._mark_db_write_error(
                f"Parallel sink '{sink_label}' write failed (rows buffered): {type(exc).__name__}: {exc}"
            )

    def _schedule_remote_flush(self, engine_name: str) -> None:
        now_mono = time.monotonic()
        with self._remote_flush_lock:
            if self._remote_flush_inflight:
                return
            if now_mono - self._remote_last_flush_started_monotonic < self._remote_flush_min_interval_seconds:
                return
            self._remote_flush_inflight = True
            self._remote_last_flush_started_monotonic = now_mono
        thread = threading.Thread(
            target=self._flush_remote_outbox_once,
            args=(engine_name,),
            daemon=True,
            name=f"tn-flush-{self.gateway_id}",
        )
        thread.start()

    def _flush_remote_outbox_once(self, engine_name: str) -> None:
        schedule_again = False
        try:
            # Higher defaults reduce pending buildup and cloud lag under multi-tag 1s gateways.
            max_batches = max(1, int(os.environ.get("TRUSTNODE_REMOTE_FLUSH_MAX_BATCHES", "40") or "40"))
            batch_limit = max(50, int(os.environ.get("TRUSTNODE_REMOTE_FLUSH_BATCH_LIMIT", "500") or "500"))
            for _ in range(max_batches):
                pending = self._load_pending(batch_limit)
                if not pending:
                    break
                pending_readings = [
                    GatewayReading(
                        ts_utc=str(r.get("ts_utc") or ""),
                        tag_name=str(r.get("tag_name") or ""),
                        # NULL stays NULL — fabricating 0.0 here turned every
                        # cloud STRING tag into a fake "0.000" (seen on cloud
                        # dashboards/KPIs). value_text carries the string.
                        value=(float(r.get("value")) if r.get("value") is not None else None),
                        value_text=(str(r.get("value_text")) if r.get("value_text") is not None else None),
                        quality=int(r.get("quality") if r.get("quality") is not None else 0),
                        quality_label=str(r.get("quality_label") or "UNKNOWN"),
                        source=str(r.get("source") or ""),
                        site=str(r.get("site") or ""),
                        area=str(r.get("area") or ""),
                        equipment=str(r.get("equipment") or ""),
                    )
                    for r in pending
                ]
                ok = self._persist_postgresql(pending_readings) if engine_name == "postgresql" else self._persist_legacy_http(pending_readings)
                ids = [int(r["id"]) for r in pending if r.get("id") is not None]
                if ok:
                    self._mark_sent(ids)
                else:
                    self._mark_failed(ids, self.db_last_error or "remote write failed")
                    # Avoid tight retry loops on remote outage/timeouts.
                    time.sleep(max(0.2, float(os.environ.get("TRUSTNODE_REMOTE_FLUSH_FAIL_SLEEP_SECONDS", "1.0") or "1.0")))
                    break
            self.db_pending_count = self._count_pending()
            schedule_again = self.db_pending_count > 0
        except Exception as exc:
            self._mark_db_write_error(f"Store-forward flush error: {exc}")
            try:
                self.db_pending_count = self._count_pending()
                schedule_again = self.db_pending_count > 0
            except Exception:
                pass
        finally:
            with self._remote_flush_lock:
                self._remote_flush_inflight = False
        if schedule_again and self.running:
            self._schedule_remote_flush(engine_name)

    def _resolve_output_file_path(self, raw_path: str, fallback_name: str) -> str:
        path_in = (raw_path or "").strip()
        if not path_in:
            path_in = self._default_data_file(fallback_name)
        if not os.path.isabs(path_in):
            path_in = os.path.join(self._default_data_dir(), path_in)
        full = os.path.abspath(path_in)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return full

    def _persist_csv_file(self, readings: List[GatewayReading]) -> bool:
        import csv

        try:
            file_path = self._resolve_output_file_path((self.db_sink or {}).get("file_path") or "", "trustnode_log.csv")
            write_header = (not os.path.exists(file_path)) or os.path.getsize(file_path) == 0
            with open(file_path, "a", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow(["ts_local", "ts_utc", "tag_name", "value", "value_text", "quality", "quality_label", "source", "site", "area", "equipment"])
                for r in readings:
                    writer.writerow([
                        _utc_str_to_local_iso(r.ts_utc),
                        r.ts_utc,
                        r.tag_name,
                        r.value,
                        getattr(r, "value_text", "") or "",
                        r.quality,
                        r.quality_label,
                        r.source,
                        r.site,
                        r.area,
                        r.equipment,
                    ])
            self._mark_db_write_success(len(readings))
            return True
        except Exception as exc:
            self._mark_db_write_error(f"DB write failed (csv_file): {exc}")
            return False

    def _persist_txt_file(self, readings: List[GatewayReading]) -> bool:
        try:
            file_path = self._resolve_output_file_path((self.db_sink or {}).get("file_path") or "", "trustnode_log.txt")
            with open(file_path, "a", encoding="utf-8") as f:
                for r in readings:
                    txt = getattr(r, "value_text", "") or ""
                    f.write(
                        f"{_utc_str_to_local_iso(r.ts_utc)}|{r.ts_utc}|{r.tag_name}|{r.value}|{txt}|"
                        f"{r.quality}|{r.quality_label}|{r.source}|{r.site}|{r.area}|{r.equipment}\n"
                    )
            self._mark_db_write_success(len(readings))
            return True
        except Exception as exc:
            self._mark_db_write_error(f"DB write failed (txt_file): {exc}")
            return False

    def _persist_postgresql(self, readings: List[GatewayReading]) -> bool:
        try:
            from sqlalchemy import create_engine, text
        except Exception as exc:
            self._mark_db_write_error(f"DB writer unavailable (SQLAlchemy missing): {exc}")
            return False

        host = (self.db_sink.get("host") or "").strip()
        port = int(self.db_sink.get("port") or 0)
        database = (self.db_sink.get("database") or "").strip() or "postgres"
        username = (self.db_sink.get("username") or "").strip()
        password = self.db_sink.get("password") or ""
        schema = (self.db_sink.get("schema") or "public").strip() or "public"
        table = (self.db_sink.get("table") or "plc_readings").strip() or "plc_readings"
        tls = bool(self.db_sink.get("tls", True))
        if not host or not port or not username:
            self._mark_db_write_error("DB sink postgresql is missing host/port/username")
            return False

        targets: list[tuple[str, int, str, str]] = [(host, port, username, "primary")]
        try:
            auto_pooler = str(os.environ.get("TRUSTNODE_SUPABASE_POOLER_AUTO", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
            if auto_pooler and host.startswith("db.") and host.endswith(".supabase.co") and int(port) == 5432:
                project_ref = host[len("db.") : -len(".supabase.co")].strip()
                pooler_host = str(os.environ.get("TRUSTNODE_SUPABASE_POOLER_HOST", "aws-1-eu-west-1.pooler.supabase.com") or "").strip()
                pooler_port = int(os.environ.get("TRUSTNODE_SUPABASE_POOLER_PORT", "6543") or "6543")
                pooler_user = username
                if username == "postgres" and project_ref:
                    pooler_user = f"postgres.{project_ref}"
                if pooler_host:
                    targets.append((pooler_host, pooler_port, pooler_user, "supabase_pooler"))
        except Exception:
            pass

        last_exc: Exception | None = None
        for tgt_host, tgt_port, tgt_user, tgt_kind in targets:
            url = f"postgresql+psycopg://{quote_plus(tgt_user)}:{quote_plus(password)}@{tgt_host}:{tgt_port}/{quote_plus(database)}"
            key = f"pg|{url}|{schema}|{table}|{tls}|{tgt_kind}"
            try:
                if self._db_engine is None or self._db_engine_key != key:
                    self._dispose_db_engine()
                    self._db_engine = create_engine(
                        url,
                        pool_pre_ping=True,
                        pool_recycle=60,
                        connect_args={
                            "sslmode": "require" if tls else "disable",
                            "connect_timeout": max(1, int(os.environ.get("TRUSTNODE_PG_CONNECT_TIMEOUT_SECONDS", "6") or "6")),
                            "options": os.environ.get(
                                "TRUSTNODE_PG_OPTIONS",
                                "-c statement_timeout=5000 -c lock_timeout=1500 -c idle_in_transaction_session_timeout=5000",
                            ),
                            "prepare_threshold": None,
                        },
                    )
                    self._db_engine_key = key
                if self._db_schema_ready_key != key:
                    with self._db_engine.begin() as conn:
                        if schema != "public":
                            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
                        conn.execute(
                            text(
                                f"""
                                CREATE TABLE IF NOT EXISTS "{schema}"."{table}" (
                                  id BIGSERIAL PRIMARY KEY,
                                  ts_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                                  tag_name TEXT NOT NULL,
                                  value DOUBLE PRECISION NULL,
                                  value_text TEXT NULL,
                                  quality INTEGER NULL,
                                  quality_label TEXT NULL,
                                  source TEXT NULL,
                                  gateway_id TEXT NULL,
                                  gateway_name TEXT NULL,
                                  device_name TEXT NULL,
                                  plc_ip TEXT NULL,
                                  database_name TEXT NULL,
                                  site TEXT NULL,
                                  area TEXT NULL,
                                  equipment TEXT NULL,
                                  tenant_id TEXT NULL,
                                  seq BIGINT NULL,
                                  raw_payload JSONB NULL,
                                  created_utc TIMESTAMPTZ NULL
                                )
                                """
                            )
                        )
                        # Keep compatibility with already-provisioned tables that were
                        # created before cloud mirror columns existed.
                        # value_text carries STRING-typed tag values (PLC STRING,
                        # OPC-UA String). Without it a status string would only
                        # survive inside raw_payload JSON — not queryable.
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS value_text TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS quality_label TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS gateway_id TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS gateway_name TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS device_name TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS plc_ip TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS database_name TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS tenant_id TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS created_utc TIMESTAMPTZ'))
                        # Keep a lightweight latest-value table hot for cloud clients.
                        conn.execute(
                            text(
                                f"""
                                CREATE TABLE IF NOT EXISTS "{schema}"."live_latest" (
                                  tenant_id TEXT NOT NULL DEFAULT 'default',
                                  gateway_id TEXT NOT NULL,
                                  tag_name TEXT NOT NULL,
                                  ts_utc TIMESTAMPTZ NOT NULL,
                                  source TEXT NULL,
                                  gateway_name TEXT NULL,
                                  device_name TEXT NULL,
                                  plc_ip TEXT NULL,
                                  database_name TEXT NULL,
                                  value DOUBLE PRECISION NULL,
                                  value_text TEXT NULL,
                                  quality INTEGER NULL,
                                  quality_label TEXT NULL,
                                  updated_utc TIMESTAMPTZ NULL,
                                  PRIMARY KEY (tenant_id, gateway_id, tag_name)
                                )
                                """
                            )
                        )
                        conn.execute(text(f'ALTER TABLE "{schema}"."live_latest" ADD COLUMN IF NOT EXISTS value_text TEXT'))
                        conn.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_live_latest_tenant_ts" ON "{schema}"."live_latest"(tenant_id, ts_utc DESC)'))
                        conn.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_live_latest_ts" ON "{schema}"."live_latest"(ts_utc DESC)'))
                        self._db_schema_ready_key = key
                tenant_id = normalize_tenant_id(str(self.db_sink.get("tenant_id") or os.environ.get("TRUSTNODE_TENANT_ID") or "default"))
                db_name = str(self.db_sink.get("name") or database or "").strip()
                now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                rows = [
                    {
                        "ts_utc": r.ts_utc,
                        "tag_name": r.tag_name,
                        "value": r.value,
                        "value_text": r.value_text,
                        "quality": r.quality,
                        "quality_label": r.quality_label,
                        "source": r.source,
                        "gateway_id": self.gateway_id,
                        "gateway_name": str(getattr(self.config, "name", "") or self.gateway_id),
                        "device_name": str(getattr(self.config, "device_name", "") or ""),
                        "plc_ip": self.config.plc_ip,
                        "database_name": db_name,
                        "site": r.site,
                        "area": r.area,
                        "equipment": r.equipment,
                        "tenant_id": tenant_id,
                        "created_utc": now_utc,
                        "raw_payload": json.dumps(r.model_dump()),
                    }
                    for r in readings
                ]
                latest_by_tag: dict[str, dict[str, Any]] = {}
                for row in rows:
                    tag_name = str(row.get("tag_name") or "")
                    if not tag_name:
                        continue
                    prev = latest_by_tag.get(tag_name)
                    if not prev:
                        latest_by_tag[tag_name] = row
                        continue
                    prev_ts = str(prev.get("ts_utc") or "")
                    cur_ts = str(row.get("ts_utc") or "")
                    if cur_ts >= prev_ts:
                        latest_by_tag[tag_name] = row
                live_rows = [
                    {
                        "tenant_id": str(r.get("tenant_id") or tenant_id),
                        "gateway_id": str(r.get("gateway_id") or self.gateway_id),
                        "tag_name": str(r.get("tag_name") or ""),
                        "ts_utc": str(r.get("ts_utc") or now_utc),
                        "source": str(r.get("source") or ""),
                        "gateway_name": str(r.get("gateway_name") or self.gateway_id),
                        "device_name": str(r.get("device_name") or ""),
                        "plc_ip": str(r.get("plc_ip") or self.config.plc_ip),
                        "database_name": str(r.get("database_name") or db_name),
                        "value": r.get("value"),
                        "value_text": r.get("value_text"),
                        "quality": r.get("quality"),
                        "quality_label": str(r.get("quality_label") or ""),
                        "updated_utc": now_utc,
                    }
                    for r in latest_by_tag.values()
                ]
                with self._db_engine.begin() as conn:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."{table}"
                            (ts_utc, tag_name, value, value_text, quality, quality_label, source, gateway_id, gateway_name, device_name, plc_ip, database_name, site, area, equipment, tenant_id, created_utc, raw_payload)
                            VALUES (CAST(:ts_utc AS timestamptz), :tag_name, :value, :value_text, :quality, :quality_label, :source, :gateway_id, :gateway_name, :device_name, :plc_ip, :database_name, :site, :area, :equipment, :tenant_id, CAST(:created_utc AS timestamptz), CAST(:raw_payload AS jsonb))
                            """
                        ),
                        rows,
                    )
                    if live_rows:
                        conn.execute(
                            text(
                                f"""
                                INSERT INTO "{schema}"."live_latest"
                                (tenant_id, gateway_id, tag_name, ts_utc, source, gateway_name, device_name, plc_ip, database_name, value, value_text, quality, quality_label, updated_utc)
                                VALUES
                                (:tenant_id, :gateway_id, :tag_name, CAST(:ts_utc AS timestamptz), :source, :gateway_name, :device_name, :plc_ip, :database_name, :value, :value_text, :quality, :quality_label, CAST(:updated_utc AS timestamptz))
                                ON CONFLICT(tenant_id, gateway_id, tag_name) DO UPDATE SET
                                  ts_utc = excluded.ts_utc,
                                  source = excluded.source,
                                  gateway_name = excluded.gateway_name,
                                  device_name = excluded.device_name,
                                  plc_ip = excluded.plc_ip,
                                  database_name = excluded.database_name,
                                  value = excluded.value,
                                  value_text = excluded.value_text,
                                  quality = excluded.quality,
                                  quality_label = excluded.quality_label,
                                  updated_utc = excluded.updated_utc
                                """
                            ),
                            live_rows,
                        )
                    self._mark_db_write_success(len(rows))
                    return True
            except Exception as exc:
                last_exc = exc
                # Retry once with fallback target (e.g. Supabase pooler over IPv4).
                continue

        self._mark_db_write_error(f"DB write failed (postgresql): {last_exc}")
        return False

    def _persist_sqlite(self, readings: List[GatewayReading]) -> bool:
        try:
            from sqlalchemy import create_engine, text
        except Exception as exc:
            self._mark_db_write_error(f"DB writer unavailable (SQLAlchemy missing): {exc}")
            return False
        sqlite_path = (self.db_sink.get("sqlite_path") or "./data/trustnode_edge.db").strip()
        table = (self.db_sink.get("table") or "plc_readings").strip() or "plc_readings"
        url = self._sqlite_url_from_path(sqlite_path)
        key = f"sqlite|{url}|{table}"
        try:
            if self._db_engine is None or self._db_engine_key != key:
                self._dispose_db_engine()
                self._db_engine = create_engine(url, pool_pre_ping=True)
                self._db_engine_key = key
            if self._db_schema_ready_key != key:
                with self._db_engine.begin() as conn:
                    conn.execute(
                        text(
                            f"""
                            CREATE TABLE IF NOT EXISTS "{table}" (
                              id INTEGER PRIMARY KEY AUTOINCREMENT,
                              ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
                              tag_name TEXT NOT NULL,
                              value REAL NULL,
                              value_text TEXT NULL,
                              quality INTEGER NULL,
                              source TEXT NULL,
                              site TEXT NULL,
                              area TEXT NULL,
                              equipment TEXT NULL,
                              seq INTEGER NULL,
                              raw_payload TEXT NULL
                            )
                            """
                        )
                    )
                self._db_schema_ready_key = key
            rows = [
                {
                    "ts_utc": r.ts_utc,
                    "tag_name": r.tag_name,
                    "value": r.value,
                    "value_text": r.value_text,
                    "quality": r.quality,
                    "source": r.source,
                    "site": r.site,
                    "area": r.area,
                    "equipment": r.equipment,
                    "raw_payload": json.dumps(r.model_dump()),
                }
                for r in readings
            ]
            with self._db_engine.begin() as conn:
                # Back-fill value_text on tables provisioned before STRING tags
                # were supported (CREATE TABLE IF NOT EXISTS won't add a column).
                try:
                    _cols = [r[1] for r in conn.execute(text(f'PRAGMA table_info("{table}")')).fetchall()]
                    if "value_text" not in _cols:
                        conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN value_text TEXT NULL'))
                except Exception:
                    pass
                conn.execute(
                    text(
                        f"""
                        INSERT INTO "{table}"
                        (ts_utc, tag_name, value, value_text, quality, source, site, area, equipment, raw_payload)
                        VALUES (:ts_utc, :tag_name, :value, :value_text, :quality, :source, :site, :area, :equipment, :raw_payload)
                        """
                    ),
                    rows,
                )
            self._mark_db_write_success(len(rows))
            return True
        except Exception as exc:
            self._mark_db_write_error(f"DB write failed (sqlite): {exc}")
            return False

    def _persist_legacy_http(self, readings: List[GatewayReading]) -> bool:
        try:
            import requests
        except Exception as exc:
            self._mark_db_write_error(f"Legacy writer unavailable (requests missing): {exc}")
            return False
        url = (self.db_sink.get("legacy_url") or "").strip()
        token = (self.db_sink.get("legacy_api_token") or "").strip()
        if not url or not token:
            self._mark_db_write_error("DB sink legacy_http is missing URL or API token")
            return False
        try:
            payload = {
                "readings": [r.model_dump() for r in readings],
                "source": self.db_sink.get("source") or "",
                "site": self.db_sink.get("site") or "",
                "area": self.db_sink.get("area") or "",
                "equipment": self.db_sink.get("equipment") or "",
            }
            response = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "X-API-TOKEN": token},
                timeout=4.0,
            )
            if response.status_code not in (200, 201, 400):
                self._mark_db_write_error(f"DB write failed (legacy_http): HTTP {response.status_code}")
                return False
            self._mark_db_write_success(len(readings))
            return True
        except Exception as exc:
            self._mark_db_write_error(f"DB write failed (legacy_http): {exc}")
            return False


class PLCManager:
    def __init__(self) -> None:
        self.max_gateways = 5
        self.workers: Dict[str, GatewayWorker] = {}
        self.active_gateway_id: str | None = None
        self.legacy_config = GatewayConfig()
        self._subscribers: Set[asyncio.Queue] = set()
        # Event-loop handle for thread-safe WS fanout (V2 readers). Captured
        # lazily the first time a gateway starts on the loop.
        self._loop: asyncio.AbstractEventLoop | None = None
        self.global_collection_triggers: List[Dict[str, Any]] = []
        self.global_collection_trigger_mode: str = "any"
        self.global_live_values: Dict[str, Dict[str, Any]] = {}
        self.global_trigger_latches: Dict[str, bool] = {}
        self.global_collection_allowed: bool = True
        self.global_collection_reason: str | None = None
        # Operator 2026-06-23: per-gateway watchdog. A single asyncio
        # task scans every running worker every 10 s. If a worker's
        # liveness counter says it has stalled, the watchdog cancels
        # the wedged read coroutine, disposes its driver clients, and
        # spawns a fresh run loop. Tracks restart attempts to back off
        # if the gateway keeps stalling (= the PLC is the problem, not
        # the worker).
        self._watchdog_task: asyncio.Task | None = None
        # Operator 2026-06-26: scan every 5 s (was 10 s) so stalls are
        # caught within ~5 s instead of ~10 s. Negligible CPU cost — the
        # scan just compares timestamps for each worker.
        self._watchdog_interval_s: float = max(
            2.0, float(os.environ.get("TRUSTNODE_WATCHDOG_INTERVAL_SECONDS", "5") or "5")
        )
        # Per-gateway restart history: list of monotonic timestamps of
        # the last N restarts. If more than _watchdog_burst_threshold
        # restarts happen inside _watchdog_burst_window_s, the
        # watchdog enters backoff for that gateway (stops trying until
        # the operator restarts manually).
        self._restart_history: Dict[str, list[float]] = {}
        self._watchdog_burst_threshold: int = max(
            1, int(os.environ.get("TRUSTNODE_WATCHDOG_BURST_THRESHOLD", "5") or "5")
        )
        self._watchdog_burst_window_s: float = max(
            30.0, float(os.environ.get("TRUSTNODE_WATCHDOG_BURST_WINDOW_SECONDS", "300") or "300")
        )
        # Gateways currently in cooldown after a restart burst.
        self._restart_cooldown_until_mono: Dict[str, float] = {}
        self._restart_cooldown_s: float = max(
            60.0, float(os.environ.get("TRUSTNODE_WATCHDOG_COOLDOWN_SECONDS", "300") or "300")
        )
        # Operator 2026-06-25: schedule + auto-recover supervisor.
        # `_user_stopped` tracks gateways the operator explicitly
        # stopped via the Stop button — auto-recover honors this set
        # and does NOT bring them back. Cleared when the operator
        # clicks Start or when the schedule turns the gateway on.
        # `_last_supervisor_action_mono` rate-limits supervisor
        # actions so a flapping start/stop loop can't hammer the PLC.
        self._user_stopped: Set[str] = set()
        self._last_supervisor_action_mono: Dict[str, float] = {}
        self._supervisor_min_interval_s: float = 30.0
        # Operator 2026-06-23 (Item 1 / no-data-loss): in-memory
        # store-and-forward buffer for historian rows whose write to
        # the local SQLite failed. The previous behaviour was a bare
        # except: pass at the broadcast site that silently dropped
        # the cycle's rows. With this buffer we capture the rows
        # instead and replay them on the next successful write
        # attempt. The buffer is bounded so a sustained DB outage
        # cannot grow memory without limit; once the cap is reached
        # we drop the OLDEST rows (FIFO) and log a single warning
        # per drop event so the operator knows data is being shed.
        from collections import deque
        self._historian_buffer: "deque[list[dict[str, Any]]]" = deque(
            maxlen=max(60, int(os.environ.get("TRUSTNODE_HISTORIAN_BUFFER_CYCLES", "600") or "600"))
        )
        self._historian_buffer_total_rows: int = 0
        self._historian_buffer_dropped: int = 0
        self._historian_buffer_last_drain_mono: float = 0.0
        self._historian_buffer_lock = threading.Lock()

    def _normalize_tag(self, raw: str) -> str:
        return str(raw or "").strip().lower()

    def _compare_by_operator(self, value: float, operator: str, threshold: float) -> bool:
        if operator == "<":
            return value < threshold
        if operator == "<=":
            return value <= threshold
        if operator == ">":
            return value > threshold
        if operator == ">=":
            return value >= threshold
        return False

    def _refresh_global_triggers(self) -> None:
        merged: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        keep_latches: Set[str] = set()
        mode = "any"
        for gid, w in self.workers.items():
            m = str(getattr(w.config, "collection_trigger_mode", "any") or "any").strip().lower()
            if m in ("any", "all"):
                mode = m
            for tr in (w.config.collection_triggers or []):
                if not bool(tr.get("enabled", True)):
                    continue
                tag = self._normalize_tag(str(tr.get("tag_name") or ""))
                if not tag:
                    continue
                trig_gid = str(tr.get("gateway_id") or gid)
                op = str(tr.get("operator") or ">=").strip()
                try:
                    val = float(tr.get("value"))
                except Exception:
                    continue
                trigger_type = str(tr.get("trigger_type") or "continuous").strip().lower()
                if trigger_type not in ("continuous", "one_time"):
                    trigger_type = "continuous"
                key = f"{trig_gid}|{tag}|{op}|{val}|{trigger_type}"
                if key in seen:
                    continue
                seen.add(key)
                keep_latches.add(key)
                merged.append(
                    {
                        "gateway_id": trig_gid,
                        "tag_name": tag,
                        "operator": op,
                        "value": val,
                        "trigger_type": trigger_type,
                        "trigger_key": key,
                        "enabled": True,
                    }
                )
        self.global_collection_triggers = merged
        self.global_collection_trigger_mode = mode
        # Drop stale latches that do not belong to current trigger set.
        self.global_trigger_latches = {k: v for k, v in self.global_trigger_latches.items() if k in keep_latches}
        # Recompute gate immediately when trigger set changes.
        self._evaluate_global_collection_gate("", [])

    def _clear_gateway_live_values(self, gateway_id: str) -> None:
        gid = str(gateway_id or "").strip()
        if not gid:
            return
        prefix = f"{gid}::"
        self.global_live_values = {k: v for k, v in self.global_live_values.items() if not k.startswith(prefix)}

    def _evaluate_global_collection_gate(
        self, gateway_id: str, readings: List[GatewayReading]
    ) -> tuple[bool, str | None]:
        now_epoch = time.time()
        for r in readings or []:
            tag = self._normalize_tag(r.tag_name)
            if not tag:
                continue
            # Skip failed reads (value None): float(None) would raise and break
            # the whole collection gate, and a bad read must not overwrite the
            # last known-good live value.
            if r.value is None:
                continue
            self.global_live_values[f"{gateway_id}::{tag}"] = {"value": float(r.value), "ts_epoch": now_epoch}

        triggers = [t for t in self.global_collection_triggers if bool(t.get("enabled", True))]
        if not triggers:
            self.global_collection_allowed = True
            self.global_collection_reason = None
            return True, None

        mode = str(self.global_collection_trigger_mode or "any").lower()
        if mode not in ("any", "all"):
            mode = "any"
        evaluated = 0
        satisfied = 0
        for tr in triggers:
            trig_gid = str(tr.get("gateway_id") or "").strip()
            tag = self._normalize_tag(str(tr.get("tag_name") or ""))
            if not tag:
                continue
            value = None
            value_ts = None
            if trig_gid:
                entry = self.global_live_values.get(f"{trig_gid}::{tag}")
                if isinstance(entry, dict):
                    value = entry.get("value")
                    value_ts = entry.get("ts_epoch")
            else:
                for k, entry in self.global_live_values.items():
                    if k.endswith(f"::{tag}"):
                        if isinstance(entry, dict):
                            value = entry.get("value")
                            value_ts = entry.get("ts_epoch")
                        break
            if value is None:
                continue
            worker = self.workers.get(trig_gid) if trig_gid else None
            interval_ms = max(200, int(getattr(worker.config, "interval_ms", 1000) if worker else 1000))
            stale_after_sec = max(5.0, (interval_ms / 1000.0) * 4.0)
            if value_ts is None or (now_epoch - float(value_ts)) > stale_after_sec:
                continue
            evaluated += 1
            cur_ok = self._compare_by_operator(float(value), str(tr.get("operator") or ">=").strip(), float(tr.get("value")))
            trigger_type = str(tr.get("trigger_type") or "continuous").strip().lower()
            trigger_key = str(tr.get("trigger_key") or f"{trig_gid}|{tag}|{tr.get('operator')}|{tr.get('value')}|continuous")
            if trigger_type == "one_time":
                was_true = bool(self.global_trigger_latches.get(trigger_key, False))
                fired = bool(cur_ok and not was_true)
                self.global_trigger_latches[trigger_key] = bool(cur_ok)
                if fired:
                    satisfied += 1
            elif cur_ok:
                satisfied += 1

        if evaluated == 0:
            self.global_collection_allowed = False
            self.global_collection_reason = "Global trigger tags not yet available (collection/write paused)."
            return False, self.global_collection_reason

        if mode == "all":
            allowed = satisfied == evaluated
            reason = None if allowed else f"Global trigger mode ALL not satisfied ({satisfied}/{evaluated})."
        else:
            allowed = satisfied > 0
            reason = None if allowed else f"Global trigger mode ANY not satisfied (0/{evaluated})."

        self.global_collection_allowed = allowed
        self.global_collection_reason = reason if not allowed else None
        return allowed, self.global_collection_reason

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def fanout_threadsafe(self, message: Dict[str, Any]) -> None:
        """Push an event to every WS subscriber from ANY thread (V2 readers).

        asyncio.Queue.put_nowait is not thread-safe, so the actual push hops
        onto the event loop via call_soon_threadsafe. A full queue (slow WS
        client) drops that client's event — same policy as V1's _broadcast.
        Never raises: fanout is best-effort by contract."""
        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            return

        def _push() -> None:
            dead = []
            for q in list(self._subscribers):
                try:
                    q.put_nowait(message)
                except asyncio.QueueFull:
                    continue
                except Exception:
                    dead.append(q)
            for q in dead:
                self._subscribers.discard(q)

        try:
            loop.call_soon_threadsafe(_push)
        except RuntimeError:
            pass  # loop shutting down

    async def _broadcast(self, message: Dict[str, Any]) -> None:
        # Persist historian at backend-side so collection does not depend on UI websocket state.
        #
        # The historian (chart) DB is INDEPENDENT of the telemetry/cloud-outbox
        # DB. We deliberately do NOT gate this write on `persisted_local` (the
        # telemetry write result): a transient telemetry.db hiccup must not blank
        # the charts for that cycle. We only require that the collection gate
        # allowed the cycle (a trigger-based block genuinely means "don't record")
        # and that there are readings to write.
        try:
            if (
                isinstance(message, dict)
                and message.get("type") == "reading"
                and message.get("collection_allowed") is not False
                and isinstance(message.get("readings"), list)
            ):
                from app.state import app_store  # local import to avoid circular import timing

                gateway_id = str(message.get("gateway_id") or "")
                worker = self.workers.get(gateway_id)
                db_name = ""
                gateway_name = gateway_id
                device_name = ""
                try:
                    db_name = str((worker.db_sink or {}).get("name") or "")
                    gateway_name = str((worker.config.name if worker else "") or gateway_id)
                    device_name = str((worker.config.device_name if worker else "") or "")
                except Exception:
                    db_name = ""
                rows = []
                for r in message.get("readings") or []:
                    if not isinstance(r, dict):
                        continue
                    tag_name = str(r.get("tag_name") or "").strip()
                    if not tag_name:
                        continue
                    rows.append(
                        {
                            "ts_utc": str(r.get("ts_utc") or datetime.now(timezone.utc).isoformat()),
                            "source": str(r.get("source") or ""),
                            "gateway_id": gateway_id,
                            "gateway_name": gateway_name,
                            "device_name": device_name,
                            "plc_ip": str((worker.config.plc_ip if worker else "") or ""),
                            "database_name": db_name,
                            "tag_name": tag_name,
                            "value": r.get("value"),
                            "value_text": r.get("value_text"),
                            "data_type": str(r.get("data_type") or ""),
                            "quality": r.get("quality"),
                            "quality_label": str(r.get("quality_label") or ""),
                        }
                    )
                if rows:
                    # Operator 2026-06-23 (Item 1): drain any previously
                    # buffered rows BEFORE this cycle's rows so we don't
                    # reorder writes. Then attempt the current cycle. If
                    # either fails, the unwritten rows go into the FIFO
                    # buffer for the next attempt. No bare-except drop.
                    #
                    # 2026-07-16 REGRESSION FIX: this used to call
                    # self._run_collection_io(...), but that helper lives on
                    # GatewayWorker — NOT on PLCManager (which owns
                    # _broadcast). Every cycle raised AttributeError, the broad
                    # `except` below swallowed it, and the rows were buffered
                    # forever: app_store.historian_readings stayed EMPTY while
                    # the sink DB filled. That silently broke every historian
                    # reader — batch triggers never fired, and batch views had
                    # no series/limits/charts/KPIs. Run the write on the owning
                    # worker's dedicated collection executor (keeping the
                    # thread-pool isolation), falling back to a plain thread.
                    if worker is not None and hasattr(worker, "_run_collection_io"):
                        await worker._run_collection_io(self._flush_historian_buffer_then_write, rows)
                    else:
                        await asyncio.to_thread(self._flush_historian_buffer_then_write, rows)
        except Exception as exc:
            # We must NEVER drop rows silently here. If we reached this
            # point with a non-empty rows list it means the helper above
            # threw before it could enqueue — push the rows into the
            # buffer and log so the operator sees what happened.
            try:
                if 'rows' in locals() and rows:
                    self._buffer_historian_rows(rows, f"broadcast exception: {exc}")
            except Exception:
                pass

        dead: List[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                if q.full():
                    _ = q.get_nowait()
                q.put_nowait(message)
            except Exception:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    # ------------------------------------------------------------------
    # Historian store-and-forward (Item 1, 2026-06-23)
    # ------------------------------------------------------------------
    def _buffer_historian_rows(self, rows: list[dict[str, Any]], reason: str) -> None:
        """Push rows into the in-memory FIFO buffer. Bounded — if full,
        the oldest cycle is dropped and a count of dropped rows is
        tracked. Logs at WARNING the first time we buffer anything in
        a new outage, and again whenever data is shed (rare).
        """
        if not rows:
            return
        with self._historian_buffer_lock:
            buf = self._historian_buffer
            buf_was_empty = (len(buf) == 0)
            at_cap = (len(buf) >= (buf.maxlen or 0) > 0)
            if at_cap:
                # deque.append at maxlen drops the leftmost cycle.
                # Capture its size first so we can record the loss.
                try:
                    dropped_cycle = buf[0]
                    self._historian_buffer_dropped += len(dropped_cycle or [])
                    self._historian_buffer_total_rows -= len(dropped_cycle or [])
                except Exception:
                    pass
            buf.append(list(rows))
            self._historian_buffer_total_rows += len(rows)
            buf_len = len(buf)
            total_rows = self._historian_buffer_total_rows
            dropped = self._historian_buffer_dropped
        try:
            if buf_was_empty:
                _GW_LOG.warning(
                    "historian-buffer-open reason=%s cycles=%d rows=%d",
                    reason, buf_len, total_rows,
                )
            if at_cap:
                _GW_LOG.error(
                    "historian-buffer-overflow reason=%s buffered_cycles=%d total_dropped_rows=%d",
                    reason, buf_len, dropped,
                )
        except Exception:
            pass

    def _flush_historian_buffer_then_write(self, current_rows: list[dict[str, Any]]) -> None:
        """Drain the buffer in FIFO order, then write the current
        cycle. Runs on a worker thread (called via asyncio.to_thread)
        so it can block on SQLite without stalling the event loop.

        Failure modes:
          * A buffered cycle fails to write → we stop draining, push
            the failed cycle BACK to the front of the buffer along
            with the current cycle, and return without raising. The
            next broadcast will retry.
          * The current cycle fails to write (but the buffer drain
            succeeded) → push the current rows into the buffer.

        This ordering preserves chronological insert order across
        recoveries — older rows always commit before newer ones.
        """
        from app.state import app_store  # local import to avoid circular import timing

        # Pop all buffered cycles into a local list so we don't hold
        # the buffer lock during SQLite I/O. If a write fails we'll
        # rebuild the buffer from whatever's left.
        with self._historian_buffer_lock:
            pending = list(self._historian_buffer)
            self._historian_buffer.clear()
            self._historian_buffer_total_rows = 0

        pending.append(list(current_rows))

        written_cycles = 0
        for idx, cycle_rows in enumerate(pending):
            try:
                app_store.append_historian_rows(cycle_rows)
                written_cycles += 1
            except Exception as exc:
                # Re-buffer this cycle and everything after it. Order
                # preserved.
                with self._historian_buffer_lock:
                    for remaining in pending[idx:]:
                        self._historian_buffer.append(list(remaining))
                        self._historian_buffer_total_rows += len(remaining or [])
                try:
                    _GW_LOG.warning(
                        "historian-write-fail buffered=%d cycles_drained=%d exc=%s: %s",
                        len(pending) - idx,
                        written_cycles,
                        type(exc).__name__,
                        exc,
                    )
                except Exception:
                    pass
                return

        # All cycles drained successfully. If the buffer was non-empty
        # before this call, log the recovery.
        if len(pending) > 1:
            try:
                self._historian_buffer_last_drain_mono = time.monotonic()
                _GW_LOG.info(
                    "historian-buffer-drained cycles=%d (incl. current); buffer now empty",
                    len(pending),
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Supervisor watchdog
    # ------------------------------------------------------------------
    def _ensure_watchdog_running(self) -> None:
        """Start the watchdog task if it isn't already. Called from
        start_gateway. We use a single task for all gateways so the
        manager can scan them collectively and never spawn duplicates."""
        if self._watchdog_task and not self._watchdog_task.done():
            return
        try:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        except RuntimeError:
            # No running event loop yet — start_gateway is the only
            # caller and it always runs inside the FastAPI loop, so
            # this branch is defensive.
            self._watchdog_task = None

    async def _stop_watchdog(self) -> None:
        task = self._watchdog_task
        self._watchdog_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def _record_restart_attempt(self, gateway_id: str) -> bool:
        """Record a watchdog-initiated restart for gateway_id. Returns
        True if the restart should proceed, False if the gateway has
        hit the burst threshold and must enter cooldown.
        """
        now_mono = time.monotonic()
        # If we're already in cooldown, refuse.
        until = self._restart_cooldown_until_mono.get(gateway_id, 0.0)
        if until and now_mono < until:
            return False
        history = self._restart_history.setdefault(gateway_id, [])
        history.append(now_mono)
        # Trim to the burst window.
        cutoff = now_mono - self._watchdog_burst_window_s
        self._restart_history[gateway_id] = [t for t in history if t >= cutoff]
        if len(self._restart_history[gateway_id]) > self._watchdog_burst_threshold:
            self._restart_cooldown_until_mono[gateway_id] = now_mono + self._restart_cooldown_s
            return False
        return True

    async def _restart_worker_due_to_stall(self, gateway_id: str, idle_s: float) -> None:
        """Tear down the wedged read loop and spawn a fresh one.

        Steps:
          1. Cancel the running task and wait up to 5 s for it to exit
             (it may be blocked in a driver call — that's fine, we move
             on and let the OS clean up the abandoned thread).
          2. Dispose all driver clients so the next cycle reconnects.
          3. Reset liveness counters and reuse the existing worker
             instance (preserves config, sinks, trigger state).
          4. Spawn a brand new run-loop task and bump restart counters.
        """
        w = self.workers.get(gateway_id)
        if not w:
            return
        if not self._record_restart_attempt(gateway_id):
            try:
                import logging
                logging.getLogger("trustnode.watchdog").warning(
                    "gateway %s exceeded restart burst threshold (%d in %.0fs); "
                    "entering cooldown for %.0fs",
                    gateway_id,
                    self._watchdog_burst_threshold,
                    self._watchdog_burst_window_s,
                    self._restart_cooldown_s,
                )
            except Exception:
                pass
            w.last_error = (
                f"PLC unreachable — paused retries for "
                f"{self._restart_cooldown_s:.0f}s after {self._watchdog_burst_threshold} "
                f"reconnect attempts in {self._watchdog_burst_window_s:.0f}s. "
                f"Will retry automatically; collection resumes when the PLC is back."
            )
            w.running = False
            # 2026-07-19: do NOT clear last_running here. Cooldown is a
            # temporary back-off (the PLC is briefly unreachable / power-
            # cycling), NOT operator intent to stop. Keeping last_running=1
            # means the supervisor's baseline auto-recover retries this
            # gateway once the cooldown window expires — so a PLC that comes
            # back after a long outage self-heals instead of staying dead
            # until someone clicks Start. (Previously we cleared it, which
            # permanently disabled auto-recover after the first burst.)
            #
            # The heartbeat-idle concern that originally motivated clearing
            # it is handled elsewhere: the sidecar tolerates an idle
            # heartbeat while a gateway is legitimately waiting on an
            # offline PLC.
            return
        try:
            import logging
            logging.getLogger("trustnode.watchdog").warning(
                "gateway %s stalled %.0fs (threshold %.0fs) — restarting worker",
                gateway_id,
                idle_s,
                w._stall_threshold_s,
            )
        except Exception:
            pass
        # Operator 2026-06-24: instrumented restart sequence. Every
        # step prints a breadcrumb to stdout BEFORE running. If a
        # stall recurs and we see "trace=N" lines stop at some N,
        # that's exactly the line that wedged. Replaces speculation
        # with measurement. Cheap (printf at most once per ~40 min).
        def _trace(step: str) -> None:
            try:
                print(f"[trustnode][watchdog-restart] gw={gateway_id} {step}", flush=True)
            except Exception:
                pass

        # 1. Cancel the wedged task.
        _trace("1.cancel.begin")
        task = w._task
        w._task = None
        if task and not task.done():
            try:
                task.cancel()
                _trace("1.cancel.done")
            except Exception as exc:
                _trace(f"1.cancel.exc={type(exc).__name__}:{exc}")
        else:
            _trace("1.cancel.skip (no task or done)")

        # 2. Shut down the socket so the worker thread's recv returns.
        import socket as _socket
        def _shutdown_socket(label: str, obj):
            _trace(f"2.shutdown.{label}.begin")
            try:
                sock_wrap = getattr(obj, "_sock", None)
                raw = getattr(sock_wrap, "sock", None) if sock_wrap is not None else None
                if raw is None:
                    _trace(f"2.shutdown.{label}.no_socket")
                    return
                try:
                    raw.shutdown(_socket.SHUT_RDWR)
                    _trace(f"2.shutdown.{label}.shutdown_ok")
                except Exception as exc:
                    _trace(f"2.shutdown.{label}.shutdown_exc={type(exc).__name__}:{exc}")
                # close() releases the fd + the CIP connection. shutdown() alone
                # unblocks a pending recv but leaves the connection allocated on
                # the PLC — leaking one connection slot per restart until the
                # controller's CIP limit is hit and open() starts hanging.
                try:
                    raw.close()
                except Exception:
                    pass
            except Exception as exc:
                _trace(f"2.shutdown.{label}.outer_exc={type(exc).__name__}:{exc}")
        _shutdown_socket("ab_pycomm3", w._ab_pycomm3_client)
        _shutdown_socket("opc", w._opc_client)

        # 3. Null the client references.
        _trace("3.null_refs.begin")
        try:
            w._ab_pycomm3_client = None
            w._ab_pycomm3_path = None
            w._ab_pylogix_client = None
            w._ab_pylogix_ip = None
            w._ab_pylogix_slot = None
            w._opc_client = None
            w._opc_endpoint = ""
            _trace("3.null_refs.done")
        except Exception as exc:
            _trace(f"3.null_refs.exc={type(exc).__name__}:{exc}")

        # 3b. CRITICAL — retire the collection executor and the connect lock.
        #
        # A stall means a read/connect thread is HUNG inside the dedicated
        # ThreadPoolExecutor (Python can't cancel it) — and if it hung mid
        # `plc.open()` it still holds `_ab_connect_lock`. The old code reused
        # both across the restart, so every restart added another zombie thread
        # to the 3-slot pool and the new worker blocked on the still-held lock.
        # After ~3 stalls the pool was fully saturated -> the new cycle could
        # never even start -> the event loop went stale -> the whole process was
        # killed (the death-spiral). Swap in a FRESH executor and a FRESH lock so
        # the new worker starts clean; the zombie thread drains into the orphaned
        # pool and dies on its own when its socket finally times out.
        _trace("3b.retire_executor.begin")
        try:
            w._orphan_collection_runtime()
            _trace("3b.retire_executor.done")
        except Exception as exc:
            _trace(f"3b.retire_executor.exc={type(exc).__name__}:{exc}")

        # 4. Operator 2026-06-25 (12h soak finding): the prior
        # asyncio.wait_for(asyncio.shield(task), timeout=2.0) was the
        # wedge point. After the socket shutdown in step 2 (verified
        # by 2.shutdown.ab_pycomm3.shutdown_ok in traces) the
        # cancelled task unwinds asynchronously on its own — the
        # `run-loop-exit reason=cancelled` log line confirms it
        # always finishes. The await on the shielded task NEVER
        # returned in 4 separate stall events across the 12h soak;
        # whether asyncio's shield + cancel ordering deadlocks here
        # or a future state issue, the await is the bug.
        #
        # We don't actually need to wait: the task's own teardown is
        # non-blocking once the socket is closed, and the new run
        # loop we spawn in step 6 doesn't share state with the old
        # one (we nulled all client refs in step 3). Worst case, the
        # cancelled task lingers a few ms before its run-loop-exit
        # callback fires — harmless.
        _trace("4.wait_for.skipped (no-await design)")

        # 5. Reset runtime state.
        _trace("5.reset_state.begin")
        w._last_progress_mono = 0.0
        w._startup_started_monotonic = time.monotonic()
        w._stalled_read_cycles = 0
        w._cycle_overrun_streak = 0
        w.last_error = (
            f"Restarted by supervisor watchdog after {idle_s:.0f}s stall."
        )
        w.latest_readings = []
        _trace("5.reset_state.done")

        # 6. Mark restart bookkeeping and spawn fresh task.
        _trace("6.spawn.begin")
        w.restart_count += 1
        w._last_restart_utc = datetime.now(timezone.utc).isoformat()
        w.running = True
        w._last_progress_mono = time.monotonic()
        try:
            # V2 engine: restart = stop + start a fresh reader thread. The
            # asyncio-task spawn below is the V1 path only.
            from app.services.collection_engine import engine_v2, engine_v2_enabled
            if engine_v2_enabled() and hasattr(self, "fanout_threadsafe"):
                engine_v2.stop_reader(gateway_id)
                w._task = None
                engine_v2.start_reader(w, self)
                _trace("6.spawn.done (v2 reader)")
            else:
                w._task = asyncio.create_task(w._run_loop(self._broadcast))
                _trace("6.spawn.done")
        except Exception as exc:
            _trace(f"6.spawn.exc={type(exc).__name__}:{exc}")
            raise

        def _on_restart_done(t: "asyncio.Task[Any]", gid: str = gateway_id) -> None:
            try:
                if t.cancelled():
                    _GW_LOG.info("run-loop-exit gateway=%s reason=cancelled (watchdog-restarted)", gid)
                    return
                exc = t.exception()
                if exc is not None:
                    _GW_LOG.error(
                        "run-loop-exit gateway=%s reason=exception (watchdog-restarted) exc=%s: %s",
                        gid, type(exc).__name__, exc,
                    )
                    return
                _GW_LOG.info("run-loop-exit gateway=%s reason=clean-return (watchdog-restarted)", gid)
            except Exception:
                pass

        if w._task is not None:  # V1 only — the V2 reader thread has no task
            w._task.add_done_callback(_on_restart_done)

    async def _watchdog_loop(self) -> None:
        try:
            while True:
                try:
                    await asyncio.sleep(self._watchdog_interval_s)
                    # 1. Stall scan over running workers.
                    for gid, w in list(self.workers.items()):
                        try:
                            stalled, idle = w.is_stalled()
                            if stalled:
                                await self._restart_worker_due_to_stall(gid, idle)
                        except Exception:
                            continue
                    # 2. Schedule + auto-recover supervisor scan over
                    # the CONFIGURED gateway list (includes stopped
                    # ones). Wrapped so any failure here can't take
                    # down the stall watchdog.
                    try:
                        await self._supervisor_scan()
                    except Exception:
                        try:
                            import logging
                            logging.getLogger("trustnode.supervisor").exception(
                                "supervisor scan failed"
                            )
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    raise
                except Exception:
                    try:
                        import logging
                        logging.getLogger("trustnode.watchdog").exception(
                            "watchdog scan failed"
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(self._watchdog_interval_s)
        except asyncio.CancelledError:
            pass

    async def _supervisor_scan(self) -> None:
        """Schedule + auto-recover supervisor.

        For every CONFIGURED gateway, decide what to do based on the
        per-gateway `schedule_enabled` and `auto_recover_enabled`
        flags + the operator's explicit-stop state.

        Policy:
        - SCHEDULE: if schedule_enabled, the gateway must be running
          inside [schedule_start, schedule_stop] (local time, daily)
          and stopped outside. The start crossing also clears the
          user-stopped flag so auto-recover takes over for the day.
        - AUTO-RECOVER: if auto_recover_enabled and the gateway is
          NOT currently running and was NOT explicitly stopped by
          the operator, restart it. Honors the existing burst
          cooldown so a flapping PLC doesn't get hammered.
        Rate-limited to one action per gateway per 30s.
        """
        now_mono = time.monotonic()
        from datetime import datetime as _dt
        # Local time-of-day for schedule comparison.
        local_now = _dt.now()
        cur_minutes = local_now.hour * 60 + local_now.minute

        # Read configured gateways from the app_store bootstrap.
        # Operator 2026-06-25 (tenant-scoping fix): scan EVERY scope's
        # gateway_configurations doc, not just the unscoped bootstrap.
        # The unscoped bootstrap is often empty for tenants whose
        # gateways live in per-tenant scoped docs (e.g. tenant-cust-*
        # |cust-*|edge-*). Reading directly from SQLite catches every
        # gateway regardless of scope.
        try:
            from app.state import app_store as _store
            db_path = getattr(_store, "_db_path", None)
            if not db_path:
                return

            # This scan runs every _watchdog_interval_s on the EVENT LOOP. The
            # SQLite connect + queries + JSON parse below (timeout 3s) blocked
            # the loop each tick, adding jitter to collection cadence and, on a
            # locked DB, contributing to the "event loop stale" kills. Offload
            # the whole read to a thread; the policy logic after stays on-loop.
            def _read_config_rows():
                import sqlite3 as _sqlite3
                import json as _json
                _gw: list = []
                _db: list = []
                _seen_gw: set = set()
                _seen_db: set = set()
                con = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
                try:
                    for (payload,) in con.execute(
                        "SELECT payload_json FROM config_documents_scoped WHERE domain='gateway_configurations'"
                    ):
                        try:
                            data = _json.loads(payload) if isinstance(payload, str) else payload
                        except Exception:
                            continue
                        items = data if isinstance(data, list) else (data.get('gateways') or data.get('items') or [])
                        for g in items:
                            if not isinstance(g, dict): continue
                            gid = str(g.get('id') or '').strip()
                            if not gid or gid in _seen_gw: continue
                            _seen_gw.add(gid)
                            _gw.append(g)
                    for (payload,) in con.execute(
                        "SELECT payload_json FROM config_documents_scoped WHERE domain='database_configurations'"
                    ):
                        try:
                            data = _json.loads(payload) if isinstance(payload, str) else payload
                        except Exception:
                            continue
                        items = data if isinstance(data, list) else (data.get('databases') or data.get('items') or [])
                        for d in items:
                            if not isinstance(d, dict): continue
                            did = str(d.get('id') or '').strip()
                            if not did or did in _seen_db: continue
                            _seen_db.add(did)
                            _db.append(d)
                finally:
                    con.close()
                return _gw, _db

            gw_rows, db_rows = await asyncio.to_thread(_read_config_rows)
        except Exception:
            return

        db_by_id = {str(d.get("id") or ""): d for d in db_rows if isinstance(d, dict)}

        def _hhmm_to_minutes(text: str, default: int) -> int:
            try:
                parts = str(text or "").split(":")
                h = int(parts[0]); m = int(parts[1]) if len(parts) > 1 else 0
                return max(0, min(23 * 60 + 59, h * 60 + m))
            except Exception:
                return default

        # Read the persisted last_running set once per scan — used by
        # the baseline restart policy below. Best-effort: if telemetry
        # is briefly unavailable we just skip baseline recovery this
        # tick (the next 10s scan retries).
        last_running_ids: set[str] = set()
        try:
            from app.state import telemetry_service as _ts
            # list_running_gateways() opens the telemetry DB (2 GB) — if a
            # collection thread holds its write lock, sqlite3.connect blocks up
            # to its timeout ON THE EVENT LOOP. Two such blocks back-to-back
            # tripped the "event loop stale" process-kill. Offload to a thread.
            last_running_ids = set(await asyncio.to_thread(_ts.list_running_gateways) or [])
        except Exception:
            pass

        for gw in gw_rows:
            if not isinstance(gw, dict):
                continue
            gid = str(gw.get("id") or "").strip()
            if not gid:
                continue
            # Rate limit.
            last = self._last_supervisor_action_mono.get(gid, 0.0)
            if now_mono - last < self._supervisor_min_interval_s:
                continue

            schedule_on = bool(gw.get("schedule_enabled"))
            # auto_recover_enabled is now a DISABLE switch (default
            # True). Operator can flip it off to suppress baseline
            # recovery for a specific gateway. The default behavior is
            # "if it was running and isn't, bring it back" — the
            # explicit Stop button is the only way to keep a gateway
            # down by user intent.
            auto_recover = bool(gw.get("auto_recover_enabled", True))
            in_window = True
            if schedule_on:
                start_m = _hhmm_to_minutes(gw.get("schedule_start"), 8 * 60)
                stop_m = _hhmm_to_minutes(gw.get("schedule_stop"), 18 * 60)
                if start_m == stop_m:
                    in_window = False
                elif start_m < stop_m:
                    in_window = start_m <= cur_minutes < stop_m
                else:
                    # Wraps midnight.
                    in_window = cur_minutes >= start_m or cur_minutes < stop_m

            # A STALLED worker (running=True but no read cycle for
            # threshold seconds — e.g. wedged in a driver call after a PLC
            # power-cycle) must count as NOT running for recovery, or the
            # supervisor sees running=True and never brings it back. This is
            # exactly why a gateway stayed dead after the PLC came back.
            _w = self.workers.get(gid)
            _stalled = False
            if _w is not None:
                try:
                    _stalled, _ = _w.is_stalled()
                except Exception:
                    _stalled = False
            running = (_w is not None and _w.running and not _stalled)
            should_run = (not schedule_on) or in_window

            # Decide.
            if schedule_on and not in_window and running:
                try:
                    await self.stop_gateway(gid)
                    self._user_stopped.discard(gid)  # schedule, not user
                    self._last_supervisor_action_mono[gid] = now_mono
                except Exception:
                    pass
                continue

            if schedule_on and in_window and not running:
                # Schedule says go. Clear user-stopped (new shift =
                # fresh state) and start.
                self._user_stopped.discard(gid)
                if await self._supervisor_try_start(gid, gw, db_by_id):
                    self._last_supervisor_action_mono[gid] = now_mono
                continue

            # Baseline recovery: if the gateway was last persisted as
            # running, the operator did NOT explicitly Stop it, the
            # disable-recover switch is OFF, and the schedule (if any)
            # allows it — bring it back. This is the "watchdog
            # restores the gateway that was running" guarantee: a PLC
            # drop, a worker crash, even a backend restart can never
            # leave a previously-running gateway down for more than
            # the next supervisor tick (~10s) + start latency.
            was_running = gid in last_running_ids
            if (
                auto_recover
                and was_running
                and not running
                and gid not in self._user_stopped
                and should_run
            ):
                if await self._supervisor_try_start(gid, gw, db_by_id):
                    self._last_supervisor_action_mono[gid] = now_mono
                continue

    async def _supervisor_try_start(self, gateway_id: str, gw: Dict[str, Any], db_by_id: Dict[str, Dict[str, Any]]) -> bool:
        """Start a gateway from its persisted config + db_sink.
        Returns True on success, False on any failure (logged)."""
        try:
            db_id = str(gw.get("database_id") or "")
            db_cfg = db_by_id.get(db_id) or {}
            # Operator 2026-06-25: same fallback the frontend uses.
            # If the configured database_id can't be resolved in any
            # scope, default to the Local SQLite — historian rows
            # land in app_store. Avoids "DB not found" deadlock when
            # config refers to a deleted/cross-scope DB.
            if not db_cfg:
                for cand in db_by_id.values():
                    if str(cand.get("engine") or "").lower() == "sqlite":
                        db_cfg = cand
                        break
            if not db_cfg:
                db_cfg = {
                    "id": "local-sqlite-default",
                    "name": "Local SQLite",
                    "engine": "sqlite",
                    "sqlite_path": "./data/trustnode_app_store.db",
                    "table": "plc_readings",
                }
            sink = None
            if db_cfg:
                sink = {
                    "id": str(db_cfg.get("id") or ""),
                    "name": str(db_cfg.get("name") or ""),
                    "engine": db_cfg.get("engine"),
                    "host": str(db_cfg.get("host") or ""),
                    "port": int(db_cfg.get("port") or 0),
                    "database": str(db_cfg.get("database") or ""),
                    "username": str(db_cfg.get("username") or ""),
                    "password": db_cfg.get("password") or "",
                    "sqlite_path": str(db_cfg.get("sqlite_path") or ""),
                    "file_path": str(db_cfg.get("file_path") or ""),
                    "legacy_url": str(db_cfg.get("legacy_url") or ""),
                    "legacy_api_token": str(db_cfg.get("legacy_api_token") or ""),
                    "source": str(db_cfg.get("source") or ""),
                    "site": str(db_cfg.get("site") or ""),
                    "area": str(db_cfg.get("area") or ""),
                    "equipment": str(db_cfg.get("equipment") or ""),
                    "schema": str(db_cfg.get("schema") or "public"),
                    "table": str(db_cfg.get("table") or "plc_readings"),
                    "tls": bool(db_cfg.get("tls")),
                    "tag_filters": [],
                    "gateway_filters": [],
                    "csv_format": "",
                    "csv_header": "",
                }
            config = GatewayConfig(
                gateway_type=str(gw.get("gateway_type") or "allen_bradley"),
                plc_ip=str(gw.get("plc_ip") or ""),
                opc_url=str(gw.get("opc_url") or ""),
                tags=list(gw.get("tags") or []),
                interval_ms=int(gw.get("interval_ms") or 1000),
                site=str(gw.get("site") or ""),
                area=str(gw.get("area") or ""),
                equipment=str(gw.get("equipment") or ""),
                collection_triggers=list(gw.get("collection_triggers") or []),
                collection_trigger_mode=str(gw.get("collection_trigger_mode") or "any"),
                schedule_enabled=bool(gw.get("schedule_enabled")),
                schedule_start=str(gw.get("schedule_start") or "08:00"),
                schedule_stop=str(gw.get("schedule_stop") or "18:00"),
                auto_recover_enabled=bool(gw.get("auto_recover_enabled")),
            )
            await self.start_gateway(
                gateway_id=gateway_id,
                config=config,
                db_sink=sink,
                db_sinks=[sink] if sink else [],
            )
            try:
                import logging
                logging.getLogger("trustnode.supervisor").info(
                    "supervisor-start gateway=%s", gateway_id,
                )
            except Exception:
                pass
            return True
        except Exception as exc:
            try:
                import logging
                logging.getLogger("trustnode.supervisor").warning(
                    "supervisor-start-failed gateway=%s exc=%s: %s",
                    gateway_id, type(exc).__name__, exc,
                )
            except Exception:
                pass
            return False

    def _get_or_create_worker(
        self,
        gateway_id: str,
        config: GatewayConfig,
        db_sink: Dict[str, Any] | None,
        db_sinks: List[Dict[str, Any]] | None = None,
    ) -> GatewayWorker:
        if gateway_id in self.workers:
            w = self.workers[gateway_id]
            w.set_config(config)
            w.set_db_sink(db_sink, db_sinks)
            w.set_collection_gate_cb(self._evaluate_global_collection_gate)
            return w
        if len(self.workers) >= self.max_gateways:
            raise ValueError(f"Gateway limit reached ({self.max_gateways})")
        w = GatewayWorker(
            gateway_id=gateway_id,
            config=config,
            db_sink=db_sink,
            db_sinks=db_sinks,
            collection_gate_cb=self._evaluate_global_collection_gate,
        )
        self.workers[gateway_id] = w
        return w

    @staticmethod
    def _endpoint_key(config: GatewayConfig) -> tuple[str, str]:
        gateway_type = str(getattr(config, "gateway_type", "") or "").strip().lower()
        plc_ip = str(getattr(config, "plc_ip", "") or "").strip().lower()
        opc_url = str(getattr(config, "opc_url", "") or "").strip().lower()
        endpoint = opc_url if gateway_type == "siemens_opcua" else plc_ip
        return gateway_type, endpoint

    async def start_gateway(
        self,
        gateway_id: str,
        config: GatewayConfig,
        db_sink: Dict[str, Any] | None,
        db_sinks: List[Dict[str, Any]] | None = None,
    ) -> None:
        # Keep runtime deterministic: only one active worker per physical endpoint.
        # Duplicate workers on the same PLC endpoint produce visible chart jitter/noise.
        target_key = self._endpoint_key(config)
        stale_workers: List[str] = []
        for existing_id, existing_worker in list(self.workers.items()):
            if existing_id == gateway_id:
                continue
            existing_key = self._endpoint_key(existing_worker.config)
            if target_key[1] and existing_key == target_key:
                stale_workers.append(existing_id)
        for stale_id in stale_workers:
            await self.stop_gateway(stale_id)

        # 2026-07-19 RECOVERY FIX: if a worker already exists but is NOT
        # cleanly running (watchdog cooled it down after a PLC power-cycle, a
        # stall left an abandoned task/driver session, or it's a zombie), do a
        # full teardown and recreate a FRESH worker. Reusing a half-dead worker
        # was why a manual Start (and auto-recover) did nothing: GatewayWorker
        # .start() early-returns `if self.running`, and the stale run-loop
        # task + PLC driver client never got disposed. Only reuse a worker that
        # is genuinely, cleanly running (idempotent re-start of a healthy gw).
        existing = self.workers.get(gateway_id)
        if existing is not None:
            stalled, _idle = existing.is_stalled()
            if (not existing.running) or stalled:
                await self.stop_gateway(gateway_id)  # dispose task + drivers, drop from map
        # Clear any prior watchdog cooldown/history so a manual restart frees
        # the gateway from a "gave up" state — BEFORE (re)creating the worker.
        self._restart_cooldown_until_mono.pop(gateway_id, None)
        self._restart_history.pop(gateway_id, None)
        self._user_stopped.discard(gateway_id)
        w = self._get_or_create_worker(gateway_id, config, db_sink, db_sinks)
        self._refresh_global_triggers()
        self.active_gateway_id = gateway_id
        await w.start(self._broadcast)
        # Make sure the supervisor watchdog is running. Cheap no-op if it is.
        self._ensure_watchdog_running()

    async def stop_gateway(self, gateway_id: str) -> None:
        w = self.workers.get(gateway_id)
        if not w:
            return
        await w.stop()
        self.workers.pop(gateway_id, None)
        if self.active_gateway_id == gateway_id:
            self.active_gateway_id = ""
        self._clear_gateway_live_values(gateway_id)
        self._refresh_global_triggers()
        # Operator 2026-06-25: broadcast a status event so the UI knows
        # to flip the pill from RUNNING to STOPPED IMMEDIATELY,
        # without waiting for the 20s "no fresh sample" heuristic that
        # was triggering the "Gateway not collecting" banner after a
        # legitimate Stop.
        try:
            await self._broadcast({
                "type": "gateway_status",
                "gateway_id": gateway_id,
                "running": False,
                "reason": "stopped",
            })
        except Exception:
            pass

    async def stop_all_gateways(self) -> None:
        # Operator 2026-06-18: reverted to the original sequential stop.
        # The asyncio.gather variant shipped this morning sped up bulk
        # shutdown but appears to interact with the AB / OPC-UA drivers
        # in ways the original sequential await never did. Returning to
        # the simple loop until the start/stop UX work can resume on a
        # reproducible test rig.
        for gid, w in list(self.workers.items()):
            await w.stop()
            self.workers.pop(gid, None)
        self.active_gateway_id = ""
        self.global_live_values = {}
        self._refresh_global_triggers()
        # No workers left -> watchdog has nothing to scan.
        await self._stop_watchdog()

    def list_gateway_statuses(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen_ids = set()
        for gid, w in self.workers.items():
            status = w.get_status().model_dump()
            status["gateway_id"] = gid
            out.append(status)
            seen_ids.add(str(gid))
        # Also surface the ACTIVE worker if it runs via the legacy single-gateway
        # path (started before the multi-gateway map, so it isn't in self.workers).
        # Without this, /api/plc/gateways/status returned [] while /api/plc/status
        # showed running:true — a status desync that made the multi-gateway view
        # misreport a collecting gateway as stopped. Read-only; never touches
        # collection. We attach the resolved gateway id (active_gateway_id or the
        # legacy "default") so the row lines up with the configured gateway.
        try:
            active = self.get_status()  # returns the running worker's status if any
            if getattr(active, "running", False):
                aid = str(self.active_gateway_id or "").strip()
                if not aid or aid not in seen_ids:
                    st = active.model_dump()
                    st["gateway_id"] = aid or "default"
                    out.append(st)
        except Exception:
            pass
        return out

    def get_gateway_snapshot(self, gateway_id: str) -> List[GatewayReading]:
        w = self.workers.get(gateway_id)
        return w.latest_readings[:] if w else []

    # Backward compatibility (single-gateway endpoints).
    def set_config(self, new_config: GatewayConfig) -> GatewayConfig:
        self.legacy_config = new_config
        return self.legacy_config

    def get_config(self) -> GatewayConfig:
        if self.active_gateway_id and self.active_gateway_id in self.workers:
            return self.workers[self.active_gateway_id].config
        return self.legacy_config

    def get_status(self) -> GatewayStatus:
        if self.active_gateway_id and self.active_gateway_id in self.workers:
            st = self.workers[self.active_gateway_id].get_status()
            if st.running:
                return st
        for w in self.workers.values():
            st = w.get_status()
            if st.running:
                return st
        return GatewayStatus(
            running=False,
            gateway_type=self.legacy_config.gateway_type,
            plc_ip=self.legacy_config.plc_ip,
            interval_ms=self.legacy_config.interval_ms,
            tags=self.legacy_config.tags,
            last_error=None,
            db_sink_engine=None,
            db_write_count=0,
            db_last_write_utc=None,
            db_last_error=None,
            db_pending_count=0,
            collection_blocked=not self.global_collection_allowed,
            collection_block_reason=self.global_collection_reason,
        )

    def get_snapshot(self) -> List[GatewayReading]:
        if self.active_gateway_id and self.active_gateway_id in self.workers:
            return self.workers[self.active_gateway_id].latest_readings[:]
        return []

    async def start(self) -> None:
        await self.start_gateway("default", self.legacy_config, None)

    async def stop(self) -> None:
        await self.stop_all_gateways()

    def set_db_sink(self, sink: Dict[str, Any] | None) -> None:
        # Keep for legacy database activation flow.
        if "default" in self.workers:
            self.workers["default"].set_db_sink(sink)

    def get_db_sink(self) -> Dict[str, Any] | None:
        if "default" in self.workers and self.workers["default"].db_sink:
            safe = dict(self.workers["default"].db_sink)
            if "password" in safe:
                safe["password"] = "***"
            if "legacy_api_token" in safe:
                safe["legacy_api_token"] = "***"
            return safe
        return None
