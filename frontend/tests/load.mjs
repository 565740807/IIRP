import { buildSync } from "esbuild";
import { fileURLToPath } from "node:url";

/** Bundle one source module (with its imports and the "@/..." alias) for a node test. */
export async function load(entry, translations = false) {
  const { outputFiles } = buildSync({
    stdin: {
      contents: `export * from "./${entry}";${translations ? 'export { initialize as initializeTranslations, default as testI18n } from "./i18n";' : ""}`,
      resolveDir: fileURLToPath(new URL("../src", import.meta.url)), loader: "ts",
    },
    bundle: true, write: false, platform: "node", format: "esm",
    tsconfig: fileURLToPath(new URL("../tsconfig.json", import.meta.url)),
    loader: { ".json": "json" },
  });
  const module = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`);
  if (translations) {
    await module.initializeTranslations();
    await module.testI18n.loadNamespaces(["analysis", "events"]);
  }
  return module;
}
