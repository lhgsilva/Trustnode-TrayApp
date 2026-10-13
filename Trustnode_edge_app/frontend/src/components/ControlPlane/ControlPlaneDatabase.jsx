/* Where the PORTAL's own database lives — developer-admin panel (2026-10-13).
 *
 * Owner's design: "the portal database can also be moved to a different
 * database where the portal website would be pointed out (to be configured
 * from the portal)".
 *
 * Until now that target was environment variables read at import, so moving it
 * meant editing a systemd drop-in over SSH and restarting — not something the
 * portal could do, which was the whole point of the requirement.
 *
 * Two deliberate safety properties:
 *   - Test is MANDATORY before save (the server re-tests and refuses a target
 *     it cannot reach). A saved-but-wrong target would take the portal down on
 *     the next restart, and the only way back would be a shell.
 *   - The password is never sent back to the browser. Leaving the field blank
 *     keeps the stored one, so a round-trip cannot silently erase it.
 */
import { useCallback, useEffect, useState } from "react";
import {
  getControlPlaneDatabase,
  testControlPlaneDatabase,
  saveControlPlaneDatabase,
} from "../../api";

const BLANK = {
  backend: "cloud", host: "", port: "5432", database: "postgres",
  username: "", password: "", sslmode: "require", keep_password: true,
};

export default function ControlPlaneDatabase() {
  const [form, setForm] = useState(BLANK);
  const [info, setInfo] = useState(null);
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    setBusy(true); setErr(""); setMsg("");
    try {
      const r = await getControlPlaneDatabase();
      const t = r?.target || {};
      setInfo(t);
      setForm((prev) => ({
        ...prev,
        backend: t.backend || "cloud",
        host: t.host || "",
        port: String(t.port || "5432"),
        database: t.database || "postgres",
        username: t.username || "",
        sslmode: t.sslmode || "require",
        password: "",
        keep_password: true,
      }));
    } catch (e) { setErr(e?.message || String(e)); }
    finally { setBusy(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const set = (k, v) => {
    setForm((p) => ({ ...p, [k]: v, ...(k === "password" ? { keep_password: !v } : {}) }));
    setTestResult(null);   // a changed target invalidates the previous result
  };

  const doTest = async () => {
    setBusy(true); setErr(""); setMsg(""); setTestResult(null);
    try {
      const r = await testControlPlaneDatabase(form);
      setTestResult(r?.result || null);
    } catch (e) { setErr(e?.message || String(e)); }
    finally { setBusy(false); }
  };

  const doSave = async () => {
    setBusy(true); setErr(""); setMsg("");
    try {
      const r = await saveControlPlaneDatabase(form);
      setInfo(r?.target || null);
      setMsg("Saved. Restart the backend for the portal to use it.");
      setTestResult(r?.test || null);
    } catch (e) { setErr(e?.message || String(e)); }
    finally { setBusy(false); }
  };

  const isCloud = String(form.backend || "") === "cloud";
  const canSave = Boolean(testResult && testResult.ok) && !busy;

  return (
    <section className="card" style={{ marginTop: 16 }}>
      <h3 style={{ margin: 0 }}>Portal database</h3>
      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 10 }}>
        Where this portal keeps customers, licences and users. Separate from any
        customer&apos;s own data database. Test a target before saving it; the
        change takes effect on the next backend restart.
      </div>

      {err && <div className="error" style={{ fontSize: 13 }}>{err}</div>}
      {msg && <div style={{ background: "var(--card)", color: "var(--teal)", border: "1px solid var(--stroke)", borderRadius: 8, padding: "8px 12px", fontSize: 13, marginBottom: 10 }}>{msg}</div>}

      {info && (
        <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 10 }}>
          Currently in use: <strong>{info.backend}</strong>
          {info.host ? <> · {info.host}:{info.port}/{info.database}</> : null}
          {" · "}source: <strong>{info.source}</strong>
          {info.password ? <> · password {info.password}</> : null}
          {info.env_override_active
            ? " · TRUSTNODE_CONTROL_PLANE_FORCE_ENV is set, so the environment wins and this form is ignored"
            : null}
          {info.restart_required ? " · restart pending" : null}
        </div>
      )}

      <div className="form-grid" style={{ gridTemplateColumns: "1fr" }}>
        <label>
          Backend
          <select value={form.backend} onChange={(e) => set("backend", e.target.value)} disabled={busy}>
            <option value="cloud">Cloud Postgres / Supabase</option>
            <option value="local">Local SQLite (single-host)</option>
          </select>
          <span style={{ fontSize: 11, color: "var(--muted)" }}>
            Local keeps the control plane in this host&apos;s own file — fine for one
            machine, not for a portal several edges talk to.
          </span>
        </label>

        {isCloud && (
          <>
            <label>Host
              <input type="text" value={form.host} placeholder="aws-1-eu-west-1.pooler.supabase.com"
                     onChange={(e) => set("host", e.target.value)} disabled={busy} />
            </label>
            <label>Port
              <input type="text" value={form.port} onChange={(e) => set("port", e.target.value)} disabled={busy} />
            </label>
            <label>Database
              <input type="text" value={form.database} onChange={(e) => set("database", e.target.value)} disabled={busy} />
            </label>
            <label>Username
              <input type="text" value={form.username} onChange={(e) => set("username", e.target.value)} disabled={busy} />
            </label>
            <label>Password
              <input type="password" value={form.password}
                     placeholder={info?.password ? "leave blank to keep the stored password" : ""}
                     onChange={(e) => set("password", e.target.value)} disabled={busy} />
              <span style={{ fontSize: 11, color: "var(--muted)" }}>
                Never shown back. Blank keeps the one already stored.
              </span>
            </label>
            <label>SSL mode
              <select value={form.sslmode} onChange={(e) => set("sslmode", e.target.value)} disabled={busy}>
                <option value="require">require</option>
                <option value="verify-full">verify-full</option>
                <option value="disable">disable</option>
              </select>
            </label>
          </>
        )}
      </div>

      {testResult && (
        <div style={{ fontSize: 12, marginTop: 10,
                      color: testResult.ok ? "var(--teal)" : "var(--error-text)" }}>
          {testResult.ok
            ? `✓ ${testResult.detail || "Connected."}`
            : `✗ ${testResult.error || "Connection failed."}`}
          {testResult.ok && Array.isArray(testResult.cp_tables) && testResult.cp_tables.length === 0
            ? " Provision the control-plane schema there before switching."
            : null}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
        <button type="button" className="btn btn-secondary" onClick={doTest} disabled={busy}>
          Test connection
        </button>
        <button type="button" className="btn btn-primary" onClick={doSave} disabled={!canSave}
                title={canSave ? "" : "Run a successful test first"}>
          Save &amp; point the portal here
        </button>
        <button type="button" className="btn btn-link" onClick={load} disabled={busy}>Reload</button>
      </div>
    </section>
  );
}
