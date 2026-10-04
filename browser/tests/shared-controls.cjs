const {chromium, loadFixture} = require('../common.cjs');
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path'), crypto = require('node:crypto');
(async () => {
 const f = await loadFixture(), out = process.env.IIRP_BROWSER_OUTPUT;
 await fs.mkdir(out, {recursive: false});
 const browser = await chromium.launch({headless:true});
 const report = {synthetic:true, cases:[], errors:[]};
 try {
  const ctx = await browser.newContext({viewport:{width:1440,height:900}, acceptDownloads:true});
  await ctx.route('**/*', r => new URL(r.request().url()).origin === f.base ? r.continue() : r.abort());
  const [first, second] = f.shared;
  const headers = {'X-IIRP-Client':'web', Origin:f.base};
  const exportBytes = async (v, format) => {
   const r = await ctx.request.get(`${f.base}/api/v1/events/analyses/${v.id}/export?`+new URLSearchParams({result_id:v.result_id,format}));
   assert(r.ok()); return r.body();
  };
  const action = async (batch, action) => {
   const r = await ctx.request.post(`${f.base}/api/v1/batches/${batch}/actions`, {headers, data:{action}});
   assert(r.ok()); return r.json();
  };
  await action(first.batch_id, 'pause');
  const paused = await (await ctx.request.get(`${f.base}/api/v1/batches/${first.batch_id}`)).json();
  assert.equal(paused.batch.status, 'PAUSED');
  const compute = await ctx.request.post(f.base+'/__iirp_test_compute__', {headers}); assert(compute.ok());
  const get = async a => (await ctx.request.get(`${f.base}/api/v1/events/analyses/${a}`)).json();
  const a = await get(first.analysis_id), b = await get(second.analysis_id);
  assert.equal(a.result_id, null); assert(b.result_id);
  const beforeResume = {json:await exportBytes(b,'json'),csv:await exportBytes(b,'csv')};
  assert.equal((await (await ctx.request.get(`${f.base}/api/v1/batches/${first.batch_id}`)).json()).batch.status, 'PAUSED');
  await action(first.batch_id, 'resume');
  // Run the real planner after the durable resume command; reads stay read-only.
  assert((await ctx.request.post(f.base+'/__iirp_test_compute__', {headers})).ok());
  const deadline=Date.now()+15000;let resumed;
  while(Date.now()<deadline){resumed=await get(first.analysis_id);if(resumed.result_id)break;await new Promise(r=>setTimeout(r,200));}
  assert(resumed.result_id); assert.notEqual(resumed.result_id,b.result_id);
  assert.deepEqual(resumed.data.rows,b.data.rows);
  for (const format of ['json','csv']) assert((await exportBytes(b,format)).equals(beforeResume[format]), 'Resuming another subscriber must preserve every frozen export byte');
  const wrongOwner = await ctx.request.get(`${f.base}/api/v1/events/analyses/${resumed.id}/export?`+new URLSearchParams({result_id:b.result_id,format:'json'}));
  assert.equal(wrongOwner.status(),404,'Another subscriber result must not be exported under this analysis');
  for (const v of [resumed,b]) {
   const page=await ctx.newPage(); page.on('pageerror',e=>report.errors.push(String(e)));
   const frozenResponse=page.waitForResponse(r=>new URL(r.url()).pathname===`/api/v1/events/analyses/${v.id}`&&new URL(r.url()).searchParams.get('result_id')===v.result_id&&r.ok());
   await page.goto(f.base+'/analysis/events?'+new URLSearchParams({set:v.params.event_set_id,a:v.id,version:String(v.params.event_version),result:v.result_id}));
   await page.locator('canvas').first().waitFor();
   const displayed=await(await frozenResponse).json(); assert.equal(displayed.result_id,v.result_id);assert.deepEqual(displayed.data,v.data);
   const detail=page.locator('.event-dates-result details.result-notes').filter({has:page.locator('summary').filter({hasText:'逐年事件与每日涨跌明细'})});
   await detail.locator(':scope > summary').click();
   for(const row of v.data.rows) {assert((await detail.innerText()).includes(row.label));assert((await detail.innerText()).includes(row.anchor.original_date));}
   const exports={};
   for(const format of ['json','csv']) {
    exports[format]=await exportBytes(v,format);await fs.writeFile(path.join(out,v.id+'.'+format),exports[format]);
   }
   const frozen=JSON.parse(exports.json.toString());
   assert.equal(frozen.id,v.id);assert.equal(frozen.batch_id,v.batch_id);assert.equal(frozen.result_id,v.result_id);
   assert.deepEqual(frozen.params,v.params);assert.deepEqual(frozen.data,v.data,'All frozen rows, evidence and source metadata must match the displayed result');
   assert.equal(frozen.inputs.event_version_id,v.data.metadata.event_version_id);
   assert.equal(frozen.inputs.dataset_id,v.data.metadata.dataset_id);
   // The product deliberately prefixes CSV with a UTF-8 BOM for spreadsheets.
   // Decode that signature for column assertions; raw export bytes remain the
   // input for saved evidence, hashes and the before/after stability checks.
   const csv=exports.csv.toString('utf8').replace(/^\uFEFF/, '');assert(csv.startsWith('result_id,event_version_id,dataset_id,'));
   const csvIdentities=[...csv.matchAll(/^([a-f0-9-]{36}),([a-f0-9-]{36}),([a-f0-9-]{36}),/gm)];
   assert(csvIdentities.length>0);for(const match of csvIdentities)assert.deepEqual(match.slice(1),[v.result_id,v.data.metadata.event_version_id,v.data.metadata.dataset_id]);
   assert(csv.includes(',event_evidence,'),'CSV must retain the frozen source evidence');
   const png=page.getByRole('button',{name:'导出此图 PNG',exact:true}).first();const download=page.waitForEvent('download');await png.click();await(await download).saveAs(path.join(out,v.id+'.png'));
   await page.screenshot({path:path.join(out,v.id+'-page.png')});
   report.cases.push({analysis_id:v.id,result_id:v.result_id,frozen_data_and_sources:true,export_sha256:Object.fromEntries(Object.entries(exports).map(([format,bytes])=>[format,crypto.createHash('sha256').update(bytes).digest('hex')])),pass:true});await page.close();
  }
  assert.equal(report.errors.length,0); report.pass=true;await ctx.close();
 } catch(e) {report.error=String(e); report.pass=false;process.exitCode=1;}
 finally {await browser.close();await fs.writeFile(path.join(out,'report.json'),JSON.stringify(report,null,2));}
})().catch(e=>{console.error(e);process.exitCode=1});
