/* Infrastructure Endpoints — developer-admin panel (2026-07-15).

   Single source of truth for WHERE the deployment's services live (control-plane
   API, Supabase, AI, web client). Editing here is how you re-host: change the
   value, and every new activation code carries the new host to edges — no
   hardcoded URL in the app, no operator ever types a URL.

   Rendered inside the Control Plane page (global-admin only). Reuses the app's
   card/form/button CSS tokens so dark + light both work with no new stylesheet.
*/
import { useCallback, useEffect, useMemo, useState } from "react";
import { getInfrastructureEndpoints, saveInfrastructureEndpoints } from "../../api";

// Human labels + hints for the known endpoint keys. Order = display order.
const FIELDS = [
  { key: "cloud_api_url", label: "Control-plane / Portal API URL",
    hint: "Where edges reach the licensing portal (this is what license re-check uses). e.g. https://portal.yourhost.com" },
  { key: "supabase_url", label: "Supabase project URL",
    hint: "Cloud database project URL (informational / future edge-direct reads)." },
  { key: "ai_endpoint_url", label: "AI (TrustNode Intelligence) endpoint",
    hint: "Base URL of the AI inference endpoint." },
  { key: "web_client_url", label: "Cloud web client URL",
    hint: "Optional. Base URL of the cloud read-only web app." },
];

export default function InfrastructureEndpoints({ tenantOptions = [], defaultTenant = "" }) {
  const [scope, setScope] = useState("__global__");   // "__global__" or a tenant_id
  const [values, setValues] = useState({});
  const [resolved, setResolved] = useState({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    setLoading(true); setErr(""); setMsg("");
    try {
      const tid = scope === "__global__" ? "" : scope;
      const r = await getInfrastructureEndpoints(tid);
      setValues(scope === "__global__" ? (r.global || {}) : (r.tenant || {}));
      setResolved(r.resolved || {});
    } catch (e) { setErr(e?.message || String(e)); }
    finally { setLoading(false); }
  }, [scope]);

  useEffect(() => { load(); }, [load]);

  const setField = (k, v) => setValues((prev) => ({ ...prev, [k]: v }));

  const save = async () => {
    setSaving(true); setErr(""); setMsg("");
    try {
      const tid = scope === "__global__" ? "" : scope;
      const r = await saveInfrastructureEndpoints(values, tid);
      setResolved(r.resolved || {});
      setMsg("Saved. New activation codes will carry these endpoints.");
    } catch (e) { setErr(e?.message || String(e)); }
    finally { setSaving(false); }
  };

  const scopeLabel = scope === "__global__" ? "Deployment default (all tenants)" : `Tenant override: ${scope}`;

  return (
    <section className="card" style={{ marginTop: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
        <div>
          <h3 style={{ margin: 0 }}>Infrastructure Endpoints</h3>
          <div style={{ fontSize: 12, color: "var(--muted)" }}>
            Single source of truth for where this deployment is hosted. Editing here is how you migrate hosts —
            new activation codes carry these values to edges automatically. Operators never see or enter a URL.
          </div>
        </div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <select value={scope} onChange={(e) => setScope(e.target.value)} style={{ maxWidth: 260 }}>
            <option value="__global__">Deployment default (all tenants)</option>
            {(tenantOptions || []).map((t) => {
              const id = typeof t === "string" ? t : (t.tenant_id || t.id || "");
              const name = typeof t === "string" ? t : (t.name || t.tenant_id || id);
              return id ? <option key={id} value={id}>Tenant: {name}</option> : null;
            })}
          </select>
        </div>
      </div>

      {err && <div style={{ background: "var(--error-bg)", color: "var(--error-text)", border: "1px solid var(--error-border)", borderRadius: 8, padding: "8px 12px", fontSize: 13, marginBottom: 10 }}>{err}</div>}
      {msg && <div style={{ background: "var(--card)", color: "var(--teal)", border: "1px solid var(--stroke)", borderRadius: 8, padding: "8px 12px", fontSize: 13, marginBottom: 10 }}>{msg}</div>}

      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 10 }}>{scopeLabel}{loading ? " · loading…" : ""}</div>

      <div className="form-grid" style={{ gridTemplateColumns: "1fr" }}>
        {FIELDS.map((f) => (
          <label key={f.key}>
            {f.label}
            <input
              type="text"
              value={values[f.key] || ""}
              placeholder={scope === "__global__" ? "" : (resolved[f.key] ? `inherits: ${resolved[f.key]}` : "")}
              onChange={(e) => setField(f.key, e.target.value)}
              disabled={loading}
            />
            <span style={{ fontSize: 11, color: "var(--muted)" }}>{f.hint}</span>
          </label>
        ))}
      </div>

      {scope !== "__global__" && (
        <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 4 }}>
          Blank fields inherit the deployment default. Set a value only to override for this tenant.
        </div>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 12, alignItems: "center" }}>
        <button className="btn btn-primary btn-sm" disabled={saving || loading} onClick={save}>{saving ? "Saving…" : "Save endpoints"}</button>
        <button className="btn btn-ghost btn-sm" disabled={loading} onClick={load}>Reload</button>
      </div>

      <details style={{ marginTop: 14 }}>
        <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--muted)" }}>Effective (resolved) endpoints for this scope</summary>
        <pre style={{ fontSize: 11, background: "var(--bg)", border: "1px solid var(--stroke)", borderRadius: 8, padding: 10, overflow: "auto", color: "var(--text)" }}>
{JSON.stringify(resolved, null, 2)}
        </pre>
      </details>
    </section>
  );
}
