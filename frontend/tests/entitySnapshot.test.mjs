import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { setImmediate } from 'node:timers/promises';
import ts from 'typescript';
import { QueryClient, QueryObserver, onlineManager } from '@tanstack/react-query';
const compiled=ts.transpileModule(readFileSync(new URL('../src/taskPresentation.ts',import.meta.url),'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
const {entitySnapshotParams,entityRequestParams}=await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
for(const kind of ['company','person']) {
  const client=new QueryClient({defaultOptions:{queries:{retry:false,gcTime:Infinity}}});
  const params=new URLSearchParams('action=buy');
  const key=p=>['entity',kind,'123',entityRequestParams(p)];
  const old={data:{session_id:'old-session'},items:[{id:'old'}]};
  const fresh={data:{session_id:'new-session'},items:[{id:'new'}]};
  client.setQueryData(key(params),old,{updatedAt:Date.now()-120_000});
  let release;let baseReads=0;let cursorReads=0;
  const pending=new Promise(resolve=>{release=resolve;});
  const aliases=[];
  const observer=new QueryObserver(client,{queryKey:key(params),staleTime:5000,queryFn:()=>{baseReads++;return pending;}});
  const unsubscribe=observer.subscribe(result=>{
    const next=entitySnapshotParams(params,result.data?.data.session_id,result);
    if(next){aliases.push(next.toString());client.setQueryData(key(next),result.data,{updatedAt:result.dataUpdatedAt});}
  });
  assert.equal(baseReads,1);
  assert.equal(observer.getCurrentResult().data,old,'Existing content stays readable during the check');
  assert.deepEqual(aliases,[],'A stale cached response must not replace the route while a newer reading session is loading');
  release(fresh);await setImmediate();
  assert.equal(aliases.length,1);
  const next=new URLSearchParams(aliases[0]);assert.equal(next.get('cursor'),'new-session:0');
  const aliased=await client.fetchQuery({queryKey:key(next),staleTime:60_000,queryFn:()=>{cursorReads++;return fresh;}});
  assert.deepEqual(aliased,fresh);assert.equal(cursorReads,0,'The URL alias reuses the response just received');
  assert.equal(entitySnapshotParams(next,'new-session',{status:'success',fetchStatus:'idle'}),null,'A frozen page never changes its session');
  unsubscribe();client.clear();
}
// Failure/offline states must retain the readable cached content without hiding
// the failed refresh behind an apparently successful old cursor.
assert.equal(entitySnapshotParams(new URLSearchParams(),'old',{status:'error',fetchStatus:'idle'}),null);
assert.equal(entitySnapshotParams(new URLSearchParams(),'old',{status:'success',fetchStatus:'paused'}),null);
const offline=new QueryClient({defaultOptions:{queries:{retry:false,gcTime:Infinity}}});
const offlineKey=['entity','company','offline',''];offline.setQueryData(offlineKey,{data:{session_id:'cached'}},{updatedAt:Date.now()-120_000});
onlineManager.setOnline(false);
const paused=new QueryObserver(offline,{queryKey:offlineKey,queryFn:async()=>({data:{session_id:'fresh'}})});
const stop=paused.subscribe(()=>{});
assert.equal(paused.getCurrentResult().fetchStatus,'paused');
assert.equal(entitySnapshotParams(new URLSearchParams(),'cached',paused.getCurrentResult()),null);
stop();offline.clear();onlineManager.setOnline(true);
console.log('company/person stale snapshot, canonical reuse, frozen URL and offline/error checks passed');
