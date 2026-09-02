import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/main.ts"],
  format: ["esm"],
  target: "es2022",
  outDir: "dist",
  outExtension: () => ({ js: ".min.js" }),
  bundle: true,
  clean: true,
  minify: true,
  sourcemap: false,
  splitting: false,
  skipNodeModulesBundle: false,
});
