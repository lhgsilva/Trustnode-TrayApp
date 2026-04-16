$ErrorActionPreference = 'Stop'

$root = 'Trustnode_edge_app'
$bundleDir = Join-Path $root 'web_cloud_readonly'
$outDir = Join-Path $root 'client_page_tests'

$cssPath = Get-ChildItem -Path (Join-Path $bundleDir 'assets') -Filter 'index-*.css' | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
$jsPath = Get-ChildItem -Path (Join-Path $bundleDir 'assets') -Filter 'index-*.js' | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $cssPath -or -not $jsPath) {
  throw 'Could not find index-*.css or index-*.js in web_cloud_readonly/assets'
}

$css = Get-Content -Raw $cssPath
$js = Get-Content -Raw $jsPath
$logoBytes = [IO.File]::ReadAllBytes((Join-Path $bundleDir 'trustnode_logo.png'))
$logoData = 'data:image/png;base64,' + [Convert]::ToBase64String($logoBytes)
$js = $js.Replace('trustnode_logo.png', $logoData)
$js = $js.Replace('</script', '<\/script')

function Build-Html([string]$title, [string]$preScript, [string]$postScript) {
@"
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>$title</title>
    <style>
$css
    </style>
$preScript
  </head>
  <body>
    <div id="root"></div>
$postScript
    <script type="module">
$js
    </script>
  </body>
</html>
"@
}

$apiDirectShim = @'
    <script>
      (function(){
        const q = new URLSearchParams(window.location.search);
        const fromQuery = (q.get('api_base') || '').trim();
        if (fromQuery) {
          try { localStorage.setItem('tn_api_base', fromQuery); } catch (_) {}
        }
        const fromStorage = (function(){ try { return (localStorage.getItem('tn_api_base') || '').trim(); } catch (_) { return ''; }})();
        const apiBase = fromQuery || fromStorage || 'https://trustnode.lsapps.app';
        window.__TN_API_BASE = apiBase;
        const LIMIT_CAPS = {
          '/api/app-store/live': 240,
          '/api/app-store/historian': 800,
          '/api/app-store/logs': 1000,
          '/api/v1/history': 800,
          '/api/v1/latest': 400,
          '/api/power/latest': 240,
          '/api/power/status': 200,
          '/api/plc/gateways/status': 120
        };
        const GET_CACHE_MS = {
          '/api/app-store/live': 250,
          '/api/power/latest': 250,
          '/api/power/status': 400,
          '/api/plc/gateways/status': 500,
          '/api/v1/latest': 500,
          '/api/app-store/historian': 1200,
          '/api/app-store/logs': 1200,
          '/api/v1/history': 1200
        };
        const inflight = new Map();
        const responseCache = new Map();

        function mapApiUrl(u){
          const out = new URL(u.pathname + u.search, window.__TN_API_BASE);
          const cap = LIMIT_CAPS[out.pathname];
          if (cap && out.searchParams.has('limit')) {
            const raw = Number(out.searchParams.get('limit') || cap);
            out.searchParams.set('limit', String(Math.max(1, Math.min(cap, Number.isFinite(raw) ? raw : cap))));
          }
          return out.toString();
        }

        async function fetchWithCache(mapped, init){
          const method = String((init && init.method) || 'GET').toUpperCase();
          if (method !== 'GET') return origFetch(mapped, init);
          const path = (() => { try { return new URL(mapped).pathname; } catch (_) { return ''; } })();
          const ttl = Number(GET_CACHE_MS[path] || 0);
          const now = Date.now();
          if (ttl > 0) {
            const cached = responseCache.get(mapped);
            if (cached && now - cached.ts <= ttl) {
              return cached.response.clone();
            }
          }
          if (inflight.has(mapped)) {
            return inflight.get(mapped).then((r) => r.clone());
          }
          const req = origFetch(mapped, init).then((res) => {
            if (ttl > 0 && res.ok) {
              responseCache.set(mapped, { ts: Date.now(), response: res.clone() });
            }
            return res;
          }).finally(() => inflight.delete(mapped));
          inflight.set(mapped, req);
          return req.then((r) => r.clone());
        }

        const origFetch = window.fetch.bind(window);
        window.fetch = function(input, init){
          try {
            const raw = (typeof input === 'string') ? input : (input && input.url ? input.url : '');
            const u = new URL(raw, window.location.origin);
            if (u.pathname.startsWith('/api/')) {
              const mapped = mapApiUrl(u);
              return fetchWithCache(mapped, init);
            }
          } catch (_) {}
          return origFetch(input, init);
        };

        const NativeWS = window.WebSocket;
        function mapWs(url){
          try {
            const u = new URL(url, window.location.href);
            if (u.pathname.startsWith('/ws/')) {
              const b = new URL(window.__TN_API_BASE);
              const wsProto = (b.protocol === 'https:') ? 'wss:' : 'ws:';
              return `${wsProto}//${b.host}${u.pathname}${u.search}`;
            }
          } catch (_) {}
          return url;
        }
        window.WebSocket = function(url, protocols){
          const mapped = mapWs(url);
          return protocols ? new NativeWS(mapped, protocols) : new NativeWS(mapped);
        };
        window.WebSocket.prototype = NativeWS.prototype;
      })();
    </script>
'@

$direct = Build-Html 'Trustnode Edge - Single File Client (Cloud API HTML)' '' $apiDirectShim
Set-Content -NoNewline -Encoding UTF8 (Join-Path $outDir 'client_test.html') $direct

$dbRestShim = @'
    <script>
      (function(){
        const q = new URLSearchParams(window.location.search);
        const fromQueryApi = (q.get('api_base') || '').trim();
        const fromQueryDbUrl = (q.get('db_url') || '').trim();
        const fromQueryDbKey = (q.get('db_key') || '').trim();

        if (fromQueryApi) { try { localStorage.setItem('tn_api_base', fromQueryApi); } catch (_) {} }
        if (fromQueryDbUrl) { try { localStorage.setItem('tn_db_url', fromQueryDbUrl); } catch (_) {} }
        if (fromQueryDbKey) { try { localStorage.setItem('tn_db_key', fromQueryDbKey); } catch (_) {} }

        const fromStorageApi = (function(){ try { return (localStorage.getItem('tn_api_base') || '').trim(); } catch (_) { return ''; }})();
        const fromStorageDbUrl = (function(){ try { return (localStorage.getItem('tn_db_url') || '').trim(); } catch (_) { return ''; }})();
        const fromStorageDbKey = (function(){ try { return (localStorage.getItem('tn_db_key') || '').trim(); } catch (_) { return ''; }})();

        const API_BASE = fromQueryApi || fromStorageApi || 'https://trustnode.lsapps.app';
        const DB_URL = fromQueryDbUrl || fromStorageDbUrl || 'https://tsfreqjcrgbxdwvmxeuk.supabase.co';
        const DB_KEY = fromQueryDbKey || fromStorageDbKey || '';

        window.__TN_API_BASE = API_BASE;
        window.__TN_DB_URL = DB_URL;
        window.__TN_DB_KEY = DB_KEY;
        const LIMIT_CAPS = {
          '/api/app-store/live': 240,
          '/api/app-store/historian': 800,
          '/api/app-store/logs': 1000,
          '/api/v1/history': 800,
          '/api/v1/latest': 400,
          '/api/power/latest': 240,
          '/api/power/status': 200,
          '/api/plc/gateways/status': 120
        };
        const GET_CACHE_MS = {
          '/api/app-store/live': 250,
          '/api/power/latest': 250,
          '/api/power/status': 400,
          '/api/plc/gateways/status': 500,
          '/api/v1/latest': 500,
          '/api/app-store/historian': 1200,
          '/api/app-store/logs': 1200,
          '/api/v1/history': 1200
        };
        const inflight = new Map();
        const responseCache = new Map();

        function jsonResponse(payload, status){
          return new Response(JSON.stringify(payload), {
            status: status || 200,
            headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' },
          });
        }

        async function queryDb(path){
          if (!window.__TN_DB_URL || !window.__TN_DB_KEY) return null;
          const url = `${window.__TN_DB_URL}/rest/v1/${path}`;
          const res = await fetch(url, {
            method: 'GET',
            headers: {
              apikey: window.__TN_DB_KEY,
              Authorization: `Bearer ${window.__TN_DB_KEY}`,
              Accept: 'application/json',
            },
            cache: 'no-store',
          });
          if (!res.ok) {
            const txt = await res.text().catch(() => '');
            throw new Error(`DB query failed ${res.status}: ${txt}`);
          }
          return res.json();
        }

        async function queryWithFallback(paths){
          let err = null;
          for (const p of paths) {
            try {
              const data = await queryDb(p);
              if (Array.isArray(data)) return data;
            } catch (e) {
              err = e;
            }
          }
          if (err) throw err;
          return [];
        }

        function qualityLabel(q){
          const v = Number(q || 0);
          if (v >= 192) return 'GOOD';
          if (v >= 64) return 'UNCERTAIN';
          return 'BAD';
        }

        async function mapDbData(pathname, search){
          const sp = new URLSearchParams(search || '');
          const limitCap = Number(LIMIT_CAPS[pathname] || 1000);
          const limit = Math.max(1, Math.min(limitCap, Number(sp.get('limit') || '500')));
          if (pathname === '/api/app-store/live') {
            const rows = await queryWithFallback([
              `live_latest?select=ts_utc,source,gateway_id,gateway_name,device_name,plc_ip,database_name,tag_name,value,quality,quality_label&order=ts_utc.desc&limit=${limit}`,
              `live_latest?select=ts_utc,source,gateway_id,gateway_name,device_name,plc_ip,database_name,tag,value,quality,quality_label&order=ts_utc.desc&limit=${limit}`
            ]);
            return {
              ok: true,
              rows: rows.map(r => {
                const q = Number(r.quality ?? 0);
                return {
                  ts: r.ts_utc ?? r.ts ?? null,
                  source: r.source ?? '',
                  gateway_id: r.gateway_id ?? '',
                  gateway_name: r.gateway_name ?? r.gateway_id ?? '',
                  device_name: r.device_name ?? '',
                  plc_ip: r.plc_ip ?? '',
                  database_name: r.database_name ?? '',
                  tag: r.tag_name ?? r.tag ?? '',
                  value: (r.value === null || r.value === undefined) ? null : Number(r.value),
                  quality: q,
                  quality_label: r.quality_label ?? qualityLabel(q),
                };
              }),
            };
          }
          if (pathname === '/api/app-store/historian') {
            const tagFilter = (sp.get('tag') || '').trim();
            const like = tagFilter ? `&tag=ilike.*${encodeURIComponent(tagFilter)}*` : '';
            const rows = await queryWithFallback([
              `historian_readings?select=ts,source,gateway_id,gateway_name,device_name,plc_ip,database_name,tag,value,quality,quality_label&order=ts.desc&limit=${limit}${like}`,
              `historian_readings?select=ts,source,gateway_id,gateway_name,device_name,plc_ip,database_name,tag_name,value,quality,quality_label&order=ts.desc&limit=${limit}`
            ]);
            return {
              ok: true,
              rows: rows.map(r => {
                const q = Number(r.quality ?? 0);
                return {
                  ts: r.ts ?? r.ts_utc ?? null,
                  source: r.source ?? '',
                  gateway_id: r.gateway_id ?? '',
                  gateway_name: r.gateway_name ?? r.gateway_id ?? '',
                  device_name: r.device_name ?? '',
                  plc_ip: r.plc_ip ?? '',
                  database_name: r.database_name ?? '',
                  tag: r.tag ?? r.tag_name ?? '',
                  value: (r.value === null || r.value === undefined) ? null : Number(r.value),
                  quality: q,
                  quality_label: r.quality_label ?? qualityLabel(q),
                };
              }),
            };
          }
          if (pathname === '/api/app-store/logs') {
            const rows = await queryWithFallback([
              `app_logs?select=ts,level,category,message,gateway,device,database_name&order=ts.desc&limit=${Math.max(1, Math.min(2500, limit))}`
            ]);
            return { ok: true, rows };
          }
          return null;
        }

        function mapApiUrl(u){
          const out = new URL(u.pathname + u.search, window.__TN_API_BASE);
          const cap = LIMIT_CAPS[out.pathname];
          if (cap && out.searchParams.has('limit')) {
            const raw = Number(out.searchParams.get('limit') || cap);
            out.searchParams.set('limit', String(Math.max(1, Math.min(cap, Number.isFinite(raw) ? raw : cap))));
          }
          return out.toString();
        }

        async function fetchWithCache(mapped, init){
          const method = String((init && init.method) || 'GET').toUpperCase();
          if (method !== 'GET') return origFetch(mapped, init);
          const path = (() => { try { return new URL(mapped, window.location.origin).pathname; } catch (_) { return ''; } })();
          const ttl = Number(GET_CACHE_MS[path] || 0);
          const now = Date.now();
          if (ttl > 0) {
            const cached = responseCache.get(mapped);
            if (cached && now - cached.ts <= ttl) {
              return cached.response.clone();
            }
          }
          if (inflight.has(mapped)) {
            return inflight.get(mapped).then((r) => r.clone());
          }
          const req = origFetch(mapped, init).then((res) => {
            if (ttl > 0 && res.ok) {
              responseCache.set(mapped, { ts: Date.now(), response: res.clone() });
            }
            return res;
          }).finally(() => inflight.delete(mapped));
          inflight.set(mapped, req);
          return req.then((r) => r.clone());
        }

        const origFetch = window.fetch.bind(window);
        window.fetch = async function(input, init){
          try {
            const raw = (typeof input === 'string') ? input : (input && input.url ? input.url : '');
            const u = new URL(raw, window.location.origin);

            if (u.pathname.startsWith('/api/')) {
              const dbMapped = await mapDbData(u.pathname, u.search).catch(() => null);
              if (dbMapped) {
                return jsonResponse(dbMapped, 200);
              }
              const mapped = mapApiUrl(u);
              return fetchWithCache(mapped, init);
            }
          } catch (_) {}
          return origFetch(input, init);
        };

        const NativeWS = window.WebSocket;
        function mapWs(url){
          try {
            const u = new URL(url, window.location.href);
            if (u.pathname.startsWith('/ws/')) {
              const b = new URL(window.__TN_API_BASE);
              const wsProto = (b.protocol === 'https:') ? 'wss:' : 'ws:';
              return `${wsProto}//${b.host}${u.pathname}${u.search}`;
            }
          } catch (_) {}
          return url;
        }

        window.WebSocket = function(url, protocols){
          const mapped = mapWs(url);
          return protocols ? new NativeWS(mapped, protocols) : new NativeWS(mapped);
        };
        window.WebSocket.prototype = NativeWS.prototype;
      })();
    </script>
'@

$directDbHtml = Build-Html 'Trustnode Edge - Single File Client (Direct Cloud DB HTML)' '' $dbRestShim
Set-Content -NoNewline -Encoding UTF8 (Join-Path $outDir 'client_test_db_rest.html') $directDbHtml

$phpPreamble = @'
<?php
session_start();
const TRUSTNODE_API_BASE_DEFAULT = 'https://trustnode.lsapps.app';
if (isset($_GET['proxy'])) {
  $base = trim((string)($_GET['base'] ?? ($_SESSION['trustnode_api_base'] ?? TRUSTNODE_API_BASE_DEFAULT)));
  if ($base === '') $base = TRUSTNODE_API_BASE_DEFAULT;
  $_SESSION['trustnode_api_base'] = $base;

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
  curl_setopt($ch, CURLOPT_TIMEOUT, 35);
  curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 10);
  if ($method !== 'GET' && $body !== false && $body !== '') {
    curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
  }

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
    if (is_array($json) && !empty($json['token'])) {
      $_SESSION['trustnode_token'] = (string)$json['token'];
    }
  }

  http_response_code($http > 0 ? $http : 500);
  echo (string)$response;
  exit;
}
?>
'@

$phpShim = @'
    <script>
      (function(){
        const q = new URLSearchParams(window.location.search);
        const fromQuery = (q.get('api_base') || '').trim();
        if (fromQuery) {
          try { localStorage.setItem('tn_api_base', fromQuery); } catch (_) {}
        }
        const fromStorage = (function(){ try { return (localStorage.getItem('tn_api_base') || '').trim(); } catch (_) { return ''; }})();
        window.__TN_PROXY_BASE = fromQuery || fromStorage || 'https://trustnode.lsapps.app';
        const LIMIT_CAPS = {
          '/api/app-store/live': 240,
          '/api/app-store/historian': 800,
          '/api/app-store/logs': 1000,
          '/api/v1/history': 800,
          '/api/v1/latest': 400,
          '/api/power/latest': 240,
          '/api/power/status': 200,
          '/api/plc/gateways/status': 120
        };
        const GET_CACHE_MS = {
          '/api/app-store/live': 250,
          '/api/power/latest': 250,
          '/api/power/status': 400,
          '/api/plc/gateways/status': 500,
          '/api/v1/latest': 500,
          '/api/app-store/historian': 1200,
          '/api/app-store/logs': 1200,
          '/api/v1/history': 1200
        };
        const inflight = new Map();
        const responseCache = new Map();

        function mapProxyPath(u){
          const cap = LIMIT_CAPS[u.pathname];
          if (cap && u.searchParams.has('limit')) {
            const raw = Number(u.searchParams.get('limit') || cap);
            u.searchParams.set('limit', String(Math.max(1, Math.min(cap, Number.isFinite(raw) ? raw : cap))));
          }
          return u.pathname.replace(/^\\//, '') + u.search;
        }

        async function fetchWithCache(mapped, init, cacheKey, path){
          const method = String((init && init.method) || 'GET').toUpperCase();
          if (method !== 'GET') return origFetch(mapped, init);
          const ttl = Number(GET_CACHE_MS[path] || 0);
          const now = Date.now();
          if (ttl > 0) {
            const cached = responseCache.get(cacheKey);
            if (cached && now - cached.ts <= ttl) return cached.response.clone();
          }
          if (inflight.has(cacheKey)) {
            return inflight.get(cacheKey).then((r) => r.clone());
          }
          const req = origFetch(mapped, init).then((res) => {
            if (ttl > 0 && res.ok) responseCache.set(cacheKey, { ts: Date.now(), response: res.clone() });
            return res;
          }).finally(() => inflight.delete(cacheKey));
          inflight.set(cacheKey, req);
          return req.then((r) => r.clone());
        }
        const origFetch = window.fetch.bind(window);
        window.fetch = function(input, init){
          try {
            const raw = (typeof input === 'string') ? input : (input && input.url ? input.url : '');
            const u = new URL(raw, window.location.origin);
            if (u.pathname.startsWith('/api/')) {
              const proxyPath = mapProxyPath(u);
              const proxy = `?proxy=${encodeURIComponent(proxyPath)}&base=${encodeURIComponent(window.__TN_PROXY_BASE)}`;
              const cacheKey = `${window.__TN_PROXY_BASE}|${proxyPath}`;
              return fetchWithCache(proxy, init, cacheKey, u.pathname);
            }
          } catch (_) {}
          return origFetch(input, init);
        };

        const NativeWS = window.WebSocket;
        function mapWs(url){
          try {
            const u = new URL(url, window.location.href);
            if (u.pathname.startsWith('/ws/')) {
              const b = new URL(window.__TN_PROXY_BASE);
              const wsProto = (b.protocol === 'https:') ? 'wss:' : 'ws:';
              return `${wsProto}//${b.host}${u.pathname}${u.search}`;
            }
          } catch (_) {}
          return url;
        }

        window.WebSocket = function(url, protocols){
          const mapped = mapWs(url);
          return protocols ? new NativeWS(mapped, protocols) : new NativeWS(mapped);
        };
        window.WebSocket.prototype = NativeWS.prototype;
      })();
    </script>
'@

$phpPage = Build-Html 'Trustnode Edge - Single File Client (Cloud API PHP Proxy)' '' $phpShim
Set-Content -NoNewline -Encoding UTF8 (Join-Path $outDir 'client_test.php') ($phpPreamble + "`r`n" + $phpPage)

$phpDbPreamble = @'
<?php
session_start();

const TRUSTNODE_API_BASE_DEFAULT = 'https://trustnode.lsapps.app';

function tn_cfg(string $key, string $default = ''): string {
  $v = getenv($key);
  if ($v === false || $v === null || $v === '') return $default;
  return (string)$v;
}

function tn_out_json(int $code, array $payload): void {
  http_response_code($code);
  header('Content-Type: application/json; charset=utf-8');
  header('Cache-Control: no-store, no-cache, must-revalidate');
  echo json_encode($payload, JSON_UNESCAPED_SLASHES);
  exit;
}

function tn_db(): PDO {
  static $pdo = null;
  if ($pdo instanceof PDO) return $pdo;
  $dsn = tn_cfg('TRUSTNODE_DB_DSN');
  $user = tn_cfg('TRUSTNODE_DB_USER');
  $pass = tn_cfg('TRUSTNODE_DB_PASS');
  if ($dsn === '') {
    throw new RuntimeException('Missing TRUSTNODE_DB_DSN');
  }
  $pdo = new PDO($dsn, $user, $pass, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
  ]);
  return $pdo;
}

function tn_try_queries(PDO $pdo, array $queries): array {
  $lastErr = null;
  foreach ($queries as $item) {
    try {
      $stmt = $pdo->prepare($item['sql']);
      foreach (($item['params'] ?? []) as $k => $v) {
        $stmt->bindValue($k, $v, is_int($v) ? PDO::PARAM_INT : PDO::PARAM_STR);
      }
      $stmt->execute();
      return $stmt->fetchAll();
    } catch (Throwable $e) {
      $lastErr = $e;
    }
  }
  if ($lastErr) throw $lastErr;
  return [];
}

if (isset($_GET['dbproxy'])) {
  try {
    $pdo = tn_db();
    $kind = (string)$_GET['dbproxy'];
    $limit = max(1, min(1200, (int)($_GET['limit'] ?? 500)));

    if ($kind === 'live') {
      $rows = tn_try_queries($pdo, [
        [
          'sql' => 'SELECT ts_utc AS ts, source, gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name AS tag, value, quality, quality_label FROM live_latest ORDER BY ts_utc DESC LIMIT :lim',
          'params' => [':lim' => $limit],
        ],
      ]);
      tn_out_json(200, ['ok' => true, 'rows' => $rows]);
    }

    if ($kind === 'historian') {
      $needle = trim((string)($_GET['tag'] ?? ''));
      $rows = tn_try_queries($pdo, [
        [
          'sql' => 'SELECT ts_utc AS ts, source, gateway_id, gateway_name, device_name, plc_ip, database_name, tag_name AS tag, value, quality, quality_label FROM historian_readings WHERE (:tag = \'\' OR tag_name ILIKE :tag_like) ORDER BY ts_utc DESC LIMIT :lim',
          'params' => [':lim' => $limit, ':tag' => $needle, ':tag_like' => '%'.$needle.'%'],
        ],
      ]);
      tn_out_json(200, ['ok' => true, 'rows' => $rows]);
    }

    if ($kind === 'logs') {
      $rows = tn_try_queries($pdo, [
        [
          'sql' => 'SELECT ts_utc AS ts, level, category, message, gateway_name AS gateway, device_name AS device, database_name FROM app_logs ORDER BY ts_utc DESC LIMIT :lim',
          'params' => [':lim' => max(1, min(2500, $limit))],
        ],
      ]);
      tn_out_json(200, ['ok' => true, 'rows' => $rows]);
    }

    tn_out_json(400, ['ok' => false, 'error' => 'Unknown dbproxy type']);
  } catch (Throwable $e) {
    tn_out_json(500, ['ok' => false, 'error' => $e->getMessage()]);
  }
}

if (isset($_GET['proxy'])) {
  $base = trim((string)($_GET['base'] ?? ($_SESSION['trustnode_api_base'] ?? TRUSTNODE_API_BASE_DEFAULT)));
  if ($base === '') $base = TRUSTNODE_API_BASE_DEFAULT;
  $_SESSION['trustnode_api_base'] = $base;

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
  curl_setopt($ch, CURLOPT_TIMEOUT, 35);
  curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 10);
  if ($method !== 'GET' && $body !== false && $body !== '') {
    curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
  }

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
    if (is_array($json) && !empty($json['token'])) {
      $_SESSION['trustnode_token'] = (string)$json['token'];
    }
  }

  http_response_code($http > 0 ? $http : 500);
  echo (string)$response;
  exit;
}
?>
'@

$phpDbShim = @'
    <script>
      (function(){
        const q = new URLSearchParams(window.location.search);
        const fromQuery = (q.get('api_base') || '').trim();
        if (fromQuery) {
          try { localStorage.setItem('tn_api_base', fromQuery); } catch (_) {}
        }
        const fromStorage = (function(){ try { return (localStorage.getItem('tn_api_base') || '').trim(); } catch (_) { return ''; }})();
        window.__TN_PROXY_BASE = fromQuery || fromStorage || 'https://trustnode.lsapps.app';
        const LIMIT_CAPS = {
          '/api/app-store/live': 240,
          '/api/app-store/historian': 800,
          '/api/app-store/logs': 1000,
          '/api/v1/history': 800,
          '/api/v1/latest': 400,
          '/api/power/latest': 240,
          '/api/power/status': 200,
          '/api/plc/gateways/status': 120
        };
        const GET_CACHE_MS = {
          '/api/app-store/live': 250,
          '/api/power/latest': 250,
          '/api/power/status': 400,
          '/api/plc/gateways/status': 500,
          '/api/v1/latest': 500,
          '/api/app-store/historian': 1200,
          '/api/app-store/logs': 1200,
          '/api/v1/history': 1200
        };
        const inflight = new Map();
        const responseCache = new Map();

        function capLimit(rawLimit, path){
          const cap = Number(LIMIT_CAPS[path] || 1000);
          const parsed = Number(rawLimit || cap);
          return Math.max(1, Math.min(cap, Number.isFinite(parsed) ? parsed : cap));
        }

        function mapProxyPath(u){
          const cap = LIMIT_CAPS[u.pathname];
          if (cap && u.searchParams.has('limit')) {
            const raw = Number(u.searchParams.get('limit') || cap);
            u.searchParams.set('limit', String(Math.max(1, Math.min(cap, Number.isFinite(raw) ? raw : cap))));
          }
          return u.pathname.replace(/^\\//, '') + u.search;
        }

        async function fetchWithCache(mapped, init, cacheKey, path){
          const method = String((init && init.method) || 'GET').toUpperCase();
          if (method !== 'GET') return origFetch(mapped, init);
          const ttl = Number(GET_CACHE_MS[path] || 0);
          const now = Date.now();
          if (ttl > 0) {
            const cached = responseCache.get(cacheKey);
            if (cached && now - cached.ts <= ttl) return cached.response.clone();
          }
          if (inflight.has(cacheKey)) {
            return inflight.get(cacheKey).then((r) => r.clone());
          }
          const req = origFetch(mapped, init).then((res) => {
            if (ttl > 0 && res.ok) responseCache.set(cacheKey, { ts: Date.now(), response: res.clone() });
            return res;
          }).finally(() => inflight.delete(cacheKey));
          inflight.set(cacheKey, req);
          return req.then((r) => r.clone());
        }

        const origFetch = window.fetch.bind(window);
        window.fetch = function(input, init){
          try {
            const raw = (typeof input === 'string') ? input : (input && input.url ? input.url : '');
            const u = new URL(raw, window.location.origin);
            if (u.pathname === '/api/app-store/live') {
              const limit = capLimit(u.searchParams.get('limit') || '500', u.pathname);
              const target = `?dbproxy=live&limit=${encodeURIComponent(limit)}`;
              return fetchWithCache(target, { method: 'GET', cache: 'no-store' }, `dbproxy|live|${limit}`, u.pathname);
            }
            if (u.pathname === '/api/app-store/historian') {
              const limit = capLimit(u.searchParams.get('limit') || '500', u.pathname);
              const tag = u.searchParams.get('tag') || '';
              const target = `?dbproxy=historian&limit=${encodeURIComponent(limit)}&tag=${encodeURIComponent(tag)}`;
              return fetchWithCache(target, { method: 'GET', cache: 'no-store' }, `dbproxy|historian|${limit}|${tag}`, u.pathname);
            }
            if (u.pathname === '/api/app-store/logs') {
              const limit = capLimit(u.searchParams.get('limit') || '500', u.pathname);
              const target = `?dbproxy=logs&limit=${encodeURIComponent(limit)}`;
              return fetchWithCache(target, { method: 'GET', cache: 'no-store' }, `dbproxy|logs|${limit}`, u.pathname);
            }
            if (u.pathname.startsWith('/api/')) {
              const proxyPath = mapProxyPath(u);
              const proxy = `?proxy=${encodeURIComponent(proxyPath)}&base=${encodeURIComponent(window.__TN_PROXY_BASE)}`;
              const cacheKey = `${window.__TN_PROXY_BASE}|${proxyPath}`;
              return fetchWithCache(proxy, init, cacheKey, u.pathname);
            }
          } catch (_) {}
          return origFetch(input, init);
        };

        const NativeWS = window.WebSocket;
        function mapWs(url){
          try {
            const u = new URL(url, window.location.href);
            if (u.pathname.startsWith('/ws/')) {
              const b = new URL(window.__TN_PROXY_BASE);
              const wsProto = (b.protocol === 'https:') ? 'wss:' : 'ws:';
              return `${wsProto}//${b.host}${u.pathname}${u.search}`;
            }
          } catch (_) {}
          return url;
        }

        window.WebSocket = function(url, protocols){
          const mapped = mapWs(url);
          return protocols ? new NativeWS(mapped, protocols) : new NativeWS(mapped);
        };
        window.WebSocket.prototype = NativeWS.prototype;
      })();
    </script>
'@

$phpDbPage = Build-Html 'Trustnode Edge - Single File Client (Direct Cloud DB PHP)' '' $phpDbShim
Set-Content -NoNewline -Encoding UTF8 (Join-Path $outDir 'client_test_db.php') ($phpDbPreamble + "`r`n" + $phpDbPage)

# Compatibility alias, kept for previous test path.
Set-Content -NoNewline -Encoding UTF8 (Join-Path $outDir 'client_test_db_php.html') $directDbHtml

Write-Host 'OK: regenerated 4 single-file variants (HTML API, HTML direct DB, PHP API, PHP direct DB).'
