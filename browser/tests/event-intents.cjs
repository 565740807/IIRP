// Ported from sealed G regressions; assertions retained. Synthetic HTTP/PG only.
const {chromium,loadFixture}=require('../common.cjs');
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
const phase=process.argv[2];assert(phase);const manifest=process.env.IIRP_INTENT_MANIFEST;assert(manifest);const out=process.env.IIRP_INTENT_OUTPUT||path.join('/tmp',`iirp-event-intents-${phase}`);
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const f=await loadFixture();assert(f.synthetic&&f.no_external_provider_calls&&f.database.startsWith('iirp_v1_test_'));assert.equal(new URL(f.base).hostname,'127.0.0.1');assert.notEqual(new URL(f.base).port,'18081');
 await fs.mkdir(out,{recursive:false});const browser=await chromium.launch({headless:true});
 const report={phase,database:f.database,browser:browser.version(),cases:[],errors:[]};
 const url=v=>`${f.base}/analysis/events?${new URLSearchParams({set:v.params.event_set_id,a:v.id,version:String(v.params.event_version),result:v.result_id})}`;
 try{
  for(const test of ['cached-navigation','late-create-failure','late-create-success','lost-response-retry','edited-then-failure']){
   const item={name:test};report.cases.push(item);const ctx=await browser.newContext({viewport:{width:1440,height:900},locale:'zh-CN'});
   const page=await ctx.newPage();page.setDefaultTimeout(15000);page.on('pageerror',e=>report.errors.push(String(e)));
   let release=()=>{};
   try{
    await ctx.route('**/*',r=>new URL(r.request().url()).origin===f.base?r.continue():r.abort());
    await ctx.route('**/api/v1/analyses/*/refresh*',r=>r.abort());
    const v=f.events[0];await page.goto(url(v));await page.locator('.event-dates-result canvas').first().waitFor();
    const source=page.locator('#event-source-management');await source.locator(':scope > summary').click();
    const years=page.getByLabel('目标历史年数（完整导入范围仍保留）',{exact:true});
    const saved=source.locator('details').filter({has:page.locator('summary').filter({hasText:'已保存分析版本'})}).last();
    await saved.locator(':scope > summary').click();
    const buttons=saved.getByRole('button');const collection=await(await page.request.get(`${f.base}/api/v1/events/sets/${v.params.event_set_id}?version=1`)).json();const first=collection.analyses.findIndex(x=>x.id===f.events[0].id),second=collection.analyses.findIndex(x=>x.id===f.events[1].id);assert(first>=0&&second>=0);
    await buttons.nth(first).click();await page.waitForURL(u=>u.searchParams.get('a')===v.id&&!u.searchParams.has('result'));await pause(600);await years.fill('3');
    if(test==='cached-navigation'){
     // Select this same cached request explicitly: restore its saved conditions.
     await buttons.nth(first).click();
     await pause(700);item.expected=v.params.historical_years;item.actual=await years.inputValue();item.dirty=(await page.locator('main').innerText()).includes('研究条件有未应用改动');
     await page.screenshot({path:path.join(out,test+'.png')});assert.equal(item.actual,String(item.expected));assert.equal(item.dirty,false);
    }else{
     let arrived;const held=new Promise(r=>arrived=r);const gate=new Promise(r=>release=r);
     await page.route('**/api/v1/events/sets/*/analyses',async route=>{if(route.request().method()!=='POST')return route.continue();item.request=route.request().postDataJSON();let response;if(test==='late-create-success'||test==='lost-response-retry'){response=await route.fetch();assert(response.ok());item.accepted=await response.json();}arrived();await gate;if(response&&test==='late-create-success')return route.fulfill({response});await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'synthetic delayed start failure'})});});
     await page.getByRole('button',{name:'更新行情并生成新分析',exact:true}).click();await held;
     // The second saved research shares a workspace, but has a different intent.
     if(test.startsWith('late-create-')){await buttons.nth(second).click();await page.waitForURL(u=>u.searchParams.get('a')===f.events[1].id);}else if(test==='edited-then-failure'){await years.fill('4');}
     release();await pause(900);item.url=page.url();item.continueVisible=await page.getByRole('button',{name:'继续启动分析',exact:true}).isVisible();item.actual=await years.inputValue();
     await page.screenshot({path:path.join(out,test+'.png')});
     if(test.startsWith('late-create-')){
      assert.equal(new URL(page.url()).searchParams.get('a'),f.events[1].id);assert.equal(item.continueVisible,false,'Old command must not become the current request');assert.equal(item.actual,String(f.events[1].params.historical_years));
     } else if(test==='edited-then-failure'){
      assert.equal(item.actual,'4');assert.equal(item.continueVisible,false);await page.reload();await source.locator(':scope > summary').click();assert.equal(await years.inputValue(),'4','Reload preserves the newer draft');
     }
     if(test!=='late-create-success'){
      // A lost response retries the accepted command, never a fresh payload.
      await page.unroute('**/api/v1/events/sets/*/analyses');
      if(test==='lost-response-retry'){await page.reload();await page.getByRole('button',{name:'继续启动分析',exact:true}).waitFor();}
      const retry=page.getByRole('button',{name:'继续启动分析',exact:true});
      if(!await retry.isVisible())await page.getByText('之前条件的分析启动记录',{exact:true}).click();
      const received=page.waitForResponse(r=>r.request().method()==='POST'&&/\/events\/sets\/[^/]+\/analyses$/.test(new URL(r.url()).pathname));
      await (await retry.isVisible()?retry:page.getByRole('button',{name:'恢复之前的分析启动',exact:true})).click();const response=await received;assert(response.ok());assert.deepEqual(response.request().postDataJSON(),item.request);item.recovered=await response.json();
      if(item.accepted)assert.equal(item.recovered.analysis_id,item.accepted.analysis_id);
      await page.waitForURL(u=>u.searchParams.get('a')===item.recovered.analysis_id);await pause(400);assert.equal(await page.getByRole('button',{name:'继续启动分析',exact:true}).isVisible(),false);if(test==='edited-then-failure'){item.afterRecovery=await years.inputValue();item.dirtyAfterRecovery=(await page.locator('main').innerText()).includes('研究条件有未应用改动');assert.equal(item.afterRecovery,'4');assert.equal(item.dirtyAfterRecovery,true);}
     }
    }
    item.pass=true;
   }catch(e){item.pass=false;item.error=String(e);await page.screenshot({path:path.join(process.env.IIRP_BROWSER_OUTPUT,(item.name||item.mode||'case')+'-failure.png')}).catch(()=>{});console.log(test,String(e));}finally{release();await ctx.close();}
  }
 }finally{await browser.close();report.pass=report.cases.every(c=>c.pass)&&report.errors.length===0;await fs.writeFile(path.join(out,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));if(!report.pass)process.exitCode=1;}
})().catch(e=>{console.error(e);process.exitCode=1});
