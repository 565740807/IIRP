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
 const h=transport();const a=mount(h.client,options.eventResearchOptions('a'));const b=mount(h.client,options.eventResearchOptions('a'));
 assert.equal(h.calls.length,1);assert(h.calls[0].init.signal instanceof AbortSignal);
 h.calls[0].gate.resolve(eventValue());await pause();assert.equal(a.observer.getCurrentResult().data,b.observer.getCurrentResult().data);
 assert(h.calls[0].bytes>0);a.unsubscribe();b.unsubscribe();
 const c=mount(h.client,options.eventResearchOptions('a'));await pause();assert.equal(h.calls.length,1);c.unsubscribe();h.close();
});

test('task progress marks only the same research stale',()=>{
 assert.equal(keys.mutableResearchForBatch(keys.researchKeys.analysis('a'),'a'),true);
 assert.equal(keys.mutableResearchForBatch(keys.researchKeys.events('a'),'a'),true);
 assert.equal(keys.mutableResearchForBatch(keys.researchKeys.events('b'),'a'),false);
});

test('a failed immutable read can explicitly retry; cancellation never posts task actions',async()=>{
 const h=transport();const a=mount(h.client,options.eventResearchOptions('a'));h.calls[0].gate.resolve('abort');await pause();
 assert(a.observer.getCurrentResult().isError);
 const retry=a.observer.refetch();h.calls[1].gate.resolve(eventValue());await retry;assert(a.observer.getCurrentResult().isSuccess);
 a.unsubscribe();const b=mount(h.client,options.eventResearchOptions('b'));b.unsubscribe();await pause();
 assert(h.calls[2].init.signal.aborted);assert(h.calls.every(x=>x.init.method==='GET'));h.close();
});
