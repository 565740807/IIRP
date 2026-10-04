// Ported from sealed G regressions; assertions retained. Synthetic HTTP/PG only.
const {chromium,loadFixture}=require('../common.cjs');
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const phase=process.argv[2];assert(phase);const output=process.env.IIRP_BROWSER_OUTPUT;
(async()=>{const f=await loadFixture();assert(f.synthetic&&f.database.startsWith('iirp_v1_test_'));
await fs.mkdir(output,{recursive:false});const browser=await chromium.launch({headless:true});const report={phase,browser:browser.version(),cases:[],errors:[]};
try{for(const width of [1366,1440,1920])for(const kind of ['events','monthly','interval','earnings']){
 const c={width,height:width===1366?768:1080,kind};report.cases.push(c);if(width===1440)c.height=900;
 const context=await browser.newContext({viewport:{width,height:c.height},locale:'zh-CN',acceptDownloads:true});const page=await context.newPage();page.setDefaultTimeout(15000);page.on('pageerror',e=>report.errors.push(String(e)));
 await context.route('**/*',r=>new URL(r.request().url()).origin===f.base?r.continue():r.abort());
 try{const view=kind==='events'?f.events[0]:f.native[kind],ids=kind==='events'?view.result_id:view.results.map(r=>r.result_id).join(',');
 const q=new URLSearchParams({a:view.id,...(kind==='events'?{set:view.params.event_set_id,version:String(view.params.event_version),result:ids}:{result_ids:ids})});
 const calls=[];page.on('response',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/v1/'))calls.push({path:u.pathname+u.search,status:r.status()})});
 await page.goto(`${f.base}/analysis/${kind}?${q}`);await page.locator('canvas').first().waitFor();assert.match(await page.title(),/IIRP/);assert((await page.locator('main').innerText()).length>500);assert.equal(await page.locator('vite-error-overlay').count(),0);
 await page.screenshot({path:path.join(output,`${kind}-${width}-top.png`)});
 const canvas=page.locator('canvas').first();await canvas.scrollIntoViewIfNeeded();const handle=await canvas.elementHandle();const box=await canvas.boundingBox();await page.mouse.move(box.x+box.width*.55,box.y+box.height*.5);
 const before=await page.evaluate(()=>scrollY);await page.waitForTimeout(2400);assert(await handle.evaluate(e=>e.isConnected),'Task polling must not remount the chart');assert(Math.abs((await page.evaluate(()=>scrollY))-before)<3,'Polling must not move reading position');
 await page.screenshot({path:path.join(output,`${kind}-${width}-chart.png`)});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'No page-wide horizontal overflow');
 await page.keyboard.press('Tab');c.keyboardFocus=await page.evaluate(()=>document.activeElement?.tagName);assert.notEqual(c.keyboardFocus,'BODY');
 // CDP visual scale only; native browser menu zoom requires a separate headed check.
 const session=await context.newCDPSession(page);await session.send('Emulation.setPageScaleFactor',{pageScaleFactor:1.25});await page.waitForTimeout(100);assert(await canvas.isVisible());await session.send('Emulation.setPageScaleFactor',{pageScaleFactor:1});await page.setViewportSize({width:width-100,height:c.height});await page.waitForTimeout(100);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.setViewportSize({width,height:c.height});
 const prefix=kind==='events'?'/api/v1/events/analyses/':'/api/v1/analyses/';const param=kind==='events'?'result_id':'result_ids';c.resultIds=ids;c.exports=[];
 for(const format of ['json','csv']){const exportUrl=f.base+prefix+view.id+'/export?'+new URLSearchParams({[param]:ids,format});const response=await context.request.get(exportUrl);assert(response.ok());const bytes=await response.body();assert(bytes.length>0);await fs.writeFile(path.join(output,`${kind}-${width}.${format}`),bytes);c.exports.push({format,bytes:bytes.length,sha256:crypto.createHash('sha256').update(bytes).digest('hex')});}
 const links=await page.locator(`a[href*="/analyses/${view.id}/export"]`).evaluateAll(es=>es.map(e=>e.getAttribute('href')));assert(links.length>=2);assert(links.every(link=>new URL(link,locationBase()).searchParams.get(param)===ids));
 const png=page.getByRole('button',{name:'导出此图 PNG',exact:true}).first();assert(await png.count(),'Frozen PNG control is required');{const download=page.waitForEvent('download');await png.click();await(await download).saveAs(path.join(output,`${kind}-${width}.png`));}c.requests=calls;c.pass=true;console.log('PASS',kind,width);
 }catch(e){c.pass=false;c.error=String(e);await page.screenshot({path:path.join(output,`${kind}-${width}-failure.png`)}).catch(()=>{});console.log('FAIL',kind,width,String(e));}finally{await context.close();await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));}
}}
finally{await browser.close();report.pass=report.cases.every(c=>c.pass)&&!report.errors.length;await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));if(!report.pass)process.exitCode=1;}
function locationBase(){return f.base}
})().catch(e=>{console.error(e);process.exitCode=1});
