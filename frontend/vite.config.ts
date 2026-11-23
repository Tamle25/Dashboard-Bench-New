import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Khi phát triển, mọi lời gọi /api được chuyển tới FastAPI ở cổng 8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
