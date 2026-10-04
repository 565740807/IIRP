// Independent assertions against the same TypeScript reducer used by Feed.
// No browser/runtime/source service and no implementation copied into tests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('../frontend/node_modules/typescript');
const file = path.resolve(__dirname, '../frontend/src/feedMerge.ts');
const compiled = ts.transpileModule(fs.readFileSync(file,'utf8'), { compilerOptions: { target:ts.ScriptTarget.ES2022, module:ts.ModuleKind.CommonJS } });
const loaded = { exports:{} };
new Function('exports','module',compiled.outputText)(loaded.exports,loaded);
const {mergeGroups,mergeRemovalIds}=loaded.exports;
const a={id:'A',revision_id:'A1',revision_created_at:'2026-09-10T12:00:00+00:00',transaction_dates:['2026-09-09']};
const b={id:'B',revision_id:'B1',revision_created_at:'2026-09-10T12:00:00+00:00',transaction_dates:['2026-09-08']};
const c={id:'C',revision_id:'C1',revision_created_at:'2026-09-10T12:00:00+00:00',transaction_dates:['2026-09-07']};
const cases=[];
function test(name,run){run();cases.push(name)}
test('A tombstone for an unloaded old page survives until that page is appended',()=>{
 const removed=mergeRemovalIds([],['C'],[]);
 const first=mergeGroups([a],[], 'update','transaction',removed);
 assert.deepEqual(first,[a]);
 const second=mergeGroups(first,[b,c], 'append','transaction',removed);
 assert.deepEqual(second.map(g=>g.id),['A','B','C']);
 assert.equal(second[2]._removed,true);
 assert.equal(second[0],a);
});
test('Tombstones survive later unrelated delta pages and do not duplicate',()=>{
 const removed=mergeRemovalIds(['C'],['C','B'],[a]);
 const next=mergeRemovalIds(removed,[],[a]);
 assert.deepEqual(next,['C','B']);
 const restored=JSON.parse(JSON.stringify({groups:[a],removedIds:next}));
 const page=mergeGroups(restored.groups,[b,c],'append','transaction',restored.removedIds);
 assert.equal(page.filter(g=>g._removed).length,2);
});
test('A later matching revision revives only its own tombstone',()=>{
 const revised={...c,revision_id:'C2',revision_created_at:'2026-09-11T12:00:00+00:00'};
 const removed=mergeRemovalIds(['B','C'],[],[revised]);
 assert.deepEqual(removed,['B']);
 const merged=mergeGroups([a,{...c,_removed:true}],[revised],'update','transaction',removed);
 assert.equal(merged[1].revision_id,'C2');assert.equal(merged[1]._removed,undefined);
});
test('Old frozen append and older late revision cannot replace displayed new data',()=>{
 const revised={...b,revision_id:'B2',revision_created_at:'2026-09-11T12:00:00+00:00'};
 assert.equal(mergeGroups([a,revised],[b,c],'append','transaction')[1].revision_id,'B2');
 assert.equal(mergeGroups([a,revised],[b],'update','transaction')[1].revision_id,'B2');
});
test('Repeated group IDs deduplicate and updates retain existing relative reading positions',()=>{
 const revised={...b,revision_id:'B2',revision_created_at:'2026-09-11T12:00:00+00:00',transaction_dates:['2026-09-12']};
 const newGroup={...c,id:'NEW',transaction_dates:['2026-09-10']};
 const merged=mergeGroups([a,b],[newGroup,newGroup,revised],'update','transaction');
 assert.deepEqual(merged.map(g=>g.id),['NEW','A','B']);assert.equal(merged[2],revised);
});
console.log(JSON.stringify({passed:cases.length,cases},null,2));
