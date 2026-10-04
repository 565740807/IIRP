// Ported from sealed G regressions; assertions retained. Synthetic HTTP/PG only.
// Actual API response delivery gates; never fabricates research facts or statuses.
// IIRP_RECOVERY_MANIFEST must identify a labelled isolated UX app, not main.
const {chromium,loadFixture}=require('../common.cjs');
const fs = require('node:fs/promises'), path = require('node:path'), assert = require('node:assert/strict');
const manifest = process.env.IIRP_RECOVERY_MANIFEST, output = process.env.IIRP_UI_OUTPUT;
assert(manifest && output, 'Set IIRP_RECOVERY_MANIFEST and fresh IIRP_UI_OUTPUT');
const report = {started:new Date().toISOString(),pass:false,cases:[],errors:[],writes:[]};
const labels = {ticker:'证券代码（最多 20 个）',years:'历史年数',excluded:'排除年份',selectedYears:'指定历史年份（留空使用年数）'};
(async()=>{
 const f=await loadFixture();
 assert(f.synthetic && f.database.startsWith('iirp_v1_test_') && f.no_external_provider_calls);
 const origin=new URL(f.base).origin;assert.equal(new URL(origin).hostname,'127.0.0.1');assert.notEqual(new URL(origin).port,'18081');
 report.fixture={database:f.database,base:origin,synthetic:true};await fs.mkdir(output,{recursive:false});
 const browser=await chromium.launch({headless:true});report.browser=browser.version();
 try {
  for(const kind of ['monthly','interval','earnings']) for(const frozen of [false,true]) {
   const name=kind+(frozen?'-frozen':'-first-read'),item={name,pass:false};report.cases.push(item);
   const context=await browser.newContext({viewport:{width:1366,height:768},locale:'zh-CN',acceptDownloads:true});
   const page=await context.newPage();page.setDefaultTimeout(20000);page.on('pageerror',e=>report.errors.push(String(e)));
   const id=f.analyses[kind],endpoint=origin+'/api/v1/analyses/'+id;
   let original;for(let i=0;i<80;i++){const r=await page.request.get(endpoint);assert(r.ok());original=await r.json();if(original.results.length)break;await page.waitForTimeout(500);}
   assert(original.results.length,'A readable result is required; whole research may still run');
   const resultId=original.results[0].result_id;
   const originalFrozen=await(await page.request.get(endpoint+'?result_ids='+resultId)).json();item.initialStatus=original.status;item.result_id=resultId;
   let release,arrive,held=false;const gate=new Promise(r=>release=r),arrived=new Promise(r=>arrive=r);
   await context.route('**/*',async route=>{
    const req=route.request(),u=new URL(req.url());
    if(u.origin!==origin||req.method()!=='GET'){if(req.method()!=='GET')report.writes.push({name,method:req.method(),path:u.pathname,blocked:true});return route.abort();}
    if(u.pathname==='/api/v1/analyses/'+id&&!held){held=true;const response=await route.fetch();item.delayedResponse=await response.json();arrive();await gate;return route.fulfill({response});}
    return route.continue();
   });
   const values=async()=>({ticker:await page.getByLabel(labels.ticker,{exact:true}).inputValue(),years:await page.getByLabel(labels.years,{exact:true}).inputValue(),benchmark:await page.getByLabel(/^基准对照/).inputValue(),excluded:await page.getByLabel(labels.excluded,{exact:true}).inputValue(),selectedYears:await page.getByLabel(labels.selectedYears,{exact:true}).inputValue()});
   const shot=async suffix=>{await page.screenshot({path:path.join(output,name+'-'+suffix+'.png'),animations:'disabled'});};
   const checkApplied=async()=>{
    await page.waitForFunction(({tickers,years})=>document.querySelector('.ticker-field input')?.value===tickers && [...document.querySelectorAll('label')].find(l=>l.firstChild?.textContent.trim()==='历史年数')?.querySelector('input')?.value===years,{tickers:original.params.tickers.join(', '),years:String(original.params.historical_years)});
    assert.equal(await page.getByLabel(/^基准对照/).inputValue(),original.params.benchmark||'');
   };
   try {
    const params=new URLSearchParams({a:id});if(frozen)params.set('result_ids',resultId);
    await page.goto(origin+'/analysis/'+kind+'?'+params);await arrived;
    for(const d of await page.locator('details').filter({has:page.getByLabel(labels.ticker,{exact:true})}).all())if(!await d.evaluate(e=>e.open))await d.locator(':scope > summary').click();
    await page.getByLabel(labels.ticker,{exact:true}).fill('AAPL');await page.getByLabel(labels.years,{exact:true}).fill('3');await page.getByLabel(/^基准对照/).selectOption('^GSPC');
    await page.locator('.advanced-options > summary').click();await page.getByLabel(labels.excluded,{exact:true}).fill('2020');await page.getByLabel(labels.selectedYears,{exact:true}).fill('2018, 2019, 2020');
    item.draft=await values();await shot('before');release();
    await page.waitForURL(u=>u.searchParams.get('historical_years')===String(original.params.historical_years));
    await page.locator('.statistics-panel').first().waitFor();await page.waitForTimeout(2600);item.after=await values();item.canonical=page.url();
    assert.deepEqual(item.after,item.draft,'Background canonicalization/poll must not discard unapplied conditions');
    assert.match(await page.locator('main').innerText(),/研究条件已修改，尚未应用/);await shot('after');
    if(frozen) assert.equal(new URL(page.url()).searchParams.get('result_ids'),resultId);
    // A user selecting the saved research explicitly does restore its conditions.
    await page.locator('details.result-notes > summary').first().click();
    await page.locator(`a[href="/analysis/${kind}?a=${id}"]`).first().click();await checkApplied();item.explicitSelection=await values();
    // A frozen link has immutable saved conditions; copy into a genuinely new page.
    const copied=new URL(page.url());copied.searchParams.set('result_ids',resultId);
    await page.goto(copied.href);await checkApplied();await page.locator('.statistics-panel').first().waitFor();
    await page.locator('.research-exports > summary').click();const download=page.waitForEvent('download');await page.getByRole('link',{name:'导出当前结果 CSV',exact:true}).click();await(await download).saveAs(path.join(output,name+'-saved.csv'));
    await page.reload();await checkApplied();
    const copyPage=await context.newPage();await copyPage.goto(copied.href);await copyPage.getByLabel(labels.ticker,{exact:true}).waitFor({state:'attached'});await copyPage.waitForURL(u=>u.searchParams.get('historical_years')===String(original.params.historical_years));assert.equal(await copyPage.getByLabel(labels.ticker,{exact:true}).inputValue(),original.params.tickers.join(', '));
    await copyPage.locator('.research-exports > summary').click();const second=copyPage.waitForEvent('download');await copyPage.getByRole('link',{name:'导出当前结果 CSV',exact:true}).click();await(await second).saveAs(path.join(output,name+'-copied.csv'));
    assert((await fs.readFile(path.join(output,name+'-saved.csv'))).equals(await fs.readFile(path.join(output,name+'-copied.csv'))));
    const afterFrozen=await(await page.request.get(endpoint+'?result_ids='+resultId)).json();assert.deepEqual(afterFrozen.results,originalFrozen.results);assert.deepEqual(afterFrozen.params,originalFrozen.params);item.frozenDataUnchanged=true;item.csvIdentical=true;item.pass=true;console.log('PASS '+name);
   } finally {release();await context.close();await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));}
  }
  assert.equal(report.errors.length,0);assert.equal(report.writes.filter(w=>/\/analyses$/.test(w.path)).length,0);report.pass=true;
 } catch(error){report.error=String(error);console.error(error);process.exitCode=1;}
 finally {await browser.close();report.finished=new Date().toISOString();await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));}
})().catch(e=>{console.error(e);process.exitCode=1;});
