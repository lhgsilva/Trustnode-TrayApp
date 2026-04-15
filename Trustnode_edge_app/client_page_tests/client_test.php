<?php
session_start();

// Configure this for your deployed TrustNode cloud API.
const TRUSTNODE_API_BASE = 'https://trustnode.lsapps.app';

if (isset($_GET['proxy'])) {
    $path = trim((string)$_GET['proxy']);
    $path = ltrim($path, '/');
    $url = rtrim(TRUSTNODE_API_BASE, '/') . '/' . $path;

    $method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
    $body = file_get_contents('php://input');

    $headers = [
        'Accept: application/json',
        'Cache-Control: no-store',
    ];

    if ($method !== 'GET') {
        $headers[] = 'Content-Type: application/json';
    }

    $token = $_SESSION['trustnode_token'] ?? '';
    if ($token !== '') {
        $headers[] = 'Authorization: Bearer ' . $token;
    }

    $ch = curl_init($url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
    curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
    curl_setopt($ch, CURLOPT_TIMEOUT, 20);
    curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 10);

    if ($method !== 'GET' && $body !== false && $body !== '') {
        curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
    }

    $response = curl_exec($ch);
    $httpCode = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err = curl_error($ch);
    curl_close($ch);

    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store, no-cache, must-revalidate');

    if ($err) {
        http_response_code(502);
        echo json_encode(['ok' => false, 'error' => 'Proxy error', 'detail' => $err]);
        exit;
    }

    if ($path === 'api/auth/login' && $httpCode >= 200 && $httpCode < 300) {
        $json = json_decode((string)$response, true);
        if (is_array($json) && !empty($json['token'])) {
            $_SESSION['trustnode_token'] = (string)$json['token'];
        }
    }

    http_response_code($httpCode > 0 ? $httpCode : 500);
    echo (string)$response;
    exit;
}
?>
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>TrustNode Client Test (PHP)</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root { --bg:#1e1e1e; --card:#252526; --stroke:#3c3c3c; --text:#d4d4d4; --muted:#9da0a6; --brand:#007acc; --ok:#2ea043; --danger:#d1242f; --header:#000; --header-text:#d4d4d4; }
    [data-theme="light"] { --bg:#f3f3f3; --card:#eceff3; --stroke:#c5ccd6; --text:#1f2328; --muted:#5f6b7a; --brand:#007acc; --ok:#2ea043; --danger:#d1242f; --header:#2d2d30; --header-text:#d4d4d4; }
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font-family:Segoe UI,sans-serif}
    .top{background:var(--header);color:var(--header-text);padding:10px 14px;border-bottom:1px solid #222;display:flex;justify-content:space-between;align-items:center;gap:10px}
    .wrap{padding:12px;max-width:1500px;margin:0 auto}.card{background:var(--card);border:1px solid var(--stroke);border-radius:12px;padding:12px;margin-bottom:12px}
    .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.grow{flex:1 1 220px}
    input,select,button{background:transparent;color:var(--text);border:1px solid var(--stroke);border-radius:8px;padding:8px 10px}button{cursor:pointer}
    .btn{background:var(--brand);border-color:var(--brand);color:#fff}.btn-ok{background:var(--ok);border-color:var(--ok);color:#fff}
    .tabs{display:flex;gap:8px;flex-wrap:wrap}.tab{padding:8px 12px;border:1px solid var(--stroke);border-radius:999px;cursor:pointer}.tab.active{background:var(--brand);border-color:var(--brand);color:#fff}
    .grid-4{display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:10px}.kpi{border:1px solid var(--stroke);border-radius:10px;padding:10px}.kpi .label{color:var(--muted);font-size:12px}.kpi .value{font-size:22px;font-weight:700}
    .views>section{display:none}.views>section.active{display:block}.split{display:grid;grid-template-columns:2fr 1fr;gap:10px}
    .table{width:100%;border-collapse:collapse}.table th,.table td{border-bottom:1px solid var(--stroke);padding:6px;font-size:13px;text-align:left}
    .status{padding:4px 8px;border-radius:999px;font-size:12px;border:1px solid var(--stroke)}.ok{color:#b6f3bf;background:rgba(46,160,67,.2)}.bad{color:#ffd7d9;background:rgba(209,36,47,.2)}
    canvas{width:100%!important;height:280px!important}
    @media(max-width:900px){.split,.grid-4{grid-template-columns:1fr}}
  </style>
</head>
<body>
<div class="top">
  <div><strong>TrustNode Client Test (PHP)</strong></div>
  <div class="row"><span id="connState" class="status bad">Disconnected</span><button id="themeBtn">Toggle Theme</button></div>
</div>

<div class="wrap">
  <div class="card">
    <div class="row">
      <input id="username" placeholder="username" value="admin" />
      <input id="password" type="password" placeholder="password" value="admin" />
      <button id="loginBtn" class="btn">Sign In (via PHP proxy)</button>
    </div>
    <div style="color:var(--muted);font-size:12px">This file proxies API calls using PHP session token (`?proxy=...`).</div>
  </div>

  <div class="card"><div class="tabs" id="tabs"><div class="tab active" data-view="dashboard">Dashboard</div><div class="tab" data-view="reporting">Reporting</div><div class="tab" data-view="historian">Historian</div><div class="tab" data-view="power">Power Overview</div></div></div>

  <div class="views">
    <section id="view-dashboard" class="active"><div class="card grid-4"><div class="kpi"><div class="label">Live Rows</div><div class="value" id="kpiLiveRows">-</div></div><div class="kpi"><div class="label">Gateways</div><div class="value" id="kpiGateways">-</div></div><div class="kpi"><div class="label">Avg Sample Age (s)</div><div class="value" id="kpiAge">-</div></div><div class="kpi"><div class="label">Last Update</div><div class="value" id="kpiLast">-</div></div></div><div class="card"><canvas id="liveChart"></canvas></div></section>
    <section id="view-reporting"><div class="card row"><select id="repAgg"><option value="raw">RAW</option><option value="minute">MINUTE</option><option value="hour">HOUR</option><option value="day">DAY</option></select><input id="repTag" placeholder="tag contains..." /><button id="repLoad" class="btn-ok">Load</button></div><div class="split"><div class="card"><canvas id="repChart"></canvas></div><div class="card" style="max-height:320px;overflow:auto"><table class="table" id="repTable"><thead><tr><th>Time</th><th>Value</th></tr></thead><tbody></tbody></table></div></div></section>
    <section id="view-historian"><div class="card row"><input id="histTag" placeholder="Filter by tag" /><select id="histLimit"><option>200</option><option>500</option><option>1000</option></select><button id="histLoad" class="btn-ok">Refresh</button></div><div class="card" style="max-height:420px;overflow:auto"><table class="table" id="histTable"><thead><tr><th>Timestamp</th><th>Gateway</th><th>Tag</th><th>Value</th><th>Quality</th></tr></thead><tbody></tbody></table></div></section>
    <section id="view-power"><div class="card grid-4"><div class="kpi"><div class="label">Voltage (V)</div><div class="value" id="pVoltage">-</div></div><div class="kpi"><div class="label">Current (A)</div><div class="value" id="pCurrent">-</div></div><div class="kpi"><div class="label">Active Power (kW)</div><div class="value" id="pPower">-</div></div><div class="kpi"><div class="label">Energy (kWh)</div><div class="value" id="pEnergy">-</div></div><div class="kpi"><div class="label">Power Factor</div><div class="value" id="pPf">-</div></div><div class="kpi"><div class="label">Frequency (Hz)</div><div class="value" id="pFreq">-</div></div></div><div class="split"><div class="card"><canvas id="powerChart"></canvas></div><div class="card"><canvas id="energyChart"></canvas></div></div></section>
  </div>
</div>

<script>
(() => {
  const state={timers:[],histRows:[]};
  const fmtN=(v)=>Number.isFinite(Number(v))?Number(v).toFixed(3):'-';
  const fmtTs=(s)=>{const d=new Date(s);if(Number.isNaN(d.getTime()))return String(s||'');const p=(x)=>String(x).padStart(2,'0');return `${p(d.getDate())}/${p(d.getMonth()+1)}/${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;};
  const req=async(path,opts={})=>{const r=await fetch(`?proxy=${encodeURIComponent(path.replace(/^\//,''))}`,Object.assign({cache:'no-store'},opts));if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json();};

  const chartCfg={responsive:true,maintainAspectRatio:false,animation:false,parsing:false};
  const liveChart=new Chart(document.getElementById('liveChart'),{type:'line',data:{labels:[],datasets:[{label:'Live Avg',data:[],borderColor:'#16a34a',tension:.2}]},options:chartCfg});
  const repChart=new Chart(document.getElementById('repChart'),{type:'line',data:{labels:[],datasets:[{label:'Report',data:[],borderColor:'#007acc'}]},options:chartCfg});
  const powerChart=new Chart(document.getElementById('powerChart'),{type:'line',data:{labels:[],datasets:[{label:'kW',data:[],borderColor:'#22c55e'}]},options:chartCfg});
  const energyChart=new Chart(document.getElementById('energyChart'),{type:'bar',data:{labels:[],datasets:[{label:'kWh',data:[],backgroundColor:'#0ea5e9'}]},options:chartCfg});

  const normalize=(rows)=>{const m=new Map();(rows||[]).forEach(r=>{const k=`${r.ts}|${r.gateway_id||r.gateway_name}|${r.tag}`;if(!m.has(k))m.set(k,r);});return [...m.values()].sort((a,b)=>new Date(a.ts)-new Date(b.ts));};
  const getV=(obj,keys)=>{for(const k of keys){const v=Number(obj?.[k]);if(Number.isFinite(v))return v;}return NaN;};

  async function login(){
    const u=document.getElementById('username').value,p=document.getElementById('password').value;
    await req('api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
    document.getElementById('connState').className='status ok'; document.getElementById('connState').textContent='Connected';
    startPolling(); await Promise.all([loadLive(),loadHistorian(),loadPowerLatest(),loadPowerHistory(),loadReporting()]);
  }

  async function loadLive(){
    const d=await req('api/app-store/live?limit=600'); const rows=normalize(d.rows||[]);
    const byTs=new Map(); rows.forEach(r=>{const t=fmtTs(r.ts),v=Number(r.value); if(!Number.isFinite(v))return; const a=byTs.get(t)||[];a.push(v);byTs.set(t,a);});
    const labels=[...byTs.keys()].slice(-80),vals=labels.map(k=>{const a=byTs.get(k)||[];return a.length?a.reduce((x,y)=>x+y,0)/a.length:null;});
    liveChart.data.labels=labels; liveChart.data.datasets[0].data=vals; liveChart.update('none');
    const gateways=new Set(rows.map(r=>String(r.gateway_id||r.gateway_name||''))).size; const now=Date.now();
    const ages=rows.map(r=>{const t=Date.parse(r.ts);return Number.isFinite(t)?Math.max(0,(now-t)/1000):0;});
    document.getElementById('kpiLiveRows').textContent=String(rows.length); document.getElementById('kpiGateways').textContent=String(gateways);
    document.getElementById('kpiAge').textContent=ages.length?(ages.reduce((a,b)=>a+b,0)/ages.length).toFixed(2):'-';
    document.getElementById('kpiLast').textContent=rows.length?fmtTs(rows[rows.length-1].ts):'-';
  }

  async function loadHistorian(){
    const lim=document.getElementById('histLimit').value||'500'; const needle=(document.getElementById('histTag').value||'').toLowerCase();
    const d=await req(`api/app-store/historian?limit=${encodeURIComponent(lim)}`); const rows=normalize(d.rows||[]).reverse().filter(r=>!needle||String(r.tag||'').toLowerCase().includes(needle)).slice(0,800);
    state.histRows=rows; const tb=document.querySelector('#histTable tbody'); tb.innerHTML=''; rows.forEach(r=>{const tr=document.createElement('tr'); tr.innerHTML=`<td>${fmtTs(r.ts)}</td><td>${r.gateway_name||r.gateway_id||'-'}</td><td>${r.tag||'-'}</td><td>${fmtN(r.value)}</td><td>${r.quality_label||r.quality||'-'}</td>`; tb.appendChild(tr);});
  }

  function aggregate(rows,mode){ if(mode==='raw')return rows.map(r=>({k:fmtTs(r.ts),v:Number(r.value)})).filter(x=>Number.isFinite(x.v)); const b=new Map(); rows.forEach(r=>{const d=new Date(r.ts),v=Number(r.value); if(Number.isNaN(d.getTime())||!Number.isFinite(v))return; let key=''; if(mode==='minute')key=`${d.getFullYear()}-${d.getMonth()+1}-${d.getDate()} ${d.getHours()}:${d.getMinutes()}`; if(mode==='hour')key=`${d.getFullYear()}-${d.getMonth()+1}-${d.getDate()} ${d.getHours()}:00`; if(mode==='day')key=`${d.getFullYear()}-${d.getMonth()+1}-${d.getDate()}`; const a=b.get(key)||[]; a.push(v); b.set(key,a);}); return [...b.entries()].map(([k,a])=>({k,v:a.reduce((x,y)=>x+y,0)/a.length})); }

  async function loadReporting(){
    const needle=(document.getElementById('repTag').value||'').toLowerCase(),mode=document.getElementById('repAgg').value;
    const rows=(state.histRows.length?state.histRows:normalize((await req('api/app-store/historian?limit=1500')).rows||[])).filter(r=>!needle||String(r.tag||'').toLowerCase().includes(needle));
    const data=aggregate(rows,mode).slice(-200); repChart.data.labels=data.map(x=>x.k); repChart.data.datasets[0].data=data.map(x=>x.v); repChart.update('none');
    const tb=document.querySelector('#repTable tbody'); tb.innerHTML=''; data.slice(-120).forEach(x=>{const tr=document.createElement('tr'); tr.innerHTML=`<td>${x.k}</td><td>${fmtN(x.v)}</td>`; tb.appendChild(tr);});
  }

  async function loadPowerLatest(){ const d=await req('api/power/latest'); const row=d?.row||d?.latest||{},v=row.values||row.values_scaled||row.values_raw||{}; const voltage=getV(v,['voltage_v','voltage_l1_v']), current=getV(v,['current_a','current_l1_a']), powerW=getV(v,['active_power_total_w','active_power_w']), energyWh=getV(v,['energy_total_wh','energy_wh']), pf=getV(v,['power_factor_total','power_factor']), freq=getV(v,['frequency_hz']); document.getElementById('pVoltage').textContent=fmtN(voltage); document.getElementById('pCurrent').textContent=fmtN(current); document.getElementById('pPower').textContent=Number.isFinite(powerW)?fmtN(powerW/1000):'-'; document.getElementById('pEnergy').textContent=Number.isFinite(energyWh)?fmtN(energyWh/1000):'-'; document.getElementById('pPf').textContent=fmtN(pf); document.getElementById('pFreq').textContent=fmtN(freq); }
  async function loadPowerHistory(){ const d=await req('api/power/history?limit=300'); const rows=normalize(d.rows||[]); const labels=rows.map(r=>fmtTs(r.ts)).slice(-120), p=rows.map(r=>{const v=getV(r.values||{},['active_power_total_w','active_power_w']); return Number.isFinite(v)?v/1000:null;}).slice(-120), e=rows.map(r=>{const v=getV(r.values||{},['energy_total_wh','energy_wh']); return Number.isFinite(v)?v/1000:null;}).slice(-120); powerChart.data.labels=labels; powerChart.data.datasets[0].data=p; powerChart.update('none'); energyChart.data.labels=labels; energyChart.data.datasets[0].data=e; energyChart.update('none'); }

  function startPolling(){ state.timers.forEach(clearInterval); state.timers=[]; const visible=()=>document.visibilityState==='visible'; state.timers.push(setInterval(()=>visible()&&loadLive(),2200)); state.timers.push(setInterval(()=>visible()&&loadHistorian(),4600)); state.timers.push(setInterval(()=>visible()&&loadPowerLatest(),2200)); state.timers.push(setInterval(()=>visible()&&loadPowerHistory(),5200)); }

  document.getElementById('loginBtn').onclick=()=>login().catch(e=>{document.getElementById('connState').className='status bad';document.getElementById('connState').textContent='Login failed';alert(e.message);});
  document.getElementById('histLoad').onclick=()=>loadHistorian().catch(e=>alert(e.message));
  document.getElementById('repLoad').onclick=()=>loadReporting().catch(e=>alert(e.message));
  document.getElementById('tabs').addEventListener('click',(e)=>{const t=e.target.closest('.tab');if(!t)return; document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active')); t.classList.add('active'); document.querySelectorAll('.views>section').forEach(s=>s.classList.remove('active')); document.getElementById(`view-${t.dataset.view}`).classList.add('active');});
  document.getElementById('themeBtn').onclick=()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='light'?'dark':'light';};
})();
</script>
</body>
</html>
