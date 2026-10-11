// Generate the TypeScript API types: docs/openapi.json → src/generated/api.ts, and each
// optional feature's own src/features/<name>/openapi.json → src/features/<name>/generated/api.ts.
import { existsSync, mkdirSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = dirname(fileURLToPath(import.meta.url));
export function contracts() {
  const features = join(root, "src/features");
  const found = [[join(root, "../docs/openapi.json"), join(root, "src/generated/api.ts")]];
  if (existsSync(features)) {
    for (const name of readdirSync(features).sort()) {
      const schema = join(features, name, "openapi.json");
      if (existsSync(schema)) found.push([schema, join(features, name, "generated/api.ts")]);
    }
  }
  return found;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  for (const [schema, output] of contracts()) {
    mkdirSync(dirname(output), { recursive: true });
    const result = spawnSync(process.execPath, [join(root, "node_modules/openapi-typescript/bin/cli.js"), schema, "-o", output], { stdio: "inherit" });
    if (result.status !== 0) process.exit(result.status ?? 1);
  }
}
