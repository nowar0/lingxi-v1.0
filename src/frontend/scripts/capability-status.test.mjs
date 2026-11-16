import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

// In-memory module execution; no service, real fetch, main app, or user profile.
const source=readFileSync(new URL('../src/lib/capabilityStatus.ts',import.meta.url),'utf8')
const output=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText
const exports={}
vm.runInNewContext(output,{exports,Set},{filename:'capabilityStatus.contract.cjs'})
const {parseCapabilityStatus,loadCapabilityStatus,capabilityStateLabel,capabilityVerificationLabel,capabilityRequestSequence}=exports
const item=(extra={})=>({id:'memory-v2',name:'四层记忆 V2',state:'installed_disabled',implemented:true,installed:true,enabled:false,note:'需明确激活',...extra})
const inventory=(extra={})=>({source:'runtime',data_root:'SYNTHETIC/isolated-data',items:[item()],verification:{formal_user_data:'not_tested',source_tests:'see-delivery-report'},...extra})

test('runtime inventory is lazy and GET-only with cancellation forwarded',async()=>{
 const calls=[];const controller=new AbortController();const fixture=inventory()
 const request=async(path,options)=>{calls.push({path,options});return fixture}
 assert.equal(calls.length,0)
 assert.equal(await loadCapabilityStatus(request,'connected',controller.signal),fixture)
 assert.equal(calls.length,1);assert.equal(calls[0].path,'/v11/features');assert.equal(calls[0].options.signal,controller.signal)
 assert.equal(calls[0].options.method,undefined);assert.equal(calls[0].options.body,undefined)
})
test('demo offline and checking never query or fabricate a real inventory',async()=>{
 for(const connection of ['demo','offline','checking','disconnected']){
  assert.equal(await loadCapabilityStatus(async()=>{throw Error('must not request')},connection),undefined)
 }
})
test('missing paths source or booleans are rejected rather than becoming zero or disabled',()=>{
 for(const bad of [null,{},inventory({source:'demo'}),inventory({data_root:''}),inventory({items:null}),inventory({verification:null}),inventory({items:[item({installed:'true'})]}),inventory({items:[item({name:''})]})])assert.throws(()=>parseCapabilityStatus(bad),/合同无效/)
})
test('duplicate ids and contradictory ready enabled planned states are refused',()=>{
 for(const items of [[item(),item()],[item({implemented:false,installed:true})],[item({enabled:true,installed:false})],[item({state:'installed_enabled'})],[item({state:'installed_disabled',enabled:true})],[item({state:'deferred'})],[item({state:'planned'})]])assert.throws(()=>parseCapabilityStatus(inventory({items})))
})
test('unknown additive metadata and unnamed future states stay compatible but are not shown as ready',()=>{
 const fixture=inventory({new_metadata:{value:true},items:[item({state:'future_state',extra_field:'kept'})]})
 assert.equal(parseCapabilityStatus(fixture),fixture)
 assert.equal(capabilityStateLabel(fixture.items[0]),'未识别状态 · future_state')
 assert.equal(Object.hasOwn(fixture.items[0],'loaded'),false)
})
test('installed enabled stopped planned deferred unavailable are distinct truthful labels',()=>{
 assert.equal(capabilityStateLabel(item()),'已安装 · 已停用')
 assert.equal(capabilityStateLabel(item({state:'installed_enabled',enabled:true})),'已安装 · 已启用')
 const later={implemented:false,installed:false,enabled:false}
 assert.equal(capabilityStateLabel(item({...later,state:'planned'})),'后期规划 · 未安装')
 assert.equal(capabilityStateLabel(item({...later,state:'deferred'})),'用户延期 · 未安装')
 assert.equal(capabilityStateLabel(item({...later,state:'unavailable'})),'不可用 · 未安装')
 assert.equal(capabilityStateLabel(item({state:'configured_program_local'})),'已配置 · 未独立存储')
})
test('verification never treats delivery pointer or untested as passed',()=>{
 assert.equal(capabilityVerificationLabel('not_tested'),'未验收')
 assert.match(capabilityVerificationLabel('see-delivery-report'),/不等于已验收/)
 assert.equal(capabilityVerificationLabel('unknown'),'服务端记录：unknown')
})
test('empty runtime list is preserved without inventing capabilities',()=>{
 const value=inventory({items:[],verification:{}});assert.equal(parseCapabilityStatus(value),value);assert.equal(value.items.length,0)
})
test('read errors propagate instead of producing installed or disabled placeholders',async()=>{
 await assert.rejects(loadCapabilityStatus(async()=>{throw Error('synthetic offline')},'connected'),/synthetic offline/)
})
test('request sequence invalidates late responses errors and finally on refresh or unmount',()=>{
 const seq=capabilityRequestSequence();const old=seq.begin();const fresh=seq.begin();assert.equal(seq.current(old),false);assert.equal(seq.current(fresh),true);seq.invalidate();assert.equal(seq.current(fresh),false)
})
test('status section mounts explicitly and has no install scan execute or loaded-state invention',()=>{
 const page=readFileSync(new URL('../src/pages/SkillsPage.tsx',import.meta.url),'utf8')
 const panel=readFileSync(new URL('../src/components/skills/CapabilityStatusPanel.tsx',import.meta.url),'utf8')
 assert.match(page,/id:\s*'status',\s*label:\s*'功能状态'/)
 assert.match(page,/section\s*===\s*'status'\s*\?\s*<CapabilityStatusPanel/)
 assert.match(panel,/useApp\(\)/);assert.match(panel,/connection !== 'connected'/)
 for(const forbidden of ["'/install'","'/scan'","'/execute'","json('POST'",'item.loaded','dangerouslySetInnerHTML'])assert.ok(!panel.includes(forbidden))
 assert.match(panel,/启用状态不代表服务已连接/);assert.match(panel,/上次结果 · 当前未核实/)
 assert.match(panel,/setValue\(next\)/);assert.ok(!panel.includes('setValue(undefined)'))
 assert.match(panel,/sequence\.current\.current\(request\)/);assert.match(panel,/abort\.current\?\.abort\(\)/)
 assert.match(panel,/AbortSignal\.timeout\(15000\)/)
})
test('backend deferred additions and strict runtime availability remain runtime-derived, not OS acceptance',()=>{
 const backend=readFileSync(new URL('../../app/feature_status.py',import.meta.url),'utf8')
 for(const id of ['qq','office-ocr','always-online','search-browser-enhanced','terminal-enhanced','workflow-ptc'])assert.ok(backend.includes(`'id': '${id}'`))
 assert.match(backend,/'source': 'runtime'/);assert.match(backend,/'state': 'deferred'/)
 assert.match(backend,/if manager\.personal_use:\s*return manager\.strict_runtime_status\(mode\)/)
 assert.match(backend,/strict\s*=\s*_strict_status\(manager,\s*'ordinary'\)/);assert.match(backend,/mcp\s*=\s*_strict_status\(manager,\s*'mcp'\)/)
  assert.match(backend,/value\s*=\s*manager\.strict_runtime_status\(mode\)/);assert.match(backend,/type\(value\) is not dict/);assert.match(backend,/except Exception:[\s\S]*?return blocked/)
 const strict=backend.slice(backend.indexOf("{'id': 'plugin-strict-runtime'"),backend.indexOf("{'id': 'memory-v2'"))
 assert.match(strict,/'state': 'installed_enabled' if strict\['available'\] and mcp\['available'\] else 'installed_disabled'/)
 assert.match(strict,/'enabled': strict\['available'\] and mcp\['available'\]/);assert.match(strict,/'ordinary':\s*strict,'mcp':\s*mcp/)
 assert.match(strict,/以当前用户权限直接运行/)
 assert.match(strict,/保留超时、停止、依赖和文件完整性检查/)
 assert.match(strict,/不需要沙箱配置/)
 const host=backend.slice(backend.indexOf("{'id': 'plugin-host'"),backend.indexOf("{'id': 'plugin-strict-runtime'"))
 assert.match(host,/'state': 'installed_enabled'/);assert.match(host,/'enabled': True/)
 for(const [ordinary,mcp] of [[false,false],[true,false],[false,true],[true,true]]){
  const enabled=ordinary&&mcp,value=inventory({items:[item({id:'plugin-strict-runtime',state:enabled?'installed_enabled':'installed_disabled',enabled,ordinary:{available:ordinary},mcp:{available:mcp}})]})
  assert.equal(parseCapabilityStatus(value),value);assert.equal(capabilityStateLabel(value.items[0]),enabled?'已安装 · 已启用':'已安装 · 已停用')
 }
 assert.match(backend,/'frozen_exe': 'see-delivery-report'/);assert.match(backend,/'formal_user_data': 'not_tested'/)
 assert.ok(!backend.includes("'loaded': True"));assert.ok(!backend.includes("'strict-ready'"))
})
