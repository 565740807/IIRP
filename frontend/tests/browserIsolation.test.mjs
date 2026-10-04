import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const {validateManifest, verifyFixture} = require('../../browser/common.cjs');
const valid = {base:'http://127.0.0.1:18897',database:'iirp_v1_test_synthetic',validation_id:'a'.repeat(32),synthetic:true,no_external_provider_calls:true};
test('browser guard rejects original or unlabelled endpoints before networking', async () => {
 for(const change of [{base:'http://127.0.0.1:18081'}, {base:'http://example.invalid:18897'}, {base:'http://127.0.0.1:18897/path'}, {database:'iirp_v1'}, {synthetic:false}, {validation_id:'missing'}]) {
  let calls=0;
  await assert.rejects(verifyFixture({...valid,...change},async()=>{calls++;throw Error('must not fetch')}));
  assert.equal(calls,0);
 }
});
test('browser guard checks live database and per-run identity, not just port', async () => {
 validateManifest(valid);
 for(const change of [{database:'iirp_v1'}, {validation_id:'b'.repeat(32)}, {no_external_provider_calls:false}])
  await assert.rejects(verifyFixture(valid,async()=>({ok:true,json:async()=>({...valid,...change})})));
 await assert.rejects(verifyFixture(valid,async()=>({ok:false})));
 assert.deepEqual(await verifyFixture(valid,async()=>({ok:true,json:async()=>valid})),valid);
});
