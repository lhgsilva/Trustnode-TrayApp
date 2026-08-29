/* Diagnostics — what this machine is doing, and how much of it is TrustNode.

   2026-08-28. Three questions answered side by side, because each one lies on
   its own: "CPU is at 80%" means something quite different when 5% of it is
   ours, and "the gateway is running" means nothing if the historian stopped
   taking rows.

   Two rules the page follows.

   It reports the DURABLE stamp. `historian_write_count` is stamped on the
   commit path itself; the older `db_*` counters measure the lossy distribution
   path and once read "no writes" for 5.6 hours while the historian was taking
   48 rows a second. A diagnostics page that repeats that lie is worse than no
   page at all.

   It never becomes the load it is measuring. One request every few seconds
   against an endpoint that caches for two, and nothing here computes over the
   historian. */
import { useCallback, useEffect, useRef, useState } from "react";
import { getSystemDiagnostics } from "../../api";

const REFRESH_MS = 4000;

function bytes(n) {
  const v = Number(n || 0);
  if (!Number.isFinite(v) || v <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(v) / Math.log(1024)));
  return `${(v / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function rate(n) {
  return `${bytes(n)}/s`;
}

function duration(s) {
  const v = Math.max(0, Math.floor(Number(s || 0)));
  const d = Math.floor(v / 86400);
  const h = Math.floor((v % 86400) / 3600);
  const m = Math.floor((v % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

/* Age of a stamp in seconds. The backend writes the canonical
   "YYYY-MM-DD HH:MM:SS.mmm" in UTC with no zone suffix, so it has to be told
   it is UTC - read as local time it would look hours stale. */
function ageSeconds(stamp) {
  const raw = String(stamp || "").trim();
  if (!raw) return null;
  const ms = Date.parse(raw.replace(" ", "T") + (/[Zz+]/.test(raw) ? "" : "Z"));
  if (!Number.isFinite(ms)) return null;
  return Math.max(0, (Date.now() - ms) / 1000);
}

function Bar({ percent, tone }) {
  const p = Math.max(0, Math.min(100, Number(percent || 0)));
  return (
    <div className="diag-bar" title={`${p.toFixed(1)}%`}>
      <div className={`diag-bar-fill ${tone || ""}`} style={{ width: `${p}%` }} />
    </div>
  );
}

/* One headline number. `sub` carries the context that makes it meaningful -
   a percentage with no denominator is not a diagnostic. */
function Stat({ label, value, sub, percent, tone }) {
  return (
    <div className="diag-stat">
      <div className="diag-stat-label">{label}</div>
      <div className={`diag-stat-value ${tone || ""}`}>{value}</div>
      {percent === undefined ? null : <Bar percent={percent} tone={tone} />}
      {sub ? <div className="diag-stat-sub">{sub}</div> : null}
    </div>
  );
}

function toneFor(percent, warn = 75, bad = 90) {
  const p = Number(percent || 0);
  if (p >= bad) return "bad";
  if (p >= warn) return "warn";
  return "";
}

export default function DiagnosticsPage({ canRefresh = true }) {
  const [snap, setSnap] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [paused, setPaused] = useState(false);
  const alive = useRef(true);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const res = await getSystemDiagnostics();
      if (!alive.current) return;
      setSnap(res);
      setError("");
    } catch (err) {
      if (alive.current) setError(String(err?.message || err));
    } finally {
      if (alive.current) setBusy(false);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    load();
    return () => { alive.current = false; };
  }, [load]);

  useEffect(() => {
    if (paused) return undefined;
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, [load, paused]);

  const machine = snap?.machine || {};
  const ours = snap?.trustnode || {};
  const totals = ours.totals || {};
  const pipeline = snap?.pipeline || {};
  const storage = snap?.storage || {};
  const gateways = Array.isArray(pipeline.gateways) ? pipeline.gateways : [];
  const meters = Array.isArray(pipeline.meters) ? pipeline.meters : [];
  const transfer = pipeline.transfer || {};

  const cpu = machine.cpu || {};
  const mem = machine.memory || {};
  const disk = machine.disk || {};
  const net = machine.network || {};

  // Our share of what is actually in use, not of the whole machine - "we are
  // 4% of a box that is 95% busy" is the number an operator needs.
  const ourCpuShare = Number(cpu.percent || 0) > 0
    ? (Number(totals.cpu_percent || 0) / Number(cpu.percent)) * 100
    : 0;
  const ourMemShare = Number(mem.total_bytes || 0) > 0
    ? (Number(totals.rss_bytes || 0) / Number(mem.total_bytes)) * 100
    : 0;

  // Growth is derived from what the store actually holds - its size over the
  // age of its oldest row - rather than a number typed in here, so it stays
  // true when the tag count changes.
  const daysToFull = (() => {
    const size = Number(storage.app_store_bytes || 0);
    const free = Number(disk.free_bytes || 0);
    const oldest = ageSeconds(storage.oldest_raw_utc);
    if (!size || !free || !oldest || oldest < 3600) return null;
    const perDay = size / (oldest / 86400);
    if (!(perDay > 0)) return null;
    return Math.max(0, Math.round(free / perDay));
  })();

  return (
    <div className="diag-page">
      <div className="row diag-head">
        <div className="muted" style={{ fontSize: 12 }}>
          {snap?.ts_utc ? `Sampled ${snap.ts_utc} UTC` : "Loading…"}
          {machine.uptime_s ? ` · machine up ${duration(machine.uptime_s)}` : ""}
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button type="button" className="btn btn-secondary btn-sm"
            onClick={() => setPaused((p) => !p)}
            title={paused ? "Resume automatic refresh" : "Stop refreshing — useful while reading a value"}>
            {paused ? "Resume" : "Pause"}
          </button>
          <button type="button" className="btn btn-primary btn-sm"
            disabled={busy || !canRefresh} onClick={load}>
            {busy ? "Reading…" : "Refresh"}
          </button>
        </div>
      </div>

      {error ? <div className="info-note warn">{error}</div> : null}
      {machine.available === false ? (
        <div className="info-note warn">
          Machine metrics are unavailable in this build ({machine.reason || "psutil missing"}).
          The data-path section below still works.
        </div>
      ) : null}

      {/* A store that only grows is the failure that ends collection outright:
          when the disk fills, writes stop. Measured on this install 2026-08-28
          — no retention policy at all, 2.85 GB/day, ~30 days of headroom, and
          nothing anywhere said so. Retention DELETES data, so this states the
          arithmetic and links the page that owns the decision; it never picks
          a policy on the operator's behalf. */}
      {snap && storage.retention_active === false ? (
        <div className="info-note warn">
          <strong>Nothing is ever deleted — no retention policy is active.</strong>{" "}
          The store is {bytes(storage.app_store_bytes)} and{" "}
          {disk.free_bytes ? `${bytes(disk.free_bytes)} of disk remains` : "the disk is filling"}.
          {daysToFull !== null
            ? ` At the current growth rate that is about ${daysToFull} day(s) of headroom.`
            : ""}{" "}
          When the disk fills, collection stops. Choose a policy under
          Database and Backup → Backup and Retention.
        </div>
      ) : null}

      {/* ---------------------------------------------------------- machine */}
      <section className="card">
        <h3 style={{ marginTop: 0 }}>This computer</h3>
        <div className="diag-stat-grid">
          <Stat label="CPU" value={`${Number(cpu.percent || 0).toFixed(0)}%`}
            percent={cpu.percent} tone={toneFor(cpu.percent)}
            sub={`${cpu.cores_logical || 0} logical cores · ${cpu.cores_physical || 0} physical`} />
          <Stat label="Memory" value={`${Number(mem.percent || 0).toFixed(0)}%`}
            percent={mem.percent} tone={toneFor(mem.percent, 80, 92)}
            sub={`${bytes(mem.used_bytes)} used of ${bytes(mem.total_bytes)} · ${bytes(mem.available_bytes)} free`} />
          <Stat label="Disk" value={`${Number(disk.percent || 0).toFixed(0)}%`}
            percent={disk.percent} tone={toneFor(disk.percent, 85, 95)}
            sub={`${bytes(disk.free_bytes)} free of ${bytes(disk.total_bytes)}`} />
          <Stat label="Network" value={`${rate(net.recv_bytes_per_s)} in`}
            sub={`${rate(net.send_bytes_per_s)} out · ${bytes(net.bytes_recv)} / ${bytes(net.bytes_sent)} since boot`} />
        </div>
      </section>

      {/* -------------------------------------------------------- TrustNode */}
      <section className="card">
        <h3 style={{ marginTop: 0 }}>TrustNode's share</h3>
        <div className="diag-stat-grid">
          <Stat label="CPU used by TrustNode" value={`${Number(totals.cpu_percent || 0).toFixed(1)}%`}
            percent={totals.cpu_percent} tone={toneFor(totals.cpu_percent, 40, 70)}
            sub={`${ourCpuShare.toFixed(0)}% of everything this machine is doing`} />
          <Stat label="Memory used by TrustNode" value={bytes(totals.rss_bytes)}
            percent={ourMemShare} tone={toneFor(ourMemShare, 30, 50)}
            sub={`${ourMemShare.toFixed(1)}% of ${bytes(mem.total_bytes)} installed`} />
          <Stat label="Processes" value={String(totals.process_count || 0)}
            sub="backend service, its children, and the desktop shell" />
          <Stat label="Data store" value={bytes(storage.app_store_bytes)}
            sub={storage.wal_bytes
              ? `plus ${bytes(storage.wal_bytes)} write-ahead log`
              : "no write-ahead log pending"} />
        </div>

        <div className="table db-table diag-proc-table" style={{ marginTop: 12 }}>
          <div className="thead">
            <span>Process</span><span>PID</span><span>Role</span>
            {/* psutil reports per-process CPU against ONE core, so a busy
                process legitimately shows 101%. The headline above is
                normalised across all cores; saying so here stops the two
                numbers looking like they contradict each other. */}
            <span title="Percentage of a single core — the headline above is across all cores">
              CPU (1 core)
            </span>
            <span>Memory</span><span>Threads</span><span>Started (UTC)</span>
          </div>
          {(ours.processes || []).map((p) => (
            <div className="trow" key={`proc-${p.pid}`}>
              <span>{p.name}</span>
              <span>{p.pid}</span>
              <span className="muted">{p.role}</span>
              <span>{Number(p.cpu_percent || 0).toFixed(1)}%</span>
              <span>{bytes(p.rss_bytes)}</span>
              <span>{p.threads}</span>
              <span className="muted">{p.started_utc || "-"}</span>
            </div>
          ))}
          {!(ours.processes || []).length ? (
            <div className="trow"><span className="muted">No process information.</span></div>
          ) : null}
        </div>
      </section>

      {/* -------------------------------------------------------- data path */}
      <section className="card">
        <h3 style={{ marginTop: 0 }}>Data being collected</h3>
        <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
          "Stored" is the historian commit stamp — the durable one. A gateway can
          report Running with a frozen sink counter while the historian is taking
          rows every second, so this is the column that answers "is it collecting".
        </div>
        <div className="table db-table diag-gw-table">
          <div className="thead">
            <span>Gateway</span><span>Type</span><span>State</span><span>Interval</span>
            <span>Tags</span><span>Stored</span><span>Last stored</span><span>Distribution</span>
          </div>
          {gateways.map((g) => {
            const age = ageSeconds(g.historian_last_write_utc);
            const stale = age !== null && g.running
              && age > Math.max(10, (Number(g.interval_ms || 1000) / 1000) * 5);
            return (
              <div className="trow" key={`gw-${g.gateway_id}`}>
                <span title={g.gateway_id}>{g.name || g.gateway_id}</span>
                <span className="muted">{g.gateway_type || "-"}</span>
                <span className={g.running ? "status-online" : "status-offline"}>
                  {g.running ? "RUNNING" : "STOPPED"}
                </span>
                <span>{g.interval_ms ? `${g.interval_ms} ms` : "-"}</span>
                <span>{g.tag_count ?? "-"}</span>
                <span>{g.historian_write_count ?? "-"}</span>
                <span className={stale ? "status-warning" : ""}>
                  {age === null ? "-" : `${age.toFixed(0)}s ago`}
                </span>
                <span className={Number(g.distribution_stalled_s || 0) > 120 ? "status-warning" : "muted"}>
                  {g.distribution_stage || "-"}
                  {Number(g.distribution_stalled_s || 0) > 0
                    ? ` (${Math.round(g.distribution_stalled_s)}s)` : ""}
                </span>
              </div>
            );
          })}
          {!gateways.length ? (
            <div className="trow"><span className="muted">No gateways are configured.</span></div>
          ) : null}
        </div>
        {gateways.filter((g) => g.last_error).map((g) => (
          <div className="info-note warn" key={`err-${g.gateway_id}`}>
            <strong>{g.name || g.gateway_id}:</strong> {String(g.last_error).slice(0, 300)}
          </div>
        ))}

        {meters.length ? (
          <>
            <h4 style={{ marginBottom: 6 }}>Power meters</h4>
            <div className="table db-table diag-meter-table">
              <div className="thead">
                <span>Meter</span><span>State</span><span>Last poll</span>
                <span>Cycle</span><span>Registers</span><span>Error</span>
              </div>
              {meters.map((m) => {
                const st = m.status || {};
                const mt = m.metrics || {};
                return (
                  <div className="trow" key={`meter-${m.device_id}`}>
                    <span>{st.name || m.device_id}</span>
                    <span className={st.running || st.connected ? "status-online" : "status-offline"}>
                      {st.running || st.connected ? "RUNNING" : "STOPPED"}
                    </span>
                    <span>{st.last_poll_utc || st.last_sample_utc || "-"}</span>
                    <span>{mt.cycle_ms ? `${Math.round(mt.cycle_ms)} ms` : "-"}</span>
                    <span>{mt.register_count ?? mt.registers ?? "-"}</span>
                    <span className="muted">{String(st.last_error || "").slice(0, 60) || "-"}</span>
                  </div>
                );
              })}
            </div>
            {(pipeline.meters_summary || {}).writer_queue_depth !== undefined ? (
              <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                Meter writer: queue {pipeline.meters_summary.writer_queue_depth},{" "}
                {pipeline.meters_summary.writer_batches} batch(es) written,{" "}
                {pipeline.meters_summary.writer_dropped_rows} row(s) dropped.
                A queue that keeps climbing means storage is slower than polling.
              </div>
            ) : null}
          </>
        ) : null}
      </section>

      {/* ---------------------------------------------------------- transfer */}
      <section className="card">
        <h3 style={{ marginTop: 0 }}>Data being transferred</h3>
        <div className="diag-stat-grid">
          <Stat label="Waiting to reach the cloud" value={String(transfer.outbox_depth ?? "-")}
            sub={transfer.oldest_pending_utc
              ? `oldest sample ${transfer.oldest_pending_utc}`
              : "nothing pending"}
            tone={Number(transfer.outbox_depth || 0) > 100000 ? "warn" : ""} />
          <Stat label="Cloud forwarding" value={transfer.ingest_enabled ? "Enabled" : "Disabled"}
            sub="rows are stored locally either way — the outbox replays when it is turned on" />
        </div>
        {transfer.last_outbox_error && Object.keys(transfer.last_outbox_error).length ? (
          <div className="info-note warn">
            Last forwarding error: {JSON.stringify(transfer.last_outbox_error).slice(0, 300)}
          </div>
        ) : null}
        {pipeline.transfer_error ? (
          <div className="info-note warn">Transfer status unavailable: {pipeline.transfer_error}</div>
        ) : null}
      </section>
    </div>
  );
}
