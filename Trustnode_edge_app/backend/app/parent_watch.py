"""Parent-process watch (operator 2026-08-21, NO-ORPHANS FIX).

The desktop app (Electron) spawns this backend and passes its own PID in
TRUSTNODE_PARENT_PID. If Electron dies for ANY reason — force-killed from
Task Manager, crashed, aborted boot — the backend must not linger as an
invisible trustnode-service.exe holding the port and the SQLite files.

Windows: open a SYNCHRONIZE handle to the parent and block on it in a
daemon thread; the wait returns the instant the parent exits, then we
os._exit(0). Fallback (non-Windows or handle failure): poll psutil.

Never uses os.kill(pid, 0) — on Windows that TERMINATES the target.
"""
from __future__ import annotations

import os
import sys
import threading
import time


def _exit_now(reason: str) -> None:
    try:
        print(f"[trustnode][parent-watch] {reason} — backend exiting so no orphan process remains", flush=True)
    except Exception:
        pass
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)


def _watch_windows(ppid: int) -> bool:
    """Block on the parent's process handle. Returns False if the handle
    could not be opened (caller falls back to polling)."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        SYNCHRONIZE = 0x00100000
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(ppid))
        if not handle:
            return False
        INFINITE = 0xFFFFFFFF
        WAIT_OBJECT_0 = 0x00000000
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        while True:
            rc = kernel32.WaitForSingleObject(handle, 5000)
            if rc == WAIT_OBJECT_0:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass
                _exit_now(f"parent process {ppid} exited")
                return True
            # WAIT_TIMEOUT (0x102): parent still alive, keep waiting. Any other
            # code (WAIT_FAILED): fall back to polling.
            if rc != 0x00000102:
                return False
    except Exception:
        return False


def _watch_poll(ppid: int) -> None:
    try:
        import psutil  # bundled with the backend (see trustnode-service.spec)
    except Exception:
        psutil = None  # type: ignore
    misses = 0
    while True:
        time.sleep(2.0)
        alive = True
        try:
            if psutil is not None:
                alive = psutil.pid_exists(ppid)
            else:
                return  # no safe way to check — do nothing rather than guess
        except Exception:
            alive = True
        misses = 0 if alive else misses + 1
        if misses >= 2:  # two consecutive misses (4 s) — parent is gone
            _exit_now(f"parent process {ppid} no longer exists")


def start_from_env() -> bool:
    """Arm the watch when TRUSTNODE_PARENT_PID is set. Returns True if armed."""
    raw = str(os.environ.get("TRUSTNODE_PARENT_PID", "") or "").strip()
    if not raw:
        return False
    try:
        ppid = int(raw)
    except ValueError:
        return False
    if ppid <= 0 or ppid == os.getpid():
        return False

    def _run() -> None:
        if sys.platform == "win32" and _watch_windows(ppid):
            return
        _watch_poll(ppid)

    threading.Thread(target=_run, name="trustnode-parent-watch", daemon=True).start()
    print(f"[trustnode][boot] parent-watch armed on pid {ppid} (backend exits with the desktop app)", flush=True)
    return True
