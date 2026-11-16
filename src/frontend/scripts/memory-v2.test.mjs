import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const source = file => fs.readFileSync(path.join(root, file), 'utf8')
// Compile in memory only; never bootstrap the app, access a database, fetch a URL,
// write dist, need credentials, or call a model.
const { outputText } = ts.transpileModule(source('src/lib/memoryV2.ts'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } })
const module = { exports: {} }
new Function('require', 'module', 'exports', outputText)(id => {
  assert.equal(id, '../api/client')
  return { api: () => { throw new Error('tests must inject a transport') } }
}, module, module.exports)
const { createMemoryV2Client, loadCompleteMemoryV2Graph, memoryV2NebulaData, memoryV2Query, memoryV2Curve, memoryV2ReinforcementPoints, unresolvedMemoryV2Migrations, canActivateMemoryV2, memoryV2Conflict, memoryV2OperationId, memoryV2ContentRevision, memoryV2RestoreVersionProblem } = module.exports
const item = (id = 'mem/真实 1', extra = {}) => ({ id, scope: 'companion', owner_id: 'companion-global', key: '真实标题', value: '真实内容', layer: 2, category: 'experience', importance: .8, pinned: false, revision: 7, current_revision: 7, version_id: `${id}:3`, is_current: true, status: 'active', eligible: true, source: { kind: 'manual', actor: 'user', known: true }, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', reinforced_at: null, reinforcement_count: 0, activation: .42, half_life_days: 180, relations: [], ...extra })
const capture = () => {
  const calls = []
  const client = createMemoryV2Client(async (url, init) => { calls.push({ url, method: init?.method || 'GET', body: init?.body ? JSON.parse(init.body) : undefined }); return { accepted: true } })
  return { client, calls }
}

test('graph scan forwards cancellation into its actual transport',async()=>{
  const controller=new AbortController();let started
  const ready=new Promise(resolve=>started=resolve)
  const client=createMemoryV2Client(async(url,init)=>{
    assert.match(url,/\/graph\?/);assert.equal(init.signal,controller.signal);started()
    return new Promise((resolve,reject)=>init.signal.addEventListener('abort',()=>reject(init.signal.reason),{once:true}))
  })
  const reading=loadCompleteMemoryV2Graph(client,{scope:'companion'},undefined,controller.signal)
  await ready;controller.abort();await assert.rejects(reading,error=>error.name==='AbortError')
})

test('client is lazy and GET list/graph/context retain scope/layer/session/pagination orthogonality', async () => {
  const { client, calls } = capture()
  assert.equal(calls.length, 0)
  await client.list({ scope: 'work', mode: 'work', session_id: 42, layer: 1, offset: 80, limit: 40, include_deleted: true })
  assert.equal(calls[0].url, '/v10/memory?scope=work&mode=work&session_id=42&layer=1&offset=80&limit=40&include_deleted=true')
  await client.graph({ scope: 'companion', layer: 4, offset: 200, limit: 200 })
  assert.equal(calls[1].url, '/v10/memory/graph?scope=companion&layer=4&offset=200&limit=200')
  await client.context('work', 42, '计划 & 项目', true, 40)
  assert.equal(calls[2].url, '/v10/memory/context?mode=work&session_id=42&query=%E8%AE%A1%E5%88%92+%26+%E9%A1%B9%E7%9B%AE&history=true&limit=40')
  assert.ok(calls.every(call => call.method === 'GET' && call.body === undefined))
  assert.equal(memoryV2Query({ offset: 0, include_deleted: false, missing: undefined, empty: '' }), '?offset=0&include_deleted=false')
})

test('migration status/batch/resolve/activation match actual router paths and explicit CAS/confirmation', async () => {
  const { client, calls } = capture()
  await client.migrations('anomaly', 40, 40)
  await client.migrateBatch('legacy-v2', 100)
  const anomaly = { source_ref: 'legacy:events:1', revision: 4 }
  await client.resolve(anomaly, 'confirm', { value: '修正' })
  await client.resolve(anomaly, 'skip')
  await client.resolve(anomaly, 'retry')
  await client.activate()
  assert.equal(calls[0].url, '/v10/memory/migrations?status=anomaly&offset=40&limit=40')
  assert.deepEqual(calls[1], { url: '/v10/memory/migrations/batch', method: 'POST', body: { batch_id: 'legacy-v2', limit: 100 } })
  assert.deepEqual(calls[2].body, { source_ref: anomaly.source_ref, expected_revision: 4, action: 'confirm', user_confirmed: true, corrected: { value: '修正' } })
  assert.equal(calls[3].body.action, 'skip')
  assert.equal(calls[4].body.action, 'retry')
  assert.equal(calls[3].body.expected_revision, 4)
  assert.deepEqual(calls[5], { url: '/v10/memory/activate', method: 'POST', body: { user_confirmed: true } })
})

test('CRUD/versions preserve actual revision, encoded id, provenance and no fake source/confirm date', async () => {
  const { client, calls } = capture(), current = item()
  const body = { scope: 'companion', key: '一', value: '二', layer: 1, category: 'experience', importance: .2, pinned: true }
  await client.create(body)
  await client.get(current.id)
  await client.get(current.id, 3)
  await client.patch(current.id, { expected_revision: 7, value: '已修改' })
  await client.delete(current)
  await client.restore(current)
  await client.versions(current.id, 40, 40)
  assert.deepEqual(calls[0].body, body)
  assert.equal(calls[0].body.source, undefined)
  assert.equal(calls[0].body.confirmed_at, undefined)
  assert.equal(calls[1].url, '/v10/memory/' + encodeURIComponent(current.id))
  assert.ok(calls[2].url.endsWith('?revision=3'))
  assert.deepEqual(calls[3].body, { expected_revision: 7, value: '已修改' })
  assert.equal(calls[4].method, 'DELETE')
  assert.ok(calls[4].url.endsWith('?expected_revision=7'))
  assert.equal(calls[4].body, undefined)
  assert.deepEqual(calls[5].body, { expected_revision: 7 })
  assert.ok(calls[6].url.endsWith('/versions?offset=40&limit=40'))
})

test('historical restore sends exact explicit-confirmation wire body using content ID, never entity revision', async () => {
  const {client,calls}=capture(),current=item(),historical=item(current.id,{revision:4,version_id:`${current.id}:2`,is_current:false})
  assert.equal(memoryV2ContentRevision(historical),2)
  assert.equal(calls.length,0,'reading/constructing a restore intent is not a request')
  await client.restoreContentVersion(current,historical,{user_confirmed:true,operation_id:'restore-op-1234'})
  assert.deepEqual(calls,[{url:'/v10/memory/'+encodeURIComponent(current.id)+'/restore-version',method:'POST',body:{expected_revision:7,version_revision:2,user_confirmed:true,operation_id:'restore-op-1234'}}])
  assert.doesNotMatch(JSON.stringify(calls),/reinforce|share|core-confirm|delete_chat|preview/)
})
test('historical restore guards wrong source/core/shared/deleted/current and malformed IDs before transport',()=>{
  const current=item(),historical=item(current.id,{version_id:`${current.id}:2`,is_current:false})
  assert.equal(memoryV2RestoreVersionProblem(current,historical),'')
  const badPairs=[
    [item(current.id,{scope:'shared'}),{...historical,scope:'shared'}],
    [item(current.id,{layer:4}),historical],[current,{...historical,layer:4}],
    [item(current.id,{is_current:false}),historical],
    [current,{...historical,core_confirmation:{actor:'user'}}],
    [item(current.id,{status:'deleted'}),historical],[current,{...historical,redacted:true}],
    [current,{...historical,eligible:false}],[current,{...historical,source:{known:false}}],
    [current,{...historical,owner_id:'another'}],[current,{...historical,id:'another'}],
    [current,{...historical,is_current:true}],[current,{...historical,version_id:current.version_id}],
    [current,{...historical,version_id:'not-content-id'}],
  ]
  const {client,calls}=capture()
  for(const [now,old] of badPairs)assert.throws(()=>client.restoreContentVersion(now,old,{user_confirmed:true,operation_id:'restore-op-1234'}))
  for(const confirmation of [{user_confirmed:false,operation_id:'restore-op-1234'},{user_confirmed:true,operation_id:'bad'},{user_confirmed:true,operation_id:'无效操作编号'},{user_confirmed:true,operation_id:12345678}])assert.throws(()=>client.restoreContentVersion(current,historical,confirmation))
  assert.equal(calls.length,0)
  for(const tail of ['0','01','-1','1.5','2147483648','7x'])assert.equal(memoryV2ContentRevision({id:'x',version_id:'x:'+tail}),undefined)
})
test('restore CAS conflict is returned without automatic GET retry or changing expected revision',async()=>{
  const calls=[];const client=createMemoryV2Client(async(url,init)=>{calls.push({url,body:JSON.parse(init.body)});throw Object.assign(Error('conflict'),{status:409})})
  const current=item(),historical=item(current.id,{version_id:`${current.id}:2`,is_current:false})
  await assert.rejects(()=>client.restoreContentVersion(current,historical,{user_confirmed:true,operation_id:'restore-op-1234'}),error=>memoryV2Conflict(error))
  assert.equal(calls.length,1);assert.equal(calls[0].body.expected_revision,7);assert.equal(current.revision,7)
})
test('history restore UI has independent frozen-snapshot dialog, cancel no-op and human CAS adoption',()=>{
  const ui=source('src/components/memory/MemoryV2Panel.tsx')
  for(const text of ['以此历史内容创建新版本','将恢复的历史字段','将保留的当前字段','共享授权不会恢复','取消，不恢复','确认创建新内容版本（一次）'])assert.ok(ui.includes(text),text)
  assert.match(ui,/<Dialog open=\{Boolean\(restoreVersion\)\}/)
  assert.match(ui,/!restoreVersion \|\| !restoreVersionAcknowledged \|\| restoreVersionAttempted \|\| restoreVersionLatest \|\| lock\.current/)
  assert.match(ui,/memoryV2\.restoreContentVersion\(pending\.current, pending\.historical, \{ user_confirmed: true, operation_id: pending\.operationId \}\)/)
  const cancel=ui.slice(ui.indexOf('const cancelRestoreVersion ='),ui.indexOf('const performRestoreVersion ='))
  assert.doesNotMatch(cancel,/memoryV2\./)
  const handler=ui.slice(ui.indexOf('const performRestoreVersion ='),ui.indexOf('const contextSearch ='))
  assert.match(handler,/const latest = await memoryV2\.get\(pending\.current\.id\)/)
  assert.match(handler,/setRestoreVersionLatest\(latest\)/)
  assert.doesNotMatch(handler,/setRestoreVersion\(current =>.*current: latest|\.reinforce\(|\.share\(|\.confirmCore\(/)
  assert.match(handler,/setSelected\(result\); setHistorical\(result\)/)
  assert.match(handler,/setVersionsOpen\(false\)/)
  assert.match(handler,/setVersionOffset\(0\); refresh\(\)/)
  assert.match(handler,/syncGuard\.current\.protection\.versionsOpen = false/)
  assert.doesNotMatch(handler,/setVersionsOpen\(true\)/)
  assert.match(ui,/historyOwner\.current\+\+/)
  assert.doesNotMatch(ui,/version_id\?\.split\(':'.*\|\| item\.revision/)
})

test('share/core/reinforce use independent user confirmations and idempotency ids, never browse actions', async () => {
  const { client, calls } = capture(), current = item()
  await client.share(current, '共享新标题')
  await client.confirmCore(current, 'confirmation-id', '本次核心')
  await client.reinforce(current, 'user-event-id')
  assert.deepEqual(calls[0].body, { expected_revision: 7, user_confirmed: true, target_key: '共享新标题' })
  assert.ok(calls[0].url.endsWith('/share'))
  assert.deepEqual(calls[1].body, { expected_revision: 7, user_confirmed: true, confirmation_id: 'confirmation-id', value: '本次核心' })
  assert.ok(calls[1].url.endsWith('/core-confirm'))
  assert.deepEqual(calls[2].body, { expected_revision: 7, action: 'user_confirm', event_id: 'user-event-id', user_initiated: true })
  assert.ok(calls[2].url.endsWith('/reinforce'))
  assert.notEqual(memoryV2OperationId('core'), memoryV2OperationId('core'))
})

test('forget preview is separate from token confirmation and never requests raw chat/backup deletion', async () => {
  const { client, calls } = capture()
  await client.forgetPreview('x')
  assert.deepEqual(calls, [{ url: '/v10/memory/x/forget-preview', method: 'POST', body: undefined }])
  await client.forgetConfirm({ operation_id: 'op', token: 'backend-issued-token' })
  assert.deepEqual(calls[1], { url: '/v10/memory/forget/confirm', method: 'POST', body: { operation_id: 'op', token: 'backend-issued-token', user_confirmed: true } })
  assert.doesNotMatch(JSON.stringify(calls), /delete_chat|delete_backup|erase_external/)
})

test('graph aggregates beyond 200, including actual cross-page relations without inventing edges', async () => {
  const nodes = Array.from({ length: 437 }, (_, index) => item(`mem-${index}`, { layer: index % 4 + 1 }))
  const edge = { id: 'cross', from_id: 'mem-3', to_id: 'mem-436', kind: 'confirmed', asserted: true }
  nodes[3].relations = [edge]
  nodes[436].relations = [edge]
  const calls = [], progress = []
  const result = await loadCompleteMemoryV2Graph({ graph: async filters => {
    calls.push(filters)
    return { nodes: nodes.slice(filters.offset, filters.offset + filters.limit), edges: [], total: nodes.length, offset: filters.offset, limit: filters.limit }
  } }, { scope: 'work', session_id: 9 }, (loaded, total) => progress.push([loaded, total]))
  assert.equal(result.nodes.length, 437)
  assert.deepEqual(calls.map(call => call.offset), [0, 200, 400])
  assert.ok(calls.every(call => call.limit === 200 && call.scope === 'work' && call.session_id === 9))
  assert.deepEqual(progress, [[200, 437], [400, 437], [437, 437]])
  assert.deepEqual(result.edges, [edge])
})

test('graph rejects unstable total, duplicates, missing pages and mismatched offset instead of showing incomplete all-data', async () => {
  let call = 0
  await assert.rejects(() => loadCompleteMemoryV2Graph({ graph: async f => ({ nodes: [item(String(call++))], edges: [], total: call === 1 ? 2 : 3, offset: f.offset }) }, {}), /总数已变化/)
  await assert.rejects(() => loadCompleteMemoryV2Graph({ graph: async f => ({ nodes: [item('same')], edges: [], total: 2, offset: f.offset }) }, {}), /排序或版本已变化/)
  await assert.rejects(() => loadCompleteMemoryV2Graph({ graph: async f => ({ nodes: [], edges: [], total: 2, offset: f.offset }) }, {}), /分页契约异常/)
  await assert.rejects(() => loadCompleteMemoryV2Graph({ graph: async () => ({ nodes: [item()], edges: [], total: 1, offset: 40 }) }, {}), /分页契约异常/)
})

test('graph cancellation cannot publish an incomplete scan', async () => {
  const controller = new AbortController()
  await assert.rejects(() => loadCompleteMemoryV2Graph({ graph: async () => { controller.abort(); return { nodes: [item()], edges: [], total: 1, offset: 0 } } }, {}, undefined, controller.signal), /abort/i)
})

test('Nebula adapter maps L1–L4 and genuine importance/activation, never promotes similarity to fact', () => {
  const nodes = [1, 2, 3, 4].map(layer => item(`m${layer}`, { layer, importance: .137, activation: .294 }))
  const data = memoryV2NebulaData({ nodes, edges: [
    { from_id: 'm1', to_id: 'm2', kind: 'confirmed', asserted: true },
    { from_id: 'm2', to_id: 'm3', kind: 'similarity', asserted: true },
    { from_id: 'm3', to_id: 'm4', kind: 'confirmed' },
  ] })
  assert.deepEqual(data.nodes.map(n => n.layer), ['L1', 'L2', 'L3', 'L4'])
  assert.ok(data.nodes.every(n => n.importance === .137 && n.activation === .294 && n.scope === 'companion'))
  assert.deepEqual(data.edges.map(edge => edge.confirmed), [true, false, false])
  assert.equal(nodes[0].layer, 1)
})

test('activation requires an exhausted scan and no pending/anomaly/blocked; explicitly skipped is resolved', () => {
  const page = { counts: { complete: 20, skipped: 2 }, authority: 'legacy' }
  assert.equal(canActivateMemoryV2(undefined, true), false)
  assert.equal(canActivateMemoryV2(page, false), false)
  assert.equal(canActivateMemoryV2(page, true), true)
  for (const status of ['pending', 'anomaly', 'blocked']) assert.equal(canActivateMemoryV2({ ...page, counts: { ...page.counts, [status]: 1 } }, true), false)
  assert.equal(canActivateMemoryV2({ ...page, authority: 'v2' }, true), false)
  assert.equal(unresolvedMemoryV2Migrations({ pending: 2, anomaly: 3, blocked: 1, skipped: 8 }), 6)
})

test('curve uses backend half-life and last genuine timestamp; L3/L4 do not decay', () => {
  for (const half_life_days of [1, 30, 180]) {
    const current = item('curve', { half_life_days })
    const curve = memoryV2Curve(current, Date.parse(current.created_at))
    assert.equal(curve[0].at, Date.parse(current.created_at))
    assert.equal(curve[0].activation, 1)
    assert.ok(Math.abs(curve[20].activation - .5) < 1e-10)
    assert.ok(Math.abs(curve[60].activation - .125) < 1e-10)
  }
  assert.ok(memoryV2Curve(item('long', { layer: 3, half_life_days: null })).every(point => point.activation === 1))
  assert.deepEqual(memoryV2Curve(item('invalid', { created_at: 'unknown' })), [])
})

test('reinforcement count never fabricates a history series; only real returned events are markers', () => {
  assert.deepEqual(memoryV2ReinforcementPoints(item('no-events', { reinforcement_count: 99 })), [])
  const last = item('last', { reinforcement_count: 99, reinforced_at: '2026-03-01T00:00:00Z' })
  assert.equal(memoryV2ReinforcementPoints(last).length, 1)
  assert.equal(memoryV2ReinforcementPoints(last)[0].at, last.reinforced_at)
  const events = [{ id: 'a', action: 'user_confirm', at: '2026-02-01T00:00:00Z' }, { id: 'b', action: 'user_remention', at: '2026-03-01T00:00:00Z' }]
  assert.deepEqual(memoryV2ReinforcementPoints(item('events', { reinforcement_events: events })), events)
})

test('UI remounts scope/session boundaries, requests all graph pages, and uses controlled read-only selection', () => {
  const ui = source('src/components/memory/MemoryV2Panel.tsx')
  assert.match(ui, /key=\{`\$\{scope\}:\$\{workSessionId \?\? 'none'\}`\}/)
  assert.match(ui, /loadCompleteMemoryV2Graph\(memoryV2, filters/)
  assert.match(ui, /selected=\{selected\}/)
  assert.match(ui, /onSelect=\{id=>void selectItem\(id\)\.then\(/)
  const explorer=source('src/components/memory/MemoryExplorer.tsx')
  assert.match(explorer, /selectedId=\{p\.detailOpen\?item\?\.id:undefined\}/)
  assert.match(explorer, /p\.onSelect\(id\)/)
  const selection = ui.slice(ui.indexOf('const selectItem ='), ui.indexOf('const run =', ui.indexOf('const selectItem =')))
  assert.doesNotMatch(selection, /reinforce|patch|share|confirmCore|migrateBatch/)
  assert.match(ui, /scope === 'work' \? \{ mode: 'work', session_id: workSessionId \}/)
})

test('UI destructive/sharing/core/reinforce actions are dialog-gated; no auto-migration/reinforce effects', () => {
  const ui = source('src/components/memory/MemoryV2Panel.tsx')
  for (const copy of ['onClick={() => setStartOpen(true)}', '不会抹除外部备份', '恢复', '共享']) {
    if (copy === '不会抹除外部备份') assert.match(ui, /无法抹除外部备份|不能抹除外部备份/)
    else assert.ok(ui.includes(copy), copy)
  }
  assert.match(ui, /memoryV2\.confirmCore\(item, action\.operationId, coreValue\)/)
  assert.match(ui, /!action \|\| conflict \|\| !acknowledged/)
  assert.match(ui, /forgetPhrase !== '彻底忘记'/)
  assert.match(ui, /memoryV2\.forgetPreview\(action\.item\.id\)/)
  assert.match(ui, /memoryV2\.forgetConfirm\(forgetPreview\)/)
  assert.match(ui, /<Dialog open=\{Boolean\(action\)\}/)
  assert.match(ui, /<Dialog open=\{activateOpen\}/)
  assert.match(ui, /canActivateMemoryV2\(page, exhausted\)/)
  assert.match(ui, /!\['habit', 'project'\]\.includes\(draft\.category\)/)
  const effects = [...ui.matchAll(/useEffect\(\(\) => \{([\s\S]*?)\n  \},/g)].map(match => match[1]).join('\n')
  assert.doesNotMatch(effects, /memoryV2\.(migrateBatch|activate|reinforce|share|confirmCore|forgetConfirm)\(/)
})

test('409 conflicts are detectable and UI re-GETs instead of overwriting preserved drafts/tokens', () => {
  assert.equal(memoryV2Conflict({ status: 409 }), true)
  assert.equal(memoryV2Conflict({ status: 422 }), false)
  const ui = source('src/components/memory/MemoryV2Panel.tsx')
  assert.match(ui, /const latest = await memoryV2\.get\(id\)/)
  assert.match(ui, /setConflict\(latest\)/)
  assert.match(ui, /setForgetPreview\(undefined\); setAcknowledged\(false\)/)
  assert.match(ui, /输入保留/)
  assert.match(ui, /重新读取最新记录/)
})

test('wire endpoint names are present in the real backend, not a mocked alternate API', () => {
  const backend = source('../app/memory/v2_api.py')
  for (const route of ['/graph', '/context', '/migrations', '/migrations/batch', '/migrations/resolve', '/activate', '/forget/confirm', '/{entity_id}/versions', '/{entity_id}/core-confirm', '/{entity_id}/reinforce', '/{entity_id}/restore', '/{entity_id}/share', '/{entity_id}/forget-preview', '/{entity_id}']) assert.ok(backend.includes(`"${route}"`), route)
})
