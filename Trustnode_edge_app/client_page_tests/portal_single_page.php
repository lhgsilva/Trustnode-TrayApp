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
    :root { color-scheme: dark; }
    html, body { margin:0; height:100%; background:#111; font-family:Segoe UI, Arial, sans-serif; }
    .shell { height:100%; display:flex; flex-direction:column; }
    .bar { height:42px; display:flex; align-items:center; justify-content:space-between; padding:0 12px; background:#1e1e1e; border-bottom:1px solid #2f2f2f; color:#d4d4d4; }
    .bar a { color:#4fc3f7; text-decoration:none; font-size:12px; }
    .frame { flex:1; width:100%; border:0; background:#111; }
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
