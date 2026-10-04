import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

const compiled = ts.transpileModule(
  readFileSync(new URL("../src/earningsCorrectionSources.ts", import.meta.url), "utf8"),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } },
).outputText;
const { correctionSources } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
const event = {
  fiscal_year: 2025, fiscal_quarter: 2, announced_date: "2025-05-01",
  evidence: [
    { provider: "manual_review", source_url: "https://apple.example/release", announced_date: "2025-05-01", fiscal_year: 2025, fiscal_quarter: 2 },
    { provider: "manual_period_review", source_url: "https://sec.example/10q", fiscal_year: 2025, fiscal_quarter: 2, period_kind: "regular", period_kind_evidence: "10-Q quarterly cover" },
  ],
};
assert.deepEqual(correctionSources(event), {
  source_url: "https://apple.example/release",
  period_kind: "regular",
  period_kind_source_url: "https://sec.example/10q",
  period_kind_evidence: "10-Q quarterly cover",
  time_evidence: null,
});
const withdrawn = { ...event, evidence: [...event.evidence, { provider: "manual_period_review", fiscal_year: 2025, fiscal_quarter: 2, period_kind: "unknown" }] };
assert.deepEqual(correctionSources(withdrawn), {
  source_url: "https://apple.example/release",
  period_kind: "unknown",
  period_kind_source_url: null,
  period_kind_evidence: null,
  time_evidence: null,
});
const changedDate = { ...event, announced_date: "2025-05-02" };
assert.equal(correctionSources(changedDate).source_url, "");
assert.equal(correctionSources({ ...event, evidence: [...event.evidence, { provider: "manual_period_review", source_url: "https://sec.example/transition", fiscal_year: 2025, fiscal_quarter: 2, period_kind: "transition", period_kind_evidence: "transition" }] }).period_kind, "transition");
console.log("native earnings correction keeps date and period sources separate and latest review authoritative");

const noIdentity = { ...event, evidence: [{
  provider: "SEC release", source_url: "https://sec.example/old",
  announced_date: "2025-05-01", period_kind: "regular",
  period_kind_evidence: "Old evidence has no FY/Q fields",
}] };
assert.equal(correctionSources(noIdentity).period_kind, "unknown");

const legacyCombined = { ...event, evidence: [{
  provider: "manual_review", source_url: "https://sec.example/10q",
  announced_date: "2025-05-01", fiscal_year: 2025, fiscal_quarter: 2,
  period_kind: "regular", period_kind_evidence: "Legacy combined claim",
}] };
assert.equal(correctionSources(legacyCombined).period_kind, "unknown");
assert.equal(correctionSources(legacyCombined).source_url, "");

const timed = { ...event, evidence: [{ ...event.evidence[0], time_evidence: "Actual public release at 4:05 p.m. ET" }, event.evidence[1]] };
assert.equal(correctionSources(timed).time_evidence, "Actual public release at 4:05 p.m. ET");
