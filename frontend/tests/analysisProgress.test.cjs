const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
 const root = path.resolve(__dirname, "../..");
 const ts = require(path.join(root, 'frontend/node_modules/typescript'));
 const code = ts.transpileModule(fs.readFileSync(path.join(root, 'frontend/src/analysisProgress.ts'), 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText;
 const { mergeAnalysisResults: merge, matchesAnalysis, resultSet, remainingAnalysisProgress, analysisConditions } = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
 const item = (symbol, version, date) => ({ symbol, security_id: symbol, result_id: version, input_version: version, created_at: date, is_current: true, data: { effective_n: version === 'a2' ? 8 : 4 } });
 const a1 = item('A', 'a1', '2026-09-14T12:00:00Z'), a2 = item('A', 'a2', '2026-09-14T12:01:00Z'), b1 = item('B', 'b1', '2026-09-14T12:01:20Z');
 const response = (results, time='2026-09-14T12:00:00Z') => ({ id:'research1', params: { tickers: ['A','B'], month:9, benchmark:'^GSPC' }, batch:{ updated_at:time }, results });
 const checks=[]; const check=(label,fn)=>{fn();checks.push(label)};
 const previous=response([a1]);
 check('Each prepared ticker is added without discarding already readable content',()=>assert.deepEqual(merge(previous,response([b1])).results,[a1,b1]));
 check('A newer immutable result replaces only its own ticker',()=>assert.deepEqual(merge(response([a1,b1]),response([a2])).results,[a2,b1]));
 check('A late older version cannot overwrite the new result or its valid N',()=>assert.deepEqual(merge(response([a2,b1]),response([a1])).results,[a2,b1]));
 check('Late publishing an older dataset cannot replace a newer dataset',()=>{const newest={...a2,data_published_at:'2026-09-14T12:01:00Z'};const late={...a1,created_at:'2026-09-14T12:05:00Z',data_published_at:'2026-09-14T12:00:00Z'};assert.strictEqual(merge(response([newest]),response([late])).results[0],newest)});
 check('Unchanged immutable item keeps referential identity for chart rendering',()=>assert.strictEqual(merge(previous,response([{...a1}])).results[0],a1));
 check('Current-input validity is refreshed even when saved result id is unchanged',()=>assert.equal(merge(previous,response([{...a1,is_current:false}])).results[0].is_current,false));
 check('New conditions do not inherit stale ticker results',()=>assert.deepEqual(merge(previous,{...response([]), params:{...previous.params,month:10}}).results,[]));
 check('Another research cannot inherit previous charts',()=>assert.deepEqual(merge(previous,{...response([]),id:'research2'}).results,[]));
 check('An explicit frozen older result remains authoritative',()=>assert.deepEqual(merge(response([a2]),response([a1]),'a1').results,[a1]));
 check('Frozen transition fallback requires exactly the intended versions',()=>{assert.equal(matchesAnalysis(previous,'research1','a1'),true);assert.equal(matchesAnalysis(previous,'research1','a2'),false);assert.equal(matchesAnalysis(previous,'research2'),false)});
 check('Older progress response does not reset a newer batch stage',()=>assert.equal(merge({...response([a2],'2026-09-14T12:02:00Z'),status:'SUCCEEDED'},{...response([a1]),status:'RUNNING'}).status,'SUCCEEDED'));
 check('Result version set is deterministic across ticker order',()=>assert.equal(resultSet([b1,a1]),resultSet([a1,b1])));
 const currentComplete = {...a2, symbol:'ADBE', coverage:{complete:true}, is_current:true};
 const scopes = [
   {symbol:'ADBE',status:'RUNNING',progress:{stage:'等待可执行任务'}},
   {symbol:'IBM',status:'RUNNING',progress:{stage:'正在获取历史行情'}},
 ];
 const progress = (results=[currentComplete], items=scopes) => remainingAnalysisProgress({ ...response(results), params:{tickers:['ADBE','IBM']}, batch:{items} });
 check('Published complete ADBE is not selected again while its scope status lags',()=>{
   const pending=progress();assert.deepEqual(pending.completedSymbols,['ADBE']);assert.deepEqual(pending.remainingSymbols,['IBM']);assert.equal(pending.scope.symbol,'IBM');assert.equal(pending.stage,'IBM · 正在获取历史行情');
 });
 check('A partial published result remains unfinished even when it is current',()=>{
   const pending=progress([{...currentComplete,coverage:{complete:false}}]);assert.deepEqual(pending.completedSymbols,[]);assert.deepEqual(pending.remainingSymbols,['ADBE','IBM']);
 });
 check('A complete result from old inputs does not count as current completion',()=>assert.deepEqual(progress([{...currentComplete,is_current:false}]).completedSymbols,[]));
 check('Lagging remaining scope with no concrete stage gets a truthful confirmation message',()=>{
   const pending=progress(undefined,[scopes[0],{...scopes[1],progress:{stage:'等待可执行任务'}}]);assert.equal(pending.stage,'IBM · 正在确认其余结果');
 });
 check('Missing remaining scope does not fall back to the completed ticker',()=>assert.equal(progress(undefined,[scopes[0]]).stage,'IBM · 正在确认其余结果'));
 check('When all results arrive before scope convergence, no ticker is falsely shown as remaining',()=>{
   const pending=progress([currentComplete,{...currentComplete,symbol:'IBM'}]);assert.deepEqual(pending.remainingSymbols,[]);assert.equal(pending.stage,'结果已就绪，正在确认任务完成');
 });
 check('Unknown legacy coverage is not represented as a known gap',()=>{
   const pending=progress([{...currentComplete,coverage:null}]);assert.deepEqual(pending.unknownSymbols,['ADBE']);assert.deepEqual(pending.gapSymbols,[]);
 });
 check('A later daily cutoff preserves reading conditions while a changed month does not',()=>{
   const a={month:9,cutoff_date:'2026-09-14',price_range:{end:'old'}};
   assert.equal(analysisConditions(a),analysisConditions({...a,cutoff_date:'2026-09-21',price_range:{end:'new'}}));
   assert.notEqual(analysisConditions(a),analysisConditions({...a,month:10}));
 });
 check('Rechecked coverage replaces unknown metadata without replacing the frozen financial object',()=>{
   const old=response([{...a1,coverage:null,coverage_basis:'unknown'}]);
   const next=merge(old,response([{...a1,coverage:{complete:true},coverage_basis:'rechecked_frozen_dataset'}]));
   assert.equal(next.results[0].coverage.complete,true);assert.strictEqual(next.results[0].data,old.results[0].data);
 });
 console.log(JSON.stringify({passed:checks.length,checks},null,2));
})().catch(e=>{console.error(e);process.exitCode=1});
