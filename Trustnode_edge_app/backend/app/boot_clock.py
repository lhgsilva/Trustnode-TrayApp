"""Process boot clock (operator 2026-08-21, BOOT-HEALTH FIX).

Imported FIRST by app/__main__.py so T0 sits as close to process start as
Python allows. Every boot marker that matters to the desktop splash and to
the release gate is expressed as "+X.XXs after process start":

  * /api/health logs "first /api/health served +X.XXs" on its first hit.
  * the boot-health watchdog (app.main) dumps thread stacks if that first
    hit has not happened within its thresholds.
  * deferred init logs when gateways resume / when it completes.

The release gate (scripts/boot_log_check.py) asserts these against SLOs.
"""
import time

T0_MONO = time.monotonic()
T0_WALL = time.time()


def age_s() -> float:
    """Seconds since the process started (monotonic)."""
    return time.monotonic() - T0_MONO
