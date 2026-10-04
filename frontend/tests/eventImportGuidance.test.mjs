import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const source = readFileSync(new URL("../src/eventImportGuidance.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { formatRequestedQuarters, importGuidance, importedScopeHistoricalYears, importedScopeDateCategory, requestedQuarterError } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
assert.equal(formatRequestedQuarters([2]), "Q2");
assert.equal(formatRequestedQuarters([1, 4]), "Q1、Q4");
assert.equal(formatRequestedQuarters([{}]), "待确认");
assert.equal(importedScopeHistoricalYears({fiscal_year_start:2024,fiscal_year_end:2025,fiscal_quarters:[2]}), 2);
assert.equal(importedScopeHistoricalYears({year_start:2018,year_end:2025}), 8);
assert.equal(importedScopeHistoricalYears({fiscal_year_start:2025,fiscal_year_end:2024}), null);
assert.equal(importedScopeHistoricalYears({year_start:"2024",year_end:2025}), null);
assert.equal(importedScopeDateCategory({fiscal_quarters:[2]}, "earnings"), "Q2");
assert.equal(importedScopeDateCategory({fiscal_quarters:[1,2,3,4]}, "earnings"), null);
assert.equal(importedScopeDateCategory({fiscal_quarters:[2]}, "custom"), null);
assert.equal(importedScopeDateCategory({fiscal_quarters:["2"]}, "earnings"), null);


assert.match(requestedQuarterError(JSON.stringify({scope:{fiscal_year_start:2024,fiscal_year_end:2025}}), "仅Q2", "earnings"), /默认 Q1—Q4/);
assert.match(requestedQuarterError(JSON.stringify({scope:{fiscal_quarters:[1,2,3,4]}}), "only Q2", "earnings"), /不一致/);
assert.equal(requestedQuarterError(JSON.stringify({scope:{fiscal_quarters:[2]}}), "仅Q2", "earnings"), null);
assert.equal(requestedQuarterError("not JSON", "仅Q2", "earnings"), null);
assert.match(requestedQuarterError(JSON.stringify({scope:{fiscal_quarters:[1,2,3,4]}}), "Fiscal quarter: only Q2", "earnings"), /不一致/);
const first = {message:"raw parser internals",details:[
  {loc:["company","exchange"],type:"extra_forbidden",msg:"Extra inputs are not permitted"},
  {loc:["company","mic"],type:"extra_forbidden",msg:"Extra inputs are not permitted"},
  {loc:["company","security_type"],type:"extra_forbidden",msg:"Extra inputs are not permitted"},
  {loc:["scope","include_future"],type:"extra_forbidden",msg:"Extra inputs are not permitted"},
]};
const feedback = importGuidance(first,"earnings","Apple FY2024—2025 only Q2");
assert.match(feedback.message,/company.exchange_mic/);
assert.match(feedback.message,/scope.include_scheduled/);
assert.match(feedback.message,/股类先澄清/);
assert.doesNotMatch(feedback.message,/raw parser internals|Pydantic|Extra inputs/);
assert.match(feedback.repair,/Apple FY2024—2025 only Q2/);
assert.match(feedback.repair,/保持所有已核实/);
const earningsSupportsSchema = { $defs: { Event: { properties: { sources: { items: { $ref: "#/$defs/Source" } } } }, Source: { properties: { supports: { items: { enum: ["日期", "财年财季", "财期类型"] } } } } }, properties: { events: { items: { $ref: "#/$defs/Event" } } } };
const realFirst = importGuidance({message:"raw parser internals",details:[
  {loc:["events",0,"sources",0,"supports",2],type:"literal_error",msg:"Input should be..."},
]},"earnings","Apple FY2024—2025 only Q2",earningsSupportsSchema);
assert.match(realFirst.message,/events\.0\.sources\.0\.supports\.2.*合法值：日期、财年财季、财期类型/);
assert.match(realFirst.repair,/来源 supports 合法值：日期、财年财季、财期类型/);
assert.doesNotMatch(realFirst.repair,/raw parser internals/);
const syntax = importGuidance({message:"Expecting property name enclosed in double quotes: line 3 column 4 (char 22)",status:422},"custom","");
assert.match(syntax.message,/第 3 行、第 4 列/);
assert.doesNotMatch(syntax.repair,/Expecting property name/);
console.log("event import guidance passed");
