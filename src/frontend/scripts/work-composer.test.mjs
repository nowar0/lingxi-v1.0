import { test } from 'node:test'
import assert from 'node:assert/strict'
import { effortLevels, normalizedEffort, modelLabel, saveWorkSelection, workModelChoices, availableEfforts, compatibleEffort } from '../src/lib/workComposerSettings.ts'

function transport(binding, options = {}) {
  const calls = []
  const request = async (path, init) => {
    const body = init?.body ? JSON.parse(init.body) : undefined
    calls.push({ path, method: init?.method || 'GET', body })
    if (!init) return structuredClone(binding)
    if (path.includes('/presets')) {
      if (options.failPreset) throw new Error('模拟保存失败')
      return { ...body, id: binding.preset?.name === body.name ? binding.preset.id : 'isolated-new', revision: body.expected_revision + 1 }
    }
    if (options.failBinding) throw new Error('模拟绑定冲突')
    return { revision: binding.revision + 1, preset: { id: body.preset_id } }
  }
  return { request, calls }
}
const inherited = { id: 'shared', revision: 3, name: '原有专业预设', model: 'work', effort: 'low', skill_ids: ['skill-a'], tools: ['workspace_read'], brief: '保留当前约束' }
test('module choices use backend-valid role keys and static provider full names', () => {
  const choices = workModelChoices({ roles: { work: 'module_work/fixture-a', companion: 'module_companion/fixture-b' }, models: ['provider/test-model'] })
  assert.deepEqual(choices.map(item => item.value), ['work', 'companion', 'provider/test-model'])
  assert.match(choices[1].label, /fixture-b/)
  assert.equal(workModelChoices({ roles: { chat: 'provider/test' }, models: [] })[0].value, 'chat')
})
test('model dependent effort levels and safe normalization', () => {
  assert.deepEqual(availableEfforts({ levels: ['low','high','max'] }), ['auto','low','high','max'])
  assert.deepEqual(availableEfforts({ levels: ['none','enabled'] }), ['auto','none','enabled'])
  assert.deepEqual(availableEfforts(), ['auto'])
  assert.equal(compatibleEffort('max', { levels: ['low','high'] }), 'auto')
  for (const level of effortLevels) assert.equal(normalizedEffort(level), level)
  assert.equal(normalizedEffort('invalid'), 'auto')
  assert.equal(modelLabel('module_work/provider/model'), 'provider/model')
})
test('first adjustment creates session preset and does not change global or companion state', async () => {
  const { request, calls } = transport({ revision: 0, preset: null })
  await saveWorkSelection(request, 17, 0, 'work', 'high')
  assert.equal(calls[1].method, 'POST')
  assert.equal(calls[1].body.effort, 'high')
  assert.equal('tools' in calls[1].body, false) // Let the backend supply its actual defaults.
  assert.deepEqual(calls[2].body, { preset_id: 'isolated-new', expected_revision: 0 })
  assert.ok(calls.every(call => !call.path.includes('ui-state') && !call.path.includes('modules')))
})
test('inherited skills, empty tool limits and instructions survive cloning', async () => {
  const { request, calls } = transport({ revision: 4, preset: { ...inherited, tools: [] } })
  await saveWorkSelection(request, 17, 4, 'module_work/another-model', 'max')
  assert.equal(calls[1].path, '/v7/presets')
  assert.deepEqual(calls[1].body.skill_ids, ['skill-a'])
  assert.deepEqual(calls[1].body.tools, [])
  assert.equal(calls[1].body.brief, inherited.brief)
})
test('subsequent adjustments reuse the dedicated preset with optimistic revision', async () => {
  const { request, calls } = transport({ revision: 8, preset: { ...inherited, name: '工作区输入栏 · 会话 #17' } })
  await saveWorkSelection(request, 17, 8, 'work', 'medium')
  assert.equal(calls[1].path, '/v7/presets/shared')
  assert.equal(calls[1].method, 'PUT')
  assert.equal(calls[1].body.expected_revision, 3)
})
test('stale binding aborts before any write', async () => {
  const { request, calls } = transport({ revision: 5, preset: inherited })
  await assert.rejects(saveWorkSelection(request, 17, 4, 'work', 'max'), /其他窗口/)
  assert.equal(calls.length, 1)
})
test('failed preset is never bound or reported saved', async () => {
  const { request, calls } = transport({ revision: 4, preset: inherited }, { failPreset: true })
  await assert.rejects(saveWorkSelection(request, 17, 4, 'work', 'max'), /模拟保存失败/)
  assert.equal(calls.length, 2)
})
test('binding conflict propagates to the UI', async () => {
  const { request } = transport({ revision: 4, preset: inherited }, { failBinding: true })
  await assert.rejects(saveWorkSelection(request, 17, 4, 'work', 'max'), /模拟绑定冲突/)
})
