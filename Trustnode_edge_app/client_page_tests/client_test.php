<?php
session_start();

const TRUSTNODE_API_BASE_DEFAULT = 'https://trustnode.lsapps.app';

if (isset($_GET['proxy'])) {
  $base = trim((string)($_SESSION['trustnode_api_base'] ?? TRUSTNODE_API_BASE_DEFAULT));
  if (isset($_GET['base'])) {
    $candidate = trim((string)$_GET['base']);
    if ($candidate !== '') {
      $base = rtrim($candidate, '/');
      $_SESSION['trustnode_api_base'] = $base;
    }
  }

  $path = ltrim((string)$_GET['proxy'], '/');
  $url = rtrim($base, '/') . '/' . $path;
  $method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
  $body = file_get_contents('php://input');

  $headers = ['Accept: application/json', 'Cache-Control: no-store'];
  if ($method !== 'GET') $headers[] = 'Content-Type: application/json';

  $token = $_SESSION['trustnode_token'] ?? '';
  if ($token !== '') $headers[] = 'Authorization: Bearer ' . $token;

  $ch = curl_init($url);
  curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
  curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
  curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
  curl_setopt($ch, CURLOPT_TIMEOUT, 25);
  curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 10);
  if ($method !== 'GET' && $body !== false && $body !== '') curl_setopt($ch, CURLOPT_POSTFIELDS, $body);

  $response = curl_exec($ch);
  $http = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
  $err = curl_error($ch);
  curl_close($ch);

  header('Content-Type: application/json; charset=utf-8');
  header('Cache-Control: no-store, no-cache, must-revalidate');

  if ($err) {
    http_response_code(502);
    echo json_encode(['ok' => false, 'error' => 'proxy_error', 'detail' => $err]);
    exit;
  }

  if ($path === 'api/auth/login' && $http >= 200 && $http < 300) {
    $json = json_decode((string)$response, true);
    if (is_array($json) && !empty($json['token'])) $_SESSION['trustnode_token'] = (string)$json['token'];
  }

  http_response_code($http > 0 ? $http : 500);
  echo (string)$response;
  exit;
}
?>
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>TrustNode Hosted Client (Single File PHP)</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root { --bg:#1e1e1e; --header:#000; --header-text:#d4d4d4; --card:#252526; --stroke:#3c3c3c; --text:#d4d4d4; --muted:#9da0a6; --brand:#007acc; --ok:#2ea043; --danger:#d1242f; --sidebar:#252526; --sidebar-text:#d4d4d4; --nav-active:#094771; --row:#2a2d2e; }
    [data-theme="light"] { --bg:#f3f3f3; --header:#2d2d30; --header-text:#d4d4d4; --card:#eceff3; --stroke:#c5ccd6; --text:#1f2328; --muted:#5f6b7a; --brand:#007acc; --ok:#2ea043; --danger:#d1242f; --sidebar:#252526; --sidebar-text:#d4d4d4; --nav-active:#094771; --row:#e5eaf0; }
    *{box-sizing:border-box} html,body{height:100%} body{margin:0;background:var(--bg);color:var(--text);font-family:"Segoe UI","Trebuchet MS",sans-serif}
    .shell{display:grid;grid-template-rows:58px 1fr;height:100%}.header{background:var(--header);color:var(--header-text);border-bottom:1px solid #1f1f1f;display:flex;align-items:center;justify-content:space-between;padding:8px 12px;gap:10px}
    .brand{font-weight:700}.header .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
    .status{border:1px solid var(--stroke);border-radius:999px;padding:4px 10px;font-size:12px}.status.ok{color:#b6f3bf;background:rgba(46,160,67,.2)}.status.bad{color:#ffd7d9;background:rgba(209,36,47,.2)}
    .main{display:grid;grid-template-columns:230px 1fr;min-height:0}.sidebar{background:var(--sidebar);color:var(--sidebar-text);border-right:1px solid #333;padding:10px;overflow:auto}
    .nav-btn{width:100%;text-align:left;padding:10px;border:1px solid transparent;background:transparent;color:inherit;border-radius:8px;cursor:pointer;margin-bottom:4px}.nav-btn.active{background:var(--nav-active);color:#fff}
    .content{overflow:auto;padding:12px}.card{background:var(--card);border:1px solid var(--stroke);border-radius:12px;padding:12px;margin-bottom:12px}.card-title{margin:0 0 10px 0;font-size:18px}
    .form-grid{display:grid;gap:10px;grid-template-columns:repeat(6,minmax(120px,1fr))}.form-grid-4{display:grid;gap:10px;grid-template-columns:repeat(4,minmax(120px,1fr))}.form-grid-3{display:grid;gap:10px;grid-template-columns:repeat(3,minmax(120px,1fr))}
    .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.grow{flex:1 1 220px}
    label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)} input,select,button,textarea{background:transparent;color:var(--text);border:1px solid var(--stroke);border-radius:8px;padding:8px 10px} button{cursor:pointer}
    .btn{background:var(--brand);color:#fff;border-color:var(--brand)}.btn-ok{background:var(--ok);color:#fff;border-color:var(--ok)}.btn-danger{background:var(--danger);color:#fff;border-color:var(--danger)}
    .kpis{display:grid;gap:10px;grid-template-columns:repeat(6,minmax(120px,1fr))}.kpi{background:color-mix(in srgb,var(--card) 88%, #000 12%);border:1px solid var(--stroke);border-radius:10px;padding:10px}.kpi .t{font-size:12px;color:var(--muted)}.kpi .v{font-size:22px;font-weight:700;margin-top:4px}
    .split{display:grid;gap:10px;grid-template-columns:2fr 1fr}.split-eq{display:grid;gap:10px;grid-template-columns:1fr 1fr}.chart-wrap{height:320px}.table-wrap{max-height:420px;overflow:auto}
    table{width:100%;border-collapse:collapse} th,td{padding:7px 6px;border-bottom:1px solid var(--stroke);font-size:13px;text-align:left;white-space:nowrap} tbody tr:nth-child(odd){background:var(--row)}
    .auth{max-width:520px;margin:50px auto}.hidden{display:none !important}.muted{color:var(--muted);font-size:12px}
    @media (max-width:1200px){.kpis{grid-template-columns:repeat(3,minmax(120px,1fr))}.form-grid{grid-template-columns:repeat(3,minmax(120px,1fr))}}
    @media (max-width:900px){.main{grid-template-columns:1fr}.sidebar{display:flex;overflow:auto;gap:6px}.nav-btn{min-width:170px}.split,.split-eq{grid-template-columns:1fr}.form-grid,.form-grid-4,.form-grid-3,.kpis{grid-template-columns:1fr}}
  </style>
</head>
<body>
<div id="authRoot" class="auth card">
  <h2 class="card-title">Sign In</h2>
  <div class="form-grid-3">
    <label>Cloud URL<input id="authUrl" value="<?= htmlspecialchars((string)($_SESSION['trustnode_api_base'] ?? TRUSTNODE_API_BASE_DEFAULT), ENT_QUOTES) ?>" /></label>
    <label>Username<input id="authUser" value="admin" /></label>
    <label>Password<input id="authPass" value="admin" type="password" /></label>
  </div>
  <div class="row" style="margin-top:10px"><button id="authLogin" class="btn">Sign In</button><button id="authTheme" type="button">Toggle Theme</button><span id="authMsg" class="muted"></span></div>
</div>

<div id="appRoot" class="shell hidden">
  <div class="header"><div class="row"><div class="brand">Trustnode Edge - Hosted Single File (PHP)</div><span id="connBadge" class="status bad">DISCONNECTED</span></div><div class="row"><span id="lastUpdate" class="muted">-</span><button id="themeToggle">Theme</button><button id="logoutBtn" class="btn-danger">Logout</button></div></div>
  <div class="main">
    <aside class="sidebar"><button class="nav-btn active" data-page="dashboard">Dashboard</button><button class="nav-btn" data-page="reporting">Reporting</button><button class="nav-btn" data-page="historian">Historian</button><button class="nav-btn" data-page="power">Power Overview</button></aside>
    <section class="content">
      <div id="page-dashboard"><div class="card"><h3 class="card-title">Dashboard</h3><div class="form-grid-4"><label>Gateway<select id="dashGateway"></select></label><label>Device<select id="dashDevice"></select></label><label>Tag<select id="dashTag"></select></label><label>Window<select id="dashWindow"><option value="60">Last 60</option><option value="120">Last 120</option><option value="240">Last 240</option></select></label></div></div><div class="card kpis"><div class="kpi"><div class="t">Live Rows</div><div class="v" id="kLiveRows">-</div></div><div class="kpi"><div class="t">Gateways</div><div class="v" id="kGateways">-</div></div><div class="kpi"><div class="t">Devices</div><div class="v" id="kDevices">-</div></div><div class="kpi"><div class="t">Tags</div><div class="v" id="kTags">-</div></div><div class="kpi"><div class="t">Avg Sample Age (s)</div><div class="v" id="kAge">-</div></div><div class="kpi"><div class="t">Current Value</div><div class="v" id="kCurrent">-</div></div></div><div class="card"><div class="chart-wrap"><canvas id="dashChart"></canvas></div></div></div>
      <div id="page-reporting" class="hidden"><div class="card"><h3 class="card-title">Reporting</h3><div class="form-grid"><label>From<input id="repFrom" type="datetime-local" /></label><label>To<input id="repTo" type="datetime-local" /></label><label>Gateway<select id="repGateway"></select></label><label>Tag contains<input id="repTagLike" placeholder="power / temp / ..." /></label><label>Aggregation<select id="repAgg"><option value="raw">RAW</option><option value="minute">Minute</option><option value="hour">Hour</option><option value="day">Day</option></select></label><label>Method<select id="repMethod"><option value="avg">AVG</option><option value="sum">SUM</option><option value="min">MIN</option><option value="max">MAX</option></select></label></div><div class="row" style="margin-top:10px"><button id="repLoad" class="btn-ok">Load Data</button><button id="repCsv" class="btn">Export CSV</button></div></div><div class="split"><div class="card"><div class="chart-wrap"><canvas id="repChart"></canvas></div></div><div class="card table-wrap"><table><thead><tr><th>Timestamp</th><th>Value</th></tr></thead><tbody id="repTableBody"></tbody></table></div></div></div>
      <div id="page-historian" class="hidden"><div class="card"><h3 class="card-title">Historian</h3><div class="form-grid"><label>From<input id="hisFrom" type="datetime-local" /></label><label>To<input id="hisTo" type="datetime-local" /></label><label>Gateway<select id="hisGateway"></select></label><label>Device<input id="hisDevice" placeholder="device contains..." /></label><label>Tag<input id="hisTag" placeholder="tag contains..." /></label><label>Quality<select id="hisQuality"><option value="all">All</option><option value="GOOD">GOOD</option><option value="UNCERTAIN">UNCERTAIN</option><option value="BAD">BAD</option></select></label></div><div class="row" style="margin-top:10px"><button id="hisRefresh" class="btn-ok">Refresh</button><button id="hisCsv" class="btn">Export CSV</button><button id="hisJson" class="btn">Export JSON</button></div></div><div class="card table-wrap"><table><thead><tr><th>Timestamp</th><th>Tag</th><th>Value</th><th>Quality</th><th>Device</th><th>Gateway</th></tr></thead><tbody id="hisTableBody"></tbody></table></div></div>
      <div id="page-power" class="hidden"><div class="card"><h3 class="card-title">Power Management Overview</h3><div class="form-grid-4"><label>Device<select id="powDevice"></select></label><label>History Limit<select id="powLimit"><option value="120">120</option><option value="240">240</option><option value="480">480</option></select></label><label>Primary Metric<select id="powMetric"><option value="active_power_w">Active Power</option><option value="voltage_v">Voltage</option><option value="current_a">Current</option><option value="energy_wh">Energy</option></select></label><label>Chart Type<select id="powType"><option value="line">Line</option><option value="bar">Bar</option></select></label></div></div><div class="kpis card"><div class="kpi"><div class="t">Voltage (V)</div><div class="v" id="pV">-</div></div><div class="kpi"><div class="t">Current (A)</div><div class="v" id="pA">-</div></div><div class="kpi"><div class="t">Active Power (kW)</div><div class="v" id="pKW">-</div></div><div class="kpi"><div class="t">Energy (kWh)</div><div class="v" id="pKWH">-</div></div><div class="kpi"><div class="t">Power Factor</div><div class="v" id="pPF">-</div></div><div class="kpi"><div class="t">Frequency (Hz)</div><div class="v" id="pHZ">-</div></div></div><div class="split-eq"><div class="card"><div class="chart-wrap"><canvas id="powChart1"></canvas></div></div><div class="card"><div class="chart-wrap"><canvas id="powChart2"></canvas></div></div></div></div>
    </section>
  </div>
</div>

<script>
(() => {
  const S = { baseUrl:'', token:'', timers:[], liveRows:[], historianRows:[], logsRows:[], powerLatest:null, powerHistory:[], reportRows:[] };
  const E = (id) => document.getElementById(id);
  const fmtNum = (v,d=3) => Number.isFinite(Number(v)) ? Number(v).toFixed(d) : '-';
  const tsMs = (v) => { const m = Date.parse(String(v||'')); return Number.isFinite(m) ? m : NaN; };
  const fmtTs = (v) => { const d = new Date(tsMs(v)); if (Number.isNaN(d.getTime())) return String(v||''); const p=(x)=>String(x).padStart(2,'0'); return `${p(d.getDate())}/${p(d.getMonth()+1)}/${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; };
  const toCsv = (rows) => { if (!rows.length) return ''; const cols = Object.keys(rows[0]); const esc=(x)=>`"${String(x??'').replaceAll('"','""')}"`; return [cols.join(','), ...rows.map(r=>cols.map(c=>esc(r[c])).join(','))].join('\n'); };
  const download = (name, content, type='text/plain;charset=utf-8') => { const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([content],{type})); a.download=name; a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),1000); };

  async function req(path, opts={}) {
    const baseParam = encodeURIComponent(E('authUrl').value.trim());
    const headers = Object.assign({ 'Content-Type':'application/json', 'Cache-Control':'no-store' }, opts.headers || {});
    const r = await fetch(`?proxy=${encodeURIComponent(path.replace(/^\//,''))}&base=${baseParam}`, Object.assign({}, opts, { headers, cache:'no-store' }));
    if (!r.ok) throw new Error(`HTTP ${r.status} ${path}`);
    return r.json();
  }

  async function login() {
    S.baseUrl = E('authUrl').value.trim().replace(/\/+$/, '');
    const data = await req('/api/auth/login', { method:'POST', body: JSON.stringify({ username:E('authUser').value, password:E('authPass').value }) });
    S.token = data.token || 'proxy-session';
    E('authMsg').textContent = 'Connected'; E('connBadge').className='status ok'; E('connBadge').textContent='CONNECTED';
    E('authRoot').classList.add('hidden'); E('appRoot').classList.remove('hidden');
    startTimers(); await refreshAll();
  }

  function uniq(rows){ const m=new Map(); for(const r of rows||[]){ const k=`${r.ts}|${r.gateway_id||r.gateway_name||''}|${r.device_name||''}|${r.tag||''}`; if(!m.has(k)) m.set(k,r);} return [...m.values()].sort((a,b)=>tsMs(a.ts)-tsMs(b.ts)); }
  function gatewayList(){ const s=new Set(); [...S.liveRows,...S.historianRows].forEach(r=>s.add(String(r.gateway_id||r.gateway_name||''))); return ['all',...[...s].filter(Boolean).sort((a,b)=>a.localeCompare(b))]; }
  function deviceList(){ const s=new Set(); [...S.liveRows,...S.historianRows].forEach(r=>s.add(String(r.device_name||''))); return ['all',...[...s].filter(Boolean).sort((a,b)=>a.localeCompare(b))]; }
  function tagList(){ const s=new Set(); [...S.liveRows,...S.historianRows].forEach(r=>s.add(String(r.tag||''))); return ['all',...[...s].filter(Boolean).sort((a,b)=>a.localeCompare(b))]; }
  function fillSelect(id, values, keep=true){ const el=E(id); const prev=el.value; el.innerHTML=values.map(v=>`<option value="${v}">${v}</option>`).join(''); if(keep && values.includes(prev)) el.value=prev; }

  async function refreshLive(){ const [live, logs] = await Promise.all([req('/api/app-store/live?limit=3000'), req('/api/app-store/logs?limit=1000').catch(()=>({rows:[]}))]); S.liveRows=uniq(live.rows||[]); S.logsRows=logs.rows||[]; E('lastUpdate').textContent=`Last update ${fmtTs(new Date().toISOString())}`; fillFilters(); renderDashboard(); }
  async function refreshHistorian(){ const hist = await req('/api/app-store/historian?limit=4000'); S.historianRows=uniq(hist.rows||[]).reverse(); fillFilters(); renderHistorian(); if(!S.reportRows.length) renderReporting(); }
  async function refreshPower(){ const [latest, history] = await Promise.all([req('/api/power/latest').catch(()=>({row:null})), req(`/api/power/history?limit=${encodeURIComponent(E('powLimit')?.value||240)}`).catch(()=>({rows:[]}))]); S.powerLatest=latest.row||latest.latest||null; S.powerHistory=uniq((history.rows||[]).map(r=>{ const vals=r.values||r.values_scaled||r.values_raw||{}; return { ts:r.ts, device_id:r.device_id||r.device_name||'', voltage_v:Number(vals.voltage_v??vals.voltage_l1_v), current_a:Number(vals.current_a??vals.current_l1_a), active_power_w:Number(vals.active_power_total_w??vals.active_power_w), energy_wh:Number(vals.energy_total_wh??vals.energy_wh), power_factor:Number(vals.power_factor_total??vals.power_factor), frequency_hz:Number(vals.frequency_hz) }; })); renderPower(); }
  async function refreshAll(){ await Promise.all([refreshLive(), refreshHistorian(), refreshPower()]); }

  function fillFilters(){ const g=gatewayList(), d=deviceList(), t=tagList(); ['dashGateway','repGateway','hisGateway'].forEach(id=>fillSelect(id,g)); fillSelect('dashDevice',d); fillSelect('dashTag',t); fillSelect('powDevice',['all', ...new Set(S.powerHistory.map(r=>String(r.device_id||''))).values()].filter(Boolean)); }
  function filterRows(rows,{gateway='all',device='all',tag='all',from='',to='',quality='all'}){ const fromMs=from?Date.parse(from):NaN; const toMs=to?Date.parse(to):NaN; return (rows||[]).filter(r=>{ if(gateway!=='all'&&String(r.gateway_id||r.gateway_name||'')!==gateway) return false; if(device!=='all'&&String(r.device_name||'')!==device) return false; if(tag!=='all'&&String(r.tag||'')!==tag) return false; const t=tsMs(r.ts); if(Number.isFinite(fromMs)&&t<fromMs) return false; if(Number.isFinite(toMs)&&t>toMs) return false; if(quality!=='all'&&String(r.quality_label||'').toUpperCase()!==quality) return false; return true; }); }
  function agg(rows,interval='raw',method='avg'){ if(interval==='raw') return rows.map(r=>({ts:fmtTs(r.ts),value:Number(r.value)})).filter(x=>Number.isFinite(x.value)); const b=new Map(); rows.forEach(r=>{ const d=new Date(tsMs(r.ts)); const v=Number(r.value); if(Number.isNaN(d.getTime())||!Number.isFinite(v)) return; let k=''; if(interval==='minute') k=`${d.getFullYear()}-${d.getMonth()+1}-${d.getDate()} ${d.getHours()}:${d.getMinutes()}`; if(interval==='hour') k=`${d.getFullYear()}-${d.getMonth()+1}-${d.getDate()} ${d.getHours()}:00`; if(interval==='day') k=`${d.getFullYear()}-${d.getMonth()+1}-${d.getDate()}`; const a=b.get(k)||[]; a.push(v); b.set(k,a); }); return [...b.entries()].map(([k,a])=>{ let v=a[a.length-1]; if(method==='sum') v=a.reduce((x,y)=>x+y,0); else if(method==='min') v=Math.min(...a); else if(method==='max') v=Math.max(...a); else v=a.reduce((x,y)=>x+y,0)/a.length; return {ts:k,value:v}; }); }

  function renderDashboard(){ const rows=filterRows(S.liveRows,{gateway:E('dashGateway').value||'all',device:E('dashDevice').value||'all',tag:E('dashTag').value||'all'}); E('kLiveRows').textContent=String(rows.length); E('kGateways').textContent=String(new Set(rows.map(r=>String(r.gateway_id||r.gateway_name||''))).size); E('kDevices').textContent=String(new Set(rows.map(r=>String(r.device_name||''))).size); E('kTags').textContent=String(new Set(rows.map(r=>String(r.tag||''))).size); const age=rows.length?rows.map(r=>Math.max(0,(Date.now()-tsMs(r.ts))/1000)).reduce((a,b)=>a+b,0)/rows.length:NaN; E('kAge').textContent=Number.isFinite(age)?age.toFixed(2):'-'; E('kCurrent').textContent=rows.length?fmtNum(rows[rows.length-1].value):'-'; const win=Number(E('dashWindow').value||120); const byTs=new Map(); rows.forEach(r=>{const k=fmtTs(r.ts),v=Number(r.value); if(!Number.isFinite(v)) return; const a=byTs.get(k)||[]; a.push(v); byTs.set(k,a);}); const labels=[...byTs.keys()].slice(-win); const vals=labels.map(k=>{const a=byTs.get(k)||[]; return a.length?a.reduce((x,y)=>x+y,0)/a.length:null;}); charts.dash.data.labels=labels; charts.dash.data.datasets[0].data=vals; charts.dash.update('none'); }
  function renderReporting(){ const rows=filterRows(S.historianRows,{gateway:E('repGateway').value||'all',from:E('repFrom').value||'',to:E('repTo').value||''}).filter(r=>{const n=(E('repTagLike').value||'').trim().toLowerCase(); return !n || String(r.tag||'').toLowerCase().includes(n);}); const series=agg(rows,E('repAgg').value||'raw',E('repMethod').value||'avg').slice(-300); S.reportRows=series; charts.rep.data.labels=series.map(x=>x.ts); charts.rep.data.datasets[0].data=series.map(x=>x.value); charts.rep.update('none'); E('repTableBody').innerHTML=series.slice(-150).map(r=>`<tr><td>${r.ts}</td><td>${fmtNum(r.value)}</td></tr>`).join(''); }
  function renderHistorian(){ const rows=filterRows(S.historianRows,{gateway:E('hisGateway').value||'all',from:E('hisFrom').value||'',to:E('hisTo').value||'',quality:E('hisQuality').value||'all'}).filter(r=>{const d=(E('hisDevice').value||'').trim().toLowerCase(); const t=(E('hisTag').value||'').trim().toLowerCase(); if(d&&!String(r.device_name||'').toLowerCase().includes(d)) return false; if(t&&!String(r.tag||'').toLowerCase().includes(t)) return false; return true;}); E('hisTableBody').innerHTML=rows.slice(0,1800).map(r=>`<tr><td>${fmtTs(r.ts)}</td><td>${r.tag||'-'}</td><td>${fmtNum(r.value)}</td><td>${r.quality_label||r.quality||'-'}</td><td>${r.device_name||'-'}</td><td>${r.gateway_name||r.gateway_id||'-'}</td></tr>`).join(''); }
  function renderPower(){ const device=E('powDevice').value||'all'; const data=S.powerHistory.filter(r=>device==='all'||String(r.device_id||'')===device); const metric=E('powMetric').value||'active_power_w'; const type=E('powType').value||'line'; const last=data[data.length-1]||(S.powerLatest&&S.powerLatest.values?{ voltage_v:Number(S.powerLatest.values.voltage_v??S.powerLatest.values.voltage_l1_v), current_a:Number(S.powerLatest.values.current_a??S.powerLatest.values.current_l1_a), active_power_w:Number(S.powerLatest.values.active_power_total_w??S.powerLatest.values.active_power_w), energy_wh:Number(S.powerLatest.values.energy_total_wh??S.powerLatest.values.energy_wh), power_factor:Number(S.powerLatest.values.power_factor_total??S.powerLatest.values.power_factor), frequency_hz:Number(S.powerLatest.values.frequency_hz)}:{}); E('pV').textContent=fmtNum(last.voltage_v,3); E('pA').textContent=fmtNum(last.current_a,3); E('pKW').textContent=Number.isFinite(last.active_power_w)?fmtNum(last.active_power_w/1000,3):'-'; E('pKWH').textContent=Number.isFinite(last.energy_wh)?fmtNum(last.energy_wh/1000,3):'-'; E('pPF').textContent=fmtNum(last.power_factor,3); E('pHZ').textContent=fmtNum(last.frequency_hz,3); const lim=Number(E('powLimit').value||240); const labels=data.slice(-lim).map(r=>fmtTs(r.ts)); const vals=data.slice(-lim).map(r=>Number(r[metric])); const vals2=data.slice(-lim).map(r=>Number(r.energy_wh)); charts.pow1.config.type=type; charts.pow1.data.labels=labels; charts.pow1.data.datasets[0].label=metric; charts.pow1.data.datasets[0].data=vals; charts.pow1.update('none'); charts.pow2.data.labels=labels; charts.pow2.data.datasets[0].data=vals2.map(v=>Number.isFinite(v)?v/1000:null); charts.pow2.update('none'); }

  function startTimers(){ S.timers.forEach(clearInterval); S.timers=[]; const vis=()=>document.visibilityState==='visible'; S.timers.push(setInterval(()=>vis()&&refreshLive().catch(()=>{}),2000)); S.timers.push(setInterval(()=>vis()&&refreshHistorian().catch(()=>{}),4500)); S.timers.push(setInterval(()=>vis()&&refreshPower().catch(()=>{}),2500)); }
  function setPage(page){ ['dashboard','reporting','historian','power'].forEach(p=>E(`page-${p}`).classList.toggle('hidden',p!==page)); document.querySelectorAll('.nav-btn').forEach(b=>b.classList.toggle('active',b.dataset.page===page)); }

  const charts = {
    dash:new Chart(E('dashChart'),{type:'line',data:{labels:[],datasets:[{label:'Live Trend',data:[],borderColor:'#16a34a',tension:.25,pointRadius:0}]},options:{responsive:true,maintainAspectRatio:false,animation:false,parsing:false}}),
    rep:new Chart(E('repChart'),{type:'line',data:{labels:[],datasets:[{label:'Report Series',data:[],borderColor:'#0ea5e9',tension:.2,pointRadius:0}]},options:{responsive:true,maintainAspectRatio:false,animation:false,parsing:false}}),
    pow1:new Chart(E('powChart1'),{type:'line',data:{labels:[],datasets:[{label:'Power',data:[],borderColor:'#22c55e',backgroundColor:'rgba(34,197,94,.35)'}]},options:{responsive:true,maintainAspectRatio:false,animation:false,parsing:false}}),
    pow2:new Chart(E('powChart2'),{type:'bar',data:{labels:[],datasets:[{label:'Energy (kWh)',data:[],backgroundColor:'#0ea5e9'}]},options:{responsive:true,maintainAspectRatio:false,animation:false,parsing:false}})
  };

  E('authLogin').onclick = () => login().catch(err => E('authMsg').textContent = err.message);
  E('authTheme').onclick = E('themeToggle').onclick = () => { document.documentElement.dataset.theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light'; };
  E('logoutBtn').onclick = () => location.reload();
  document.querySelectorAll('.nav-btn').forEach(btn => btn.onclick = () => setPage(btn.dataset.page));
  ['dashGateway','dashDevice','dashTag','dashWindow'].forEach(id => E(id).onchange = renderDashboard);
  ['repGateway','repFrom','repTo','repTagLike','repAgg','repMethod'].forEach(id => E(id).onchange = renderReporting);
  ['hisGateway','hisFrom','hisTo','hisDevice','hisTag','hisQuality'].forEach(id => E(id).onchange = renderHistorian);
  ['powDevice','powLimit','powMetric','powType'].forEach(id => E(id).onchange = () => { if (id==='powLimit') refreshPower().catch(()=>{}); else renderPower(); });
  E('repLoad').onclick = renderReporting;
  E('hisRefresh').onclick = () => refreshHistorian().catch(()=>{});
  E('repCsv').onclick = () => download(`report_${Date.now()}.csv`, toCsv(S.reportRows), 'text/csv;charset=utf-8');
  E('hisCsv').onclick = () => { const rows=[...E('hisTableBody').querySelectorAll('tr')].map(tr=>{ const t=tr.querySelectorAll('td'); return {timestamp:t[0]?.textContent||'',tag:t[1]?.textContent||'',value:t[2]?.textContent||'',quality:t[3]?.textContent||'',device:t[4]?.textContent||'',gateway:t[5]?.textContent||''}; }); download(`historian_${Date.now()}.csv`, toCsv(rows), 'text/csv;charset=utf-8'); };
  E('hisJson').onclick = () => download(`historian_${Date.now()}.json`, JSON.stringify(S.historianRows, null, 2), 'application/json;charset=utf-8');
})();
</script>
</body>
</html>
