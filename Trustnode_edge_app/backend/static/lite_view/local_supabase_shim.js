// LOCAL SUPABASE SHIM — operator 2026-06-18
//
// The forked cloud Lite at /trustnode/lite/app/ was written to talk to
// Supabase. This shim REPLACES `@supabase/supabase-js`'s `createClient`
// with a tiny adapter that routes calls to the local edge's
// /api/lite-local/* endpoints.
//
// Supported surface (the cloud Lite uses these):
//   - supabase.from(table).select(...).eq(...).limit(...).order(...)
//     → resolved by a per-table mapping to /api/lite-local/*
//   - supabase.channel(name).on(...).subscribe()
//     → no-op; polling-based refresh kicks in via React useEffect.
//   - supabase.auth.getSession()
//     → returns the LAN view-link token stored at boot.
//
// What's NOT supported and silently degrades:
//   - Realtime push (channels just return an empty subscription)
//   - INSERT/UPDATE/DELETE (read-only Lite for now)
//   - Storage / Auth signup-magic-link / RPC
//
// This is a FIRST CUT. Tables not in TABLE_TO_ENDPOINT return empty.

const VIEW_LINK_TOKEN_KEY = "tn_lite_local_token";
const VIEW_LINK_SCOPE_KEY = "tn_lite_local_scope";

function _readToken() {
  try {
    const u = new URL(window.location.href);
    const t = u.searchParams.get("token");
    if (t) {
      sessionStorage.setItem(VIEW_LINK_TOKEN_KEY, t);
      return t;
    }
    return sessionStorage.getItem(VIEW_LINK_TOKEN_KEY) || "";
  } catch (_) { return ""; }
}

const TOKEN = _readToken();

// The Lite's main script calls _resolveViewLink → /api/lite-local/validate
// BEFORE rendering. That call writes the scope (tenant/customer/edge) to
// sessionStorage so the shim's table handlers can synthesize rows that
// match the cloud Lite's effectiveTenantId gate.
function _scope() {
  try {
    const raw = sessionStorage.getItem(VIEW_LINK_SCOPE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (_) { return null; }
}

async function _localFetch(path, params = {}) {
  const u = new URL(path, window.location.origin);
  if (TOKEN) u.searchParams.set("token", TOKEN);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== "") u.searchParams.set(k, String(v));
  }
  // Operator 2026-06-18: also send the JWT (from /api/auth/login) as a
  // Bearer header so a logged-in user without a view-link token still
  // gets data. lite-local accepts EITHER auth path.
  let jwt = "";
  try { jwt = localStorage.getItem("trustnode_auth_token") || ""; } catch (_) {}
  const headers = jwt ? { "Authorization": `Bearer ${jwt}` } : {};
  try {
    const res = await fetch(u.toString(), { method: "GET", credentials: "omit", headers });
    if (!res.ok) return { data: [], error: { message: `${res.status} ${res.statusText}` } };
    return { data: await res.json(), error: null };
  } catch (e) {
    return { data: [], error: { message: String(e?.message || e) } };
  }
}

// Map cloud-Lite table reads → local-edge endpoints. Each function
// receives the chain's filter context and returns the rows in the shape
// the cloud Lite expects (column names match).
const TABLE_HANDLERS = {
  live_latest: async (ctx) => {
    const r = await _localFetch("/api/lite-local/live", { limit: ctx.limit || 2000 });
    return { data: Array.isArray(r.data?.rows) ? r.data.rows : [], error: r.error };
  },
  historian_readings: async (ctx) => {
    const r = await _localFetch("/api/lite-local/historian", {
      limit: ctx.limit || 5000,
      from_utc: ctx.eq?.from_utc || "",
      to_utc: ctx.eq?.to_utc || "",
      gateway: ctx.eq?.gateway_id || "",
      tag: ctx.eq?.tag_name || "",
    });
    return { data: Array.isArray(r.data?.rows) ? r.data.rows : [], error: r.error };
  },
  dashboard_configurations: async () => {
    const boot = await _localFetch("/api/lite-local/bootstrap");
    const data = boot.data?.data || {};
    const cfg = data.dashboard_configurations || {};
    const rows = [];
    if (cfg && (cfg.widgets || cfg.profiles)) {
      // Anchor the synthetic row to the VIEW-LINK scope when present.
      // This is what makes the cloud Lite's effectiveTenantId gate pass:
      // its filter at "(p.tenant_id !== effectiveTenantId)" only succeeds
      // when both sides agree, so we MUST emit the view-link's tenant.
      const sc = _scope() || {};
      const s = data?.app_settings || {};
      const tenant_id  = String(sc.tenant_id  || s.tenant_id  || "default");
      const customer_id= String(sc.customer_id|| s.customer_id|| "");
      const edge_id    = String(sc.edge_id    || s.edge_id    || "");
      const scope_key = `${tenant_id}|${customer_id}|${edge_id}`;
      rows.push({
        scope_key,
        payload_json: cfg,
        version: 1,
        updated_utc: new Date().toISOString(),
        tenant_id,
      });
    }
    return { data: rows, error: boot.error };
  },
  cp_edges: async () => {
    const sc = _scope() || {};
    if (sc.edge_id) {
      return { data: [{ edge_id: sc.edge_id, edge_name: sc.edge_id, tenant_id: sc.tenant_id || "default" }], error: null };
    }
    const boot = await _localFetch("/api/lite-local/bootstrap");
    const s = boot.data?.data?.app_settings || {};
    const edge_id = s.edge_id || "";
    return { data: edge_id ? [{ edge_id, edge_name: s.edge_name || edge_id, tenant_id: s.tenant_id || "default" }] : [], error: null };
  },
  cp_customers: async () => {
    const sc = _scope() || {};
    if (sc.customer_id) {
      return { data: [{ customer_id: sc.customer_id, company_name: sc.customer_id, tenant_id: sc.tenant_id || "default" }], error: null };
    }
    const boot = await _localFetch("/api/lite-local/bootstrap");
    const s = boot.data?.data?.app_settings || {};
    return { data: s.customer_id ? [{ customer_id: s.customer_id, company_name: s.customer_id, tenant_id: s.tenant_id || "default" }] : [], error: null };
  },
  lite_profiles: async () => {
    const sc = _scope() || {};
    if (sc.tenant_id) {
      return { data: [{ tenant_id: sc.tenant_id, customer_id: sc.customer_id || "" }], error: null };
    }
    const boot = await _localFetch("/api/lite-local/bootstrap");
    const s = boot.data?.data?.app_settings || {};
    return { data: [{ tenant_id: s.tenant_id || "default", customer_id: s.customer_id || "" }], error: null };
  },
  alarms_setup: async () => {
    const boot = await _localFetch("/api/lite-local/bootstrap");
    const a = boot.data?.data?.alarms_setup || { rules: [], events: [] };
    return { data: Array.isArray(a.rules) ? a.rules : [], error: boot.error };
  },
  triggers_limits: async () => {
    const boot = await _localFetch("/api/lite-local/bootstrap");
    const t = boot.data?.data?.triggers_limits || {};
    return { data: Array.isArray(t.rules) ? t.rules : [], error: boot.error };
  },
  app_logs: async (ctx) => {
    // No /api/lite-local/logs endpoint yet — return empty.
    return { data: [], error: null };
  },
  generated_reports: async () => {
    // Reports queue is cloud-only; LAN slim Lite shows none for now.
    return { data: [], error: null };
  },
  report_templates: async () => {
    const boot = await _localFetch("/api/lite-local/bootstrap");
    const r = boot.data?.data?.report_templates || { templates: [] };
    return { data: Array.isArray(r.templates) ? r.templates : [], error: boot.error };
  },
};

// Chainable builder mimicking the supabase-js fluent API just enough for
// the cloud Lite's call sites.
function _builder(table) {
  const ctx = { table, columns: "*", eq: {}, limit: null, order: null };
  const exec = async () => {
    const handler = TABLE_HANDLERS[table];
    if (!handler) return { data: [], error: null };
    const out = await handler(ctx);
    // Honour client-side filters that the local API didn't apply.
    let rows = Array.isArray(out.data) ? out.data : [];
    for (const [k, v] of Object.entries(ctx.eq || {})) {
      if (v === undefined || v === null) continue;
      rows = rows.filter((r) => String(r?.[k] ?? "") === String(v));
    }
    if (Number.isFinite(ctx.limit)) rows = rows.slice(0, ctx.limit);
    if (ctx.order) {
      const { col, asc } = ctx.order;
      rows = rows.slice().sort((a, b) => {
        const av = a?.[col]; const bv = b?.[col];
        if (av === bv) return 0;
        return (av > bv ? 1 : -1) * (asc ? 1 : -1);
      });
    }
    return { data: rows, error: out.error };
  };
  const api = {
    select: (cols) => { ctx.columns = cols || "*"; return api; },
    eq: (k, v) => { ctx.eq[k] = v; return api; },
    in: (k, vs) => { ctx.eq[k] = vs; return api; },
    gte: (k, v) => { ctx.eq[k] = v; return api; },
    lte: (k, v) => { ctx.eq[k] = v; return api; },
    gt: (k, v) => { ctx.eq[k] = v; return api; },
    lt: (k, v) => { ctx.eq[k] = v; return api; },
    ilike: () => api,
    or: () => api,
    not: () => api,
    is: () => api,
    limit: (n) => { ctx.limit = Number(n) || null; return api; },
    order: (col, opts) => { ctx.order = { col, asc: !(opts && opts.ascending === false) }; return api; },
    range: () => api,
    maybeSingle: async () => {
      const r = await exec();
      return { data: r.data?.[0] || null, error: r.error };
    },
    single: async () => {
      const r = await exec();
      return { data: r.data?.[0] || null, error: r.error };
    },
    then: (resolve, reject) => exec().then(resolve, reject),
    catch: (rej) => exec().catch(rej),
    // Insert/update/delete are no-ops on read-only Lite.
    insert: async () => ({ data: null, error: { message: "read-only Lite" } }),
    update: async () => ({ data: null, error: { message: "read-only Lite" } }),
    delete: async () => ({ data: null, error: { message: "read-only Lite" } }),
    upsert: async () => ({ data: null, error: { message: "read-only Lite" } }),
  };
  return api;
}

function _channel(name) {
  return {
    on: function() { return this; },
    subscribe: function(cb) { try { cb && cb("SUBSCRIBED"); } catch (_) {} return this; },
    unsubscribe: async function() { return { error: null }; },
  };
}

function _auth() {
  // Operator 2026-06-18: synthesize a Supabase-shape session so the
  // cloud Lite's gate (`!!session || isViewLinkMode`) lets the
  // dashboard hook run. The JWT may come from either:
  //   (a) /api/auth/login → localStorage.trustnode_auth_token
  //   (b) /api/lite-local/validate → sessionStorage.tn_lite_local_token
  function _activeSession() {
    let jwt = "";
    try { jwt = localStorage.getItem("trustnode_auth_token") || ""; } catch (_) {}
    if (!jwt && TOKEN) jwt = TOKEN;
    if (!jwt) return null;
    // Supabase's session has user + access_token + claims under user.user_metadata.
    // The cloud Lite only reads session truthiness + session.user.id for log lines.
    return {
      access_token: jwt,
      token_type: "bearer",
      expires_at: Math.floor(Date.now() / 1000) + 3600 * 12,
      user: {
        id: "lan-viewer",
        email: "lan@trustnode",
        user_metadata: { tn_lan: true },
      },
    };
  }
  return {
    getSession: async () => ({ data: { session: _activeSession() }, error: null }),
    getUser: async () => {
      const s = _activeSession();
      return { data: { user: s ? s.user : null }, error: null };
    },
    signOut: async () => {
      try { localStorage.removeItem("trustnode_auth_token"); } catch (_) {}
      try { sessionStorage.removeItem("tn_lite_local_token"); } catch (_) {}
      try { sessionStorage.removeItem("tn_lite_local_scope"); } catch (_) {}
      // Send the user back to /trustnode/login/ next render.
      try { window.location.replace("/trustnode/login/?variant=lite&return=/trustnode/lite/app/"); } catch (_) {}
      return { error: null };
    },
    onAuthStateChange: () => ({ data: { subscription: { unsubscribe: () => {} } } }),
    signInWithPassword: async () => ({ data: null, error: { message: "use /trustnode/login/" } }),
  };
}

export function createClient(_url, _anonKey, _opts) {
  return {
    from: _builder,
    channel: _channel,
    removeChannel: async () => ({ error: null }),
    auth: _auth(),
    storage: {
      from: (_bucket) => ({
        download: async () => ({ data: null, error: { message: "no storage on LAN" } }),
        upload: async () => ({ data: null, error: { message: "no storage on LAN" } }),
        getPublicUrl: () => ({ data: { publicUrl: "" } }),
      }),
    },
    rpc: async () => ({ data: null, error: { message: "no rpc on LAN" } }),
  };
}
