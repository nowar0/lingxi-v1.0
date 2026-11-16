import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const helper=readFileSync(new URL('../src/lib/memoryV2SyncPanel.ts',import.meta.url),'utf8')
const exports={}
vm.runInNewContext(ts.transpileModule(helper,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports})
const {memorySyncProtectionActive,memorySyncPanelDecision,memorySyncPanelStatus,memorySyncWritesReady}=exports
const clean={editOpen:false,actionOpen:false,versionsOpen:false,restoreOpen:false,busy:false}
const panel=readFileSync(new URL('../src/components/memory/MemoryV2Panel.tsx',import.meta.url),'utf8').replace(/\r\n/g,'\n')
const browser=panel.slice(panel.indexOf('function MemoryV2Browser('),panel.indexOf('function newDraft('))

test('every protected draft approval history and running operation defers reload',()=>{
 assert.equal(memorySyncPanelDecision(clean,false),'reload')
 for(const key of Object.keys(clean)){
  const value={...clean,[key]:true};assert.equal(memorySyncProtectionActive(value),true);assert.equal(memorySyncPanelDecision(value,false),'defer')
 }
})
test('initial baseline is not an external change and blocks unverified writes without erasing a protected draft',async()=>{
 const syncSource=readFileSync(new URL('../src/lib/memoryV2Sync.ts',import.meta.url),'utf8'),syncExports={}
 vm.runInNewContext(ts.transpileModule(syncSource,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports:syncExports,Date,JSON,Number,Object,Promise,URLSearchParams,AbortController,Error})
 let resolve;const pending=new Promise(done=>resolve=done);const timers=new Map();let serial=0
 const state={draft:'typed before the first read',externalPending:false,baselinePending:false,approved:true,decisions:[]}
 const controller=syncExports.createMemoryV2Sync({context:{connected:true,visible:true,scope:'companion'},fetcher:()=>pending,
  scheduler:{setTimeout(fn){const id=++serial;timers.set(id,fn);return id},clearTimeout(id){timers.delete(id)}},
  onChange:change=>{const decision=memorySyncPanelDecision({...clean,editOpen:true},state.externalPending,change.initial);state.decisions.push(decision);if(decision==='verify-baseline')state.baselinePending=true;else if(decision==='defer')state.externalPending=true}})
 controller.start();assert.equal(memorySyncWritesReady(controller.status(),false),false)
 resolve({ok:true,status:200,json:async()=>({cursor:'a'.repeat(64),changed:false,read_only:true})});await new Promise(done=>setImmediate(done))
 assert.deepEqual(state.decisions,['verify-baseline']);assert.equal(state.externalPending,false);assert.equal(state.baselinePending,true)
 assert.equal(state.draft,'typed before the first read');assert.equal(state.approved,true);assert.equal(memorySyncWritesReady(controller.status(),state.baselinePending),false)
 const initialBranch=browser.slice(browser.indexOf("if (decision === 'verify-baseline')"),browser.indexOf('detailRequest.current++; historyOwner.current++; searchRequest.current++',browser.indexOf('onExternalChange.current =')))
 assert.ok(!initialBranch.includes('setAcknowledged(false)'));assert.ok(!initialBranch.includes('setDraft('));assert.ok(!initialBranch.includes('setExternalPending(true)'))
 controller.stop()
})
test('local historical restore ends protection but next own-plus-foreign projection is still processed',async()=>{
 const restore=browser.slice(browser.indexOf('const performRestoreVersion ='),browser.indexOf('const contextSearch ='))
 assert.match(restore,/setSelected\(result\); setHistorical\(result\)/)
 assert.match(restore,/setVersionsOpen\(false\)/)
 assert.match(restore,/syncGuard.current.protection.versionsOpen = false/)
 assert.ok(!restore.includes('setVersionsOpen(true)'));assert.ok(!restore.includes('ignoreNext'))
 assert.equal(memorySyncPanelDecision(clean,false,false),'reload')
 assert.equal(memorySyncPanelDecision({...clean,versionsOpen:true},false,false),'defer')
 const syncSource=readFileSync(new URL('../src/lib/memoryV2Sync.ts',import.meta.url),'utf8'),syncExports={}
 vm.runInNewContext(ts.transpileModule(syncSource,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports:syncExports,Date,JSON,Number,Object,Promise,URLSearchParams,AbortController,Error})
 const timers=new Map();let serial=0,calls=0;const decisions=[]
 const local={selectedId:'same-memory',revision:1,historyOpen:true}
 const controller=syncExports.createMemoryV2Sync({context:{connected:true,visible:true,scope:'companion'},
  fetcher:async()=>({ok:true,status:200,json:async()=>({cursor:(++calls===1?'a':'b').repeat(64),changed:calls>1,read_only:true})}),
  scheduler:{setTimeout(fn,ms){const id=++serial;timers.set(id,{fn,ms});return id},clearTimeout(id){timers.delete(id)}},
  onChange:change=>{if(change.initial)return;const decision=memorySyncPanelDecision({...clean,versionsOpen:local.historyOpen},false,change.initial);decisions.push(decision);if(decision==='reload')local.revision=3}})
 controller.start();await new Promise(done=>setImmediate(done))
 // Restore completed r2; another window writes r3 before the next cursor read.
 local.revision=2;local.historyOpen=false
 const poll=[...timers.values()].find(value=>value.ms===15000);timers.clear();poll.fn();await new Promise(done=>setImmediate(done))
 assert.deepEqual(decisions,['reload']);assert.equal(local.selectedId,'same-memory');assert.equal(local.revision,3)
 assert.equal(controller.status().cursor,'b'.repeat(64));controller.stop()
})
test('pending external change is retained without repeated destructive clearing',()=>{
 assert.equal(memorySyncPanelDecision(clean,true),'retain-pending')
 assert.equal(memorySyncPanelDecision({...clean,editOpen:true},true),'retain-pending')
 assert.match(browser,/if \(decision === 'retain-pending'\) return/)
})
test('status never calls offline hidden unverified or failed polling synchronized',()=>{
 assert.match(memorySyncPanelStatus(undefined,false,true),/未连接/)
 assert.match(memorySyncPanelStatus(undefined,true,false),/隐藏/)
 assert.match(memorySyncPanelStatus(undefined,true,true),/首次/)
 assert.equal(memorySyncPanelStatus({error:'HTTP 503 needs manual retry'},true,true),'HTTP 503 needs manual retry')
 assert.match(memorySyncPanelStatus({started:true,last_checked:1,stale:true},true,true),/尚未重新核实/)
 assert.match(memorySyncPanelStatus({started:true,last_checked:1,stale:false},true,true),/已是最新/)
})
test('controller only watches stable scope filter owner and no mutation callback',()=>{
 assert.match(browser,/const \{ connection \} = useApp\(\)/)
 assert.match(browser,/document\.addEventListener\('visibilitychange', update\)/)
 assert.match(browser,/fetcher: async \(path, init\) => \{ await establishHostSession\(\); return fetch\(path, init\) \}/)
 assert.match(browser,/connected: connection === 'connected', visible, scope/)
 assert.match(browser,/session_id: scope === 'work' \? workSessionId : undefined/)
 assert.match(browser,/\}, \[scope, workSessionId, layer, category, includeDeleted\]\)/)
 assert.match(browser,/controller\.stop\(\)/)
 const callback=browser.slice(browser.indexOf('onExternalChange.current ='),browser.indexOf('useEffect(() => {\n    const update'))
 for(const mutation of ['memoryV2.patch','memoryV2.create','memoryV2.reinforce','memoryV2.get','setEditing(','setDraft(','setAction(undefined)'])assert.ok(!callback.includes(mutation),mutation)
 for(const reset of ['setAcknowledged(false)','setRelationAcknowledged(false)','setForgetPreview(undefined)','setRestoreVersionAcknowledged(false)'])assert.ok(callback.includes(reset),reset)
})
test('unprotected changes clear old selection and never fetch an ID outside refreshed scope',()=>{
 const clearing=browser.slice(browser.indexOf('const clearStaleViews ='),browser.indexOf('const onExternalChange'))
 for(const token of ['detailRequest.current++','historyOwner.current++','searchRequest.current++','setSelected(undefined)','setSearch(undefined)','setHistorical(undefined)','setPage(undefined)','setGraph(undefined)','setReload(value => value + 1)'])assert.ok(clearing.includes(token),token)
 assert.doesNotMatch(clearing,/selectedId/);assert.doesNotMatch(clearing,/memoryV2\.get\(/);assert.doesNotMatch(clearing,/retainSelectedId/)
 assert.match(browser,/else clearStaleViews\(\)/)
 assert.match(browser,/!externalPending && <SourceDetails item=\{selected\}/)
 assert.match(browser,/!externalPending && !historical.redacted && <SourceDetails/)
})
test('external pending gates handlers as well as visible controls without losing draft',()=>{
 assert.match(browser,/if \(lock.current \|\| syncGuard.current.externalPending \|\| !syncReadiness.current.writesReady\) return/)
 assert.match(browser,/if \(conflict \|\| syncGuard.current.externalPending \|\| !syncReadiness.current.writesReady\) return/)
 assert.match(browser,/if \(!action \|\| conflict \|\| !acknowledged \|\| syncGuard.current.externalPending \|\| !syncReadiness.current.writesReady\) return/)
 assert.match(browser,/restoreVersionLatest \|\| lock.current \|\| syncGuard.current.externalPending/)
 assert.match(browser,/<fieldset disabled=\{externalPending \|\| !writesReady\}/)
 assert.match(browser,/disabled=\{busy \|\| externalPending \|\| !writesReady \|\| Boolean\(conflict\) \|\| !draft.key/)
 assert.match(browser,/disabled=\{busy \|\| externalPending \|\| !writesReady \|\| !restoreVersionAcknowledged/)
 const deferred=browser.slice(browser.indexOf("if (decision === 'defer')"),browser.indexOf("} else clearStaleViews()"))
 assert.ok(!deferred.includes('setDraft('));assert.ok(!deferred.includes('setEditing('));assert.ok(!deferred.includes('setRestoreVersion(undefined)'))
 assert.match(browser,/外部更新，待重新比较/)
})
test('manual check cannot adopt a new CAS or discard an open draft',()=>{
 const manual=browser.slice(browser.indexOf('const checkExternalUpdates ='),browser.indexOf('const manualSyncRef='))
 assert.match(manual,/if \(manualSyncBlocked\|\|lock.current\) return/)
 assert.match(browser,/manualSyncBlocked=editOpen\|\|Boolean\(action\)\|\|relationOpen\|\|versionsOpen\|\|Boolean\(restoreVersion\)\|\|busy/)
 assert.match(manual,/setExternalPending\(false\)/)
 assert.match(manual,/clearStaleViews\(\)/)
 for(const token of ['setEditing(','setDraft(','setAction(','memoryV2.patch','memoryV2.get'])assert.ok(!manual.includes(token),token)
 assert.match(browser,/取消编辑或关闭历史后，在设置的「同步」中检查更新/)
})
test('stale detail search and history success cannot republish after external invalidation',()=>{
 assert.match(browser,/request === detailRequest.current/)
 assert.match(browser,/request === searchRequest.current/)
 assert.match(browser,/owner === historyOwner.current/)
 assert.match(browser,/\[filters, offset, reload, unavailable, externalPending, settingsOnly\]/)
 assert.match(browser,/\[filters, reload, graphOpen, unavailable, externalPending\]/)
 assert.match(browser,/\[versionsOpen, selected\?\.id, versionOffset, reload, externalPending\]/)
})
