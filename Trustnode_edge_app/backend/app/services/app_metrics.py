# -*- coding: utf-8 -*-
"""Record the application's own resource use as historian tags.

2026-08-31: a customer's install ran for 24 hours and froze. Task Manager
showed a 1.8 GB process - which is the only reason anyone knew where to look,
and only because someone happened to open Task Manager at the right moment.
Sampling from outside for eight minutes proved nothing either way: the service
oscillated between 452 MB and 602 MB with no trend, and a leak that takes a day
to matter cannot be seen in eight minutes.

So the app measures itself, continuously, into the historian. Once these are
ordinary readings, every existing dashboard chart, alarm rule and report works
on them unchanged, and after a day of running the question "what grew?" has an
answer with a timestamp instead of a screenshot.

Deliberately cheap: one psutil pass every 30 s over this process and its
Electron siblings, eight numbers, appended through the same path as any other
reading. If psutil is missing or a process vanishes mid-sample the sampler
skips that tick rather than raising - diagnostics must never be able to harm
the thing they measure.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List

GATEWAY_ID = "trustnode-app-metrics"
GATEWAY_NAME = "TrustNode App"
DEVICE_NAME = "Edge process"
#: How often to sample. 30 s keeps a full day at 2 880 points per tag - dense
#: enough to see a slope, small enough to be invisible next to real collection.
INTERVAL_S = 30.0

#: The Electron shell and its renderers. Matched by name because they are
#: siblings of this process, not children of it: the desktop shell starts the
#: service, so walking children from here finds nothing.
_UI_NAME_HINTS = ("trustnode", "electron")


def _collect() -> List[Dict[str, Any]]:
    """One sample: this service, the UI processes, and the totals."""
    import psutil

    me = psutil.Process(os.getpid())
    svc_mem = 0.0
    svc_cpu = 0.0
    svc_threads = 0
    svc_handles = 0
    try:
        with me.oneshot():
            svc_mem = float(me.memory_info().rss) / (1024 * 1024)
            svc_cpu = float(me.cpu_percent(interval=None))
            svc_threads = int(me.num_threads())
            svc_handles = int(getattr(me, "num_handles", lambda: 0)())
    except Exception:
        return []

    ui_mem = 0.0
    ui_cpu = 0.0
    ui_procs = 0
    my_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pid = int(proc.info.get("pid") or 0)
            name = str(proc.info.get("name") or "").lower()
            if pid == my_pid or not name:
                continue
            if not any(h in name for h in _UI_NAME_HINTS):
                continue
            if "service" in name:          # the backend itself, counted above
                continue
            ui_mem += float(proc.memory_info().rss) / (1024 * 1024)
            ui_cpu += float(proc.cpu_percent(interval=None))
            ui_procs += 1
        except Exception:
            # A process that exits mid-scan is normal, not an error.
            continue

    def row(tag: str, value: float, unit: str) -> Dict[str, Any]:
        return {
            "gateway_id": GATEWAY_ID, "gateway_name": GATEWAY_NAME,
            "device_name": DEVICE_NAME, "database_name": "Local SQLite",
            "tag_name": tag, "value": float(value), "quality": 192,
            "quality_label": "GOOD", "source": "app_metrics",
            "data_type": "REAL", "unit": unit,
        }

    return [
        row("app_service_mem_mb", round(svc_mem, 1), "MB"),
        row("app_service_cpu_pct", round(svc_cpu, 1), "%"),
        row("app_service_threads", svc_threads, ""),
        row("app_service_handles", svc_handles, ""),
        row("app_ui_mem_mb", round(ui_mem, 1), "MB"),
        row("app_ui_cpu_pct", round(ui_cpu, 1), "%"),
        row("app_ui_processes", ui_procs, ""),
        row("app_total_mem_mb", round(svc_mem + ui_mem, 1), "MB"),
    ]


class AppMetricsSampler:
    """A daemon thread writing the app's own vitals into the historian."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_sample: List[Dict[str, Any]] = []
        self.last_error: str = ""

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="app-metrics",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def sample_once(self) -> List[Dict[str, Any]]:
        """A sample without storing it - for /api/diagnostics/processes."""
        try:
            rows = _collect()
            if rows:
                self.last_sample = rows
                self.last_error = ""
            return rows
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            return []

    def _run(self) -> None:
        # First psutil cpu_percent() call always returns 0.0; prime it so the
        # first stored sample is a real number rather than a misleading zero.
        try:
            _collect()
        except Exception:
            pass
        while not self._stop.wait(INTERVAL_S):
            try:
                rows = self.sample_once()
                if not rows:
                    continue
                from app.state import app_store
                app_store.append_historian_rows(rows)
            except Exception as exc:  # noqa: BLE001
                self.last_error = "%s: %s" % (type(exc).__name__, exc)


sampler = AppMetricsSampler()
