import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Modules under chart/ and component/ that the core imports: they stay with the core.
const ECHARTS_SHARED = /^lib\/(chart\/helper\/|chart\/custom\/customSeriesRegister\.js|component\/(axis|helper)\/|component\/tooltip\/(seriesFormatTooltip|tooltipMarkup)\.js|component\/axisPointer\/modelHelper\.js)/;

/**
 * ECharts in three chunks so none passes the size limit (check-chunks.mjs fails the build when one
 * does): the chart types, the components, and the core with the helpers the core imports, so the
 * core never imports the other two.
 */
function echartsChunk(id: string) {
  const file = id.split("/echarts/").pop()!;
  if (ECHARTS_SHARED.test(file)) return "vendor-echarts";
  if (/^(charts\.js|lib\/export\/charts\.js|lib\/chart\/)/.test(file)) return "vendor-echarts-charts";
  if (/^(components\.js|lib\/export\/components\.js|lib\/component\/)/.test(file)) return "vendor-echarts-components";
  return "vendor-echarts";
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  build: {
    manifest: true,
    rollupOptions: {
      output: {
        // Keep dependency assignment explicit so lazy routes retain their boundary.
        onlyExplicitManualChunks: true,
        manualChunks(id) {
          if (id.includes("commonjsHelpers")) return "vendor-runtime";
          if (!id.includes("/node_modules/")) return;
          if (/\/(react|react-dom|scheduler)\//.test(id)) return "vendor-react";
          if (/\/(react-router|react-router-dom)\//.test(id)) return "vendor-router";
          if (id.includes("/@tanstack/")) {
            if (id.includes("table")) return "vendor-table";
            if (id.includes("virtual")) return "vendor-virtual";
            return "vendor-query";
          }
          if (id.includes("/zrender/")) return "vendor-zrender";
          if (id.includes("/echarts/")) return echartsChunk(id);
          if (id.includes("/@radix-ui/")) {
            if (id.includes("/react-dialog/")) return "vendor-radix-dialog";
            return "vendor-radix";
          }
          if (/\/(i18next|react-i18next|i18next-resources-to-backend)\//.test(id)) return "vendor-i18n";
        },
      },
    },
  },
  // Keep the browser's Host so same-origin writes pass in development too.
  server: { proxy: { "/api": { target: "http://127.0.0.1:18081", changeOrigin: false } } },
});
