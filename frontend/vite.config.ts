import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  // Keep the browser's Host so same-origin writes pass in development too.
  server: { proxy: { "/api": { target: "http://127.0.0.1:18081", changeOrigin: false } } },
});
