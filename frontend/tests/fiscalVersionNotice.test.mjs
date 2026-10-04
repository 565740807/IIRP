import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

const compiled = ts.transpileModule(
  readFileSync(new URL("../src/fiscalVersionNotice.ts", import.meta.url), "utf8"),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } },
).outputText;
const { fiscalVersionNotice } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
assert.match(fiscalVersionNotice("native_earnings", "research-v8-robustness"), /旧版冻结/);
assert.match(fiscalVersionNotice("native_earnings", "research-v9-fiscal-kind-evidence"), /公告日期来源/);
assert.match(fiscalVersionNotice("event_import", "event-dates-v6-robustness"), /覆盖分母/);
assert.match(fiscalVersionNotice("native_event_dates", "event-dates-v6-robustness"), /原生财报日期观察/);
assert.doesNotMatch(fiscalVersionNotice("native_event_dates", "event-dates-v6-robustness"), /导入年份/);
assert.equal(fiscalVersionNotice("native_earnings", "research-v10-time-source-attribution"), null);
assert.equal(fiscalVersionNotice("event_import", "event-dates-v7-fiscal-scope-evidence"), null);
assert.match(fiscalVersionNotice("native_earnings", "unrelated"), /无法确认/);
assert.match(fiscalVersionNotice("event_import", undefined), /无法确认/);
console.log("old immutable fiscal results receive specific version warnings; new results do not");
