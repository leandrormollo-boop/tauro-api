import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const componentFiles = [
  "01-nav-hero.jsx",
  "02-quote-widget.jsx",
  "03-services-tracking.jsx",
  "04-stats-contact-footer.jsx",
  "05-app.jsx",
];

const components = await Promise.all(
  componentFiles.map((name) =>
    readFile(join(root, "web", "components", name), "utf8"),
  ),
);

const source = [
  'import React from "react";',
  'import * as ReactDOM from "react-dom/client";',
  ...components,
].join("\n\n");

await build({
  stdin: {
    contents: source,
    loader: "jsx",
    resolveDir: root,
    sourcefile: "tauro-web.jsx",
  },
  bundle: true,
  define: {
    "process.env.NODE_ENV": '"production"',
  },
  format: "iife",
  legalComments: "none",
  minify: true,
  // Se sirve desde /static (mount único, cabeceras de cache correctas).
  // Railway NO tiene node: este .js compilado SE COMMITEA.
  outfile: join(root, "static", "js", "app.js"),
  platform: "browser",
  sourcemap: false,
  target: ["es2020"],
});
