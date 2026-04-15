<?php
/*
  TrustNode DB Direct Client (single-file PHP)
  - UI + DB query endpoints in one file
  - Configure DB via env vars (recommended):
      TRUSTNODE_DB_DSN=pgsql:host=...;port=5432;dbname=postgres;sslmode=require
      TRUSTNODE_DB_USER=postgres
      TRUSTNODE_DB_PASS=...
*/

declare(strict_types=1);
session_start();

function cfg(string $key, string $default = ''): string {
  $v = getenv($key);
  if ($v === false || $v === null || $v === '') return $default;
  return (string)$v;
}

function db(): PDO {
  static $pdo = null;
  if ($pdo instanceof PDO) return $pdo;
  $dsn = cfg('TRUSTNODE_DB_DSN');
  $user = cfg('TRUSTNODE_DB_USER');
  $pass = cfg('TRUSTNODE_DB_PASS');
  if ($dsn === '') {
    throw new RuntimeException('Missing TRUSTNODE_DB_DSN');
  }
  $pdo = new PDO($dsn, $user, $pass, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
  ]);
  return $pdo;
}

function outJson(int $code, array $payload): void {
  http_response_code($code);
  header('Content-Type: application/json; charset=utf-8');
  header('Cache-Control: no-store, no-cache, must-revalidate');
  echo json_encode($payload, JSON_UNESCAPED_SLASHES);
  exit;
}

function pickTsExpr(PDO $pdo): string {
  // Keep local-time semantics in UI by returning raw timestamptz and formatting client-side.
  return 'sample_ts_utc';
}

function tryQueries(PDO $pdo, array $queries, callable $mapper): array {
  $lastErr = null;
  foreach ($queries as $item) {
    try {
      $stmt = $pdo->prepare($item['sql']);
      foreach (($item['params'] ?? []) as $k => $v) {
        $stmt->bindValue($k, $v, is_int($v) ? PDO::PARAM_INT : PDO::PARAM_STR);
      }
      $stmt->execute();
      $rows = $stmt->fetchAll();
      return $mapper($rows);
    } catch (Throwable $e) {
      $lastErr = $e;
    }
  }
  if ($lastErr) throw $lastErr;
  return [];
}

if (isset($_GET['dbq'])) {
  try {
    $pdo = db();
    $dbq = (string)$_GET['dbq'];
    $limit = max(1, min(5000, (int)($_GET['limit'] ?? 500)));

    if ($dbq === 'health') {
      $ok = (bool)$pdo->query('select 1')->fetchColumn();
      outJson(200, ['ok' => $ok]);
    }

    if ($dbq === 'live') {
      $queries = [
        [
          'sql' => "
            SELECT
              l.sample_ts_utc AS ts,
              l.gateway_id,
              l.gateway_id AS gateway_name,
              l.machine_id AS device_name,
              l.machine_id,
              l.plant_id,
              l.customer_id,
              l.tenant_id,
              l.edge_monotonic_seq,
              l.quality_code AS quality,
              COALESCE(t.value->>'tag_name', '') AS tag,
              NULLIF(t.value->>'value','')::double precision AS value,
              COALESCE(NULLIF(t.value->>'quality_code','')::int, l.quality_code, 0) AS quality_tag
            FROM latest_machine_state l
            CROSS JOIN LATERAL jsonb_array_elements(l.tags_json) AS t(value)
            ORDER BY l.sample_ts_utc DESC
            LIMIT :lim
          ",
          'params' => [':lim' => $limit],
        ],
        [
          'sql' => "
            SELECT
              ts_utc AS ts,
              gateway_id,
              gateway_name,
              device_name,
              '' AS machine_id,
              '' AS plant_id,
              '' AS customer_id,
              COALESCE(tenant_id,'default') AS tenant_id,
              0 AS edge_monotonic_seq,
              quality,
              tag_name AS tag,
              value::double precision AS value,
              quality AS quality_tag
            FROM live_latest
            ORDER BY ts_utc DESC
            LIMIT :lim
          ",
          'params' => [':lim' => $limit],
        ],
      ];

      $rows = tryQueries($pdo, $queries, function(array $raw): array {
        return array_map(function($r) {
          $q = isset($r['quality_tag']) ? (int)$r['quality_tag'] : (int)($r['quality'] ?? 0);
          return [
            'ts' => $r['ts'] ?? null,
            'gateway_id' => $r['gateway_id'] ?? '',
            'gateway_name' => $r['gateway_name'] ?? ($r['gateway_id'] ?? ''),
            'device_name' => $r['device_name'] ?? '',
            'tag' => $r['tag'] ?? '',
            'value' => isset($r['value']) ? (float)$r['value'] : null,
            'quality' => $q,
            'quality_label' => $q >= 192 ? 'GOOD' : ($q >= 64 ? 'UNCERTAIN' : 'BAD'),
          ];
        }, $raw);
      });

      outJson(200, ['ok' => true, 'rows' => $rows]);
    }

    if ($dbq === 'historian') {
      $tagLike = trim((string)($_GET['tag'] ?? ''));
      $params = [':lim' => $limit];
      $tagFilter = '';
      if ($tagLike !== '') {
        $tagFilter = ' AND COALESCE(t.value->>\'tag_name\',\'\') ILIKE :tag '; 
        $params[':tag'] = '%'.$tagLike.'%';
      }

      $queries = [
        [
          'sql' => "
            SELECT
              s.sample_ts_utc AS ts,
              s.gateway_id,
              s.gateway_id AS gateway_name,
              s.machine_id AS device_name,
              COALESCE(t.value->>'tag_name','') AS tag,
              NULLIF(t.value->>'value','')::double precision AS value,
              COALESCE(NULLIF(t.value->>'quality_code','')::int, s.quality_code, 0) AS quality
            FROM telemetry_samples_raw s
            CROSS JOIN LATERAL jsonb_array_elements(s.tags_json) AS t(value)
            WHERE 1=1 {$tagFilter}
            ORDER BY s.sample_ts_utc DESC, s.edge_monotonic_seq DESC
            LIMIT :lim
          ",
          'params' => $params,
        ],
        [
          'sql' => "
            SELECT
              ts AS ts,
              gateway_id,
              gateway_name,
              device_name,
              tag,
              value::double precision AS value,
              quality
            FROM historian_readings
            WHERE (:tag IS NULL OR tag ILIKE :tag)
            ORDER BY ts DESC
            LIMIT :lim
          ",
          'params' => [
            ':lim' => $limit,
            ':tag' => $tagLike === '' ? null : '%'.$tagLike.'%'
          ],
        ],
      ];

      $rows = tryQueries($pdo, $queries, function(array $raw): array {
        return array_map(function($r) {
          $q = (int)($r['quality'] ?? 0);
          return [
            'ts' => $r['ts'] ?? null,
            'gateway_id' => $r['gateway_id'] ?? '',
            'gateway_name' => $r['gateway_name'] ?? ($r['gateway_id'] ?? ''),
            'device_name' => $r['device_name'] ?? '',
            'tag' => $r['tag'] ?? '',
            'value' => isset($r['value']) ? (float)$r['value'] : null,
            'quality' => $q,
            'quality_label' => $q >= 192 ? 'GOOD' : ($q >= 64 ? 'UNCERTAIN' : 'BAD'),
          ];
        }, $raw);
      });

      outJson(200, ['ok' => true, 'rows' => $rows]);
    }

    if ($dbq === 'logs') {
      $queries = [
        [
          'sql' => "
            SELECT
              at_utc AS ts,
              actor_type AS level,
              action AS category,
              CONCAT(outcome, ' | ', COALESCE(details::text,'')) AS message
            FROM ingest_audit_log
            ORDER BY at_utc DESC
            LIMIT :lim
          ",
          'params' => [':lim' => $limit],
        ],
        [
          'sql' => "
            SELECT
              ts AS ts,
              level,
              category,
              message
            FROM app_logs
            ORDER BY ts DESC
            LIMIT :lim
          ",
          'params' => [':lim' => $limit],
        ],
      ];

      $rows = tryQueries($pdo, $queries, fn(array $raw) => $raw);
      outJson(200, ['ok' => true, 'rows' => $rows]);
    }

    if ($dbq === 'power_latest' || $dbq === 'power_history') {
      $isHistory = $dbq === 'power_history';
      $lim = $isHistory ? max(1, min(2000, $limit)) : max(1, min(200, $limit));
      $sql = "
        SELECT
          sample_ts_utc AS ts,
          gateway_id,
          machine_id,
          tags_json
        FROM telemetry_samples_raw
        WHERE EXISTS (
          SELECT 1
          FROM jsonb_array_elements(tags_json) e
          WHERE (e->>'tag_name') ILIKE 'voltage%'
             OR (e->>'tag_name') ILIKE 'current%'
             OR (e->>'tag_name') ILIKE 'active_power%'
             OR (e->>'tag_name') ILIKE 'energy%'
             OR (e->>'tag_name') ILIKE 'power_factor%'
             OR (e->>'tag_name') ILIKE 'frequency%'
        )
        ORDER BY sample_ts_utc DESC
        LIMIT :lim
      ";
      $stmt = $pdo->prepare($sql);
      $stmt->bindValue(':lim', $lim, PDO::PARAM_INT);
      $stmt->execute();
      $rows = $stmt->fetchAll();

      if (!$isHistory && count($rows) > 1) {
        $rows = [ $rows[0] ];
      }

      $mapped = array_map(function($r){
        $tags = json_decode((string)($r['tags_json'] ?? '[]'), true);
        $values = [];
        if (is_array($tags)) {
          foreach ($tags as $t) {
            $k = (string)($t['tag_name'] ?? '');
            if ($k === '') continue;
            $values[$k] = isset($t['value']) ? (float)$t['value'] : null;
          }
        }
        return [
          'ts' => $r['ts'] ?? null,
          'gateway_id' => $r['gateway_id'] ?? '',
          'device_id' => $r['machine_id'] ?? '',
          'values' => $values,
        ];
      }, $rows);

      outJson(200, ['ok' => true, 'rows' => $mapped, 'row' => $mapped[0] ?? null]);
    }

    outJson(400, ['ok' => false, 'error' => 'Unknown dbq']);
  } catch (Throwable $e) {
    outJson(500, ['ok' => false, 'error' => $e->getMessage()]);
  }
}
?>
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>TrustNode DB Query Client (PHP)</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root { --bg:#1e1e1e; --card:#252526; --stroke:#3c3c3c; --text:#d4d4d4; --muted:#9da0a6; --brand:#007acc; --ok:#2ea043; --danger:#d1242f; --header:#000; --header-text:#d4d4d4; }
    [data-theme="light"] { --bg:#f3f3f3; --card:#eceff3; --stroke:#c5ccd6; --text:#1f2328; --muted:#5f6b7a; --brand:#007acc; --ok:#2ea043; --danger:#d1242f; --header:#2d2d30; --header-text:#d4d4d4; }
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font-family:Segoe UI,sans-serif}
    .top{background:var(--header);color:var(--header-text);padding:10px 14px;border-bottom:1px solid #222;display:flex;justify-content:space-between;align-items:center;gap:10px}
    .wrap{padding:12px;max-width:1500px;margin:0 auto}.card{background:var(--card);border:1px solid var(--stroke);border-radius:12px;padding:12px;margin-bottom:12px}
    .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.grow{flex:1 1 240px}
    input,select,button{background:transparent;color:var(--text);border:1px solid var(--stroke);border-radius:8px;padding:8px 10px}button{cursor:pointer}
    .btn{background:var(--brand);border-color:var(--brand);color:#fff}.btn-ok{background:var(--ok);border-color:var(--ok);color:#fff}
    .tabs{display:flex;gap:8px;flex-wrap:wrap}.tab{padding:8px 12px;border:1px solid var(--stroke);border-radius:999px;cursor:pointer}.tab.active{background:var(--brand);border-color:var(--brand);color:#fff}
    .grid-4{display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:10px}.kpi{border:1px solid var(--stroke);border-radius:10px;padding:10px}.kpi .label{color:var(--muted);font-size:12px}.kpi .value{font-size:22px;font-weight:700}
    .views>section{display:none}.views>section.active{display:block}.split{display:grid;grid-template-columns:2fr 1fr;gap:10px}
    .table{width:100%;border-collapse:collapse}.table th,.table td{border-bottom:1px solid var(--stroke);padding:6px;font-size:13px;text-align:left}
    .status{padding:4px 8px;border-radius:999px;font-size:12px;border:1px solid var(--stroke)}.ok{color:#b6f3bf;background:rgba(46,160,67,.2)}.bad{color:#ffd7d9;background:rgba(209,36,47,.2)}
    canvas{width:100%!important;height:280px!important}
    .muted{font-size:12px;color:var(--muted)}
    @media(max-width:900px){.split,.grid-4{grid-template-columns:1fr}}
  </style>
</head>
<body>
<div class="top">
  <div><strong>TrustNode Client Test (Direct DB Query via PHP)</strong></div>
  <div class="row"><span id="connState" class="status bad">Disconnected</span><button id="themeBtn">Toggle Theme</button></div>
</div>

<div class="wrap">
  <div class="card">
    <div class="row">
      <button id="connectBtn" class="btn">Connect</button>
      <label class="row">Live interval (ms): <input id="liveMs" value="2000" style="width:110px" /></label>
      <label class="row">Historian interval (ms): <input id="histMs" value="4500" style="width:110px" /></label>
    </div>
    <div class="muted">This page reads directly from cloud database tables via server-side SQL (PHP PDO). Best for controlled server environments.</div>
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
  const req=async(q)=>{const r=await fetch(`?dbq=${q}`,{cache:'no-store'});if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json();};

  const chartCfg={responsive:true,maintainAspectRatio:false,animation:false,parsing:false};
  const liveChart=new Chart(document.getElementById('liveChart'),{type:'line',data:{labels:[],datasets:[{label:'Live Avg',data:[],borderColor:'#16a34a',tension:.2}]},options:chartCfg});
  const repChart=new Chart(document.getElementById('repChart'),{type:'line',data:{labels:[],datasets:[{label:'Report',data:[],borderColor:'#007acc'}]},options:chartCfg});
  const powerChart=new Chart(document.getElementById('powerChart'),{type:'line',data:{labels:[],datasets:[{label:'kW',data:[],borderColor:'#22c55e'}]},options:chartCfg});
  const energyChart=new Chart(document.getElementById('energyChart'),{type:'bar',data:{labels:[],datasets:[{label:'kWh',data:[],backgroundColor:'#0ea5e9'}]},options:chartCfg});

  const normalize=(rows)=>{const m=new Map();(rows||[]).forEach(r=>{const k=`${r.ts}|${r.gateway_id||r.gateway_name}|${r.tag}`;if(!m.has(k))m.set(k,r);});return [...m.values()].sort((a,b)=>new Date(a.ts)-new Date(b.ts));};
  const getV=(obj,keys)=>{for(const k of keys){const v=Number(obj?.[k]);if(Number.isFinite(v))return v;}return NaN;};

  async function connect(){
    await req('health');
    document.getElementById('connState').className='status ok';
    document.getElementById('connState').textContent='Connected';
    startPolling();
    await Promise.all([loadLive(),loadHistorian(),loadPowerLatest(),loadPowerHistory(),loadReporting()]);
  }

  async function loadLive(){
    const d=await req('live&limit=800'); const rows=normalize(d.rows||[]);
    const byTs=new Map(); rows.forEach(r=>{const t=fmtTs(r.ts),v=Number(r.value); if(!Number.isFinite(v))return; const a=byTs.get(t)||[];a.push(v);byTs.set(t,a);});
    const labels=[...byTs.keys()].slice(-100),vals=labels.map(k=>{const a=byTs.get(k)||[];return a.length?a.reduce((x,y)=>x+y,0)/a.length:null;});
    liveChart.data.labels=labels; liveChart.data.datasets[0].data=vals; liveChart.update('none');
    const gateways=new Set(rows.map(r=>String(r.gateway_id||r.gateway_name||''))).size; const now=Date.now();
    const ages=rows.map(r=>{const t=Date.parse(r.ts);return Number.isFinite(t)?Math.max(0,(now-t)/1000):0;});
    document.getElementById('kpiLiveRows').textContent=String(rows.length); document.getElementById('kpiGateways').textContent=String(gateways);
    document.getElementById('kpiAge').textContent=ages.length?(ages.reduce((a,b)=>a+b,0)/ages.length).toFixed(2):'-';
    document.getElementById('kpiLast').textContent=rows.length?fmtTs(rows[rows.length-1].ts):'-';
  }

  async function loadHistorian(){
    const lim=document.getElementById('histLimit').value||'500'; const needle=(document.getElementById('histTag').value||'').trim();
    const d=await req(`historian&limit=${encodeURIComponent(lim)}&tag=${encodeURIComponent(needle)}`); const rows=normalize(d.rows||[]).reverse().slice(0,1000);
    state.histRows=rows; const tb=document.querySelector('#histTable tbody'); tb.innerHTML=''; rows.forEach(r=>{const tr=document.createElement('tr'); tr.innerHTML=`<td>${fmtTs(r.ts)}</td><td>${r.gateway_name||r.gateway_id||'-'}</td><td>${r.tag||'-'}</td><td>${fmtN(r.value)}</td><td>${r.quality_label||r.quality||'-'}</td>`; tb.appendChild(tr);});
  }

  function aggregate(rows,mode){ if(mode==='raw')return rows.map(r=>({k:fmtTs(r.ts),v:Number(r.value)})).filter(x=>Number.isFinite(x.v)); const b=new Map(); rows.forEach(r=>{const d=new Date(r.ts),v=Number(r.value); if(Number.isNaN(d.getTime())||!Number.isFinite(v))return; let key=''; if(mode==='minute')key=`${d.getFullYear()}-${d.getMonth()+1}-${d.getDate()} ${d.getHours()}:${d.getMinutes()}`; if(mode==='hour')key=`${d.getFullYear()}-${d.getMonth()+1}-${d.getDate()} ${d.getHours()}:00`; if(mode==='day')key=`${d.getFullYear()}-${d.getMonth()+1}-${d.getDate()}`; const a=b.get(key)||[]; a.push(v); b.set(key,a);}); return [...b.entries()].map(([k,a])=>({k,v:a.reduce((x,y)=>x+y,0)/a.length})); }

  async function loadReporting(){
    const needle=(document.getElementById('repTag').value||'').toLowerCase(),mode=document.getElementById('repAgg').value;
    const rows=(state.histRows||[]).filter(r=>!needle||String(r.tag||'').toLowerCase().includes(needle));
    const data=aggregate(rows,mode).slice(-220); repChart.data.labels=data.map(x=>x.k); repChart.data.datasets[0].data=data.map(x=>x.v); repChart.update('none');
    const tb=document.querySelector('#repTable tbody'); tb.innerHTML=''; data.slice(-120).forEach(x=>{const tr=document.createElement('tr'); tr.innerHTML=`<td>${x.k}</td><td>${fmtN(x.v)}</td>`; tb.appendChild(tr);});
  }

  async function loadPowerLatest(){
    const d=await req('power_latest&limit=1'); const row=d.row||{}; const v=row.values||{};
    const voltage=getV(v,['voltage_v','voltage_l1_v']), current=getV(v,['current_a','current_l1_a']), powerW=getV(v,['active_power_total_w','active_power_w']), energyWh=getV(v,['energy_total_wh','energy_wh']), pf=getV(v,['power_factor_total','power_factor']), freq=getV(v,['frequency_hz']);
    document.getElementById('pVoltage').textContent=fmtN(voltage); document.getElementById('pCurrent').textContent=fmtN(current);
    document.getElementById('pPower').textContent=Number.isFinite(powerW)?fmtN(powerW/1000):'-'; document.getElementById('pEnergy').textContent=Number.isFinite(energyWh)?fmtN(energyWh/1000):'-';
    document.getElementById('pPf').textContent=fmtN(pf); document.getElementById('pFreq').textContent=fmtN(freq);
  }

  async function loadPowerHistory(){
    const d=await req('power_history&limit=300'); const rows=normalize(d.rows||[]);
    const labels=rows.map(r=>fmtTs(r.ts)).slice(-120), p=rows.map(r=>{const v=getV(r.values||{},['active_power_total_w','active_power_w']); return Number.isFinite(v)?v/1000:null;}).slice(-120), e=rows.map(r=>{const v=getV(r.values||{},['energy_total_wh','energy_wh']); return Number.isFinite(v)?v/1000:null;}).slice(-120);
    powerChart.data.labels=labels; powerChart.data.datasets[0].data=p; powerChart.update('none');
    energyChart.data.labels=labels; energyChart.data.datasets[0].data=e; energyChart.update('none');
  }

  function startPolling(){
    state.timers.forEach(clearInterval); state.timers=[];
    const liveMs=Math.max(500,parseInt(document.getElementById('liveMs').value||'2000',10));
    const histMs=Math.max(1000,parseInt(document.getElementById('histMs').value||'4500',10));
    const visible=()=>document.visibilityState==='visible';
    state.timers.push(setInterval(()=>visible()&&loadLive(), liveMs));
    state.timers.push(setInterval(()=>visible()&&loadHistorian(), histMs));
    state.timers.push(setInterval(()=>visible()&&loadPowerLatest(), liveMs));
    state.timers.push(setInterval(()=>visible()&&loadPowerHistory(), histMs+500));
  }

  document.getElementById('connectBtn').onclick=()=>connect().catch(e=>{document.getElementById('connState').className='status bad';document.getElementById('connState').textContent='Error';alert(e.message);});
  document.getElementById('histLoad').onclick=()=>loadHistorian().catch(e=>alert(e.message));
  document.getElementById('repLoad').onclick=()=>loadReporting().catch(e=>alert(e.message));
  document.getElementById('tabs').addEventListener('click',(e)=>{const t=e.target.closest('.tab');if(!t)return; document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active')); t.classList.add('active'); document.querySelectorAll('.views>section').forEach(s=>s.classList.remove('active')); document.getElementById(`view-${t.dataset.view}`).classList.add('active');});
  document.getElementById('themeBtn').onclick=()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='light'?'dark':'light';};
})();
</script>
</body>
</html>
