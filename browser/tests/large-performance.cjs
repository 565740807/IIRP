/* Generated isolated data only. Starts no worker and performs no collection. */
const {chromium,loadFixture} = require('../common.cjs');
const fs = require('node:fs/promises');
const path = require('node:path');
const {createHash} = require('node:crypto');
const assert = require('node:assert/strict');
const base = process.env.IIRP_UI_URL;
const out = process.env.IIRP_UI_OUTPUT;
assert(base && out && process.env.IIRP_UI_SYNTHETIC === '1', 'Requires explicit isolated benchmark server');
const samples = Number(process.env.IIRP_PERFORMANCE_SAMPLES || 100);
const report = { synthetic: true, mocked: false, worker_started: false, samples,
  claim_boundary: 'Real headless Chrome on isolated generated data; 50 inert jobs are not actual concurrent downloads. Navigation includes browser automation and two animation frames; database buffers are warm; the local-only request guard disables the browser HTTP cache.',
  viewports: [], errors: [], foreign_requests: [], writes: [], artifacts: [] };
const stats = (values) => {
  const a = values.toSorted((x,y) => x-y);
  return { samples:a.length, p50_ms:+((a[Math.floor((a.length-1)/2)]+a[Math.floor(a.length/2)])/2).toFixed(3),
    p95_ms:+a[Math.ceil(a.length*.95)-1].toFixed(3), max_ms:+a.at(-1).toFixed(3) };
};
let browser;
async function painted(page) { await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))); }
async function ready(page, kind) {
  const selector = kind === 'home' || kind === 'feed' ? '.feed-card' : 'main tbody tr';
  await page.waitForFunction(selector => { const row=document.querySelector(selector); return row && row.getBoundingClientRect().height > 0; }, selector, {polling:'raf'});
  await painted(page);
}
async function navigate(page, route, kind) {
  if (page.url().startsWith(base)) page.observedLongTasks.push(...await page.evaluate(() => window.__longTasks || []));
  const started = performance.now();
  await page.goto(base + route, {waitUntil:'domcontentloaded'});
  await ready(page, kind);
  return performance.now()-started;
}
async function shot(page, name) {
  const file = path.join(out, name + '.png'); await page.screenshot({path:file}); report.artifacts.push(file);
}
(async () => {
  await loadFixture();
  await fs.mkdir(out, {recursive:true});
  const html = await fs.readFile(path.join(__dirname,'../../frontend/dist/index.html'));
  report.build = {index_sha256:createHash('sha256').update(html).digest('hex'), asset_paths:[...html.toString().matchAll(/(?:src|href)="([^"]+)"/g)].map(m=>m[1])};
  browser = await chromium.launch({headless:true});
  for (const viewport of [{width:1366,height:768},{width:1440,height:900}]) {
    const context = await browser.newContext({viewport,locale:'zh-CN'});
    await context.route('**/*', route => {
      const request = route.request();
      if (!request.url().startsWith(base + '/')) { report.foreign_requests.push(request.url()); return route.abort(); }
      if (!['GET','HEAD'].includes(request.method())) { report.writes.push({url:request.url(),method:request.method()}); return route.abort(); }
      return route.continue();
    });
    await context.addInitScript(() => {
      window.__longTasks = [];
      new PerformanceObserver(list => window.__longTasks.push(...list.getEntries().map(e => e.duration))).observe({type:'longtask',buffered:true});
    });
    const page = await context.newPage(); page.observedLongTasks = []; page.setDefaultTimeout(30000);
    page.on('pageerror', e => report.errors.push(String(e)));
    page.on('console', m => { if(m.type()==='error') report.errors.push(m.text()); });
    const result = {viewport, first_navigation_ms:{}, navigation:{}, groups:[], interactions:{}, long_tasks:[]};
    report.viewports.push(result);
    for (const [label,route,kind] of [
      ['home','/','home'], ['company_recent50','/companies/0000000001?recent_count=50','entity'],
      ['person_recent50','/people/0000000999?recent_count=50','entity'],
      ['company_three_month','/companies/0000000001','entity'], ['person_three_month','/people/0000000999','entity']
    ]) {
      result.first_navigation_ms[label] = +(await navigate(page,route,kind)).toFixed(3);
      // Wide first-open cost is reported once; 100 fresh and frozen API requests
      // are measured separately, without fabricating a browser cache hit.
      if(label.endsWith('three_month')) continue;
      const values=[];
      for(let i=0;i<samples;i++) values.push(await navigate(page,route,kind));
      result.navigation[label]=stats(values);
      if(kind==='home') await shot(page,`home-${viewport.width}`);
      console.log(viewport.width,label,JSON.stringify(result.navigation[label]));
    }
    await navigate(page,'/insiders','feed');
    const unique = new Set();
    for(let index=0;index<15;index++) {
      await ready(page,'feed');
      const counts = await page.evaluate(() => ({
        dom:document.querySelectorAll('*').length, cards:document.querySelectorAll('.feed-card').length,
        rows:document.querySelectorAll('.feed-card tbody tr').length,
        overflow:document.documentElement.scrollWidth > innerWidth,
        heap:performance.memory?.usedJSHeapSize ?? null,
        identity:[...document.querySelectorAll('.feed-card > summary')].map(el=>el.innerText)
      }));
      counts.identity.forEach(key=>unique.add(key)); delete counts.identity;
      assert.equal(counts.cards,20); assert.equal(counts.overflow,false);
      result.groups.push({page:index+1,...counts});
      if(index<14) {
        await page.locator('.feed-panel > .pagination').getByRole('button',{name:'下一页',exact:true}).click();
        await page.locator('.feed-panel > .pagination').getByText(`第 ${index+2} 页`,{exact:true}).waitFor();
      }
    }
    result.unique_groups = unique.size;
    assert.equal(unique.size,300);
    assert(Math.max(...result.groups.map(g=>g.dom)) <= result.groups[0].dom+100, 'DOM must stay bounded after 300 groups');
    await page.evaluate(()=>scrollTo(0,0)); await painted(page); await shot(page,`feed-page15-${viewport.width}`);
    const expanded=[];
    const card=page.locator('.feed-card').first();
    for(let i=0;i<samples;i++) {
      const start=performance.now(); await card.locator('summary').click();
      await card.locator('tbody tr').first().waitFor({state:'visible'}); await painted(page);
      expanded.push(performance.now()-start); await card.locator('summary').click();
    }
    result.interactions.expand=stats(expanded);
    await page.locator('.feed-panel > .pagination').getByRole('button',{name:'上一页',exact:true}).focus();
    await page.keyboard.press('Enter');
    await page.locator('.feed-panel > .pagination').getByText('第 14 页',{exact:true}).waitFor();
    await ready(page,'feed'); result.keyboard_previous_page=true;
    const filtered=[];
    for(let i=0;i<samples;i++) {
      const label=i%2 ? '全部申报' : '买入';
      const response=page.waitForResponse(r=>r.url().includes('/api/v1/feed?') && r.status()===200);
      const start=performance.now();
      await page.getByRole('button',{name:label,exact:true}).click(); await response; await ready(page,'feed');
      filtered.push(performance.now()-start);
    }
    result.interactions.filter=stats(filtered);
    await navigate(page,'/companies/0000000001?recent_count=50','entity');
    const submitted=[];
    for(let i=0;i<samples;i++) {
      await page.getByLabel('最近条数（留空按日期）',{exact:true}).fill(String(i%2 ? 50 : 49));
      const expected=i%2 ? 50 : 49;
      const start=performance.now();
      await page.getByRole('button',{name:'应用本地筛选',exact:true}).click();
      await page.waitForFunction(n=>document.querySelectorAll('main tbody tr').length===n, expected, {polling:'raf'});
      await painted(page);
      submitted.push(performance.now()-start);
    }
    result.interactions.local_filter_submit=stats(submitted);
    result.long_tasks.push(...page.observedLongTasks,...await page.evaluate(()=>window.__longTasks));
    result.long_tasks = {count:result.long_tasks.length,max_ms:Math.max(0,...result.long_tasks),over_200_ms:result.long_tasks.filter(n=>n>200).length};
    // Inspect the existing route return behavior separately from DOM bounds.
    await navigate(page,'/insiders','feed');
    for(let i=1;i<15;i++) {
      await page.locator('.feed-panel > .pagination').getByRole('button',{name:'下一页',exact:true}).click();
      await page.locator('.feed-panel > .pagination').getByText(`第 ${i+1} 页`,{exact:true}).waitFor();
      await ready(page,'feed');
    }
    const returningCard=page.locator('.feed-card').nth(2);
    if (!(await returningCard.evaluate(el=>el.open))) await returningCard.locator('summary').click();
    await returningCard.locator('tbody tr').first().waitFor({state:'visible'});
    const companyLink=returningCard.locator('a[href^="/companies/"]').first();
    await companyLink.scrollIntoViewIfNeeded(); await painted(page);
    const beforeReturn=await page.evaluate(()=>({url:location.href,scroll:scrollY,first:document.querySelector('.feed-card')?.dataset.groupId,opened:[...document.querySelectorAll('.feed-card[open]')].map(el=>({id:el.dataset.groupId,first:el.querySelector('tbody tr')?.textContent})),page:document.querySelector('.feed-panel > .pagination span')?.textContent,storage:Object.fromEntries(Object.entries(sessionStorage).filter(([key])=>key.startsWith('iirp.scroll.')))}));
    await companyLink.click(); await ready(page,'entity');
    await page.goBack({waitUntil:'domcontentloaded'}); await ready(page,'feed');
    const afterReturn=await page.evaluate(()=>({url:location.href,scroll:scrollY,first:document.querySelector('.feed-card')?.dataset.groupId,opened:[...document.querySelectorAll('.feed-card[open]')].map(el=>({id:el.dataset.groupId,first:el.querySelector('tbody tr')?.textContent})),page:document.querySelector('.feed-panel > .pagination span')?.textContent,storage:Object.fromEntries(Object.entries(sessionStorage).filter(([key])=>key.startsWith('iirp.scroll.')))}));
    result.return_position = { before:beforeReturn,after:afterReturn,passed:beforeReturn.url===afterReturn.url && beforeReturn.page===afterReturn.page && beforeReturn.first===afterReturn.first && JSON.stringify(beforeReturn.opened)===JSON.stringify(afterReturn.opened) && Math.abs(beforeReturn.scroll-afterReturn.scroll)<5 };
    await context.close();
  }
  const finalHtml=await fs.readFile(path.join(__dirname,'../../frontend/dist/index.html'));
  assert.equal(createHash('sha256').update(finalHtml).digest('hex'),report.build.index_sha256,'Build changed during the benchmark');
  assert.equal(report.errors.length,0); assert.equal(report.foreign_requests.length,0); assert.equal(report.writes.length,0);
})().catch(error=>{report.failure=String(error.stack||error);process.exitCode=1;}).finally(async()=>{
  if(browser) await browser.close(); await fs.mkdir(out,{recursive:true});
  await fs.writeFile(path.join(out,'report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify({out,failure:report.failure,viewports:report.viewports.length}));
});
