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

# 2026-10-20: the login this gate uses to drive the running app used to be
# a literal default inside validate_full_12h.py, so the gate "just worked"
# by carrying a real operator password in the repo. The password now comes
# from the environment or from .env, which is gitignored.
#
# Checked HERE, before anything runs: the credential is not needed until
# the 10-minute full-system stage, and discovering it is missing after ten
# minutes of source checks is its own small cruelty.
if not env.get("VAL_PASS"):
    _dotenv = os.path.join(os.path.dirname(HERE), ".env")
    try:
        with open(_dotenv, encoding="utf-8") as _fh:
            for _line in _fh:
                _line = _line.strip()
                if _line.startswith("VAL_PASS=") and not _line.startswith("#"):
                    env["VAL_PASS"] = _line.split("=", 1)[1].strip().strip("'\"")
                    break
    except OSError:
        pass
if not env.get("VAL_PASS"):
    print("RELEASE GATE: cannot run - no VAL_PASS.", flush=True)
    print("  The full-system stage signs in to the running app. Put the", flush=True)
    print("  password in .env (gitignored) as  VAL_PASS=...  or set it in", flush=True)
    print("  the environment. VAL_USER defaults to admin-mari.", flush=True)
    sys.exit(2)

SOURCE_CHECKS = [
    # 2026-10-20: a lazy import naming something that does not exist runs
    # only when its line runs, and an except around it hides the whole
    # feature. That is how the report-template mirror was dead for months.
    ("lazy imports resolve", "test_lazy_imports_resolve.py"),
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
    # 2026-10-22: the portal runs the CLOUD control-plane store, which
    # re-implements ~21 methods of the SQLite one. upsert_license grew
    # package_key/seats/limits on 2026-08-22; the cloud copy did not, so
    # creating a licence raised TypeError and the portal showed a bare
    # 500. Every SQLite-path test passed throughout.
    ("cloud store matches the sqlite store", "test_cloud_store_signatures.py"),
    # 2026-10-22: getRuntimeSurface() calls every hosted page "cloud", and
    # isReadOnlySession() treated "cloud" as read-only unconditionally - so
    # the DEVELOPER PORTAL refused every edit for a global admin, in the
    # frontend, before any request reached the backend.
    ("portal is writable for an admin", "test_portal_not_readonly.py"),
    # 2026-10-22: the backend is spawned AFTER the splash, so anything that
    # kills the process at the FIRST BrowserWindow presents as "the backend
    # never responded" - and that death is native, so no JS handler fires
    # and nothing is logged. A breadcrumb now makes the next launch fall
    # back to software rendering instead of failing the same way.
    ("app starts without a usable GPU", "test_first_window_safety.py"),
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
    ("file exports: scope, rate, xlsx", "test_file_exports.py"),
    ("csv/txt/excel written for real", "test_export_files_e2e.py"),
    ("historical dashboards freeze", "test_dashboard_historical.py"),
    ("cycle-triggered reports", "test_report_cycle_triggers.py"),
    ("reports on the phone (lite LAN)", "test_lite_reports.py"),
    ("the cloud report mirror binds", "test_cloud_report_mirror.py"),
    ("report segments + per-section windows", "test_report_segments.py"),
    ("a slow start-up retries", "test_startup_reads_retry.py"),
    ("report reads do not wait on the writer", "test_reports_store_reads.py"),
    ("report layout + side-by-side", "test_report_layout.py"),
    ("a TCU test report renders", "smoke_tcu_test_report.py"),
    ("cycle trigger -> report", "smoke_cycle_trigger_report.py"),
    ("the schedule editor can set a cycle", "test_schedule_ui_contract.py"),
    # 2026-10-14: the onboarding chain - customer, licence, edge, activation
    # code, login. Every link has failed in production at least once.
    ("onboarding: licence, activation, login", "test_onboarding_activation_login.py"),
    # 2026-10-20: the line above drives the STORE. This one drives the
    # HTTP endpoint, and while it was in neither the gate nor CI,
    # local-finalize returned 400 on every activation and nobody saw it.
    ("activation leaves a usable admin", "test_activation_creates_admin.py"),
    # 2026-10-22: a new machine activates from the LOGIN screen, with no
    # Authorization header. The edge fetched the licence modules with two
    # upstream GETs forwarding that absent header; both were refused, the
    # failure was swallowed, and the edge held NO entitlements - so every
    # menu showed whatever was ticked on the licence.
    ("activation carries the licence modules", "test_activation_carries_modules.py"),
    # 2026-10-22: isPageLicensed() opens with `if (!required) return true`,
    # so a page missing from MODULE_KEY_BY_PAGE shows on every edge whatever
    # the licence says - three were. And admins skipped the licence gate
    # outright, which is who actually uses a customer edge.
    ("the menu shows only licensed modules", "test_menu_licence_gating.py"),
    # 2026-10-22, from a customer install: after activation the locally
    # created users could not log in, and many sites have no internet at
    # all. This drives an ACTIVATED edge with every cloud variable stripped
    # and the cloud URL pointed at a black hole: activation, an admin
    # creating a user, that user logging in, an admin resetting them, and
    # the local recovery path - none of which may need the network.
    ("local users + recovery, fully offline", "test_local_users_offline.py"),
    # 2026-10-22: "Request Code" did nothing on a customer machine.
    # send_code() needs a Supabase URL AND an anon key and read the
    # ENVIRONMENT ONLY - a fresh install has no .env and the activation
    # code never carried a key, so it returned supabase_not_configured
    # while the screen still said a code had been sent.
    ("the cloud can issue a recovery code", "test_recovery_code_config.py"),
    # 2026-10-20: the whole journey in one run - customer+email, licence,
    # edge, code, activation, login, recovery. The per-link tests each
    # build their own starting state, so none of them saw that activation
    # blanked the company contact_email it had just been given.
    ("onboarding flow, end to end", "validate_onboarding_flow.py"),
    # 2026-10-20 storage work: the archive that let 7.8 GB of legacy files
    # go without losing the 10.6M readings only they still held; the four
    # retention dials that were declared and read by nothing; and the
    # admin purge, whose tenant scoping is all that protects one customer
    # from deleting another on the shared cloud database.
    ("legacy stores fold, nothing lost", "test_legacy_archive.py"),
    ("every retention dial prunes", "test_retention_prunes_everything.py"),
    ("admin purge is scoped and gated", "test_admin_purge.py"),
    # 2026-10-20: LAN Lite showed "no samples" while the full app on the
    # same machine charted live data - /api/lite-local/live returned two
    # different row shapes depending on which source answered.
    ("Lite live rows carry the right names", "test_lite_live_contract.py"),
    # and the cloud grew to 116 GB because the policy cloud block was
    # normalised and read by nothing.
    ("cloud retention is gated and scoped", "test_cloud_retention_gate.py"),
    # 2026-10-14: a NEW customer's machine has no portal address, nothing in
    # localStorage and an empty local store. The shipped bundle is the only
    # thing that knows where to send the code.
    ("fresh install can activate", "test_fresh_install_activation.py"),
    # 2026-10-17: compaction deleted its own staged result every tick, so the
    # store never shrank, the engine ran continuously and collection starved.
    ("compaction keeps its result", "test_retention_compaction_loop.py"),
    # 2026-10-17: Lite collapsed 4 Y axes to 2 and ignored every unit key, so
    # a dashboard profile rendered differently in the web client view.
    ("lite chart axes match the edge", "test_lite_chart_axes.py"),
    # 2026-10-17: "fit to content" left colWidths unset and trusted ReportLab
    # to size the column - it stretches a Paragraph cell to the full page
    # instead, so every pill stayed page-wide and could only sit on the left.
    # The header band read neither the width nor the position key at all.
    ("a pill fits its text and can be placed", "test_report_pill_width.py"),
    # 2026-10-17: the Data Export window was typed as local wall time and
    # sent in a field called from_utc. On a UTC+1 machine the live store
    # answered 0 rows for a window that held 27,234.
    ("the export window is UTC", "test_export_window_utc.py"),
    # 2026-10-20 (SECURITY): password-reset/public/issue took no credentials
    # and returned the reset token in its own response body - account
    # takeover in two requests, on a route reachable from the internet.
    ("public reset leaks no token", "test_public_reset_no_token_leak.py"),
    # 2026-10-20: sign in and recover with an emailed code, and change a
    # password from the user menu. The code endpoints must stay PUBLIC -
    # behind the auth gate they are unreachable by the only people who need
    # them, which is how the first version shipped.
    ("sign-in and recovery by code", "test_login_codes.py"),
    # 2026-10-17: "Clear All Alarms" only emptied React state. alarms_setup is
    # a protected collection, so the save was refused server-side (12 times
    # since 2026-08-26) and the catch block said nothing - the 300 stored
    # alarms came back on every restart.
    ("a cleared alarm list stays cleared", "test_alarm_clear_persists.py"),
    # 2026-10-17: nothing has ever linted backend/static/lite_view - no build
    # reads it - so an identifier declared in one function and used from
    # another ships. buildManualY was doing exactly that, and the tag monitor
    # threw before it drew anything.
    ("lite view has no undefined names", "test_lite_view_undef.py"),
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
