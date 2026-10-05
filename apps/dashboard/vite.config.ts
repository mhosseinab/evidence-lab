import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";

const apiTarget = process.env.EVIDENCE_LAB_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [vue()],
  base: "/static/",
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: {
      "/api": apiTarget,
      "/health": apiTarget,
    },
  },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"],
    clearMocks: true,
    restoreMocks: true,
  },
});
