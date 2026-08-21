// Remote Access page (Connections group) — plan 2026-08-21 §3.4/§3.5.
// Replaces the old "LAN Sharing" card. ONE place for: ON/OFF, the three
// surface URLs per IP (+ hostname URLs) with Copy + QR, HTTP/HTTPS posture
// (HTTPS only / Allow HTTP), certificate download + trust guide, the
// licence state of each surface, and the active remote sessions with
// revoke. Every API call goes through api.js (fetchWithTimeout +
// getControlApiBase → Bearer attached), so it works from any LAN PC.
//
// Backend contract (fields may be absent on older backends — everything
// below is defensive): GET /api/lan-sharing/status → enabled, running,
// port, lan_port, primary_port, ips, lite_urls, full_urls, view_urls,
// hostname, hostname_urls{full,view,lite}, https{available,port,https_only,
// urls{full,view,lite},cert_fingerprint_sha256,cert_url}, http_enabled,
// licensed{lan_access,local_web_app,remote_admin_lan,view_share_links},
// sessions[{username,role,surface,ip,since_utc,last_seen_utc}], last_error,
// bind_host.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getLanSharingStatus,
  enableLanSharing,
  disableLanSharing,
  updateLanSharingConfig,
  revokeLanSharingSession,
  getLanSharingCertificateUrl,
} from "../../api";
import { copyText } from "./copyText";
import { QrCode } from "./QrCode";

export const REMOTE_ACCESS_WIZARD_DONE_KEY = "tn_remote_access_wizard_done";

// Owner-approved product names (plan §0/§7).
export const PRODUCT_NAMES = {
  full: "TrustNode Edge",
  client: "TrustNode Local View",
  lite: "TrustNode Lite (legacy)",
};

const SURFACE_LABELS = {
  desktop: "Desktop",
  loopback: "Desktop",
  full: "TrustNode Edge",
  lan_full: "TrustNode Edge",
  client: "TrustNode Local View",
  lan_client: "TrustNode Local View",
  view: "TrustNode Local View",
  lite: "Lite (legacy)",
  lan_lite: "Lite (legacy)",
  api: "API",
};

const URL_GRID = { gridTemplateColumns: "minmax(120px, .9fr) 2fr 2fr 2fr" };
const SESSION_GRID = { gridTemplateColumns: "1.2fr .8fr 1.4fr 1.1fr 1.3fr 1.3fr 90px" };

function surfaceLabel(surface) {
  const key = String(surface || "").toLowerCase();
  return SURFACE_LABELS[key] || (key ? key : "—");
}

function fmtTime(value) {
  if (!value) return "—";
  try {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleString();
  } catch (_) {
    return String(value);
  }
}

function buildUrl(scheme, host, port, variant) {
  const h = String(host || "").trim();
  if (!h) return "";
  const hostPart = h.includes(":") && !h.startsWith("[") ? `[${h}]` : h;
  const p = Number(port || 0);
  const portPart = p > 0 ? `:${p}` : "";
  return `${scheme}://${hostPart}${portPart}/trustnode/${variant}/`;
}

function licensePill(value, label) {
  const known = typeof value === "boolean";
  const cls = !known ? "status-warning" : (value ? "status-online" : "status-offline");
  const text = !known ? "unknown" : (value ? "licensed" : "not licensed");
  return (
    <span key={label} className={`status-pill ${cls}`} title={`${label}: ${text}`}>
      {label}: {text}
    </span>
  );
}

function UrlCell({ url, label, onCopy, onQr, copied }) {
  if (!url) return <span className="muted">—</span>;
  return (
    <span style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
      <span title={url} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0, flex: 1 }}>
        {url}
      </span>
      <button
        type="button"
        className="btn btn-secondary btn-sm"
        title={`Copy ${label} URL`}
        onClick={() => onCopy(url)}
        style={{ flexShrink: 0 }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
      <button
        type="button"
        className="btn btn-secondary btn-sm"
        title={`Show QR code for the ${label} URL`}
        onClick={() => onQr({ label, url })}
        style={{ flexShrink: 0 }}
      >
        QR
      </button>
    </span>
  );
}

function UrlTable({ title, note, rows, onCopy, onQr, copiedUrl }) {
  if (!rows.length) return null;
  return (
    <div style={{ marginTop: 14 }}>
      <h4 style={{ margin: "0 0 6px" }}>{title}</h4>
      {note ? <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>{note}</div> : null}
      <div className="table-scroll">
        <div className="table">
          <div className="thead" style={URL_GRID}>
            <span>Address</span>
            <span>Edge (full)</span>
            <span>Local View</span>
            <span>Lite</span>
          </div>
          {rows.map((r) => (
            <div key={`${title}-${r.host}`} className="trow" style={URL_GRID}>
              <span title={r.host} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.host}</span>
              <UrlCell url={r.full} label={PRODUCT_NAMES.full} onCopy={onCopy} onQr={onQr} copied={copiedUrl === r.full} />
              <UrlCell url={r.view} label={PRODUCT_NAMES.client} onCopy={onCopy} onQr={onQr} copied={copiedUrl === r.view} />
              <UrlCell url={r.lite} label={PRODUCT_NAMES.lite} onCopy={onCopy} onQr={onQr} copied={copiedUrl === r.lite} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function RemoteAccessPage({ canEdit = false, role = "", onOpenUsers = null }) {
  const roleKey = String(role || "").toLowerCase();
  const isAdmin = roleKey === "admin" || roleKey === "super";
  const [state, setState] = useState(null);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [copiedUrl, setCopiedUrl] = useState("");
  const [qr, setQr] = useState(null);
  const [showWizard, setShowWizard] = useState(false);
  const [revoking, setRevoking] = useState("");

  const refresh = useCallback(async () => {
    try {
      const body = await getLanSharingStatus();
      if (body && typeof body === "object") {
        setState(body);
        setLoadError("");
      }
    } catch (e) {
      setLoadError(String(e?.message || e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [refresh]);

  useEffect(() => {
    if (!copiedUrl) return undefined;
    const t = setTimeout(() => setCopiedUrl(""), 1500);
    return () => clearTimeout(t);
  }, [copiedUrl]);

  const runAction = useCallback(async (fn) => {
    setBusy(true);
    setActionError("");
    try {
      await fn();
      await refresh();
    } catch (e) {
      setActionError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  }, [refresh]);

  const onTurnOn = () => {
    let done = "";
    try { done = localStorage.getItem(REMOTE_ACCESS_WIZARD_DONE_KEY) || ""; } catch (_) { done = ""; }
    if (!done) {
      setShowWizard(true);
      return;
    }
    runAction(enableLanSharing);
  };
  const onWizardContinue = () => {
    try { localStorage.setItem(REMOTE_ACCESS_WIZARD_DONE_KEY, new Date().toISOString()); } catch (_) {}
    setShowWizard(false);
    runAction(enableLanSharing);
  };
  const onTurnOff = () => runAction(disableLanSharing);
  const onConfig = (patch) => runAction(() => updateLanSharingConfig(patch));
  const onRevoke = async (username) => {
    if (!username) return;
    if (!window.confirm(`Revoke every remote session of '${username}'?\n\nTheir current tokens stop working immediately; they must sign in again.`)) return;
    setRevoking(username);
    try {
      await runAction(() => revokeLanSharingSession(username));
    } finally {
      setRevoking("");
    }
  };
  const onCopy = async (url) => {
    const ok = await copyText(url);
    setCopiedUrl(ok ? url : "");
    if (!ok) setActionError("Copy failed — select the URL and copy it manually.");
  };

  const s = state || {};
  const running = Boolean(s.running);
  const enabled = Boolean(s.enabled);
  const httpPort = Number(s.lan_port || s.port || 0);
  const https = s.https && typeof s.https === "object" ? s.https : null;
  const httpsAvailable = Boolean(https?.available);
  const httpsOnly = Boolean(https?.https_only);
  const httpEnabled = s.http_enabled !== false && !httpsOnly;
  const licensed = s.licensed && typeof s.licensed === "object" ? s.licensed : null;
  const sessions = Array.isArray(s.sessions) ? s.sessions : [];
  const hostname = String(s.hostname || "").trim();

  const ips = useMemo(() => (Array.isArray(s.ips) ? s.ips.map((ip) => String(ip || "")).filter(Boolean) : []), [s.ips]);

  const httpRows = useMemo(() => {
    if (!running || !httpEnabled) return [];
    const rows = ips.map((ip, i) => ({
      host: ip,
      full: s.full_urls?.[i] || buildUrl("http", ip, httpPort, "full"),
      view: s.view_urls?.[i] || buildUrl("http", ip, httpPort, "client"),
      lite: s.lite_urls?.[i] || buildUrl("http", ip, httpPort, "lite"),
    }));
    if (hostname) {
      const hu = s.hostname_urls && typeof s.hostname_urls === "object" ? s.hostname_urls : {};
      rows.push({
        host: hostname,
        full: hu.full || buildUrl("http", hostname, httpPort, "full"),
        view: hu.view || buildUrl("http", hostname, httpPort, "client"),
        lite: hu.lite || buildUrl("http", hostname, httpPort, "lite"),
      });
    }
    return rows;
  }, [running, httpEnabled, ips, hostname, httpPort, s.full_urls, s.view_urls, s.lite_urls, s.hostname_urls]);

  const httpsRows = useMemo(() => {
    if (!running || !httpsAvailable) return [];
    const port = Number(https?.port || 0);
    const urls = https?.urls && typeof https.urls === "object" ? https.urls : {};
    const rows = ips.map((ip, i) => ({
      host: ip,
      full: urls.full?.[i] || buildUrl("https", ip, port, "full"),
      view: urls.view?.[i] || buildUrl("https", ip, port, "client"),
      lite: urls.lite?.[i] || buildUrl("https", ip, port, "lite"),
    }));
    if (hostname) {
      rows.push({
        host: hostname,
        full: buildUrl("https", hostname, port, "full"),
        view: buildUrl("https", hostname, port, "client"),
        lite: buildUrl("https", hostname, port, "lite"),
      });
    }
    return rows;
  }, [running, httpsAvailable, ips, hostname, https]);

  const statusText = running
    ? `Running on port ${httpPort || "?"}${httpsAvailable && https?.port ? ` (HTTPS ${https.port})` : ""}`
    : (enabled ? "Bind failed" : "Off");
  const certHref = httpsAvailable ? getLanSharingCertificateUrl(https?.cert_url) : "";
  const fingerprint = String(https?.cert_fingerprint_sha256 || "");

  return (
    <section className="card">
      <h3 style={{ marginTop: 0 }}>Remote Access</h3>
      <p className="muted" style={{ marginTop: 0 }}>
        When ON, this edge listens on the local network so other PCs, panels and phones can open
        <strong> {PRODUCT_NAMES.full}</strong> (full configuration, admin or engineer login),
        <strong> {PRODUCT_NAMES.client}</strong> (read-only dashboards and reports) or the legacy Lite page
        from their browser. Each user still needs an account with the matching LAN Web Access flag.
      </p>

      <div className="row" style={{ alignItems: "center", gap: 10 }}>
        <span className={`status-pill ${running ? "status-online" : (enabled ? "status-warning" : "status-offline")}`}>{statusText}</span>
        {s.bind_host ? <span className="muted" style={{ fontSize: 12 }}>bind {String(s.bind_host)}</span> : null}
        {!running && canEdit ? (
          <button type="button" className="btn btn-primary" disabled={busy} onClick={onTurnOn}>Turn ON</button>
        ) : null}
        {running && canEdit ? (
          <button type="button" className="btn btn-danger" disabled={busy} onClick={onTurnOff}>Turn OFF</button>
        ) : null}
        <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={refresh}>Refresh</button>
        {!canEdit ? <span className="muted" style={{ fontSize: 12 }}>Only an admin with the LAN access licence can change these settings.</span> : null}
      </div>

      {loadError ? <div className="lock-note" style={{ marginTop: 10 }}>Could not read the Remote Access status: {loadError}</div> : null}
      {actionError ? <div className="lock-note" style={{ marginTop: 10 }}>{actionError}</div> : null}
      {s.last_error ? <div className="lock-note" style={{ marginTop: 10 }}>Last error: {String(s.last_error)}</div> : null}

      {licensed ? (
        <div className="row" style={{ marginTop: 12, gap: 6 }}>
          {licensePill(licensed.remote_admin_lan, "Remote admin (Edge)")}
          {licensePill(licensed.local_web_app, "Local View")}
          {licensePill(licensed.view_share_links, "Share links")}
          {typeof licensed.lan_access === "boolean" && !licensed.lan_access
            ? <span className="status-pill status-offline">LAN access: not licensed</span>
            : null}
        </div>
      ) : null}

      {running && httpEnabled ? (
        <div className="lock-note" style={{ marginTop: 12 }}>
          <strong>HTTP is active.</strong> Traffic is unencrypted on this network. Prefer the HTTPS links
          {httpsAvailable ? " below" : " (enable HTTPS on the edge)"} and turn on &ldquo;HTTPS only&rdquo; once every device trusts the certificate.
        </div>
      ) : null}

      {running && httpRows.length === 0 && httpsRows.length === 0 ? (
        <div className="muted" style={{ marginTop: 12 }}>No LAN address detected yet. Check that the machine has a network adapter with an IPv4 address.</div>
      ) : null}
      {!running ? (
        <div className="muted" style={{ marginTop: 12 }}>Remote Access is off. Turn it ON to expose the URLs.</div>
      ) : null}

      <UrlTable
        title="HTTPS links (recommended)"
        note={fingerprint ? `Self-signed certificate · SHA-256 ${fingerprint}` : "Self-signed certificate generated on this edge."}
        rows={httpsRows}
        onCopy={onCopy}
        onQr={setQr}
        copiedUrl={copiedUrl}
      />
      <UrlTable
        title="HTTP links"
        note="Works on any network without certificate setup — unencrypted."
        rows={httpRows}
        onCopy={onCopy}
        onQr={setQr}
        copiedUrl={copiedUrl}
      />

      <h4 style={{ marginTop: 22, marginBottom: 6 }}>Transport security</h4>
      <div className="row" style={{ gap: 18, alignItems: "center" }}>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }} title={httpsAvailable ? "Refuse plain HTTP; only the HTTPS listener answers." : "HTTPS is not available on this edge yet."}>
          <input
            type="checkbox"
            checked={httpsOnly}
            disabled={!canEdit || busy || !httpsAvailable}
            onChange={(e) => onConfig({ https_only: e.target.checked })}
          />
          HTTPS only
        </label>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }} title="Serve the unencrypted HTTP listener (zero-friction, any network).">
          <input
            type="checkbox"
            checked={s.http_enabled !== false}
            disabled={!canEdit || busy || httpsOnly}
            onChange={(e) => onConfig({ http_enabled: e.target.checked })}
          />
          Allow HTTP
        </label>
        {httpsAvailable ? (
          <a className="btn btn-secondary btn-sm" href={certHref} download="trustnode-edge-certificate.pem" title="Download the edge certificate (PEM)">
            Download certificate
          </a>
        ) : (
          <span className="muted" style={{ fontSize: 12 }}>HTTPS listener not available on this build/edge.</span>
        )}
      </div>
      <details style={{ marginTop: 8 }}>
        <summary className="muted" style={{ cursor: "pointer", fontSize: 12 }}>How to trust the certificate (Windows / Android / iOS)</summary>
        <p className="muted" style={{ fontSize: 12, lineHeight: 1.5, marginTop: 6 }}>
          Download the certificate on the device, then install it as a trusted root:
          <strong> Windows</strong> — double-click the .pem, Install Certificate, Local Machine, place it in
          &ldquo;Trusted Root Certification Authorities&rdquo; (or use certlm.msc), restart the browser.
          <strong> Android</strong> — Settings, Security, Encryption &amp; credentials, Install a certificate,
          CA certificate, pick the file (Chrome honours it; some vendors also require the user CA to be allowed per app).
          <strong> iOS / iPadOS</strong> — open the file, install the profile in Settings, General, VPN &amp; Device Management,
          then enable full trust under Settings, General, About, Certificate Trust Settings.
          Sites with their own CA can replace the self-signed pair on the edge instead.
        </p>
      </details>

      {isAdmin ? (
        <>
          <h4 style={{ marginTop: 22, marginBottom: 6 }}>Active remote sessions</h4>
          {sessions.length === 0 ? (
            <div className="muted" style={{ fontSize: 12 }}>No remote session right now.</div>
          ) : (
            <div className="table-scroll">
              <div className="table">
                <div className="thead" style={SESSION_GRID}>
                  <span>User</span><span>Role</span><span>Surface</span><span>IP</span><span>Since</span><span>Last seen</span><span>Actions</span>
                </div>
                {sessions.map((sess, idx) => {
                  const uname = String(sess?.username || "");
                  return (
                    <div key={`${uname}-${sess?.ip || ""}-${idx}`} className="trow" style={SESSION_GRID}>
                      <span title={uname} style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{uname || "—"}</span>
                      <span>{String(sess?.role || "—")}</span>
                      <span>{surfaceLabel(sess?.surface)}</span>
                      <span>{String(sess?.ip || "—")}</span>
                      <span>{fmtTime(sess?.since_utc)}</span>
                      <span>{fmtTime(sess?.last_seen_utc)}</span>
                      <span>
                        <button
                          type="button"
                          className="btn btn-danger btn-sm"
                          disabled={busy || !uname || revoking === uname}
                          onClick={() => onRevoke(uname)}
                          title="Invalidate this user's tokens"
                        >
                          {revoking === uname ? "…" : "Revoke"}
                        </button>
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </>
      ) : null}

      {qr ? (
        <div className="modal-backdrop" onClick={() => setQr(null)}>
          <div className="modal-card" style={{ width: "min(360px, 96vw)" }} onClick={(e) => e.stopPropagation()}>
            <h3 style={{ marginTop: 0 }}>{qr.label}</h3>
            <div style={{ display: "flex", justifyContent: "center", padding: 8 }}>
              <QrCode value={qr.url} size={240} title={qr.url} />
            </div>
            <div className="muted" style={{ fontSize: 12, wordBreak: "break-all", textAlign: "center" }}>{qr.url}</div>
            <div className="row modal-actions">
              <button type="button" className="btn btn-secondary" onClick={() => onCopy(qr.url)}>{copiedUrl === qr.url ? "Copied" : "Copy URL"}</button>
              <button type="button" className="btn btn-primary" onClick={() => setQr(null)}>Close</button>
            </div>
          </div>
        </div>
      ) : null}

      {showWizard ? (
        <div className="modal-backdrop">
          <div className="modal-card" style={{ width: "min(520px, 96vw)" }}>
            <h3 style={{ marginTop: 0 }}>Turn on Remote Access</h3>
            <p style={{ fontSize: 13, lineHeight: 1.5 }}>
              The edge will start listening on every LAN address of this machine. The <strong>admin login
              becomes reachable from other computers</strong> on the network — anyone on the LAN can reach the
              sign-in page, so make sure the admin and engineer passwords are strong.
            </p>
            <p style={{ fontSize: 13, lineHeight: 1.5 }}>
              We recommend using the <strong>HTTPS links</strong> and installing the edge certificate on each
              device (&ldquo;Download certificate&rdquo; + the trust guide on this page). Plain HTTP keeps working
              on any network but is unencrypted.
            </p>
            <p style={{ fontSize: 13, lineHeight: 1.5 }}>
              Who can open what is decided per user with the <strong>LAN Web Access</strong> flags
              ({PRODUCT_NAMES.full} / {PRODUCT_NAMES.client} / Lite) in
              {onOpenUsers ? (
                <> <button type="button" className="btn btn-secondary btn-sm" onClick={() => { setShowWizard(false); onOpenUsers(); }}>Users and Access Control</button></>
              ) : (
                <> Users and Access Control</>
              )}.
            </p>
            <div className="row modal-actions">
              <button type="button" className="btn btn-secondary" onClick={() => setShowWizard(false)}>Cancel</button>
              <button type="button" className="btn btn-primary" disabled={busy} onClick={onWizardContinue}>Continue and turn ON</button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}

export default RemoteAccessPage;
