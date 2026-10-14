import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const src = fileURLToPath(new URL("../src/", import.meta.url));
const read = (file) => JSON.parse(fs.readFileSync(src + file, "utf8"));

/**
 * The shell and the home page render under the entry namespace only; pages and
 * dialogs loaded through PageLanguage bring their own. Bundling the app without
 * those lazy subtrees (and with unused exports shaken out) leaves exactly the
 * code that can render with the entry dictionary, so every key it names must be
 * there, or the reader sees the raw key.
 */
test("keys the shell and home page can render are in the entry namespace", async () => {
  const { outputFiles } = await build({
    entryPoints: [src + "App.tsx"],
    bundle: true, write: false, outdir: "entry", format: "esm", platform: "browser",
    packages: "external", jsx: "automatic", logLevel: "error",
    tsconfig: fileURLToPath(new URL("../tsconfig.json", import.meta.url)),
    loader: { ".json": "json", ".css": "empty" },
    plugins: [{
      name: "own-namespaces",
      setup(build) {
        build.onResolve({ filter: /.*/ }, (args) =>
          args.kind === "dynamic-import" && /<PageLanguage\b/.test(fs.readFileSync(args.importer, "utf8"))
            ? { path: args.path, external: true } : undefined);
      },
    }],
  });
  const code = outputFiles.map((file) => file.text).join("\n");
  for (const language of ["en", "zh"]) {
    const authored = Object.keys(read(`locales/${language}/translation.json`));
    const entry = read(`generated/locales/${language}/translation.json`);
    const missing = new Set();
    for (const [, key] of code.matchAll(/["'`](ui\.[a-z_\d.]*[a-z_\d])["'`]/g))
      if (authored.includes(key) && !(key in entry)) missing.add(key);
    // A key built from a template (`ui.overview.quote_status.${status}`) needs every key under its prefix.
    for (const [, prefix] of code.matchAll(/`(ui\.[a-z_\d.]+\.)\$\{/g))
      for (const key of authored) if (key.startsWith(prefix) && !(key in entry)) missing.add(key);
    assert.deepEqual([...missing], [], `${language}: add these keys to the entry namespace in generate-locales.mjs`);
  }
});
