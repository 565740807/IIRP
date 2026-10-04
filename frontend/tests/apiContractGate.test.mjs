import assert from "node:assert/strict";
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const temporary = mkdtempSync(join(tmpdir(), "iirp-gate-test-"));
try {
  const schema = join(temporary, "schema.json");
  const expected = join(temporary, "api.ts");
  const generator = fileURLToPath(new URL("../node_modules/openapi-typescript/bin/cli.js", import.meta.url));
  const checker = fileURLToPath(new URL("../check-api.mjs", import.meta.url));
  const contract = { openapi: "3.1.0", info: { title: "gate fixture", version: "1" }, paths: {},
    components: { schemas: { Value: { type: "string" } } } };
  writeFileSync(schema, JSON.stringify(contract));
  assert.equal(spawnSync(process.execPath, [generator, schema, "-o", expected]).status, 0);
  const run = () => spawnSync(process.execPath, [checker, schema, expected], { encoding: "utf8" });
  assert.equal(run().status, 0);
  const original = readFileSync(expected);
  contract.components.schemas.Value.type = "number";
  writeFileSync(schema, JSON.stringify(contract));
  const drift = run();
  assert.notEqual(drift.status, 0);
  assert.match(drift.stderr, /TypeScript API drift/);
  assert.deepEqual(readFileSync(expected), original);
  console.log("Contract drift fails without overwriting the generated baseline");
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
