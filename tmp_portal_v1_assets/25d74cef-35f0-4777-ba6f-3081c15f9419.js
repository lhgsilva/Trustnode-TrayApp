const { useState, useEffect, useRef } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "dark",
  "loginTarget": "portal",
  "showBackdrop": true,
  "backdropBlur": 8,
  "cardOpacity": 0.18,
  "rememberMe": true,
  "showLanguage": true,
  "language": "EN"
} /*EDITMODE-END*/;

// ---- Icons ---------------------------------------------------------------
const Icon = {
  user: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <circle cx="12" cy="8" r="4"></circle>
      <path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"></path>
    </svg>,

  lock: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <rect x="4" y="10" width="16" height="11" rx="2"></rect>
      <path d="M8 10V7a4 4 0 0 1 8 0v3"></path>
    </svg>,

  eye: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"></path>
      <circle cx="12" cy="12" r="3"></circle>
    </svg>,

  eyeOff: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M3 3l18 18"></path>
      <path d="M10.6 6.1A10.7 10.7 0 0 1 12 6c6.5 0 10 7 10 7a17.3 17.3 0 0 1-3.2 4.1"></path>
      <path d="M6.6 6.6A17.3 17.3 0 0 0 2 13s3.5 7 10 7a10.7 10.7 0 0 0 5.4-1.5"></path>
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"></path>
    </svg>,

  key: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <circle cx="8" cy="14" r="4"></circle>
      <path d="M11 11l9-9"></path>
      <path d="M16 6l3 3"></path>
    </svg>,

  cpu: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <rect x="6" y="6" width="12" height="12" rx="2"></rect>
      <rect x="9" y="9" width="6" height="6" rx="1"></rect>
      <path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"></path>
    </svg>,

  pulse: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M3 12h4l2-6 4 12 2-6h6"></path>
    </svg>,

  shield: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"></path>
      <path d="M9 12l2 2 4-4"></path>
    </svg>,

  chev: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M6 9l6 6 6-6"></path>
    </svg>,

  globe: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <circle cx="12" cy="12" r="9"></circle>
      <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"></path>
    </svg>,

  check: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M5 12l4.5 4.5L19 7"></path>
    </svg>,

  spinner: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" {...p}>
      <path d="M12 3a9 9 0 1 1-9 9" />
    </svg>,

  arrowRight: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M5 12h14M13 6l6 6-6 6"></path>
    </svg>,

  sun: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>,

  moon: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </svg>,

  cloud: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M7 18a5 5 0 1 1 .8-9.94A6 6 0 0 1 19 11a4 4 0 0 1 0 8H7z"></path>
    </svg>,

  hardware: (p) =>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <rect x="3" y="6" width="18" height="12" rx="2"></rect>
      <path d="M7 10h.01M7 14h.01M11 10h6M11 14h6"></path>
    </svg>

};

// ---- Backdrop --------------------------------------------------------
function Backdrop({ blur, show, theme }) {
  const filter = theme === 'light' ?
  `blur(${blur}px) saturate(0.85) brightness(1.06)` :
  `blur(${blur}px) saturate(1.05) brightness(0.55)`;
  return (
    <div className="backdrop" aria-hidden="true">
      {show && <img src={(window.__resources && window.__resources.factoryBg) || "assets/factory-bg.png"} alt="" style={{ filter }} />}
      <div className="backdrop-tint" style={{ opacity: "0.2" }} />
    </div>);
}

// ---- Side telemetry panel -------------------------------------------
function EdgePanel({ loginTarget }) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((x) => x + 1), 1500);
    return () => clearInterval(t);
  }, []);

  const portalNodes = [
  { id: 'EDGE-NODE-A1', site: 'Plant São Paulo · Line 2', status: 'online', tags: 1284 },
  { id: 'EDGE-NODE-B3', site: 'Plant Recife · Bottling', status: 'online', tags: 642 },
  { id: 'EDGE-NODE-C7', site: 'Plant Joinville · CNC', status: 'sync', tags: 318 },
  { id: 'EDGE-NODE-D2', site: 'Plant Manaus · Press', status: 'online', tags: 902 }];

  const edgeNodes = [
  { id: 'EDGE-NODE-LOCAL', site: 'This gateway · Line 2', status: 'online', tags: 1284 },
  { id: 'PLC · Siemens S7', site: 'OPC UA · 192.168.10.21', status: 'online', tags: 412 },
  { id: 'PLC · Allen-Bradley', site: 'EtherNet/IP · .22', status: 'online', tags: 308 },
  { id: 'Modbus RTU bridge', site: 'COM3 · 19200 baud', status: 'sync', tags: 64 }];

  const nodes = loginTarget === 'edge' ? edgeNodes : portalNodes;

  const sparkPath = (seed) => {
    const pts = [];
    for (let i = 0; i < 32; i++) {
      const v = 50 + Math.sin((i + tick + seed * 7) * 0.45) * 16 + Math.sin((i + seed) * 0.9) * 6;
      pts.push(`${i / 31 * 100},${100 - v}`);
    }
    return 'M' + pts.join(' L');
  };

  const eyebrow = loginTarget === 'edge' ? 'LIVE GATEWAY TELEMETRY' : 'LIVE FLEET TELEMETRY';
  const title = loginTarget === 'edge' ?
  'Local edge gateway' :
  'Industrial edge gateway fleet';
  const sub = loginTarget === 'edge' ?
  'On-device acquisition from PLCs, SCADA and field devices, with local buffering when the cloud is unreachable.' :
  'Collect, normalize and forward signals from PLCs, SCADA and field devices into TrustNode databases, dashboards and reports.';

  const stats = loginTarget === 'edge' ?
  [
  { k: '1', l: 'Local node' },
  { k: '12,480', sub: '/s', l: 'Tags ingested' },
  { k: '24h', l: 'Local buffer' }] :

  [
  { k: '412', l: 'Active edge nodes' },
  { k: '3.18M', sub: '/min', l: 'Tags ingested' },
  { k: '99.982%', l: 'Pipeline uptime' }];


  return (
    <aside className="edge-panel">
      <header className="edge-head">
        <div className="edge-head-row">
          <div className="edge-eyebrow">
            <span className="dot dot-live" /> {eyebrow}
          </div>
          <div className="edge-time">{new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</div>
        </div>
        <h2>{title}</h2>
        <p>{sub}</p>
      </header>

      <div className="edge-stats">
        {stats.map((s, i) =>
        <div key={i} className="stat">
            <div className="stat-k">{s.k}{s.sub && <span>{s.sub}</span>}</div>
            <div className="stat-l">{s.l}</div>
          </div>
        )}
      </div>

      <div className="edge-card">
        <div className="edge-card-head">
          <span>Acquisition throughput · last 60s</span>
          <span className="badge">OPC UA · MQTT · Modbus</span>
        </div>
        <svg className="spark" viewBox="0 0 100 60" preserveAspectRatio="none">
          <defs>
            <linearGradient id="g1" x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.55" />
              <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
            </linearGradient>
          </defs>
          <path d={sparkPath(0).replace('M', 'M-2,60 L') + ' L102,60 Z'} fill="url(#g1)" />
          <path d={sparkPath(0)} stroke="var(--accent-bright)" strokeWidth="1.2" fill="none" />
          <path d={sparkPath(1)} stroke="var(--ink-soft)" strokeWidth="0.9" fill="none" strokeDasharray="2 2" opacity="0.7" />
        </svg>
        <div className="edge-card-foot">
          <span>0s</span>
          <span>{loginTarget === 'edge' ? 'Avg 12,480 msg/s' : 'Avg 78,420 msg/s'}</span>
          <span>now</span>
        </div>
      </div>

      <div className="edge-nodes">
        {nodes.map((n) =>
        <div key={n.id} className="node-row">
            <div className={`node-status node-${n.status}`}>
              <span className="dot" />
            </div>
            <div className="node-meta">
              <div className="node-id">{n.id}</div>
              <div className="node-site">{n.site}</div>
            </div>
            <div className="node-tags">
              <span>{n.tags.toLocaleString()}</span>
              <small>tags</small>
            </div>
          </div>
        )}
      </div>

      <footer className="edge-foot">
        <div className="edge-foot-item">
          <Icon.shield width="14" height="14" />
          <span>TLS 1.3 · mTLS to gateway</span>
        </div>
        <div className="edge-foot-item">
          <Icon.cpu width="14" height="14" />
          <span>{loginTarget === 'edge' ? 'Air-gapped capable' : 'ISA/IEC 62443 audited'}</span>
        </div>
      </footer>
    </aside>);

}

// ---- Login form ------------------------------------------------------
function LoginForm({ rememberMeDefault, loginTarget, onSubmit }) {
  const [user, setUser] = useState('');
  const [pwd, setPwd] = useState('');
  const [show, setShow] = useState(false);
  const [remember, setRemember] = useState(!!rememberMeDefault);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [touched, setTouched] = useState({ user: false, pwd: false });

  const userValid = user.trim().length >= 1;
  const pwdValid = pwd.length >= 1;
  const canSubmit = userValid && pwdValid && !busy;

  const submit = (e) => {
    e.preventDefault();
    setTouched({ user: true, pwd: true });
    if (!userValid || !pwdValid) return;
    setBusy(true);
    setError('');
    (async () => {
      try {
        const response = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: user.trim(), password: pwd }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload || !payload.token) {
          throw new Error((payload && payload.detail) || 'Invalid credentials. Verify with your plant administrator.');
        }
        try {
          localStorage.setItem('trustnode_auth_token', String(payload.token));
          localStorage.setItem('trustnode_current_user', String(user.trim()));
          localStorage.setItem('trustnode_remember_user', remember ? 'true' : 'false');
        } catch (_) {}
        onSubmit && onSubmit({ user: user.trim(), remember, token: payload.token, profile: payload.user || null });
        try {
          window.location.href = '/portal/';
        } catch (_) {}
      } catch (err) {
        setError(String((err && err.message) || 'Invalid credentials. Verify with your plant administrator.'));
      } finally {
        setBusy(false);
      }
    })();
  };

  const targetCopy = loginTarget === 'edge' ?
  { sub: 'Sign in to this gateway to configure acquisition pipelines, drivers and local buffers.', cta: 'Sign in to this edge node' } :
  { sub: 'Sign in to access your fleet dashboard, acquisition pipelines and plant-wide insights.', cta: 'Sign in to TrustNode portal' };

  return (
    <form className="form" onSubmit={submit} noValidate>
      <div className="form-headline">
        <h1 className="form-title">Welcome back</h1>
        <p className="form-sub">{targetCopy.sub}</p>
      </div>

      <label className="field">
        <span className="field-label">Username or email</span>
        <div className={`field-ctl ${touched.user && !userValid ? 'invalid' : ''}`}>
          <Icon.user width="18" height="18" />
          <input
            type="text"
            autoComplete="username"
            placeholder={loginTarget === 'edge' ? 'admin or operator' : 'e.g. m.silva@plant.io'}
            value={user}
            onChange={(e) => setUser(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, user: true }))} />
          
        </div>
        {touched.user && !userValid && <span className="field-err">Enter username.</span>}
      </label>

      <label className="field">
        <span className="field-label">
          Password
          <a className="field-link" href="#">Forgot password?</a>
        </span>
        <div className={`field-ctl ${touched.pwd && !pwdValid ? 'invalid' : ''}`}>
          <Icon.lock width="18" height="18" />
          <input
            type={show ? 'text' : 'password'}
            autoComplete="current-password"
            placeholder="Enter your password"
            value={pwd}
            onChange={(e) => setPwd(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, pwd: true }))} />
          
          <button type="button" className="ghost-btn" onClick={() => setShow((s) => !s)} aria-label={show ? 'Hide password' : 'Show password'}>
            {show ? <Icon.eyeOff width="18" height="18" /> : <Icon.eye width="18" height="18" />}
          </button>
        </div>
        {touched.pwd && !pwdValid && <span className="field-err">Enter password.</span>}
      </label>

      <div className="row-between">
        <label className="check">
          <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
          <span className="check-box"><Icon.check width="14" height="14" /></span>
          Remember this workstation
        </label>
      </div>

      {error &&
      <div className="alert">
          <span className="alert-dot" />
          {error}
        </div>
      }

      <button type="submit" className={`primary-btn ${busy ? 'busy' : ''}`} disabled={!canSubmit}>
        {busy ?
        <>
            <Icon.spinner className="spin" width="18" height="18" />
            Authenticating…
          </> :

        <>
            {targetCopy.cta}
            <Icon.arrowRight width="18" height="18" />
          </>
        }
      </button>

      <p className="legal">

      </p>
    </form>);

}

// ---- Activate form: code + admin account the customer is creating ----
function ActivateForm({ onSubmit }) {
  const [code, setCode] = useState('');
  const [adminUser, setAdminUser] = useState('');
  const [adminPwd, setAdminPwd] = useState('');
  const [confirmPwd, setConfirmPwd] = useState('');
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState('');

  const trimmedCode = code.trim();
  const codeOk = trimmedCode.length >= 6;
  const userOk = adminUser.trim().length >= 3;
  const pwdOk = adminPwd.length >= 8;
  const matchOk = adminPwd && adminPwd === confirmPwd;
  const canSubmit = codeOk && userOk && pwdOk && matchOk && !busy;

  // Live password strength (0–4)
  const strength = (() => {
    let s = 0;
    if (adminPwd.length >= 8) s++;
    if (/[A-Z]/.test(adminPwd) && /[a-z]/.test(adminPwd)) s++;
    if (/\d/.test(adminPwd)) s++;
    if (/[^A-Za-z0-9]/.test(adminPwd)) s++;
    return s;
  })();
  const strengthLabel = ['Too short', 'Weak', 'Fair', 'Strong', 'Excellent'][strength] || '';

  const submit = (e) => {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError('');
    setTimeout(() => {
      setBusy(false);
      if (trimmedCode.toLowerCase() === 'wrong') {
        setError('Activation code rejected. Verify the string and try again.');
      } else {
        setDone(true);
      }
    }, 1300);
  };

  if (done) {
    return (
      <div className="form">
        <div className="success">
          <div className="success-ring">
            <Icon.check width="36" height="36" />
          </div>
          <h2>Edge app activated</h2>
          <p>Sign in as <strong>{adminUser.trim()}</strong> with the password you just set to access the TrustNode Edge dashboard.</p>
          <button type="button" className="ghost-link" onClick={() => { setDone(false); setCode(''); setAdminUser(''); setAdminPwd(''); setConfirmPwd(''); }}>
            Activate another
          </button>
        </div>
      </div>);

  }

  return (
    <form className="form" onSubmit={submit} noValidate>
      <div className="form-headline">
        <h1 className="form-title">Activate TrustNode Edge</h1>
        <p className="form-sub">
          Paste your activation code, then choose the administrator account you'll use to sign in to this Edge app.
        </p>
      </div>

      <label className="field">
        <span className="field-label">Activation code</span>
        <div className="field-ctl">
          <Icon.key width="18" height="18" />
          <input
            type="text"
            autoComplete="off"
            spellCheck={false}
            placeholder="Paste your activation code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            aria-label="Activation code" />
        </div>
        <span className="hint">A single string provided with your Edge license.</span>
      </label>

      <div className="section-divider">
        <span>Create administrator account</span>
      </div>

      <label className="field">
        <span className="field-label">Admin login</span>
        <div className="field-ctl">
          <Icon.user width="18" height="18" />
          <input
            type="text"
            autoComplete="username"
            placeholder="e.g. plant-admin"
            value={adminUser}
            onChange={(e) => setAdminUser(e.target.value)} />
        </div>
        <span className="hint">This is the username you'll log in with after activation.</span>
      </label>

      <label className="field">
        <span className="field-label">Admin password</span>
        <div className="field-ctl">
          <Icon.lock width="18" height="18" />
          <input
            type={show ? 'text' : 'password'}
            autoComplete="new-password"
            placeholder="Set a password (8+ characters)"
            value={adminPwd}
            onChange={(e) => setAdminPwd(e.target.value)} />
          <button type="button" className="ghost-btn" onClick={() => setShow((s) => !s)} aria-label={show ? 'Hide password' : 'Show password'}>
            {show ? <Icon.eyeOff width="18" height="18" /> : <Icon.eye width="18" height="18" />}
          </button>
        </div>
        {adminPwd &&
        <div className={`pwd-meter s${strength}`} aria-hidden="true">
            <span /><span /><span /><span />
            <em>{strengthLabel}</em>
          </div>
        }
      </label>

      <label className="field">
        <span className="field-label">Confirm password</span>
        <div className="field-ctl">
          <Icon.lock width="18" height="18" />
          <input
            type={show ? 'text' : 'password'}
            autoComplete="new-password"
            placeholder="Re-enter the password"
            value={confirmPwd}
            onChange={(e) => setConfirmPwd(e.target.value)} />
        </div>
        {confirmPwd && !matchOk &&
        <span className="hint hint-warn">Passwords don't match yet.</span>
        }
      </label>

      {error &&
      <div className="alert">
          <span className="alert-dot" />
          {error}
        </div>
      }

      <button type="submit" className={`primary-btn ${busy ? 'busy' : ''}`} disabled={!canSubmit}>
        {busy ? <><Icon.spinner className="spin" width="18" height="18" /> Activating…</> :
        <>Activate Edge app<Icon.arrowRight width="18" height="18" /></>
        }
      </button>
    </form>);

}

// ---- Login target switch (Portal / Edge) ----------------------------
function TargetSwitch({ value, onChange }) {
  return (
    <div className="target-switch" role="tablist" aria-label="Login target">
      <button
        type="button"
        role="tab"
        aria-selected={value === 'portal'}
        className={value === 'portal' ? 'on' : ''}
        onClick={() => onChange('portal')}>
        
        <Icon.cloud width="14" height="14" />
        Portal login
        <small>Cloud · fleet-wide</small>
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={value === 'edge'}
        className={value === 'edge' ? 'on' : ''}
        onClick={() => onChange('edge')}>
        
        <Icon.hardware width="14" height="14" />
        Edge login
        <small>This gateway</small>
      </button>
    </div>);

}

// ---- Theme toggle ---------------------------------------------------
function ThemeToggle({ theme, onChange }) {
  return (
    <button className="theme-toggle" onClick={() => onChange(theme === 'dark' ? 'light' : 'dark')} aria-label="Toggle theme">
      <span className={`theme-icon ${theme === 'dark' ? 'on' : ''}`}><Icon.moon width="14" height="14" /></span>
      <span className={`theme-icon ${theme === 'light' ? 'on' : ''}`}><Icon.sun width="14" height="14" /></span>
    </button>);

}

// ---- App ------------------------------------------------------------
function App() {
  const [tweaks, setTweaks] = useTweaks(TWEAK_DEFAULTS);
  const [tab, setTab] = useState('login'); // login | activate
  const [loggedAs, setLoggedAs] = useState(null);

  const theme = tweaks.theme || 'dark';

  return (
    <div
      className={`shell theme-${theme}`}
      data-theme={theme}
      style={{
        '--card-alpha': tweaks.cardOpacity
      }}>
      
      <Backdrop blur={tweaks.backdropBlur} show={tweaks.showBackdrop} theme={theme} />

      <header className="topbar">
        <div className="topbar-brand">
          <img src={(window.__resources && window.__resources.logoHorizontal) || "assets/trustnode-horizontal.png"} alt="TrustNode" className="topbar-logo" />
        </div>
        <div className="topbar-right">
          {tweaks.showLanguage &&
          <button className="lang-btn">
              <Icon.globe width="14" height="14" />
              {tweaks.language}
              <Icon.chev width="12" height="12" />
            </button>
          }
          <ThemeToggle theme={theme} onChange={(v) => setTweaks('theme', v)} />
        </div>
      </header>

      <main className="main">
        <EdgePanel loginTarget="portal" />

        <section className="card-wrap">
          <div className="auth-card">
            <div className="auth-head">
              <img src={(window.__resources && window.__resources.logoFull) || "assets/trustnode-full-logo.png"} alt="TrustNode" className="auth-logo" />
            </div>

            <div className="tabs">
              <button className={tab === 'login' ? 'on' : ''} onClick={() => setTab('login')}>
                <Icon.lock width="14" height="14" />
                Login
              </button>
              <button className={tab === 'activate' ? 'on' : ''} onClick={() => setTab('activate')}>
                <Icon.key width="14" height="14" />
                Activate
              </button>
              <span className={`tabs-thumb ${tab}`} />
            </div>

            {tab === 'login' ?
            <LoginForm
              rememberMeDefault={tweaks.rememberMe}
              loginTarget="portal"
              onSubmit={(d) => setLoggedAs(d.user)} /> :


            <ActivateForm />
            }
          </div>

          <div className="auth-foot">
            <span>© 2026 TrustNode Industrial</span>
            <span className="dotsep" />
            <a href="#">Privacy</a>
            <a href="#">Terms</a>
            <a href="#">ISA/IEC 62443</a>
          </div>
        </section>
      </main>

      {loggedAs &&
      <div className="toast">
          <Icon.check width="16" height="16" />
          Welcome <strong>{loggedAs}</strong> · routing to portal…
          <button onClick={() => setLoggedAs(null)} aria-label="Dismiss">×</button>
        </div>
      }

      <TweaksPanel title="Tweaks">
        <TweakSection title="Theme">
          <TweakRadio
            label="Mode"
            value={tweaks.theme}
            onChange={(v) => setTweaks('theme', v)}
            options={[
            { value: 'dark', label: 'Dark' },
            { value: 'light', label: 'Light' }]
            } />
          
        </TweakSection>

        <TweakSection title="Backdrop">
          <TweakToggle label="Show factory image" value={tweaks.showBackdrop} onChange={(v) => setTweaks('showBackdrop', v)} />
          <TweakSlider label="Backdrop blur" min={0} max={30} step={1} value={tweaks.backdropBlur} onChange={(v) => setTweaks('backdropBlur', v)} />
        </TweakSection>

        <TweakSection title="Form">
          <TweakToggle label="Default 'remember me'" value={tweaks.rememberMe} onChange={(v) => setTweaks('rememberMe', v)} />
          <TweakToggle label="Show language switcher" value={tweaks.showLanguage} onChange={(v) => setTweaks('showLanguage', v)} />
          <TweakSelect
            label="Language"
            value={tweaks.language}
            onChange={(v) => setTweaks('language', v)}
            options={[
            { value: 'EN', label: 'English' },
            { value: 'PT', label: 'Português' },
            { value: 'ES', label: 'Español' },
            { value: 'DE', label: 'Deutsch' }]
            } />
          
        </TweakSection>
      </TweaksPanel>
    </div>);

}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);