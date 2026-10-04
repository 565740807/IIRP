import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
import { QueryClient } from "@tanstack/react-query";

const compiled = ts.transpileModule(readFileSync(new URL("../src/taskPresentation.ts", import.meta.url), "utf8"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText;
const helpers = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
assert.equal(helpers.taskEntryCategory({ running: 0, waiting: 0, attention: 1 }), "attention");
assert.equal(helpers.taskEntryCategory({ running: 0, waiting: 2, attention: 1 }), "active");
assert.equal(helpers.taskEntryCategory(), "active");
assert.equal(helpers.taskStage("等待可执行任务", "sec_latest", "QUEUED"), "正在安排 SEC 申报核对");
assert.equal(helpers.taskStage("等待 Yahoo 通道 · 补齐历史日线", "market_history", "QUEUED"), "等待 Yahoo 通道 · 补齐历史日线");
assert.equal(helpers.taskStage("", "market_history", "SUCCEEDED"), "所选范围已完成");

// Exercise the actual query cache: assigning the returned snapshot URL is an
// alias for its first response. New pages and changed filters still load once.
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const key = (params) => ["entity", "company", "123", helpers.entityRequestParams(new URLSearchParams(params))];
let reads = 0;
const fetchPage = async (params) => client.fetchQuery({ queryKey: key(params), staleTime: 60_000,
  queryFn: async () => ({ items: [{ id: `row-${++reads}` }], data: { session_id: "snapshot-1" } }),
});
const first = await fetchPage("recent_count=50");
client.setQueryData(key("recent_count=50&cursor=snapshot-1%3A0&entity_page=0"), first);
assert.deepEqual(await fetchPage("cursor=snapshot-1%3A0&entity_page=0&recent_count=50"), first);
assert.equal(reads, 1, "URL normalization must not read the first immutable page twice");
await fetchPage("recent_count=50&cursor=snapshot-1%3A20&entity_page=1");
assert.equal(reads, 2);
assert.deepEqual(await fetchPage("recent_count=50&cursor=snapshot-1%3A0&entity_page=0"), first);
assert.equal(reads, 2, "return to retained page uses its own cached snapshot");
await fetchPage("recent_count=50&action=buy");
assert.equal(reads, 3, "changed research filter cannot reuse an unrelated page");
assert.equal(helpers.acquiredSessionsText(1253, 2000), "已获取 1,253 个有效交易日 / 所需 2,000 日");
assert.equal(helpers.acquiredSessionsText(0, 253), "已获取 0 个有效交易日 / 所需 253 日");
assert.equal(helpers.acquiredSessionsText(null, 253), null);
assert.equal(helpers.acquiredSessionsText(20, null), "已获取 20 个有效交易日");
client.setQueryData(["entity", "transaction", "trade-1"], { price: "saved" });
await client.invalidateQueries({ predicate: (query) => !helpers.isFrozenEntityQuery(query.queryKey) });
assert.equal(client.getQueryState(key("recent_count=50&cursor=snapshot-1%3A0")).isInvalidated, false);
assert.equal(client.getQueryState(key("recent_count=50&action=buy")).isInvalidated, true);
assert.equal(client.getQueryState(["entity", "transaction", "trade-1"]).isInvalidated, true);
assert.deepEqual(await fetchPage("recent_count=50&cursor=snapshot-1%3A0&entity_page=0"), first);
assert.equal(reads, 3, "background progress cannot refetch a frozen reading page");
client.clear();
console.log("task entry, factual stage, cursor alias, paging, return and filter cache checks passed");
