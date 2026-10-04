// Ported from sealed G regressions; assertions retained. Synthetic HTTP/PG only.
const {chromium,loadFixture}=require('../common.cjs');
const {buildSync}=require('../../frontend/node_modules/esbuild');
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{const f=await loadFixture();assert(f.synthetic&&f.database.startsWith('iirp_v1_test_'));
await fs.mkdir(process.env.IIRP_BROWSER_OUTPUT,{recursive:false});
const b=await chromium.launch({headless:true});const report={browser:b.version(),checks:[]};
try{const context=await b.newContext();const page=await context.newPage();await page.goto(f.base+'/health/live');
const code=buildSync({entryPoints:['frontend/src/researchStorage.ts'],bundle:true,write:false,format:'iife',globalName:'drafts',platform:'browser'}).outputFiles[0].text;await page.route(f.base+'/__storage_test__.js',r=>r.fulfill({contentType:'application/javascript',body:code}));await page.addScriptTag({url:f.base+'/__storage_test__.js'});
report.checks=await page.evaluate(async()=>{
 const {writeDraft,patchDraft,initializeDraft}=drafts;const keep=['pendingStart'];const check=(ok,name)=>{if(!ok)throw Error(name);checks.push(name)};const checks=[];
 const read=async key=>{const db=await new Promise((r,j)=>{const q=indexedDB.open('iirp-research',1);q.onsuccess=()=>r(q.result);q.onerror=()=>j(q.error)});try{return await new Promise((r,j)=>{const q=db.transaction('drafts').objectStore('drafts').get(key+':'+sessionStorage.getItem('iirp:draft-tab'));q.onsuccess=()=>r(q.result);q.onerror=()=>j(q.error)})}finally{db.close()}};
 await writeDraft('case',{text:'old',pendingStart:{id:'A'}});
 await patchDraft('case',v=>({...v,pendingStart:null}));await writeDraft('case',{text:'new',pendingStart:{id:'A'}},keep);let v=await read('case');check(v.text==='new'&&v.pendingStart===null,'A cleared command is not resurrected by stale autosave');
 await patchDraft('case',v=>({...v,pendingStart:{id:'A',error:'failed'}}));await writeDraft('case',{text:'newer',pendingStart:null},keep);v=await read('case');check(v.text==='newer'&&v.pendingStart.id==='A','Autosave preserves failed command and new draft');
 await initializeDraft('case',{text:'obsolete destination',pendingStart:null});v=await read('case');check(v.text==='newer'&&v.pendingStart.id==='A','Existing destination draft is not overwritten');
 await initializeDraft('new-case',{text:'fresh',pendingStart:null});check((await read('new-case')).text==='fresh','Missing destination is initialized');
 await patchDraft('case',v=>({...v,pendingStart:{id:'B'}}));await patchDraft('case',v=>v.pendingStart?.id==='A'?{...v,pendingStart:null}:v);check((await read('case')).pendingStart.id==='B','Old command CAS cannot clear a newer command');
 for(const order of [0,1]){for(let i=0;i<10;i++){const key='race-'+order+'-'+i;await writeDraft(key,{text:'before',pendingStart:{id:'A'}});const updates=[];const listener=e=>{if(e.detail.key===key)updates.push(e.detail.value)};window.addEventListener('iirp:draft-command',listener);
 const clear=()=>patchDraft(key,v=>({...v,pendingStart:null}));const edit=()=>writeDraft(key,{text:'after',pendingStart:{id:'A'}},keep);await Promise.all(order?[edit(),clear()]:[clear(),edit()]);v=await read(key);window.removeEventListener('iirp:draft-command',listener);if(v.text!=='after'||v.pendingStart!==null)throw Error('race '+order+' '+i);}
 check(true,'Concurrent clear/autosave order '+order+' preserves both edits and command completion (10 runs)');}
 const notifications=[];const listener=e=>{if(e.detail.key==='notifications')notifications.push(e.detail.value.pendingStart?.id??null)};window.addEventListener('iirp:draft-command',listener);await writeDraft('notifications',{text:'state',pendingStart:{id:'A'}});await Promise.all([patchDraft('notifications',v=>({...v,pendingStart:null})),patchDraft('notifications',v=>({...v,pendingStart:{id:'B'}}))]);window.removeEventListener('iirp:draft-command',listener);check(notifications.length===2&&notifications.every(x=>x==='B'),'An older transaction notification cannot hide the newer optimistic command');return checks;
});report.pass=true;}catch(e){report.pass=false;report.error=String(e);throw e;}finally{await b.close();await fs.writeFile(path.join(process.env.IIRP_BROWSER_OUTPUT,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));}})().catch(e=>{console.error(e);process.exitCode=1});
