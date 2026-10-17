import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies the API to a running `modwen serve` (port 8765).
// Open http://localhost:5173/?token=<token printed by modwen serve> once so the cookie is set on 8765,
// or set VITE_MODWEN_TOKEN to send it as a header.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
