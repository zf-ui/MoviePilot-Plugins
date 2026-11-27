import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import federation from "@originjs/vite-plugin-federation";

export default defineConfig({
  plugins: [
    vue(),
    federation({
      name: "StrmGen",
      filename: "remoteEntry.js",
      exposes: {
        "./Config": "./src/components/Config.vue",
        "./Page": "./src/components/Page.vue",
      },
      shared: {
        vue: { requiredVersion: false, generate: false, singleton: true },
        vuetify: { requiredVersion: false, generate: false, singleton: true },
      },
    }),
  ],
  build: {
    target: "esnext",
    cssCodeSplit: true,
    outDir: "dist",
    assetsDir: "assets",
    emptyOutDir: true,
  },
});
