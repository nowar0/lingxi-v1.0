import {test} from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
const source=readFileSync(new URL('../src/lib/memorySource.ts',import.meta.url),'utf8')
const exports={}
vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports,require:()=>({api:()=>{throw Error('no network')}}),Set,URLSearchParams,AbortController,DOMException,TextEncoder,setTimeout,clearTimeout,JSON,Object,Number})
const {parseMemorySourcePage,createMemorySourceClient}=exports
const identity={entityId:'mem-test',expectedRevision:4,mode:'companion'}
const page=(patch={})=>({entity_id:'mem-test',revision:4,version_id:'mem-test:3',source_status:'verified',read_only:true,total:2,offset:0,limit:1,items:[{id:1,session_id:9,session_mode:'companion',role:'assistant',content:'actual <script>plain text</script>',at:'2026-01-01',speaker:{id:'c1',name:'Snapshot'}}],...patch})
const clone=x=>JSON.parse(JSON.stringify(x))

test('strict current provenance page preserves actual role text timestamp and speaker',()=>{
 assert.deepEqual(clone(parseMemorySourcePage(page(),identity,0,1)),page())
 assert.equal(parseMemorySourcePage(page({offset:5,items:[]}),identity,5,1).items.length,0)
})
test('stale revision mismatched entity unverified history and malformed page fail closed',()=>{
 for(const value of [page({revision:3}),page({entity_id:'another'}),page({version_id:'fake:4'}),page({version_id:'mem-test:99'}),page({source_status:'unknown'}),page({read_only:false}),page({total:0}),page({extra:1}),page({items:[]}),page({limit:20})])assert.throws(()=>parseMemorySourcePage(value,identity,0,1))
})
test('messages must belong to exact mode and work session; arbitrary contents stay plain data',()=>{
 const work={...identity,mode:'work',sessionId:8}
 assert.throws(()=>parseMemorySourcePage(page(),work,0,1))
 const good=page({items:[{...page().items[0],session_id:8,session_mode:'work'}]})
 assert.equal(parseMemorySourcePage(good,work,0,1).items[0].session_id,8)
 assert.throws(()=>parseMemorySourcePage(page({items:[{...good.items[0],session_id:9}]}),work,0,1))
 for(const item of [{...page().items[0],content:null},{...page().items[0],id:-1},{...page().items[0],speaker:[]},{...page().items[0],at:5}])assert.throws(()=>parseMemorySourcePage(page({items:[item]}),identity,0,1))
})
test('client is lazy GET only with encoded entity CAS and current work session, never message ID selector',async()=>{
 const calls=[];const client=createMemorySourceClient(async(path,options)=>{calls.push({path,options});return page()})
 assert.equal(calls.length,0)
 await client.load(identity,0,1)
 assert.equal(calls[0].path,'/v10/memory-sources/mem-test/messages?expected_revision=4&mode=companion&offset=0&limit=1')
 assert.equal(calls[0].options.method,'GET');assert.ok(calls[0].options.signal)
 assert.ok(!calls[0].path.includes('message_id='));assert.ok(!calls[0].path.includes('session_id='))
 const work={...identity,mode:'work',sessionId:8};let actual
 const w=createMemorySourceClient(async path=>{actual=path;return page({items:[{...page().items[0],session_id:8,session_mode:'work'}]})})
 await w.load(work,0,1);assert.match(actual,/session_id=8$/)
})
test('invalid input does not reach network',async()=>{
 let calls=0;const client=createMemorySourceClient(async()=>{calls++;return page()})
 for(const bad of [{...identity,mode:'shared'},{...identity,mode:'work'},{...identity,expectedRevision:0}])await assert.rejects(client.load(bad,0,1))
 await assert.rejects(client.load(identity,-1));assert.equal(calls,0)
})
test('close aborts inflight read and late response cannot publish',async()=>{
 let done,signal;const client=createMemorySourceClient((path,options)=>{signal=options.signal;return new Promise(resolve=>done=resolve)})
 const pending=client.load(identity,0,1);client.cancel();assert.equal(signal.aborted,true);done(page());await assert.rejects(pending,/旧原消息/)
})
test('new entity or page supersedes prior request without publishing stale text',async()=>{
 const done=[];const client=createMemorySourceClient(()=>new Promise(resolve=>done.push(resolve)))
 const old=client.load(identity,0,1);const newer=client.load(identity,1,1)
 done[1](page({offset:1,items:[{...page().items[0],id:2}]}));assert.equal((await newer).offset,1)
 done[0](page());await assert.rejects(old,/旧原消息/)
})
test('component fetches only on explicit button and bounds ownership cancellation plain text',()=>{
 const panel=readFileSync(new URL('../src/components/memory/MemorySourceMessages.tsx',import.meta.url),'utf8')
 assert.match(panel,/查看已验证原消息/);assert.match(panel,/onClick=\{\(\)=>void load\(0\)\}/)
 assert.match(panel,/client\.cancel\(\)/);assert.match(panel,/owner===epoch\.current/)
 assert.match(panel,/item\.is_current/);assert.match(panel,/item\.scope==='shared'/)
 for(const bad of ['dangerouslySetInnerHTML','Markdown','<iframe','window.open','message_ids[0]'])assert.ok(!panel.includes(bad))
 assert.match(panel,/\{message\.content\}/);assert.match(panel,/真实角色/);assert.match(panel,/当时人物快照/)
 const integration=readFileSync(new URL('../src/components/memory/MemoryV2Panel.tsx',import.meta.url),'utf8')
 assert.match(integration,/<MemorySourceMessages/);assert.match(integration,/mode=\{scope === 'work' \? 'work' : 'companion'\} workSessionId=\{workSessionId\}/)
})
test('wire endpoint and server privacy gate are actual implementation, not a fake alternative',()=>{
 const backend=readFileSync(new URL('../../app/memory/source_api.py',import.meta.url),'utf8')
 assert.match(backend,/prefix='\/v10\/memory-sources'/);assert.match(backend,/@router\.get\('\/\{entity_id\}\/messages'\)/)
 assert.match(backend,/mode=ro/);assert.match(backend,/source\.get\('message_ids'\)/)
 assert.match(backend,/row\['scope'\] == 'shared'/);assert.match(backend,/v2\._effective/)
})
