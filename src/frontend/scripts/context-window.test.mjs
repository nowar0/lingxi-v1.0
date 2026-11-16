import { test } from 'node:test'
import assert from 'node:assert/strict'
import { contextUsage, loadContextWindow } from '../src/lib/contextWindow.ts'

const fixture = (used = 6000, limit = 24000) => ({ initial_context: { estimated_input_tokens: used, available_input_tokens: limit, system: '系统与记忆', messages: [{ role: 'user', content: '<script>不执行内容</script>' }] } })
test('context ratio uses this request input budget, independent of account cumulative usage', () => {
  assert.deepEqual(contextUsage(fixture()), { used: 6000, limit: 24000, remaining: 18000, percent: 25, ring: 25 })
})
test('zero remains zero and overflow preserves actual percentage while clamping the ring', () => {
  assert.equal(contextUsage(fixture(0)).percent, 0)
  assert.deepEqual(contextUsage(fixture(30000)), { used: 30000, limit: 24000, remaining: 0, percent: 125, ring: 100 })
})
test('unknown or invalid budgets never become invented percentages', () => {
  for (const [used, limit] of [[-1, 20], [Infinity, 20], [NaN, 20], [10, 0], [10, NaN], [10, undefined]]) assert.throws(() => contextUsage(fixture(used, limit === undefined ? null : limit)))
})
test('preview reads the selected work session only and forwards cancellation, no body or mutation', async () => {
  const calls = [], controller = new AbortController(), value = fixture()
  const request = async (path, options) => { calls.push({ path, options }); return value }
  assert.equal(await loadContextWindow(request, 42, controller.signal), value)
  assert.equal(calls[0].path, '/v3/sessions/42/context?mode=work')
  assert.equal(calls[0].options.signal, controller.signal)
  assert.equal(calls[0].options.body, undefined)
  assert.equal(calls[0].options.method, undefined)
})
test('failed or malformed context is reported rather than showing false empty context', async () => {
  await assert.rejects(loadContextWindow(async () => { throw new Error('离线') }, 42), /离线/)
  await assert.rejects(loadContextWindow(async () => fixture(1, 0), 42), /预算数据无效/)
})
