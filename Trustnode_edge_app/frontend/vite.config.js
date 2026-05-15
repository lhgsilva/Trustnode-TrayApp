import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";

export default defineConfig(({ mode }) => {
  const isSingleFile = mode === "clientview";
  return {
    base: "./",
    plugins: [
      react(),
      ...(isSingleFile ? [viteSingleFile({ removeViteModuleLoader: true })] : []),
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
