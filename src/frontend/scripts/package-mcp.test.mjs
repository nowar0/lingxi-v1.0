import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
import { createHash } from 'node:crypto'
import { parsePluginArgs, pluginErrorMessage, signatureLabel } from '../src/lib/pluginPackages.ts'

// Transpile in memory: no React mount, application, network, APPDATA or emitted files.
const source = readFileSync(new URL('../src/components/skills/PackageMcpPanel.tsx', import.meta.url), 'utf8')
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } }).outputText
const exports = {}
vm.runInNewContext(code, { exports, fetch: () => { throw Error('No real network in tests') }, Date, Response, Set, Map, URL, JSON, TextEncoder,
  require: name => name === '@/lib/pluginPackages' ? { parsePluginArgs, pluginErrorMessage, signatureLabel } : name === 'react/jsx-runtime' ? { jsx: () => { throw Error('No render') }, jsxs: () => { throw Error('No render') } } : {},
}, { filename: 'PackageMcpPanel.contract.cjs' })
const { createPackageMcpClient, parsePackageMcpMetadata, parsePackageMcpPreview, packageMcpApprovalError } = exports
const hash = 'a'.repeat(64), pid = 'org.test.mcp', name = 'local', rev = 3
const declaration = { name, transport: 'stdio', entrypoint: 'server.py' }
const target = { kind: 'local_process', entrypoint: 'SYNTHETIC/package/server.py', sha256: hash, cwd: 'SYNTHETIC/package', interpreter: 'dedicated_frozen_single_server', inherits_user_environment: false }
const tool = { name: 'echo', description: 'Synthetic echo', inputSchema: { type: 'object', properties: { text: { type: 'string' }, count: { type: 'integer' } }, required: ['text'], additionalProperties: false } }
const ipcRoles = ['stdin_read','stdout_write','diagnostics_write','proxy_request_read','proxy_result_write']
const canonical = value => Array.isArray(value) ? `[${value.map(canonical).join(',')}]` : value !== null && typeof value === 'object' ? `{${Object.keys(value).sort().map(key=>`${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}` : JSON.stringify(value)
const digest = value => createHash('sha256').update(canonical(value)).digest('hex')
// Exact public McpLaunchSpec.snapshot / semantic_plan_projection DTO, not an OS receipt.
function strictSpec(snapshot) {
  const callId='1'.repeat(32),root='R:\\synthetic\\sandbox-lab-'+ '2'.repeat(32),sid='S-1-15-2-123456789'
  const paths={root,runtime:root+'\\runtime',code:root+'\\code\\'+pid+'\\'+hash,scratch:root+'\\scratch\\'+callId}
  const inventory=(relative_path,content)=>({relative_path,sha256:createHash('sha256').update(content).digest('hex'),size:Buffer.byteLength(content)})
  const runtime=[inventory('LingxiPluginWorker.exe','synthetic worker only')],code=[inventory('server.py','synthetic MCP only')]
  const operation=Object.fromEntries(['id','expected_revision','digest','mcp_name','declaration','mcp_operation','tool','args','tool_schema','tool_definition','permissions','trust','dependency_binding','config_fingerprint','state_fingerprint'].map(key=>[key,snapshot[key]??null]))
  const policy={policy:{mode:'stdio_mcp',wall_seconds:30,memory_bytes:512*1024*1024,cpu_rate:2000,active_processes:1,global_concurrency:10,capabilities:[],network_capabilities:[],fallback_allowed:false},binding:{plugin_id:pid,revision:rev,digest:hash,call_id:callId},paths,appcontainer_sid:sid,capabilities:[],network_capabilities:[],code_files:code,runtime_files:runtime,runtime_manifest_sha256:digest(runtime),acl_grants:[{path:paths.runtime,sid,rights:'RX',scope:'this-directory-and-descendants'},{path:paths.code,sid,rights:'RX',scope:'this-directory-and-descendants'},{path:paths.scratch,sid,rights:'RW',scope:'this-directory-and-descendants'},...[root,root+'\\code',root+'\\code\\'+pid,root+'\\scratch'].sort().map(path=>({path,sid,rights:'X',scope:'this-directory-only'}))],creation_flags:0x08080004,job:{limit_flags:0x2308,process_memory_bytes:512*1024*1024,job_memory_bytes:512*1024*1024,active_processes:1,cpu_control_flags:5,cpu_rate:2000,wall_seconds:30},actual_verified:false,os_actions_performed:false,fallback_allowed:false,ipc_mode:'anonymous-pipes',ipc_roles:[...ipcRoles]}
  const body={v:1,mode:'stdio_mcp',call_id:callId,nonce:'3'.repeat(64),policy,profile_name:'Lingxi.Plugin.Mcp.'+'2'.repeat(32),manifest_sha256:hash,operation_sha256:digest(operation),executable:paths.runtime+'\\LingxiPluginWorker.exe',environment:{SYSTEMROOT:'R:\\Windows',WINDIR:'R:\\Windows',TEMP:paths.scratch,TMP:paths.scratch,PYTHONIOENCODING:'utf-8',PYTHONUTF8:'1',PYTHONNOUSERSITE:'1'},permission_proxy:'unavailable-deny'}
  return {...body,fingerprint:digest(body)}
}
const preview = (operation = 'discover', extra = {}) => {
  const snapshot={ adapter: 'package-mcp-v1', action: 'call', mcp_operation: operation, id: pid, expected_revision: rev, digest: hash, mcp_name: name, declaration, target, package_root: 'SYNTHETIC/package', signature_status: 'present_unverified', risk: '严格规格是意图，不是OS验收', note: '精确服务确认', permissions: ['mcp.connect', operation === 'execute' ? 'mcp.call' : 'mcp.discover'], ...(operation === 'catalog' ? { tools: [tool] } : operation === 'execute' ? { tool: 'echo', args: { text: 'fixture' }, tool_schema: tool.inputSchema } : {}), ...extra }
  return {token:`synthetic-${operation}-token-1234567890`,fingerprint:hash,expires_at:Date.now()/1000+120,blocked:[],snapshot:{...snapshot,strict_mcp_spec:strictSpec(operation==='catalog'?{...snapshot,mcp_operation:'discover'}:snapshot)}}
}
const metadata = { id: pid, revision: rev, digest: hash, installed: true, enabled: true, items: [declaration], connects: false, credentials: 'none', risk: '不是 OS 沙箱' }
const discovered = () => ({ id: pid, revision: rev, digest: hash, mcp_name: name, tools: [tool], catalog: preview('catalog'), note: '有限有效期，调用另行确认' })
const response = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } })
function harness(result) { const calls = []; return { calls, client: createPackageMcpClient({ fetcher: async (path, init) => { const body = init.body === undefined ? undefined : JSON.parse(init.body); calls.push({ path, init, body }); return typeof result === 'function' ? result(path, init, body) : response(result) } }) } }
const equal = (actual, expected) => assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected)

test('metadata reads exact package-owned route with no connection or legacy catalog writes', async () => {
  const h = harness(metadata)
  equal(await h.client.metadata(pid), metadata)
  assert.equal(h.calls[0].path, `/v1/plugin-packages/${pid}/mcp`)
  assert.equal(h.calls[0].init.method, 'GET')
  assert.equal(h.calls[0].init.credentials, 'same-origin')
  assert.equal(h.calls[0].body, undefined)
  assert.equal(h.calls.length, 1)
  assert.throws(() => parsePackageMcpMetadata({ ...metadata, enabled: 'true' }), /格式无效/)
  assert.throws(() => parsePackageMcpMetadata({ ...metadata, connects: true }), /格式无效/)
  assert.throws(() => parsePackageMcpMetadata({ ...metadata, items: [declaration, declaration] }), /重名/)
})
test('discover preview sends exact name and CAS revision, never connects or auto-confirms', async () => {
  const h = harness(preview())
  const value=await h.client.discoverPreview(pid, name, rev)
  equal(value.snapshot.strict_mcp_spec.policy.ipc_roles,ipcRoles)
  assert.equal(value.snapshot.strict_mcp_spec.policy.actual_verified,false);assert.equal(value.snapshot.strict_mcp_spec.policy.os_actions_performed,false)
  assert.equal(value.snapshot.strict_mcp_spec.policy.acl_grants.length,7);assert.equal(value.snapshot.strict_mcp_spec.permission_proxy,'unavailable-deny')
  assert.equal(value.snapshot.strict_mcp_spec.policy.policy.fallback_allowed,false)
  assert.throws(()=>parsePackageMcpPreview(preview('discover',{target:{...target,interpreter:'trusted_host_python'}}),'discover'),/格式无效/)
  assert.throws(()=>parsePackageMcpPreview(preview('discover',{target:{...target,inherits_user_environment:true}}),'discover'),/格式无效/)
  assert.equal(h.calls[0].path, `/v1/plugin-packages/${pid}/mcp/discover-preview`)
  equal(h.calls[0].body, { name, expected_revision: rev })
  assert.equal(h.calls.length, 1)
  assert.throws(() => parsePackageMcpPreview(preview('execute'), 'discover'), /格式无效/)
})
test('HTTP diagnostic preserves exact URL but grants no strict stdio execution or redirect/proxy ambiguity', async () => {
  const declaration = { name, transport: 'http', url: 'http://127.0.0.1:7000/mcp' }, target = { kind: 'network', url: declaration.url, redirects: false, credentials: 'none', environment_proxy: false }
  // The parser may display a blocked HTTP description, but current backend refuses
  // HTTP before _issue; never fabricate a stdio strict spec or live HTTP token.
  const value = preview('discover', { declaration, target });delete value.snapshot.strict_mcp_spec
  value.token=null;value.expires_at=null;value.blocked=[{code:'mcp_stdio_required'}]
  equal(parsePackageMcpPreview(value, 'discover'), value)
  const h=harness({});await assert.rejects(h.client.discover(value));assert.equal(h.calls.length,0)
  for (const altered of [{ ...target, redirects: true }, { ...target, environment_proxy: true }, { ...target, url: 'https://elsewhere.invalid' }]) assert.throws(() => parsePackageMcpPreview({...value,snapshot:{...value.snapshot,target:altered}}, 'discover'), /格式无效/)
})
test('discovery requires one-use confirmation; preserves full tools and catalog TTL', async () => {
  const found = discovered(), h = harness(found), value = preview()
  equal(await h.client.discover(value), found)
  equal(h.calls[0].body, { token: value.token, fingerprint: hash })
  assert.equal(h.calls[0].path, '/v1/plugin-packages/mcp/discover')
  await assert.rejects(h.client.discover(value), /不能重试/)
  assert.equal(h.calls.length, 1)
})
test('expired, blocked, wrong operation or missing token never reaches transport', async () => {
  const h = harness(discovered())
  for (const value of [{ ...preview(), expires_at: 1 }, { ...preview(), blocked: [{ code: 'disabled' }] }, { ...preview(), token: null, expires_at: null, blocked: [{ code: 'blocked' }] }]) {
    assert.ok(packageMcpApprovalError(value))
    await assert.rejects(h.client.discover(value))
  }
  await assert.rejects(h.client.discover(preview('execute')), /格式无效/)
  assert.equal(h.calls.length, 0)
})
test('discovery response version, digest, MCP and real tool catalog are exact bound', async () => {
  for (const changed of [{ ...discovered(), digest: 'b'.repeat(64) }, { ...discovered(), revision: 4 }, { ...discovered(), mcp_name: 'different' }, { ...discovered(), catalog: preview('catalog', { tools: [] }) }]) await assert.rejects(harness(changed).client.discover(preview()), /格式无效/)
})
test('call preview uses real discovered catalog token, exact JSON args and schema', async () => {
  const h = harness(preview('execute'))
  await h.client.callPreview(pid, name, rev, discovered(), 'echo', '{"text":"fixture"}')
  assert.equal(h.calls[0].path, `/v1/plugin-packages/${pid}/mcp/call-preview`)
  equal(h.calls[0].body, { name, expected_revision: rev, catalog_token: discovered().catalog.token, catalog_fingerprint: hash, tool: 'echo', args: { text: 'fixture' } })
  assert.equal(h.calls.length, 1)
})
test('expired/mismatched discovery, unknown tools, malformed/secret args never preview', async () => {
  const h = harness(preview('execute'))
  const expired = discovered(); expired.catalog.expires_at = 1
  await assert.rejects(h.client.callPreview(pid, name, rev, expired, 'echo', '{}'), /过期/)
  await assert.rejects(h.client.callPreview(pid, name, rev + 1, discovered(), 'echo', '{}'), /不匹配/)
  await assert.rejects(h.client.callPreview(pid, name, rev, discovered(), 'missing', '{}'), /不匹配/)
  for (const text of ['[]', '{', '{"api_key":"never-send"}', '{"value":"sk-synthetic"}']) await assert.rejects(h.client.callPreview(pid, name, rev, discovered(), 'echo', text))
  assert.equal(h.calls.length, 0)
})
test('call args equality is structural, but changed args or tool are rejected', async () => {
  const args = { count: 2, text: 'fixture' }
  await harness(preview('execute', { args: { text: 'fixture', count: 2 } })).client.callPreview(pid, name, rev, discovered(), 'echo', JSON.stringify(args))
  await assert.rejects(harness(preview('execute', { args: { text: 'changed' } })).client.callPreview(pid, name, rev, discovered(), 'echo', '{"text":"fixture"}'), /格式无效/)
  await assert.rejects(harness(preview('execute', { tool_schema: { type: 'array' } })).client.callPreview(pid, name, rev, discovered(), 'echo', '{"text":"fixture"}'), /格式无效/)
  await assert.rejects(harness(preview('execute', { target: { ...target, cwd: 'different' } })).client.callPreview(pid, name, rev, discovered(), 'echo', '{"text":"fixture"}'), /格式无效/)
})
test('execution is separately confirmed and consumed even on unknown, HTTP or malformed result', async () => {
  for (const handler of [() => response({ ok: true, id: pid, mcp_name: name, fingerprint: hash, result: 'fixture' }), () => { throw Error('network') }, () => response({ detail: { code: 'revision_conflict', message: '包版本已变化', details: 'hidden' } }, 409), () => response({ ok: true, id: 'wrong', result: 'bad' })]) {
    const h = harness(handler), value = preview('execute')
    await h.client.execute(value).catch(() => {})
    await assert.rejects(h.client.execute(value), /不能重试/)
    assert.equal(h.calls[0].path, '/v1/plugin-packages/mcp/execute')
    equal(h.calls[0].body, { token: value.token, fingerprint: hash })
    assert.equal(h.calls.length, 1)
  }
})
test('nested server errors stay human readable and never stringify unknown payloads', async () => {
  await assert.rejects(harness(() => response({ detail: { code: 'blocked', message: '插件包依赖不可用', details: { unknown: 'private' } } }, 409)).client.metadata(pid), error => error.message === '插件包依赖不可用')
  assert.throws(() => createPackageMcpClient({ apiBase: 'https://external.invalid' }), /同源/)
  assert.throws(() => createPackageMcpClient({ apiBase: '//external/path' }), /同源/)
})
test('real backend routes and snapshot fields are the tested wire contract', () => {
  const routes = readFileSync(new URL('../../app/plugin_api.py', import.meta.url), 'utf8'), backend = readFileSync(new URL('../../app/package_mcp.py', import.meta.url), 'utf8')
  for (const route of ["'/{plugin_id}/mcp'", "'/{plugin_id}/mcp/discover-preview'", "'/mcp/discover'", "'/{plugin_id}/mcp/call-preview'", "'/mcp/execute'"]) assert.ok(routes.includes(route), route)
  for (const field of ['catalog_token', 'catalog_fingerprint', 'tool_schema', 'signature_status', 'inherits_user_environment', 'expected_revision','strict_mcp_spec','dedicated_frozen_single_server']) assert.ok(backend.includes(field), field)
  const strict=readFileSync(new URL('../../app/plugin_mcp_strict.py',import.meta.url),'utf8'),policy=readFileSync(new URL('../../app/plugin_sandbox_policy.py',import.meta.url),'utf8')
  const actualRoles=/MCP_IPC_ROLES\s*=\s*\(([^)]+)\)/m.exec(policy)
  assert.ok(actualRoles);assert.deepEqual(Array.from(actualRoles[1].matchAll(/'([^']+)'/g),match=>match[1]),ipcRoles)
  assert.match(backend,/value\['strict_mcp_spec'\] = copy\.deepcopy\(spec\)/)
  assert.match(strict,/plan\.ipc_roles != MCP_IPC_ROLES/);assert.match(strict,/'policy':\s*_clone\(semantic_plan_projection\(plan\)\)/)
  assert.match(strict,/'permission_proxy':'unavailable-deny'/);assert.match(policy,/actual_verified: bool = False/);assert.match(policy,/os_actions_performed: bool = False/)
  const body=/body = \{('v':1, 'mode':'stdio_mcp'[\s\S]*?)\n\s*return \{\*\*body, 'fingerprint':_hash\(body\)\}/.exec(strict)
  assert.ok(body);const dtoFields=Array.from(body[1].matchAll(/'([a-z0-9_]+)'\s*:/g),match=>match[1]);assert.deepEqual(Object.keys(preview().snapshot.strict_mcp_spec).sort(),[...dtoFields,'fingerprint'].sort())
})
test('UI is independently dialog-gated, never auto-discovers or retries on mount', () => {
  assert.match(source, /void client\.metadata\(pluginId\)/)
  assert.match(source, /setAttempted\(true\)/)
  assert.match(source, /!ack/)
  assert.match(source, /仅批准本次连接与发现/)
  assert.match(source, /仅批准本次工具执行/)
  assert.match(source, /revision, digest, installed, enabled/)
  const owner = readFileSync(new URL('../src/components/skills/PluginPackagesPanel.tsx', import.meta.url), 'utf8')
  assert.match(owner, /<PackageMcpPanel/)
  assert.ok(!owner.includes('skills / MCP 仅声明，不自动执行'))
})
test('package-owned skills are read-only in all old catalog mutation paths', () => {
  const skills = readFileSync(new URL('../src/components/skills/SkillsPanel.tsx', import.meta.url), 'utf8')
  assert.match(skills, /const packageSkillReadOnly/)
  assert.match(skills, /pkgskill_/)
  assert.match(skills, /if \(packageSkillReadOnly\(value\)\) throw/)
  assert.match(skills, /if \(packageSkillReadOnly\(skill\)\) throw/)
  assert.match(skills, /disabled=\{busy \|\| !ready \|\| packageSkillReadOnly\(item\)\}/)
  assert.match(skills, /<fieldset disabled=\{packageSkillReadOnly\(skill\)\}/)
  assert.match(skills, /deleting\.kind === 'skills' && packageSkillReadOnly/)
  assert.match(skills, /packageSkillReadOnly\(skill\) \? '插件技能'/)
  assert.match(skills, /启停、更新或删除，请在「本地插件」中操作/)
})
