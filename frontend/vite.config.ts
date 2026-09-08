import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwind from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig({
  plugins: [react(), tailwind()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  server: {
    port: 5181,
    proxy: { "/api": "http://127.0.0.1:8010" },
  },
});
