import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildSync } from 'esbuild';
import { QueryClient, QueryObserver, focusManager } from '@tanstack/react-query';
import { fileURLToPath } from 'node:url';

const { outputFiles } = buildSync({entryPoints:[fileURLToPath(new URL('../src/researchQueries.ts', import.meta.url))], bundle:true, write:false, platform:'node', format:'esm'});
const options = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);
const {outputFiles: identityCode} = buildSync({entryPoints:[fileURLToPath(new URL('../src/queryIdentity.ts', import.meta.url))], bundle:true, write:false, platform:'node', format:'esm'});
const keys = await import(`data:text/javascript;base64,${Buffer.from(identityCode[0].text).toString('base64')}`);
const pause = () => new Promise(resolve => setTimeout(resolve, 10));
const deferred = () => { let resolve; const promise = new Promise(r => resolve=r); return {promise,resolve}; };
const eventValue = (id='a', result='r') => ({id,batch_id:'batch-'+id,status:'RUNNING',params:{},result_id:result,data:{metadata:{},source:{note:'完整人工备注'}},progress:[],results:[{id:result}]});
function transport() {
  const calls=[];
  globalThis.document={hidden:false};
  globalThis.fetch=(url,init)=>{
    const gate=deferred(); const call={url,init,gate,bytes:0};calls.push(call);
    init.signal?.addEventListener('abort',()=>gate.resolve('abort'));
    return gate.promise.then(value=>{
      if(value==='abort') throw new DOMException('cancelled','AbortError');
      const text=JSON.stringify(value); call.bytes=Buffer.byteLength(text);return new Response(text,{status:200});
    });
  };
  const client=new QueryClient({defaultOptions:{queries:{retry:false,gcTime:Infinity}}});
  return {calls,client,close:()=>client.clear()};
}
function mount(client, definition) {
  const observer=new QueryObserver(client, definition);const states=[];
  const unsubscribe=observer.subscribe(state=>states.push(state));
  return {observer,states,unsubscribe};
}

test('Events: simultaneous consumers share one actual fetch and warm remount sends no GET',async()=>{
 const h=transport();const a=mount(h.client,options.eventResearchOptions('a','r'));const b=mount(h.client,options.eventResearchOptions('a','r'));
 assert.equal(h.calls.length,1);assert(h.calls[0].init.signal instanceof AbortSignal);
 h.calls[0].gate.resolve(eventValue());await pause();assert.equal(a.observer.getCurrentResult().data,b.observer.getCurrentResult().data);
 assert(h.calls[0].bytes>0);a.unsubscribe();b.unsubscribe();
 const c=mount(h.client,options.eventResearchOptions('a','r'));await pause();assert.equal(h.calls.length,1);c.unsubscribe();h.close();
});

test('immutable reads ignore invalidation/focus; dynamic batch remains refreshable with visible failure/recovery',async()=>{
 const h=transport();h.client.mount();const a=mount(h.client,options.eventResearchOptions('a','r'));h.calls[0].gate.resolve(eventValue());await pause();
 const batch=mount(h.client,options.batchOptions('batch-a'));h.calls[1].gate.resolve({batch_id:'batch-a',batch:{id:'batch-a',status:'RUNNING',items:[]}});await pause();
 const original=a.observer.getCurrentResult().data;
 const pending=h.client.invalidateQueries();await pause();assert.equal(h.calls.length,3);assert.match(h.calls[2].url,/batches/);
 h.calls[2].gate.resolve({batch_id:'batch-a',batch:{id:'batch-a',status:'PARTIAL',items:[{wait_reason:'benchmark_missing_prices'}]}});await pending;
 focusManager.setFocused(false);focusManager.setFocused(true);await pause();assert.equal(h.calls.length,3);
 assert.equal(a.observer.getCurrentResult().data,original);assert.equal(batch.observer.getCurrentResult().data.batch.status,'PARTIAL');
 assert.equal(options.eventResearchOptions('a','r').refetchInterval({state:{data:eventValue()}}),false);
 assert.equal(options.eventResearchOptions('a').refetchInterval({state:{data:eventValue()}}),2000);
 const retry=batch.observer.refetch();h.calls[3].gate.resolve('abort');await retry;assert.equal(batch.observer.getCurrentResult().isError,true);assert.equal(a.observer.getCurrentResult().data,original);
 const recovered=batch.observer.refetch();h.calls[4].gate.resolve({batch_id:'batch-a',batch:{id:'batch-a',status:'SUCCEEDED',items:[]}});await recovered;assert.equal(batch.observer.getCurrentResult().isError,false);
 a.unsubscribe();batch.unsubscribe();h.client.unmount();h.close();
});

test('frozen result set identity canonicalizes order only; securities/research/event/cursor stay isolated',async()=>{
 const h=transport();
 const one=h.client.fetchQuery(options.nativeResearchOptions('a','r2,r1'));
 const same=h.client.fetchQuery(options.nativeResearchOptions('a','r1,r2,r1'));
 assert.equal(h.calls.length,1);assert.match(h.calls[0].url,/r1%2Cr2/);
 const data={id:'a',params:{kind:'monthly',tickers:['X','Y']},batch:{updated_at:'2026-01-01'},results:[]};
 h.calls[0].gate.resolve(data);assert.deepEqual(await one,await same);
 assert.notDeepEqual(keys.researchKeys.native('a',','),keys.researchKeys.native('a',''));
 assert.notDeepEqual(keys.researchKeys.native('a','r1'),keys.researchKeys.native('a','r2'));
 assert.notDeepEqual(keys.researchKeys.native('a','r1'),keys.researchKeys.native('b','r1'));
 const p=mount(h.client,options.overlapOptions('a','r1','event-1',null));
 const old=h.calls[1];p.observer.setOptions(options.overlapOptions('a','r2','event-2','page2'));
 assert(old.init.signal.aborted);h.calls[2].gate.resolve({result_id:'r2',event_key:'event-2',items:['new'],offset:50});await pause();
 old.gate.resolve({result_id:'r1',items:['old']});await pause();
 assert.equal(p.observer.getCurrentResult().data.result_id,'r2');assert.equal(h.calls.every(x=>x.init.method==='GET'),true);
 assert.equal(keys.mutableResearchForBatch(keys.researchKeys.native('a'),'a'),true);
 assert.equal(keys.mutableResearchForBatch(keys.researchKeys.native('a','r1'),'a'),false);
 assert.equal(keys.mutableResearchForBatch(keys.researchKeys.events('b'),'a'),false);
 p.unsubscribe();h.close();
});

test('a failed immutable read can explicitly retry; cancellation never posts task actions',async()=>{
 const h=transport();const a=mount(h.client,options.eventResearchOptions('a','r'));h.calls[0].gate.resolve('abort');await pause();
 assert(a.observer.getCurrentResult().isError);
 const retry=a.observer.refetch();h.calls[1].gate.resolve(eventValue());await retry;assert(a.observer.getCurrentResult().isSuccess);
 a.unsubscribe();const b=mount(h.client,options.eventResearchOptions('b','other'));b.unsubscribe();await pause();
 assert(h.calls[2].init.signal.aborted);assert(h.calls.every(x=>x.init.method==='GET'));h.close();
});
