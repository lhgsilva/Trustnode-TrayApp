# -*- coding: utf-8 -*-
"""What this machine is doing, and how much of it is TrustNode.

2026-08-28, for "one dashboard diagnostics that shows the computer status in
terms of memory and cpu and network use, details of how much of this is from
TrustNode services, and diagnostics of data being collected and transferred".

Three questions, answered side by side, because in isolation each one lies:

  machine   - what the box has and what is left of it;
  trustnode - what OUR processes take of that. "The CPU is at 80%" means
              something quite different when 5% of it is ours;
  pipeline  - what the data path is doing: read, store, distribute, forward.

Two rules this module exists to obey.

**Never block the event loop.** `/api/health` was once an `async def` that used
`to_thread`, and it starved the shared anyio pool - historian gaps and a slow
UI followed. Everything here is plain sync, called from a plain `def` route so
FastAPI runs it in the threadpool, and it is cached so a dashboard polling every
second cannot turn into a load source of its own.

**Never scan the historian.** The app store is 13 GB on this install; a
`COUNT(*)` to answer "how many rows have we written" would cost more than the
collection it is reporting on. Counters come from the workers' own in-memory
stamps and from file sizes.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

try:  # bundled with the backend (see trustnode-service.spec)
    import psutil  # type: ignore
except Exception:  # pragma: no cover - psutil missing is not fatal
    psutil = None  # type: ignore

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_CACHE_TTL_S = 2.0

# Per-process CPU is a delta between two calls, so the Process objects have to
# survive between requests - a fresh psutil.Process() always reports 0.0%.
_PROCS: dict[int, Any] = {}
_NET_PREV: dict[str, Any] = {"at": 0.0, "sent": 0, "recv": 0}
# Machine CPU is computed from cpu_times() deltas WE keep, not from psutil's
# implicit "since the last call" baseline. That baseline is a module global
# whose first read is documented as a meaningless 0.0, and it did not survive
# the hop from the warm-up thread to the request thread here - the page showed
# "0.0% CPU" on the first load, which is the one thing a diagnostics page must
# never say by accident. cpu_times() is stateless, so this is deterministic.
_CPU_PREV: dict[str, float] = {"busy": 0.0, "total": 0.0}

# Finding our processes means walking EVERY process on the box - measured at
# 1.75 s on this machine. Sampling the ones we already know costs ~0. So the
# discovery is cached far longer than the snapshot: a new TrustNode process is
# worth noticing within half a minute, not within two seconds.
_DISCOVERY: dict[str, Any] = {"at": 0.0, "pids": {}}
_DISCOVERY_TTL_S = 30.0

# Which processes are "TrustNode". Matched on the executable name, lowercased.
_OURS = {
    "trustnode-service.exe": "backend service",
    "trustnode-service": "backend service",
    "trustnode.exe": "desktop app",
    "trustnode": "desktop app",
    "electron.exe": "desktop app (dev)",
}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _machine() -> dict[str, Any]:
    if psutil is None:
        return {"available": False, "reason": "psutil is not available in this build"}

    vm = _safe(psutil.virtual_memory)
    # Busy fraction between this call and the last one. Blocking for an
    # interval instead would hold a threadpool slot for that whole time.
    cpu = 0.0
    times = _safe(psutil.cpu_times)
    if times is not None:
        total = float(sum(times))
        idle = float(getattr(times, "idle", 0.0) or 0.0)
        # Time stolen by other VMs, and time waiting on I/O, are not this
        # machine doing work. Absent on Windows, hence the getattr.
        idle += float(getattr(times, "iowait", 0.0) or 0.0)
        busy = total - idle
        d_total = total - float(_CPU_PREV["total"])
        d_busy = busy - float(_CPU_PREV["busy"])
        if _CPU_PREV["total"] and d_total > 0:
            cpu = max(0.0, min(100.0, 100.0 * d_busy / d_total))
        _CPU_PREV["total"] = total
        _CPU_PREV["busy"] = busy
    per_cpu = _safe(lambda: psutil.cpu_percent(interval=None, percpu=True), []) or []

    data_dir = os.environ.get("TRUSTNODE_DATA_DIR") or os.path.expanduser("~/.trustnode_edge/data")
    disk = _safe(lambda: psutil.disk_usage(data_dir if os.path.isdir(data_dir) else os.path.abspath(os.sep)))

    net = _safe(psutil.net_io_counters)
    now = time.monotonic()
    rate_sent = rate_recv = 0.0
    if net is not None:
        prev_at = float(_NET_PREV.get("at") or 0.0)
        span = now - prev_at
        if prev_at and span > 0.05:
            rate_sent = max(0.0, (net.bytes_sent - int(_NET_PREV["sent"])) / span)
            rate_recv = max(0.0, (net.bytes_recv - int(_NET_PREV["recv"])) / span)
        _NET_PREV.update({"at": now, "sent": net.bytes_sent, "recv": net.bytes_recv})

    boot = _safe(psutil.boot_time, 0.0) or 0.0
    return {
        "available": True,
        "cpu": {
            "percent": round(float(cpu or 0.0), 1),
            "cores_logical": _safe(lambda: psutil.cpu_count(logical=True), 0),
            "cores_physical": _safe(lambda: psutil.cpu_count(logical=False), 0),
            "per_core": [round(float(x), 1) for x in per_cpu],
        },
        "memory": {
            "total_bytes": int(getattr(vm, "total", 0) or 0),
            "used_bytes": int(getattr(vm, "used", 0) or 0),
            "available_bytes": int(getattr(vm, "available", 0) or 0),
            "percent": round(float(getattr(vm, "percent", 0.0) or 0.0), 1),
        },
        "disk": {
            "path": data_dir,
            "total_bytes": int(getattr(disk, "total", 0) or 0),
            "used_bytes": int(getattr(disk, "used", 0) or 0),
            "free_bytes": int(getattr(disk, "free", 0) or 0),
            "percent": round(float(getattr(disk, "percent", 0.0) or 0.0), 1),
        },
        "network": {
            "bytes_sent": int(getattr(net, "bytes_sent", 0) or 0),
            "bytes_recv": int(getattr(net, "bytes_recv", 0) or 0),
            "send_bytes_per_s": round(rate_sent, 1),
            "recv_bytes_per_s": round(rate_recv, 1),
        },
        "uptime_s": int(max(0.0, time.time() - boot)) if boot else 0,
    }


def _trustnode_processes() -> dict[str, Any]:
    """Our own footprint: this backend, its children, and the desktop shell."""
    if psutil is None:
        return {"available": False, "processes": [], "totals": {}}

    me = _safe(lambda: psutil.Process(os.getpid()))
    wanted: dict[int, tuple[Any, str]] = {}

    if me is not None:
        wanted[me.pid] = (me, "backend service")
        for child in _safe(lambda: me.children(recursive=True), []) or []:
            wanted.setdefault(child.pid, (child, "backend child"))

    # The desktop shell is a separate process tree, so it has to be found by
    # name - the expensive part. Re-use the last scan until it goes stale, and
    # drop any of its pids that have since exited.
    now = time.monotonic()
    fresh = (now - float(_DISCOVERY.get("at") or 0.0)) < _DISCOVERY_TTL_S
    if fresh:
        for pid, (proc, role) in dict(_DISCOVERY["pids"]).items():
            if not _safe(proc.is_running, False):
                _DISCOVERY["pids"].pop(pid, None)
                continue
            wanted.setdefault(pid, (proc, role))
    else:
        found: dict[int, tuple[Any, str]] = {}
        for proc in _safe(lambda: psutil.process_iter(["name"]), []) or []:
            try:
                name = str((proc.info or {}).get("name") or "").lower()
            except Exception:
                continue
            role = _OURS.get(name)
            if role:
                found[proc.pid] = (proc, role)
                wanted.setdefault(proc.pid, (proc, role))
        _DISCOVERY["pids"] = found
        _DISCOVERY["at"] = now

    # Drop Process objects for processes that have gone away, so the cache of
    # CPU baselines cannot grow without bound over a 24/7 run.
    for pid in list(_PROCS):
        if pid not in wanted:
            _PROCS.pop(pid, None)

    rows: list[dict[str, Any]] = []
    total_cpu = 0.0
    total_rss = 0
    for pid, (proc, role) in wanted.items():
        handle = _PROCS.setdefault(pid, proc)
        try:
            cpu = float(handle.cpu_percent(interval=None) or 0.0)
            mem = handle.memory_info()
            rows.append({
                "pid": pid,
                "name": _safe(handle.name, "?"),
                "role": role,
                "cpu_percent": round(cpu, 1),
                "rss_bytes": int(getattr(mem, "rss", 0) or 0),
                "threads": _safe(handle.num_threads, 0),
                "started_utc": _safe(
                    lambda: time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.gmtime(handle.create_time())), ""),
            })
            total_cpu += cpu
            total_rss += int(getattr(mem, "rss", 0) or 0)
        except Exception:
            _PROCS.pop(pid, None)

    rows.sort(key=lambda r: (-r["cpu_percent"], -r["rss_bytes"]))
    cores = _safe(lambda: psutil.cpu_count(logical=True), 1) or 1
    return {
        "available": True,
        "processes": rows,
        "totals": {
            "process_count": len(rows),
            # psutil reports per-process CPU against ONE core, so a 4-core box
            # can legitimately show 400%. Normalise so it is comparable with
            # the machine figure, which is already 0-100 across all cores.
            "cpu_percent": round(total_cpu / max(1, int(cores)), 1),
            "cpu_percent_of_one_core": round(total_cpu, 1),
            "rss_bytes": total_rss,
        },
    }


def _file_bytes(path: str) -> int:
    try:
        return int(os.path.getsize(path))
    except Exception:
        return 0


def _storage() -> dict[str, Any]:
    """How big the stores are. File sizes only - never a row scan."""
    try:
        from app.state import app_store as _store
        db_path = str(getattr(_store, "_db_path", "") or "")
    except Exception:
        db_path = ""
    out = {"app_store_path": db_path}
    if db_path:
        out["app_store_bytes"] = _file_bytes(db_path)
        # A WAL that keeps growing is the visible symptom of a reader holding a
        # snapshot open, or of checkpoints not completing.
        out["wal_bytes"] = _file_bytes(db_path + "-wal")

    # Is anything ever deleted? Measured on this install 2026-08-28: NO
    # retention policy existed, so the store had grown to 13.4 GB in 4.7 days
    # and would have filled the disk in about 30 days with no warning anywhere.
    # A store that only grows is the failure mode that ends collection outright,
    # so it belongs on the page, not in a log.
    try:
        from app.state import retention_engine as _ret
        status = _ret.status() if hasattr(_ret, "status") else {}
        policy = (status or {}).get("policy")
        out["retention_policy"] = (policy or {}).get("name") if isinstance(policy, dict) else None
        out["retention_active"] = bool(policy)
        levels = (status or {}).get("levels") or []
        out["retention_levels"] = [
            {"key": l.get("key"), "rows": l.get("rows"), "keep": l.get("keep")}
            for l in levels if isinstance(l, dict)
        ]
        db = (status or {}).get("database") or {}
        out["oldest_raw_utc"] = db.get("oldest_raw_utc")
        out["raw_rows"] = db.get("raw_rows")
    except Exception as exc:
        out["retention_error"] = str(exc)[:200]
    return out


def _pipeline() -> dict[str, Any]:
    """Collected, stored, distributed, forwarded - per gateway and per meter.

    Every figure is a stamp the worker already keeps. Nothing here queries the
    historian: the durable truth for "is it storing" is
    `historian_write_count`, which is stamped on the commit path itself.
    """
    out: dict[str, Any] = {"gateways": [], "meters": [], "transfer": {}}

    try:
        from app.state import plc_manager
        for row in (plc_manager.list_gateway_statuses() or []):
            if not isinstance(row, dict):
                continue
            out["gateways"].append({
                "gateway_id": row.get("gateway_id"),
                "name": row.get("gateway_name") or row.get("name"),
                "gateway_type": row.get("gateway_type"),
                "running": bool(row.get("running")),
                "interval_ms": row.get("interval_ms"),
                "tag_count": row.get("tag_count"),
                "read_count": row.get("read_count"),
                # The durable stamp. `db_*` measures the LOSSY distribution
                # path and read as "no writes" for 5.6 h while the historian
                # was taking 48 rows/s - see the 2026-08-21 distribution wedge.
                "historian_write_count": row.get("historian_write_count"),
                "historian_last_write_utc": row.get("historian_last_write_utc"),
                "sink_write_count": row.get("sink_write_count"),
                "sink_last_write_utc": row.get("sink_last_write_utc"),
                "distribution_stage": row.get("distribution_stage"),
                "distribution_stalled_s": row.get("distribution_stalled_s"),
                "last_error": row.get("last_error"),
            })
    except Exception as exc:
        out["gateways_error"] = str(exc)[:200]

    try:
        from app.state import power_manager
        diag = power_manager.get_diagnostics() or {}
        status = diag.get("devices_status") or {}
        metrics = diag.get("devices_metrics") or {}
        meters = []
        for did in sorted(set(list(status.keys()) + list(metrics.keys()))):
            st = status.get(did) or {}
            mt = metrics.get(did) or {}
            meters.append({"device_id": did, "status": st, "metrics": mt})
        out["meters"] = meters
        out["meters_summary"] = {
            "enabled": diag.get("enabled"),
            "worker_count": diag.get("worker_count"),
            # The writer queue is the meter path's own back-pressure signal:
            # a depth that keeps climbing means storage is slower than polling.
            "writer_queue_depth": diag.get("writer_queue_depth"),
            "writer_dropped_rows": diag.get("writer_dropped_rows"),
            "writer_batches": diag.get("writer_batches"),
        }
    except Exception as exc:
        out["meters_error"] = str(exc)[:200]

    # Store-and-forward depth: how much is waiting to reach the cloud. This
    # opens the telemetry DB, which is small - but it is still I/O, so a
    # failure here must not cost the rest of the snapshot.
    try:
        from app.state import telemetry_service
        diag = telemetry_service.diagnostics() or {}
        out["transfer"] = {
            "outbox_depth": diag.get("outbox_depth"),
            "oldest_pending_utc": diag.get("oldest_unsynced_sample_ts_utc"),
            "by_gateway": diag.get("outbox_by_gateway"),
            "ingest_enabled": diag.get("ingest_enabled"),
            "last_outbox_error": diag.get("last_outbox_error"),
        }
    except Exception as exc:
        out["transfer_error"] = str(exc)[:200]

    return out


def snapshot(force: bool = False) -> dict[str, Any]:
    """The whole picture, cached for a couple of seconds.

    A diagnostics page polling once a second must not itself become load - and
    the CPU deltas are meaningless below about a second anyway.
    """
    now = time.monotonic()
    with _LOCK:
        if not force and _CACHE["data"] is not None and (now - float(_CACHE["at"])) < _CACHE_TTL_S:
            return _CACHE["data"]

    data = {
        "ok": True,
        "ts_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "machine": _machine(),
        "trustnode": _trustnode_processes(),
        "storage": _storage(),
        "pipeline": _pipeline(),
    }

    with _LOCK:
        _CACHE["at"] = time.monotonic()
        _CACHE["data"] = data
    return data


def _warm_up() -> None:
    """Pay the expensive parts once, in the background, before anyone asks.

    Two costs, both of which would otherwise land on the operator's first page
    load:

    * `process_iter` walks every process on the box - measured at ~2.2 s here;
    * psutil reports CPU as a delta since the previous call, so a first call
      with no baseline always answers 0.0% - which reads as "idle", the one
      thing a diagnostics page must not say by accident.

    Done on a daemon thread after a short delay so it competes with nothing
    during boot: the splash gives the backend 30 s to answer /api/health, and
    this must not eat into it.
    """
    if psutil is None:
        return

    def _run():
        time.sleep(5.0)
        # Run the REAL code paths, not stand-ins for them: `_machine` is what
        # seeds both psutil's CPU baseline and `_NET_PREV`, and seeding those
        # any other way is a second implementation that can drift from the one
        # that matters.
        _safe(_machine)
        _safe(_trustnode_processes)   # fills the discovery cache

    try:
        threading.Thread(target=_run, name="tn-diag-warmup", daemon=True).start()
    except Exception:
        pass


_warm_up()
