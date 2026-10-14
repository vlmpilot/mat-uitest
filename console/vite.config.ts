import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 开发期：vite dev --port 5173，/api 代理到老 console 进程（7420），前端热更新不重启后端
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", chunkSizeWarningLimit: 1600 },
  server: { proxy: { "/api": "http://127.0.0.1:7420", "/artifacts": "http://127.0.0.1:7420" } },
});
