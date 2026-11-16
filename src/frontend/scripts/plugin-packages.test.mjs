import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createPluginPackagesClient, parsePluginListing, parsePluginPreview, parsePluginValue, parsePluginArgs, approvalProblem, canPreviewCandidate, pluginErrorMessage, signatureLabel } from '../src/lib/pluginPackages.ts'

const hash = 'a'.repeat(64)
const manifest = { schema_version: 1, id: 'org.test.echo', name: '回声', version: '1.0.0', host_api: '^1.0.0', files: { 'entry.py': hash }, tools: [{ name: 'echo', input_schema: { type: 'object' } }] }
const item = { id: manifest.id, name: manifest.name, version: manifest.version, revision: 3, installed: true, enabled: false, loaded: false, needs_restart: false, missing: false, files_mismatch: false, error: '', digest: hash, previous_digest: '', pending_update: false, signature_status: 'present_unverified', source: 'C:\\plugins\\echo.zip', manifest, data_dir: 'fixture/data', config_dir: 'fixture/config', blocked: [] }
const listing = { items: [item], marketplace: { status: 'planning_only', network_requests: false }, isolation_warning: '不是 OS 沙箱' }
const preview = (action = 'enable', extra = {}) => ({ snapshot: { action, id: item.id, expected_revision: item.revision, digest: hash, permissions: [], targets: [], ...(action === 'call' ? { tool: 'echo', args: { value: '测试' }, risk: '不是 OS 沙箱' } : {}), ...extra }, fingerprint: hash, token: 'fixture-single-use-token-123456789', expires_at: Date.now() / 1000 + 120, blocked: [] })
const jsonResponse = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } })
function harness(result) {
  const requests = []
  return { requests, client: createPluginPackagesClient({ fetcher: async (path, init) => { requests.push({ path, init, body: init.body && JSON.parse(init.body) }); return typeof result === 'function' ? result(path, init) : jsonResponse(result) } }) }
}

test('real listing preserves independent installed/enabled/loaded/restart/missing/integrity/dependency fields', () => {
  assert.equal(parsePluginListing(listing), listing)
  const independent = { ...listing, items: [{ ...item, enabled: true, loaded: false, needs_restart: true, missing: true, files_mismatch: false, blocked: [{ code: 'dependency_disabled', id: 'org.test.dep' }] }] }
  assert.deepEqual(parsePluginListing(independent).items[0], independent.items[0])
  assert.throws(() => parsePluginListing({ items: [] }), /返回格式无效/)
  assert.throws(() => parsePluginListing({ ...listing, items: [{ ...item, enabled: 'false' }] }), /返回格式无效/)
  assert.equal(signatureLabel('present_unverified'), '有签名但未验证')
  assert.equal(signatureLabel('absent'), '无签名')
  assert.equal(signatureLabel('revoked'), '发布者信任已撤销')
})
test('GET listing and scan use exact routes, local path only, never upload or auto-install', async () => {
  const list = harness(listing)
  await list.client.list()
  assert.equal(list.requests[0].path, '/v1/plugin-packages')
  assert.equal(list.requests[0].init.method, 'GET')
  assert.equal(list.requests[0].init.credentials, 'same-origin')
  assert.equal(list.requests[0].body, undefined)
  const value = { items: [{ path: 'C:\\local\\echo.zip', status: 'candidate_only', id: item.id, version: item.version }], planning_only: true, auto_install: false, limit: 100 }
  const scan = harness(value)
  assert.deepEqual(await scan.client.scan('C:\\local'), value)
  assert.deepEqual(scan.requests[0].body, { directory: 'C:\\local' })
  assert.equal(scan.requests[0].path, '/v1/plugin-packages/scan')
  assert.equal(scan.requests.length, 1)
  assert.equal(canPreviewCandidate(value.items[0]), true)
  for (const candidate of [{ status: 'deferred' }, { status: 'planning_only' }, { status: 'invalid' }, { status: 'candidate_only', deferred: true }]) assert.equal(canPreviewCandidate(candidate), false)
})
test('install/update/restore preview only send source_path and exact expected revision, not enabled or ZIP bytes', async () => {
  for (const action of ['install', 'update', 'restore']) {
    const result = preview(action, { manifest, source_path: item.source, signature_status: 'present_unverified' })
    const { client, requests } = harness(result)
    await client.previewSource(action, item.source, action === 'install' ? 0 : 3, action === 'install' ? undefined : item.id)
    assert.equal(requests[0].path, action === 'install' ? '/v1/plugin-packages/install/preview' : `/v1/plugin-packages/${item.id}/${action}/preview`)
    assert.deepEqual(requests[0].body, { source_path: item.source, expected_revision: action === 'install' ? 0 : 3 })
    assert.equal(requests.length, 1)
  }
})
test('each lifecycle action is preview-only and carries CAS revision, restore is never a generic action', async () => {
  for (const action of ['enable', 'disable', 'rollback', 'uninstall', 'purge']) {
    const { client, requests } = harness(preview(action))
    await client.previewAction(item.id, action, 3)
    assert.equal(requests[0].path, `/v1/plugin-packages/${item.id}/${action}/preview`)
    assert.deepEqual(requests[0].body, { expected_revision: 3 })
  }
  await assert.rejects(harness(preview()).client.previewAction(item.id, 'restore', 3), /不支持/)
})
test('blocked, missing, expired or cross-kind authorization can never commit or execute', async () => {
  const { client, requests } = harness(item)
  for (const value of [{ ...preview(), blocked: [{ code: 'missing_dependency' }], token: null, expires_at: null }, { ...preview(), expires_at: 1 }]) {
    assert.ok(approvalProblem(value))
    await assert.rejects(client.commit(value))
  }
  await assert.rejects(client.commit(preview('call')), /跨操作/)
  await assert.rejects(client.execute(preview()), /跨操作/)
  assert.equal(requests.length, 0)
  assert.throws(() => parsePluginPreview({ ...preview(), token: null }), /返回格式无效/)
})
test('commit sends only token+fingerprint once; double click, HTTP failure and unknown result never resend', async () => {
  for (const result of [() => jsonResponse(item), () => jsonResponse({ detail: { code: 'revision_conflict', message: '插件记录已变化，请重新预览', details: { hidden: 'never show' } } }, 409), () => { throw new Error('socket disconnected') }]) {
    const { client, requests } = harness(result)
    const value = preview()
    await client.commit(value).catch(() => {})
    await assert.rejects(client.commit(value), /不能重试/)
    assert.equal(requests.length, 1)
    assert.equal(requests[0].path, '/v1/plugin-packages/commit')
    assert.deepEqual(requests[0].body, { token: value.token, fingerprint: value.fingerprint })
  }
})
test('tool preview preserves exact JSON args/targets/permissions and execute is independently one-shot', async () => {
  const value = preview('call', { permissions: ['python.execute', 'network.http'], targets: [{ permission: 'network.http', kind: 'url', value: 'http://127.0.0.1/private' }] })
  const { client, requests } = harness((path) => jsonResponse(path.endsWith('/preview') ? value : { ok: true, id: item.id, fingerprint: hash, result: 'done' }))
  assert.deepEqual(await client.previewCall(item.id, 'echo', '{"value":"测试"}', 3), value)
  assert.deepEqual(requests[0].body, { tool: 'echo', args: { value: '测试' }, expected_revision: 3 })
  assert.equal(requests[0].path, `/v1/plugin-packages/${item.id}/calls/preview`)
  await client.execute(value)
  await assert.rejects(client.execute(value), /不能重试/)
  assert.equal(requests[1].path, '/v1/plugin-packages/calls/execute')
  assert.equal(requests.length, 2)
})
test('configuration is an object; credentials are references only; no actual Key or nested credential fields', () => {
  assert.deepEqual(parsePluginValue('{"theme":"blue","options":{"limit":5}}', 'config'), { theme: 'blue', options: { limit: 5 } })
  for (const text of ['"sk-not-json-config"', '[]', '{"nested":{"API_key":"secret"}}', '{"values":[{"password":"secret"}]}']) assert.throws(() => parsePluginValue(text, 'config'))
  assert.deepEqual(parsePluginValue('["system:reference-1","vault.plugin.echo"]', 'credentials'), ['system:reference-1', 'vault.plugin.echo'])
  for (const text of ['{"key":"actual"}', '["sk-not-a-reference"]', '["contains spaces"]', '[3]']) assert.throws(() => parsePluginValue(text, 'credentials'))
  assert.throws(() => parsePluginArgs('[]'), /JSON 对象/)
  assert.throws(() => parsePluginArgs('{"secret":"never send"}'), /密钥/)
  assert.throws(() => parsePluginArgs('{'), /JSON 格式无效/)
  assert.throws(() => parsePluginArgs(JSON.stringify({ value: 'x'.repeat(65536) })), /64 KiB/)
})
test('config/state/credential editor sends a value snapshot and reads server revision rather than key-specific routes', async () => {
  for (const kind of ['config', 'state', 'credentials']) {
    const text = kind === 'credentials' ? '["vault:ref"]' : '{"choice":true}'
    const value = JSON.parse(text)
    const { client, requests } = harness((path) => jsonResponse(path.endsWith('/preview') ? preview(kind, { value }) : { id: item.id, revision: 8, value }))
    assert.deepEqual(await client.readValue(item.id, kind), { id: item.id, revision: 8, value })
    await client.previewValue(item.id, kind, 8, text)
    assert.equal(requests[0].path, `/v1/plugin-packages/${item.id}/${kind}`)
    assert.deepEqual(requests[1].body, { expected_revision: 8, value })
    assert.equal(requests.length, 2)
  }
})
test('error surfaces readable nested FastAPI message, never object/details/stringified JSON', async () => {
  assert.equal(pluginErrorMessage({ detail: { code: 'blocked', message: '依赖不可用', details: { secret: 'redacted' } } }), '依赖不可用')
  assert.equal(pluginErrorMessage({ detail: [{ msg: '字段无效', input: 'secret' }] }), '字段无效')
  assert.equal(pluginErrorMessage({ detail: { hidden: 'secret' } }), '插件操作未完成')
  const { client } = harness(() => jsonResponse({ detail: { code: 'invalid_token', message: '授权已过期，请重新预览', details: { doNotDisplay: true } } }, 403))
  await assert.rejects(client.commit(preview()), (error) => error.message === '授权已过期，请重新预览' && error.code === 'invalid_token')
})
test('malformed execution/commit responses are unknown outcomes and cannot reuse the token', async () => {
  for (const action of ['enable', 'call']) {
    const { client, requests } = harness({ ok: false, unexpected: true })
    const value = preview(action)
    const approve = () => action === 'call' ? client.execute(value) : client.commit(value)
    await assert.rejects(approve(), (error) => error.unknownOutcome === true && /结果未知/.test(error.message))
    await assert.rejects(approve(), /不能重试/)
    assert.equal(requests.length, 1)
  }
})
test('failed tool execution never replays a consumed token, including an unknown connection loss', async () => {
  for (const result of [() => jsonResponse({ detail: { message: '插件调用失败', code: 'timeout' } }, 502), () => { throw new Error('lost') }]) {
    const { client, requests } = harness(result)
    const value = preview('call')
    await assert.rejects(client.execute(value))
    await assert.rejects(client.execute(value), /不能重试/)
    assert.equal(requests.length, 1)
  }
})
test('suspicious actual Key values in generic JSON fields never reach transport', async () => {
  const { client, requests } = harness(preview('config'))
  await assert.rejects(client.previewValue(item.id, 'config', 3, '{"value":"sk-example-actual-key"}'), /真实 Key/)
  await assert.rejects(client.previewCall(item.id, 'echo', '{"value":"Bearer real-secret"}', 3), /真实凭据/)
  assert.equal(requests.length, 0)
})
test('invalid paths/revisions do not perform requests; unknown remote states are not normalized to installed', async () => {
  const { client, requests } = harness(listing)
  await assert.rejects(client.previewSource('install', '', 0))
  await assert.rejects(client.previewSource('install', 'C:\\x.zip', -1))
  await assert.rejects(client.previewAction('../unsafe', 'enable', 3))
  assert.equal(requests.length, 0)
})
