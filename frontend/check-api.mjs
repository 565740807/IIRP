// Generate into a disposable directory. A check must never repair its input.
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = dirname(fileURLToPath(import.meta.url));
const schema = resolve(process.argv[2] ?? join(root, "../docs/openapi.json"));
const expected = resolve(process.argv[3] ?? join(root, "src/generated/api.ts"));
const temporary = mkdtempSync(join(tmpdir(), "iirp-contract-"));
try {
  const generated = join(temporary, "api.ts");
  const result = spawnSync(process.execPath, [
    join(root, "node_modules/openapi-typescript/bin/cli.js"), schema, "-o", generated,
  ], { stdio: "inherit" });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exitCode = result.status ?? 1;
  else if (!readFileSync(generated).equals(readFileSync(expected))) {
    console.error("TypeScript API drift: review docs/openapi.json and explicitly run npm run generate:api");
    process.exitCode = 1;
  } else console.log("Generated TypeScript matches OpenAPI (read-only comparison)");
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
