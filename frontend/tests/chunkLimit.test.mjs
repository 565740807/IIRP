import assert from "node:assert/strict";
import { test } from "node:test";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { LIMIT, oversized } from "../check-chunks.mjs";

const script = fileURLToPath(new URL("../check-chunks.mjs", import.meta.url));

function assets(files) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "chunks-"));
  for (const [name, bytes] of Object.entries(files)) fs.writeFileSync(path.join(directory, name), "x".repeat(bytes));
  return directory;
}

test("a JavaScript chunk over 500 kB fails the build; other files and smaller chunks do not", () => {
  assert.equal(LIMIT, 500_000);
  const directory = assets({ "small.js": 1000, "edge.js": LIMIT, "big.js": LIMIT + 1, "style.css": LIMIT + 10 });
  try {
    assert.deepEqual(oversized(directory), [{ name: "big.js", bytes: LIMIT + 1 }]);
    const failed = spawnSync(process.execPath, [script, directory], { encoding: "utf8" });
    assert.equal(failed.status, 1);
    assert.match(failed.stderr, /big\.js: 500\.00 kB/);
    fs.rmSync(path.join(directory, "big.js"));
    const passed = spawnSync(process.execPath, [script, directory], { encoding: "utf8" });
    assert.equal(passed.status, 0, passed.stderr);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
