"""RELEASE GATE — the standard 10-minute validation for committing a new version.

Run this against the RUNNING freshly-built app before committing/tagging a
release. It executes the full-system suite (scripts/validate_full_12h.py) for
10 minutes and exits 0 only on "OVERALL: PASS".

Release flow:
    1. build both installers      cd desktop && npm run dist
                                  (also copies them to every folder listed in
                                   scripts/build_output_paths.txt - edit that
                                   text file to add or move a destination)
    2. install/launch the build   (quit old app first; portable re-extracts)
    3. gate                       python scripts/validate_release.py
    4. on PASS (exit 0)           commit / tag / ship
       on FAIL (exit 2)           read scripts/validation_out/validation_report.txt

Covers: BOOT HEALTH of the last launch (spawn -> /api/health 200 <= 15 s, no
splash "did not respond", backend boot instrumentation present — 2026-08-21),
collection cadence + loss, chart-feed latency, historian freshness,
local DB truth, outbox depth, cloud PG lag, API + AI-module probes,
batch/report/alarm/trigger snapshots, resource trends, and the log census
(stalls, restarts, lock-watchdog starvation dumps).

Before that it runs the source checks, which need no hardware and no running
app, and stops on the first failure rather than spending ten minutes to learn
something a second of grep would have caught. They exist because each guards a
fault that shipped: a hook read before its declaration, a commit call made on
the wrong object, an ifm channel typed wrong, a config document that silently
changed shape, a Start payload that dropped the fields the protocol needs,
an unticked tag that got collected anyway, and a PRAGMA the source asked for
but no connection ever received.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
env = dict(os.environ)
env.setdefault("VAL_DURATION_S", "600")
env.setdefault("PYTHONIOENCODING", "utf-8")

SOURCE_CHECKS = [
    ("hook order / TDZ", "test_hook_deps_tdz.py"),
    ("historian commit path", "test_historian_commit_path.py"),
    ("ifm channel typing", "test_ifm_channels_unit.py"),
    ("config document shape", "test_config_fingerprint.py"),
    ("gateway + Start payload", "test_gateway_ui_regressions.py"),
    # 2026-08-28
    ("what gets collected", "test_collect_selection.py"),
    ("sqlite write pragmas", "test_sqlite_write_pragmas.py"),
    ("config read lock-free", "test_config_read_no_write_lock.py"),
    ("config survives DB load", "test_scoped_config_survives_load.py"),
    ("modbus tcp gateway", "test_modbus_tcp_gateway.py"),
    ("device catalogue + CIP params", "test_device_catalogue.py"),
    ("oee dashboard api", "test_oee_dashboard_api.py"),
    ("ui consistency", "audit_ui_consistency.py"),
    ("power chart window", "test_power_series_window.py"),
    # 2026-08-29: a gateway whose every tag reads BAD reported running=True,
    # last_error=None and a climbing write count while the block was physically
    # off the network. Rows are not the same thing as readings.
    ("unreachable device is reported", "test_gateway_all_bad_reports.py"),
    # 2026-08-29: a hard-coded category list hid seventeen OEE widgets from
    # the picker, and a pin that never read must not draw as OFF.
    ("dashboard widget picker", "test_dashboard_widget_picker.py"),
    # 2026-08-30: boot force-stopped the power meter AND persisted
    # enabled=false, so it never came back after a restart and the operator's
    # setting was destroyed doing it.
    ("power survives a restart", "test_power_survives_restart.py"),
    # 2026-08-30: an ifm gateway with tag names but no datapoints started,
    # went green and never read anything.
    ("block gateway is addressable", "test_block_gateway_addressable.py"),
    # 2026-08-30: POINT I/O read without a PLC. SKIPs when no adapter answers,
    # so it is safe on a machine that has none.
    ("point i/o without a plc", "test_point_io_e2e.py"),
    # 2026-08-30: a save from a half-loaded page destroyed a PLC gateway with
    # 49 tags. The blank-write guard never fired - the payload was not blank,
    # just almost empty.
    ("config survives a partial save", "test_config_bulk_removal_guard.py"),
    ("config survives a slow boot", "test_config_survives_boot.py"),
    # 2026-08-30: interpolation was wrapped in `{false ?` by the Configure
    # redesign and never rehomed, so the option vanished from the UI.
    ("chart interpolation + range", "test_chart_options.py"),
    # 2026-08-31: start-up paid for a 17 s historian scan and a 1.66 GB WAL.
    ("start-up cost", "test_boot_startup_cost.py"),
    # 2026-08-31: a placeholder "default" tenant matched every legacy scope,
    # so another customer's gateway was served as the operator's own.
    ("scope never crosses customers", "test_scope_no_cross_customer.py"),
    # 2026-08-31: restart must lose nothing, and deleting must be allowed.
    # One rule replaced three intent-guessing guards: you may write what
    # you read (base_version).
    ("restart recovery + versioned writes", "test_restart_recovery.py"),
    ("deleted gateway stops; browser parity", "test_worker_and_browser_parity.py"),
    # 2026-08-31: a customer froze after 24 h. Connections were never closed
    # and each held up to 128 MB of page cache; the app now also records
    # its own memory/CPU so the next one is diagnosable from history.
    ("self metrics + connection lifetime", "test_app_self_metrics.py"),
    # 2026-08-31: the shipped exe still said "Electron", GitHub, Inc.
    ("shipped exe branding", "test_exe_branding.py"),
    # 2026-08-31: the export wrote the live buffer (a few hundred rows) or a
    # single 20 000-row page, never the whole range.
    ("historian export is complete", "test_historian_export_complete.py"),
    # 2026-08-31: an EM122 was saved as an EM525 register map and read 0.0
    # from every register while reporting itself healthy. SKIPs with no meter.
    ("power meter commissioning", "test_power_meter_e2e.py"),
    # 2026-08-31: "Today (since midnight)" was frozen at the hour it was
    # chosen, so the window slid and the totals never accumulated.
    ("power period is an anchor", "test_power_period_window.py"),
    # 2026-08-31: OEE > Overview. Stops, Pareto grouping and a previous
    # period had to exist in the service - the page may not compute OEE.
    ("oee overview contract", "test_oee_overview_contract.py"),
    # 2026-08-31: OEE > Machine Detail. Editing one field of a downtime
    # event used to blank the other three, so "add a comment" erased the
    # reason somebody walked to the machine to establish.
    ("oee machine detail contract", "test_oee_machine_detail_contract.py"),
    # 2026-08-31: the Data Export assistant - filters, aggregation, pivot,
    # streaming - in its own router so the historian read path is untouched.
    ("data export assistant", "test_data_export.py"),
    # 2026-08-31: retention deleted 7.1 GB and the 16 GB file never shrank;
    # and nothing ever pruned the cloud copy.
    ("retention reclaims disk + cloud prune", "test_retention_reclaim.py"),
    # 2026-08-30: CSV/TXT exports wrote nothing whenever a gateway filter was
    # set, because the filter compared a field GatewayConfig does not have.
    # 2026-08-31: the OEE Overview shipped with a temporal-dead-zone error
    # that took the whole page down behind the error boundary. This walk opens
    # every page in a real browser and fails on that boundary - it would have
    # caught it. It was not here because it could no longer log in and so
    # reported all sixteen pages as broken; fixed the same day. Brings its own
    # throwaway backend, so it needs no install and no hardware.
    # 2026-09-02: the front end "froze after a few hours" and saves failed
    # with "Token expired". Nothing renewed the session and nothing handled a
    # 401, so every poller failed in silence. Renewal, the 401 retry, the
    # grace window and revocation-survives-refresh are all pinned here.
    # 2026-09-02: a collection trigger added while gateways were RUNNING
    # reached no worker, so the global trigger set stayed empty - and empty
    # means "no gating". The rule was saved, shown, and every reading was
    # written exactly as if it did not exist.
    # 2026-09-02: an ifm master on fieldbus offered bytes 0-1 of a 446-byte
    # assembly and nothing else. The layout below it was derived from the
    # operator's own AL1326 and cross-checked against the same block's IoT
    # Core answers; this pins it to that capture.
    # 2026-09-02: a meter's ticked registers were stored as a LIST of names,
    # which matched no branch of the resolver and was silently replaced by the
    # profile default - so a single-phase EM122 polled the 3-phase map and
    # wrote a permanent 0.0 for two phases that do not exist.
    ("a meter collects what was ticked", "test_meter_register_selection.py"),
    ("ifm fieldbus layout", "test_ifm_fieldbus_layout.py"),
    # 2026-09-02: schedule triggers, and rules scoped to one gateway or to
    # all of them. The gate used to return ONE verdict for the whole site.
    ("schedule triggers + gateway scope", "test_collection_schedule_triggers.py"),
    ("a trigger actually gates writes", "test_collection_trigger_gates_writes.py"),
    ("a session survives a shift", "test_session_survives_a_shift.py"),
    ("every page opens in a browser", "run_ui_smoke.py"),
    ("csv / txt file exports", "test_file_sinks.py"),
]

# --------------------------------------------------------------- containment
# Every check starts its own backend, and a check that exits early never gets
# to terminate it. Six orphaned `python -m app` processes survived a sweep on
# 2026-08-31, and the checks that collect for 15-30 s then assert cadence
# failed against the CPU those orphans were using - a different pair each run,
# which is how a flaky gate teaches people to ignore it.
#
# A Job Object with KILL_ON_JOB_CLOSE kills whatever the check spawned when
# the check ends, however it ends. Falls back to a plain call on non-Windows
# or if the job cannot be created.
def _run_contained(argv):
    if os.name != "nt":
        return subprocess.call(argv, env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong)]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMIT),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    k32 = ctypes.windll.kernel32
    job = k32.CreateJobObjectW(None, None)
    if job:
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                    ctypes.byref(info), ctypes.sizeof(info))
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    if job:
        # Assigned immediately after spawn: `python -m app` takes seconds to
        # import anything, so nothing has escaped in between.
        k32.AssignProcessToJobObject(job, int(proc._handle))
    try:
        return proc.wait()
    finally:
        if job:
            k32.CloseHandle(job)      # KILL_ON_JOB_CLOSE reaps the stragglers


print("RELEASE GATE: source checks...", flush=True)
for label, script in SOURCE_CHECKS:
    rc = _run_contained([sys.executable, os.path.join(HERE, script)])
    print("  {0:26s}: {1}".format(label, "PASS" if rc == 0 else "FAIL"), flush=True)
    if rc != 0:
        print("RELEASE GATE: FAIL - do NOT commit. Run "
              "`python scripts/{0}` for the detail.".format(script), flush=True)
        sys.exit(2)

print("RELEASE GATE: running the 10-minute full-system validation...", flush=True)
rc = subprocess.call([sys.executable, os.path.join(HERE, "validate_full_12h.py")], env=env)
print(f"\nRELEASE GATE: {'PASS - safe to commit this version' if rc == 0 else 'FAIL - do NOT commit; see scripts/validation_out/validation_report.txt'}",
      flush=True)
sys.exit(rc)
