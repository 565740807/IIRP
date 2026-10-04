import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const source = readFileSync(new URL("../src/systemRefresh.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { systemRefreshInterval } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
assert.equal(systemRefreshInterval("fresh"),60_000);
for (const status of ["stale","failed","measuring",undefined]) assert.equal(systemRefreshInterval(status),5_000);
console.log("system status polling passed");
