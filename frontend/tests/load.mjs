import { buildSync } from "esbuild";
import { fileURLToPath } from "node:url";

/** Bundle one source module (with its imports and the "@/..." alias) for a node test. */
export async function load(entry) {
  const { outputFiles } = buildSync({
    entryPoints: [fileURLToPath(new URL(`../src/${entry}`, import.meta.url))],
    bundle: true, write: false, platform: "node", format: "esm",
    tsconfig: fileURLToPath(new URL("../tsconfig.json", import.meta.url)),
    loader: { ".json": "json" },
  });
  return import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`);
}
