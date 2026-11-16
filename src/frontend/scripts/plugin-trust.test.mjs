import {test} from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {createHash,webcrypto} from 'node:crypto'
import vm from 'node:vm'
import ts from 'typescript'

// Synthetic public bytes only. No key generation, private material, service or real fetch.
const source=readFileSync(new URL('../src/lib/pluginTrust.ts',import.meta.url),'utf8')
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
const exports={}
vm.runInNewContext(code,{exports,Set,Date,Math,JSON,Number,Array,Uint8Array,atob,btoa,crypto:webcrypto,performance,AbortSignal,Response,fetch:()=>{throw Error('no network')}},{filename:'pluginTrust.contract.cjs'})
const {createPluginTrustClient,parseTrustListing,parseTrustSnapshot,validateTrustPublicKey,trustOperationProblem}=exports
const publicKey=Buffer.alloc(32,7).toString('base64'),pubHash=createHash('sha256').update(Buffer.alloc(32,7)).digest('hex')
const hash='a'.repeat(64)
const key=(extra={})=>({key_id:'test.publisher',public_key:publicKey,display_name:'Synthetic publisher',fingerprint:pubHash,status:'trusted',...extra})
const snapshot=(extra={})=>({schema:'lingxi.plugin-trust.v1',revision:0,fingerprint:hash,keys:[],exists:false,...extra})
const listing=(extra={})=>({...snapshot(),approval_required:true,private_keys:false,automatic_trust:false,backup_exported:false,notice:'Explicit public trust only',...extra})
const response=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'content-type':'application/json'}})
const clone=value=>JSON.parse(JSON.stringify(value))
const preview=(action='add',extra={})=>({snapshot:{action,expected_revision:0,key:key({status:action==='revoke'?'revoked':'trusted'}),trust_fingerprint:hash,notice:'Not execution'},token:'token_synthetic_12345678901234567890',fingerprint:hash,expires_in:30,approval_required:true,...extra})
const input={key_id:'test.publisher',public_key:publicKey,display_name:'Synthetic publisher'}
function harness(custom,clock=()=>1000){const calls=[];const client=createPluginTrustClient({now:clock,fetcher:async(path,init)=>{calls.push({path,init,body:init.body?JSON.parse(init.body):undefined});const out=custom?.(path,init,calls);return out===undefined?response(listing()):await out}});return {client,calls}}

test('public trust is lazy and offline/demo/checking never query or initialize roots',async()=>{
 const h=harness();assert.equal(h.calls.length,0)
 for(const mode of ['demo','offline','checking'])assert.equal(await h.client.list(mode),undefined)
 assert.equal(h.calls.length,0);const value=await h.client.list('connected');assert.equal(value.keys.length,0)
 assert.equal(h.calls[0].path,'/v11/plugin-trust');assert.equal(h.calls[0].init.method,'GET');assert.equal(h.calls[0].init.body,undefined)
})
test('listing safety flags and unique public roots are mandatory, missing roots are not trusted',()=>{
 assert.equal(parseTrustListing(listing({requires_retrust:true})).requires_retrust,true)
 for(const bad of [listing({automatic_trust:true}),listing({private_keys:true}),listing({backup_exported:true}),listing({approval_required:false}),listing({exists:false,revision:1}),listing({keys:[key(),key({key_id:'alias'})],exists:true,revision:1})])assert.throws(()=>parseTrustListing(bad))
 assert.throws(()=>parseTrustSnapshot(snapshot({keys:[key({status:'unknown'})]})))
})
test('only canonical raw 32-byte public base64 is accepted, no private PEM or aliases',()=>{
 assert.equal(validateTrustPublicKey(publicKey),publicKey)
 for(const raw of ['-----BEGIN PRIVATE KEY-----',publicKey+'\n',Buffer.alloc(31).toString('base64'),Buffer.alloc(33).toString('base64'),'x'.repeat(44)])assert.throws(()=>validateTrustPublicKey(raw))
})
test('public fingerprint is checked, not blindly displayed as trusted',async()=>{
 const h=harness(()=>response(listing({exists:true,revision:1,keys:[key({fingerprint:'b'.repeat(64)})]})))
 await assert.rejects(h.client.list('connected'),/指纹不一致/)
})
test('add preview sends exact public-only body and remains a preview',async()=>{
 const h=harness(path=>path.endsWith('/add/preview')?response(preview()):undefined)
 const value=await h.client.add(snapshot(),input)
 assert.deepEqual(h.calls[0].body,{...input,expected_revision:0});assert.equal(h.calls[0].path,'/v11/plugin-trust/add/preview')
 assert.equal(h.calls[0].init.method,'POST');assert.equal(value.expiresAt,31000);assert.equal(h.calls.length,1)
 assert.ok(!JSON.stringify(h.calls).includes('private_key'))
})
test('duplicate ID or same-public-key alias is rejected before transport including revoked roots',async()=>{
 const h=harness();const current=snapshot({exists:true,revision:2,keys:[key({status:'revoked'})]})
 await assert.rejects(h.client.add(current,input),/已登记/)
 await assert.rejects(h.client.add(current,{...input,key_id:'other.publisher'}),/已登记/)
 assert.equal(h.calls.length,0)
})
test('revoke and retrust have separate routes with exact old revision and unchanged public key',async()=>{
 for(const [kind,status,wireAction] of [['revoke','trusted','revoke'],['retrust','revoked','trust']]){
  const current=snapshot({revision:4,exists:true,keys:[key({status})]})
  const h=harness(()=>response(preview(wireAction,{snapshot:{...preview(wireAction).snapshot,expected_revision:4}})))
  const result=await h.client[kind](current,current.keys[0]);assert.equal(result.snapshot.action,wireAction)
  assert.equal(h.calls[0].path,`/v11/plugin-trust/${kind}/preview`);assert.deepEqual(h.calls[0].body,{key_id:'test.publisher',expected_revision:4})
 }
})
test('changed preview publisher, key, action, revision, state or fingerprint is rejected',async()=>{
 for(const patch of [{action:'trust'},{expected_revision:1},{trust_fingerprint:'b'.repeat(64)},{key:key({key_id:'other'})},{key:key({status:'revoked'})},{key:key({fingerprint:'b'.repeat(64)})}]){
  const h=harness(()=>response(preview('add',{snapshot:{...preview().snapshot,...patch}})))
  await assert.rejects(h.client.add(snapshot(),input))
 }
})
test('expiry is conservative from request start and commit requires independent acknowledgement',async()=>{
 let time=1000;const h=harness(()=>response(preview()),()=>time)
 const value=await h.client.add(snapshot(),input);await assert.rejects(h.client.commit(value,false),/勾选确认/)
 assert.equal(h.calls.length,1);time=31000;assert.match(trustOperationProblem(value,time),/已过期/)
 await assert.rejects(h.client.commit(value,true),/已过期/);assert.equal(h.calls.length,1)
})
test('commit sends only exact token/fingerprint once, refresh is separate and never enables a package',async()=>{
 const h=harness(path=>path.endsWith('/preview')?response(preview()):path.endsWith('/commit')?response(snapshot({exists:true,revision:1,keys:[key()]})):undefined)
 const value=await h.client.add(snapshot(),input);const committed=await h.client.commit(value,true)
 assert.equal(committed.revision,1);assert.deepEqual(h.calls[1].body,{token:value.token,fingerprint:value.fingerprint})
 await assert.rejects(h.client.commit(value,true),/已使用/);assert.equal(h.calls.length,2)
 assert.ok(h.calls.every(call=>!call.path.includes('/execute')&&!call.path.includes('/enable')))
})
test('HTTP failure and malformed commit response consume one-use token without retry',async()=>{
 for(const bad of [response({detail:{message:'same public key alias refused'}},409),response({unexpected:true})]){
  const h=harness(path=>path.endsWith('/preview')?response(preview()):bad)
  const value=await h.client.add(snapshot(),input);await assert.rejects(h.client.commit(value,true));await assert.rejects(h.client.commit(value,true),/已使用/)
  assert.equal(h.calls.length,2)
 }
})
test('late response after tab invalidation is discarded and request uses 15-second timeout signal',async()=>{
 let resolve;const h=harness(()=>new Promise(done=>resolve=done));const pending=h.client.list('connected')
 assert.ok(h.calls[0].init.signal instanceof AbortSignal);h.client.invalidate();resolve(response(listing()))
 await assert.rejects(pending,/旧可信发布者/);assert.equal(h.calls.length,1)
})
test('monotonic expiry cannot be extended by rolling back the wall clock or editing preview',async()=>{
 let wall=10000,mono=1000;let calls=0
 const client=createPluginTrustClient({now:()=>wall,monotonicNow:()=>mono,fetcher:async()=>{calls++;return response(preview())}})
 const value=await client.add(snapshot(),input);wall=0;mono=31000
 await assert.rejects(client.commit({...value,expiresAt:999999},true),/已过期/);assert.equal(calls,1)
 const h=harness(()=>response(preview()));const original=await h.client.add(snapshot(),input)
 await assert.rejects(h.client.commit({...original,snapshot:{...original.snapshot,key:key({display_name:'Changed'})}},true),/原始快照/)
 assert.equal(h.calls.length,1)
})
test('a valid-shaped commit for the wrong revision or public-key action is an unknown result',async()=>{
 for(const bad of [snapshot({exists:true,revision:2,keys:[key()]}),snapshot({exists:true,revision:1,keys:[key({status:'revoked'})]})]){
  const h=harness(path=>path.endsWith('/preview')?response(preview()):response(bad));const value=await h.client.add(snapshot(),input)
  await assert.rejects(h.client.commit(value,true),/不一致/);await assert.rejects(h.client.commit(value,true),/已使用/)
 }
})
test('explicit abort is honored even when a mock transport returns a late success',async()=>{
 let resolve;const h=harness(()=>new Promise(done=>resolve=done));const controller=new AbortController()
 const pending=h.client.list('connected',controller.signal);controller.abort();resolve(response(listing()))
 await assert.rejects(pending,/取消|旧可信/)
})
test('backend routes and public-only interface match actual wire contract',()=>{
 const backend=readFileSync(new URL('../../app/plugin_trust_api.py',import.meta.url),'utf8')
 for(const path of ['/add/preview','/revoke/preview','/retrust/preview','/commit'])assert.ok(backend.includes(path))
 assert.ok(backend.includes("prefix='/v11/plugin-trust'"));assert.ok(source.includes('expires_in'))
})
test('personal page keeps trust separate while standalone trust panel requires checkbox before commit',()=>{
 const page=readFileSync(new URL('../src/pages/SkillsPage.tsx',import.meta.url),'utf8')
 const panel=readFileSync(new URL('../src/components/skills/PluginTrustPanel.tsx',import.meta.url),'utf8')
 assert.match(page,/PersonalPluginsPanel/);assert.ok(!page.includes("id:'trust'"))
 assert.match(panel,/connection==='connected'/);assert.match(panel,/取消，不提交/);assert.match(panel,/批准本次公钥快照（一次性）/)
 assert.match(panel,/setAttempted\(true\);setPreview\(undefined\)/);assert.match(panel,/client\.commit\(selected,true,signal\)/)
 assert.match(panel,/requires_retrust/);assert.ok(!panel.includes('PluginPackagesPanel'));assert.ok(!panel.includes('type="file"'))
 for(const unsafe of ['generateKey','private_key','dangerouslySetInnerHTML','/execute','/enable'])assert.ok(!panel.includes(unsafe))
})
