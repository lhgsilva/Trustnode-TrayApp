import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";

// Inline manifest for the client-view single-file build.
// Embedded as a data URI so the resulting HTML can be hosted on any domain
// (including the customer's own website) without needing an extra round-trip
// for /manifest.webmanifest. Two SVG icons (192 + 512) also embedded.
const TRUSTNODE_PWA_MANIFEST = {
  name: "TrustNode Client Portal",
  short_name: "TrustNode",
  start_url: "./",
  scope: "./",
  display: "standalone",
  background_color: "#ffffff",
  theme_color: "#0e1a3a",
  orientation: "any",
  icons: [
    {
      src:
        "data:image/svg+xml," +
        encodeURIComponent(
          `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">` +
            `<rect width="512" height="512" rx="96" fill="#0e1a3a"/>` +
            `<text x="256" y="320" text-anchor="middle" font-family="Arial,sans-serif" ` +
            `font-weight="bold" font-size="200" fill="#14b8a6">TN</text>` +
            `</svg>`
        ),
      sizes: "192x192",
      type: "image/svg+xml",
      purpose: "any maskable",
    },
    {
      src:
        "data:image/svg+xml," +
        encodeURIComponent(
          `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">` +
            `<rect width="512" height="512" rx="96" fill="#0e1a3a"/>` +
            `<text x="256" y="320" text-anchor="middle" font-family="Arial,sans-serif" ` +
            `font-weight="bold" font-size="200" fill="#14b8a6">TN</text>` +
            `</svg>`
        ),
      sizes: "512x512",
      type: "image/svg+xml",
      purpose: "any maskable",
    },
  ],
};

function clientViewHtmlAugment() {
  // Injects PWA + responsive meta tags into the SINGLE-FILE client-view
  // build only. The local Electron desktop and the multi-file cloud build
  // never see these changes (they go through Vite's plain html flow).
  const pwaManifestUri =
    "data:application/manifest+json;charset=utf-8," +
    encodeURIComponent(JSON.stringify(TRUSTNODE_PWA_MANIFEST));
  const injected = `
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, maximum-scale=5" />
    <meta name="theme-color" content="#0e1a3a" />
    <meta name="apple-mobile-web-app-capable" content="yes" />
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
    <meta name="apple-mobile-web-app-title" content="TrustNode" />
    <meta name="mobile-web-app-capable" content="yes" />
    <link rel="manifest" href="${pwaManifestUri}" />
  `;
  return {
    name: "trustnode-client-view-pwa-meta",
    transformIndexHtml(html) {
      // Replace the default plain viewport meta with the responsive one and
      // append the rest of the PWA tags right after charset.
      let out = html.replace(
        /<meta name="viewport"[^>]*\/?>/i,
        ""
      );
      out = out.replace(
        /(<meta charset="UTF-8" ?\/?>)/i,
        `$1${injected}`
      );
      // Best-effort service-worker registration. We DO NOT bundle a
      // service-worker script (the single-file build can't host one),
      // but if the operator deploys this file under a path that ALSO
      // serves a sw.js next to it WITH the correct JS content-type,
      // the browser will register it for offline caching.
      //
      // Most SPAs (including ours) serve a fallback HTML for missing
      // paths. We MUST detect that case and skip registration, otherwise
      // the browser logs a "MIME type ('text/html')" error and confuses
      // operators. We check both the HTTP status AND the content-type.
      const swInline = `
        <script>
        (function(){
          try {
            var nav = window.navigator || {};
            if (!('serviceWorker' in nav)) return;
            var proto = String(window.location.protocol||'').toLowerCase();
            var ua = String(nav.userAgent||'');
            if (proto !== 'https:' && proto !== 'http:') return;
            if (/electron/i.test(ua)) return;
            var hereDir = window.location.pathname.replace(/[^\\/]*$/, '');
            var swUrl = hereDir + 'sw.js';
            fetch(swUrl, { method: 'HEAD' }).then(function(r){
              if (!r || !r.ok) return;
              var ct = String(r.headers.get('content-type') || '').toLowerCase();
              if (ct.indexOf('javascript') < 0) return;
              nav.serviceWorker.register(swUrl, { scope: hereDir }).catch(function(){});
            }).catch(function(){});
          } catch (_) { /* noop */ }
        })();
        </script>`;
      out = out.replace(/<\/body>/i, swInline + "</body>");
      return out;
    },
  };
}

export default defineConfig(({ mode }) => {
  const isSingleFile = mode === "clientview";
  return {
    base: "./",
    plugins: [
      react(),
      ...(isSingleFile
        ? [clientViewHtmlAugment(), viteSingleFile({ removeViteModuleLoader: true })]
        : []),
    ],
    build: isSingleFile
      ? {
          // Inline everything (JS + CSS + small assets) into one HTML file.
          assetsInlineLimit: 100_000_000,
          chunkSizeWarningLimit: 100_000_000,
          cssCodeSplit: false,
          rollupOptions: { output: { inlineDynamicImports: true } },
        }
      : {},
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/api": "http://127.0.0.1:8000",
        "/ws": {
          target: "ws://127.0.0.1:8000",
          ws: true,
        },
      },
    },
  };
});
