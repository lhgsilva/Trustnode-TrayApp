// Builds the DashboardDesigner test harness only. Test tooling, not shipped:
// the release build never invokes this config and dist_designer is not packaged.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist_designer",
    emptyOutDir: true,
    rollupOptions: {
      input: "test_designer_entry.jsx",
      output: { format: "iife", entryFileNames: "designer.js", inlineDynamicImports: true },
    },
  },
});
