import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  PieChart,
  Pie,
  Cell,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { getCloudSyncStatus } from "../../api";

const DEFAULT_POLL_MS = 2000;
const DEFAULT_HISTORY_SEC = 300;

// When the widget is loaded from the hosted client view (browser hitting the
// VPS, no local Electron), the /sync/status endpoint reports the VPS's view
// of sync state — which is empty, because the edge runs the sync worker, not
// the VPS. Detect this so we can show a friendlier "monitor on the edge"
// message instead of misleading 0/0/never numbers.
function detectHostedClientView() {
  try {
    if (typeof window === "undefined") return false;
    const ua = String(window.navigator?.userAgent || "");
    if (/electron/i.test(ua)) return false;
    const proto = String(window.location?.protocol || "");
    if (proto !== "http:" && proto !== "https:") return false;
    const host = String(window.location?.hostname || "").toLowerCase();
    if (!host) return false;
    if (host === "localhost" || host === "127.0.0.1" || host === "::1") return false;
    return true;
  } catch {
    return false;
  }
}

function fmtNum(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return "0";
  if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`;
  if (Math.abs(v) >= 1_000) return `${(v / 1_000).toFixed(1)}k`;
  return v.toLocaleString();
}

function fmtAge(iso) {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "never";
  const dt = Math.max(0, Date.now() - t);
  if (dt < 1000) return "just now";
  if (dt < 60_000) return `${Math.round(dt / 1000)}s ago`;
  if (dt < 3_600_000) return `${Math.round(dt / 60_000)}m ago`;
  return `${Math.round(dt / 3_600_000)}h ago`;
}

function computeStatus(snap) {
  if (!snap) return { tone: "muted", label: "LOADING" };
  const target = snap.cloud_target || {};
  if (!target.enabled) return { tone: "muted", label: "DISABLED" };
  if (snap.last_data_error) return { tone: "error", label: "ERROR" };
  const lastIso = snap.last_data_sync_utc || "";
  if (!lastIso) return { tone: "warn", label: "IDLE" };
  const age = Date.now() - Date.parse(lastIso);
  if (!Number.isFinite(age)) return { tone: "warn", label: "IDLE" };
  if (age > 60_000) return { tone: "warn", label: "IDLE" };
  return { tone: "ok", label: "ONLINE" };
}

const TONE_COLORS = {
  ok: "#0ea58a",
  warn: "#d97706",
  error: "#dc2626",
  muted: "#8a98ab",
};

function StatusPill({ status, host }) {
  return (
    <div className={`dashboard-csync-pill dashboard-csync-pill-${status.tone}`}>
      <span className="dashboard-csync-pill-dot" />
      <span className="dashboard-csync-pill-text">
        {status.label}
        {host ? ` • ${host}` : ""}
      </span>
    </div>
  );
}

function Donut({ synced, backlog, accent }) {
  const total = Math.max(0, synced) + Math.max(0, backlog);
  const data = total > 0
    ? [
        { name: "Synced", value: Math.max(0, synced) },
        { name: "Backlog", value: Math.max(0, backlog) },
      ]
    : [{ name: "No data", value: 1 }];
  const colors = total > 0 ? [accent, "#3f3f46"] : ["#27272a"];
  const pct = total > 0 ? Math.round((synced / total) * 100) : 0;
  return (
    <div className="dashboard-csync-donut">
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={data}
            innerRadius="62%"
            outerRadius="86%"
            paddingAngle={total > 0 ? 2 : 0}
            dataKey="value"
            isAnimationActive={false}
          >
            {data.map((_, i) => (
              <Cell key={i} fill={colors[i % colors.length]} stroke="none" />
            ))}
          </Pie>
          {total > 0 ? (
            <Tooltip
              contentStyle={{ background: "#171717", border: "1px solid #2a2a2a", color: "#f2f4f7" }}
              formatter={(v, n) => [fmtNum(v), n]}
            />
          ) : null}
        </PieChart>
      </ResponsiveContainer>
      <div className="dashboard-csync-donut-center">
        <div className="dashboard-csync-donut-pct">{pct}%</div>
        <div className="dashboard-csync-donut-sub">synced</div>
      </div>
    </div>
  );
}

function StatTile({ label, value, sub, accent }) {
  return (
    <div className="dashboard-csync-tile">
      <div className="dashboard-csync-tile-label">{label}</div>
      <div className="dashboard-csync-tile-value" style={accent ? { color: accent } : undefined}>
        {value}
      </div>
      {sub ? <div className="dashboard-csync-tile-sub">{sub}</div> : null}
    </div>
  );
}

function ProgressBar({ synced, backlog, accent }) {
  const total = Math.max(0, synced) + Math.max(0, backlog);
  const pct = total > 0 ? Math.min(100, Math.max(0, (synced / total) * 100)) : 0;
  return (
    <div className="dashboard-csync-progress">
      <div className="dashboard-csync-progress-track">
        <div
          className="dashboard-csync-progress-fill"
          style={{ width: `${pct}%`, background: accent }}
        />
      </div>
      <div className="dashboard-csync-progress-meta">
        <span>{fmtNum(synced)} synced</span>
        <span>{fmtNum(backlog)} pending</span>
      </div>
    </div>
  );
}

function HistoryChart({ samples, accent }) {
  const data = samples.map((s) => ({ t: s.t, backlog: s.backlog }));
  return (
    <div className="dashboard-csync-history">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
          <XAxis
            dataKey="t"
            type="number"
            domain={["dataMin", "dataMax"]}
            tickFormatter={(t) => {
              const d = new Date(t);
              return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
            }}
            stroke="#8a98ab"
            fontSize={10}
            minTickGap={40}
          />
          <YAxis
            stroke="#8a98ab"
            fontSize={10}
            tickFormatter={(v) => fmtNum(v)}
            width={48}
          />
          <Tooltip
            contentStyle={{ background: "#171717", border: "1px solid #2a2a2a", color: "#f2f4f7" }}
            labelFormatter={(t) => new Date(t).toLocaleTimeString()}
            formatter={(v) => [fmtNum(v), "Backlog"]}
          />
          <Line
            type="monotone"
            dataKey="backlog"
            stroke={accent}
            dot={false}
            strokeWidth={2}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function CloudSyncStatusWidget({ widget }) {
  const cfg = widget?.config || {};
  const displayMode = String(cfg.display_mode || "combined");
  const pollMs = Math.max(500, Math.min(60_000, Number(cfg.poll_interval_ms) || DEFAULT_POLL_MS));
  const historyWindowMs = Math.max(30_000, Math.min(3_600_000, (Number(cfg.history_window_sec) || DEFAULT_HISTORY_SEC) * 1000));
  const includeConfigOutbox = cfg.include_config_outbox !== false;
  const includeTelemetryV1 = Boolean(cfg.include_telemetry_v1);
  const accent = String(widget?.color || cfg.accent_color || "#14a89a");
  const isHosted = useMemo(() => detectHostedClientView(), []);

  // The hosted client view talks to the VPS, which doesn't run the sync
  // worker (the edge does). Showing live throughput here is meaningless,
  // so render a clear placeholder instead of misleading 0/0/never numbers.
  if (isHosted) {
    return (
      <div className="dashboard-widget-block dashboard-csync-widget">
        <div className="dashboard-csync-head">
          <div className="dashboard-csync-pill dashboard-csync-pill-muted">
            <span className="dashboard-csync-pill-dot" />
            <span className="dashboard-csync-pill-text">EDGE-ONLY METRIC</span>
          </div>
        </div>
        <div className="dashboard-csync-body" style={{ alignItems: "center", justifyContent: "center", textAlign: "center", padding: "8px 12px" }}>
          <div style={{ maxWidth: 460 }}>
            <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 6 }}>
              Cloud Sync Status is reported by the edge.
            </div>
            <div style={{ fontSize: 12, color: "var(--muted, #8a98ab)", lineHeight: 1.5 }}>
              The edge device pushes historian rows to the cloud database.
              The web/remote view reads that database directly — there is no
              backlog at this layer. Open the TrustNode desktop app on the
              edge machine to see backlog drain, throughput, and last sync.
            </div>
          </div>
        </div>
      </div>
    );
  }

  const [snap, setSnap] = useState(null);
  const [err, setErr] = useState("");
  const samplesRef = useRef([]);
  const [samples, setSamples] = useState([]);
  const prevHistorianRef = useRef({ total: 0, ts: 0 });
  const [rate, setRate] = useState(0);

  useEffect(() => {
    let stopped = false;
    let timer = null;
    const tick = async () => {
      try {
        const data = await getCloudSyncStatus();
        if (stopped) return;
        const now = Date.now();
        setSnap(data);
        setErr("");

        const prev = prevHistorianRef.current;
        const curTotal = Number(data?.historian_synced_total || 0);
        if (prev.ts > 0 && now > prev.ts) {
          const dt = (now - prev.ts) / 1000;
          const dv = curTotal - prev.total;
          if (dt > 0 && dv >= 0) {
            setRate(dv / dt);
          } else if (dv < 0) {
            setRate(0);
          }
        }
        prevHistorianRef.current = { total: curTotal, ts: now };

        const next = [...samplesRef.current, { t: now, backlog: Number(data?.historian_backlog || 0) }];
        const cutoff = now - historyWindowMs;
        while (next.length > 0 && next[0].t < cutoff) next.shift();
        if (next.length > 600) next.splice(0, next.length - 600);
        samplesRef.current = next;
        setSamples(next.slice());
      } catch (e) {
        if (stopped) return;
        setErr(String(e?.message || e || "fetch failed"));
      }
    };
    tick();
    timer = setInterval(tick, pollMs);
    return () => {
      stopped = true;
      if (timer) clearInterval(timer);
    };
  }, [pollMs, historyWindowMs]);

  const status = useMemo(() => computeStatus(snap), [snap]);
  const synced = Number(snap?.historian_synced_total || 0);
  const backlog = Number(snap?.historian_backlog || 0);
  const total = Math.max(0, synced) + Math.max(0, backlog);
  const pct = total > 0 ? Math.round((synced / total) * 100) : 0;
  const host = snap?.cloud_target?.host || snap?.cloud_target?.name || "";
  const lastSyncAge = fmtAge(snap?.last_data_sync_utc || "");
  const rateLabel = rate > 0 ? `${fmtNum(Math.round(rate))} rows/s` : "0 rows/s";

  const tiles = (
    <div className="dashboard-csync-tiles">
      <StatTile label="Backlog" value={fmtNum(backlog)} sub="rows pending" accent={backlog > 0 ? TONE_COLORS.warn : TONE_COLORS.muted} />
      <StatTile label="Synced" value={fmtNum(synced)} sub="cumulative" accent={accent} />
      <StatTile label="Throughput" value={rateLabel} sub="recent average" />
      <StatTile label="Last Sync" value={lastSyncAge} sub={snap?.last_data_sync_utc ? snap.last_data_sync_utc.slice(11, 19) + " UTC" : ""} />
    </div>
  );

  const errorBanner = (err || snap?.last_data_error) ? (
    <div className="dashboard-csync-error">
      {err ? `Status fetch error: ${err}` : `Sync error: ${snap.last_data_error}`}
    </div>
  ) : null;

  const extras = (
    <div className="dashboard-csync-extras">
      {includeConfigOutbox && snap ? (
        <div className="dashboard-csync-extras-line">
          Config outbox: {fmtNum(snap.config_pending || 0)} pending
          {Number(snap.config_failed || 0) > 0 ? `, ${fmtNum(snap.config_failed)} failed` : ""}
          {", "}
          {fmtNum(snap.config_sent_total || 0)} sent
        </div>
      ) : null}
      {includeTelemetryV1 && snap?.telemetry_v1 && Object.keys(snap.telemetry_v1).length > 0 ? (
        <div className="dashboard-csync-extras-line">
          Telemetry-v1: {fmtNum(snap.telemetry_v1.pending || 0)} pending,{" "}
          {fmtNum(snap.telemetry_v1.sent_total || 0)} sent
        </div>
      ) : null}
    </div>
  );

  const body = (() => {
    switch (displayMode) {
      case "donut":
        return (
          <div className="dashboard-csync-body dashboard-csync-body-donut">
            <Donut synced={synced} backlog={backlog} accent={accent} />
            <div className="dashboard-csync-donut-side">
              <div className="dashboard-csync-donut-line">
                <strong>{fmtNum(synced)}</strong> synced
              </div>
              <div className="dashboard-csync-donut-line">
                <strong>{fmtNum(backlog)}</strong> pending
              </div>
              <div className="dashboard-csync-donut-line dashboard-csync-muted">
                {rateLabel} • last {lastSyncAge}
              </div>
            </div>
          </div>
        );
      case "stat_tiles":
        return <div className="dashboard-csync-body">{tiles}</div>;
      case "progress_bar":
        return (
          <div className="dashboard-csync-body dashboard-csync-body-progress">
            <ProgressBar synced={synced} backlog={backlog} accent={accent} />
            <div className="dashboard-csync-progress-foot">
              {pct}% synced • {rateLabel} • last {lastSyncAge}
            </div>
          </div>
        );
      case "line_history":
        return (
          <div className="dashboard-csync-body dashboard-csync-body-history">
            <HistoryChart samples={samples} accent={accent} />
            <div className="dashboard-csync-history-foot">
              Backlog now: <strong>{fmtNum(backlog)}</strong> • {rateLabel} • last {lastSyncAge}
            </div>
          </div>
        );
      case "combined":
      default:
        return (
          <div className="dashboard-csync-body dashboard-csync-body-combined">
            <div className="dashboard-csync-combined-left">
              <Donut synced={synced} backlog={backlog} accent={accent} />
            </div>
            <div className="dashboard-csync-combined-right">
              {tiles}
              {samples.length > 1 ? (
                <div className="dashboard-csync-sparkline">
                  <HistoryChart samples={samples} accent={accent} />
                </div>
              ) : null}
            </div>
          </div>
        );
    }
  })();

  return (
    <div className="dashboard-widget-block dashboard-csync-widget">
      <div className="dashboard-csync-head">
        <StatusPill status={status} host={host} />
        <div className="dashboard-csync-head-meta">
          {pct}% synced • {fmtNum(backlog)} pending
        </div>
      </div>
      {errorBanner}
      {body}
      {extras}
    </div>
  );
}

export default CloudSyncStatusWidget;
