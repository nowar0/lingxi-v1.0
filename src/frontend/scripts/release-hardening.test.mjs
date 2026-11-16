import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
const code = ts.transpileModule(readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
function harness(fetcher, { shell, hash = '', clock = () => Date.now() } = {}) {
  const exports = {}, location = { hash, pathname: '/ui/', search: '' }
  vm.runInNewContext(code, { exports, Headers, FormData, AbortSignal, URLSearchParams, TypeError, Date: { now: clock }, window: { location, history: { replaceState() { location.hash = '' } }, LingxiShell: shell }, fetch: fetcher })
  return exports
}
test('a failed bootstrap can be retried after its fragment was removed', async () => {
  let calls = 0
  const client = harness(async () => { if (++calls === 1) throw Error('network'); return new Response('{}') }, { hash: '#lingxi-bootstrap=' + 'a'.repeat(43) })
  await assert.rejects(client.establishHostSession())
  await client.establishHostSession()
  assert.equal(calls, 2)
})
test('after a long sleep the desktop bridge renews authentication before a mutation', async () => {
  let now = 100, bridges = 0
  const calls = []
  const client = harness(async path => { calls.push(path); return new Response('{}', { headers: { 'content-type': 'application/json' } }) }, { shell: { hostSession: async () => { bridges++; return { token: 'a'.repeat(43) } } }, clock: () => now })
  await client.api('/read')
  now += 13 * 3600 * 1000
  await client.api('/write', { method: 'POST' })
  assert.equal(bridges, 2)
  assert.deepEqual(calls, ['/v11/ui-auth/exchange', '/read', '/v11/ui-auth/exchange', '/write'])
})
test('401 invalidates readiness without automatically replaying a write', async () => {
  let exchanges = 0, writes = 0
  const client = harness(async path => path.includes('exchange') ? (exchanges++, new Response('{}')) : (writes++, new Response('{"detail":{"code":"ui_auth_expired"}}', { status: 401, headers: { 'content-type': 'application/json' } })), { shell: { hostSession: async () => ({ token: 'a'.repeat(43) }) } })
  await assert.rejects(client.api('/write', { method: 'POST' }), /认证已过期/)
  assert.equal(writes, 1)
  await assert.rejects(client.api('/write', { method: 'POST' }))
  assert.equal(exchanges, 2)
  assert.equal(writes, 2)
})
test('concurrent requests after a long sleep share one authentication renewal', async () => {
  let now = 100, exchanges = 0, release
  const client = harness(async path => {
    if (path.includes('exchange')) { exchanges++; if (exchanges === 2) await new Promise(resolve => { release = resolve }) }
    return new Response('{}', { headers: { 'content-type': 'application/json' } })
  }, { shell: { hostSession: async () => ({ token: 'a'.repeat(43) }) }, clock: () => now })
  await client.api('/initial')
  now += 13 * 3600 * 1000
  const first = client.api('/read-one'), second = client.api('/read-two')
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(exchanges, 2)
  release()
  await Promise.all([first, second])
})
test('malformed and missing JSON never expose parser internals', async () => {
  await assert.rejects(harness(async () => new Response('{bad', { status: 502, headers: { 'content-type': 'application/json' } })).api('/read'), /请求失败（HTTP 502）/)
  await assert.rejects(harness(async () => new Response('{bad', { headers: { 'content-type': 'application/json' } })).api('/read'), /服务响应不完整/)
})
test('recovery reads changes made by another window and tolerates a blocked localStorage mirror', () => {
  const recoveryCode = ts.transpileModule(readFileSync(new URL('../src/lib/recoveryStorage.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
  const entries = { draft: 'old' }, exports = {}
  vm.runInNewContext(recoveryCode, { exports, window: { LingxiShell: { recovery: { read: key => ({ [key]: entries[key] ?? null }), set: (key, value) => { entries[key] = value; return { ok: true } } } } }, localStorage: { setItem() { throw Error('quota') }, removeItem() { throw Error('blocked') } } })
  assert.equal(exports.recoveryStorage.getItem('draft'), 'old')
  entries.draft = 'from another window'
  assert.equal(exports.recoveryStorage.getItem('draft'), 'from another window')
  exports.recoveryStorage.setItem('draft', 'new')
  exports.recoveryStorage.removeItem('draft')
  assert.equal(exports.recoveryStorage.getItem('draft'), null)
})
