<?php
$portalHost = isset($_GET['portalHost']) && $_GET['portalHost'] !== '' ? $_GET['portalHost'] : '';
if ($portalHost === '') {
  $isHttps = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') || (isset($_SERVER['SERVER_PORT']) && (int)$_SERVER['SERVER_PORT'] === 443);
  $scheme = $isHttps ? 'https://' : 'http://';
  $host = isset($_SERVER['HTTP_HOST']) ? $_SERVER['HTTP_HOST'] : 'localhost';
  $portalHost = $scheme . $host;
}
$portalPath = isset($_GET['portalPath']) && $_GET['portalPath'] !== '' ? $_GET['portalPath'] : '/portal';
$qs = isset($_GET['qs']) ? trim((string)$_GET['qs']) : '';
$src = rtrim($portalHost, '/') . $portalPath;
if ($qs !== '') {
  $src .= '?' . $qs;
}
?>
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Trustnode Portal - Single Page (PHP)</title>
  <style>
    :root {
      color-scheme: dark light;
      --bg-canvas: #0e1116;
      --bg-card: #111827;
      --ink: #f2f4f7;
      --ink-soft: #b6c0cc;
      --line: rgba(255,255,255,.10);
      --accent: #14a89a;
    }
    @media (prefers-color-scheme: light) {
      :root {
        --bg-canvas: #eceff3;
        --bg-card: #ffffff;
        --ink: #0e1116;
        --ink-soft: #4a5566;
        --line: #d3dae3;
        --accent: #0e8479;
      }
    }
    html, body { margin:0; height:100%; background:var(--bg-canvas); color:var(--ink); font-family:Manrope, "Segoe UI", Arial, sans-serif; }
    .shell { height:100%; display:flex; flex-direction:column; }
    .bar { height:46px; display:flex; align-items:center; justify-content:space-between; padding:0 14px; background:var(--bg-card); border-bottom:1px solid var(--line); color:var(--ink); }
    .bar a { color:var(--accent); text-decoration:none; font-size:12px; font-weight:600; }
    .frame { flex:1; width:100%; border:0; background:var(--bg-canvas); }
  </style>
</head>
<body>
  <div class="shell">
    <div class="bar">
      <strong>Trustnode Developer Portal</strong>
      <a href="<?php echo htmlspecialchars($src, ENT_QUOTES, 'UTF-8'); ?>" target="_blank" rel="noopener">Open raw portal</a>
    </div>
    <iframe class="frame" title="Trustnode Portal" src="<?php echo htmlspecialchars($src, ENT_QUOTES, 'UTF-8'); ?>"></iframe>
  </div>
</body>
</html>
