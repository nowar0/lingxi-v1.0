import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { MEMORY_PAGE_SIZE, deletedMemoryForOwner, lastMemoryPageOffset, loadMemoryHistory, memoryCategories, memoryOwnerKey, memorySearchNotice } from '../src/lib/memoryBrowser.ts'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const source = (file) => fs.readFileSync(path.join(root, file), 'utf8')
// Transpile to memory and inject a stub transport: no server, network, browser,
// application bootstrap or writes to dist/static are used by these tests.
const createApi = (request) => {
  const { outputText } = ts.transpileModule(source('src/api/lingxi.ts'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } })
  const module = { exports: {} }
  new Function('require', 'module', 'exports', outputText)((id) => {
    assert.equal(id, './client')
    return { api: request, apiBlob: () => { throw new Error('unexpected blob request') }, json: () => { throw new Error('unexpected mutation') } }
  }, module, module.exports)
  return module.exports.lingxi
}

test('memory API forwards offset/limit/session and preserves backend paging counts', async () => {
  const calls = []
  const raw = { items: [{ id: 'memory-1', source: { origin: '用户添加' }, scope: 'work' }], total: 237, offset: 200, limit: 50 }
  const api = createApi(async (url) => { calls.push(url); return raw })
  const page = await api.memories('work', 42, { offset: 200, limit: 50 })
  assert.equal(calls[0], '/v4/memory?scope=work&offset=200&limit=50&session_id=42')
  assert.deepEqual({ total: page.total, offset: page.offset, limit: page.limit }, { total: 237, offset: 200, limit: 50 })
  assert.equal(page.items[0].source, '用户添加')
  assert.equal(raw.items[0].source.origin, '用户添加')
  await api.memories('companion')
  assert.equal(calls[1], '/v4/memory?scope=companion&offset=0&limit=200')
})

test('search API passes through semantic degradation, notes and distinct totals', async () => {
  const calls = []
  const raw = { items: [], semantic_status: 'failed', semantic_note: '向量索引不可用', note: '保留关键词', total: 2, total_records: 237, semantic_chunks: 0, semantic_backend: 'local_vector' }
  const api = createApi(async (url) => { calls.push(url); return raw })
  assert.deepEqual(await api.searchMemories('work', '旧聊天 & 计划', 42), raw)
  assert.equal(calls[0], `/v4/memory/search?scope=work&q=${encodeURIComponent('旧聊天 & 计划')}&semantic=true&session_id=42`)
})

test('deletion undo is bound to scope and exact work-session owner', () => {
  const deleted = { ownerKey: memoryOwnerKey('work', 42), item: { id: 'same-id', status: 'deleted' } }
  assert.equal(deletedMemoryForOwner(deleted, memoryOwnerKey('work', 42)), deleted.item)
  for (const key of [memoryOwnerKey('work', 43), memoryOwnerKey('work'), memoryOwnerKey('companion'), memoryOwnerKey('shared')]) assert.equal(deletedMemoryForOwner(deleted, key), undefined)
  assert.equal(memoryOwnerKey('companion', 42), memoryOwnerKey('companion', 43))
})

test('active memory page mounts V2 and keeps each scope and work session isolated', () => {
  const page = source('src/pages/MemoryPage.tsx')
  const panel = source('src/components/memory/MemoryV2Panel.tsx')
  assert.match(page, /<MemoryV2Panel workSessionId=\{activeWorkSessionId\}/)
  assert.match(panel, /<MemoryV2Browser key=\{`\$\{scope\}:\$\{workSessionId \?\? 'none'\}`\}/)
  assert.match(panel, /view === 'star' \? '列表' : '星空'/)
  assert.match(panel, /aria-label=\{`切换到\$\{view === 'star' \? '列表' : '星空'\}视图`\}/)
})

test('deleting a final-page item clamps to the last existing backend page', () => {
  assert.equal(MEMORY_PAGE_SIZE, 50)
  for (const [total, expected] of [[0, 0], [1, 0], [50, 0], [51, 50], [200, 150], [237, 200]]) assert.equal(lastMemoryPageOffset(total), expected)
})

test('source 404 does not hide deleted memory versions', async () => {
  const calls = [], versions = [{ revision: 1, value: '原记忆', status: 'active' }, { revision: 2, status: 'deleted' }]
  const result = await loadMemoryHistory(async (url) => {
    calls.push(url)
    if (url.endsWith('/source')) throw Object.assign(new Error('记忆不存在'), { status: 404 })
    return { versions }
  }, 'deleted-id')
  assert.deepEqual(calls, ['/v4/memory/deleted-id/source', '/v4/memory/deleted-id/versions'])
  assert.equal(result.versions, versions)
  assert.match(result.source.note, /来源原文已不可用/)
  assert.equal(result.versionsNote, undefined)
})

test('independent source and version errors are visible rather than failing Promise.all', async () => {
  const result = await loadMemoryHistory(async (url) => {
    if (url.endsWith('/source')) return { messages: [{ id: 1, role: 'user', content: '仍能读取来源' }] }
    throw new Error('版本读取超时')
  }, 'item')
  assert.equal(result.source.messages[0].content, '仍能读取来源')
  assert.deepEqual(result.versions, [])
  assert.match(result.versionsNote, /版本读取超时/)
  const bothFailed = await loadMemoryHistory(async () => { throw new Error('离线') }, 'item')
  assert.match(bothFailed.source.note, /来源读取失败/)
  assert.match(bothFailed.versionsNote, /历史版本读取失败/)
})

test('failed, disabled and unknown semantic states cannot look like full semantic search', () => {
  for (const status of ['failed', 'unavailable', 'unknown']) {
    const note = memorySearchNotice({ semantic_status: status, semantic_note: 'CPU 模型不可用', note: '后端说明' })
    assert.match(note, /降级为关键词/)
    assert.match(note, /CPU 模型不可用/)
    assert.match(note, /后端说明/)
  }
  assert.match(memorySearchNotice({ semantic_status: 'disabled' }), /已关闭.*仅显示关键词/)
  assert.equal(memorySearchNotice({ semantic_status: 'complete', note: '本地CPU向量' }), '本地CPU向量')
})

test('category options match exactly the existing backend category set', () => {
  const backend = source('../app/memory/stage7.py')
  const shared = backend.match(/SHARED_CATEGORIES = \{([^}]+)\}/)[1]
  const categories = backend.match(/CATEGORIES = SHARED_CATEGORIES \| \{([^}]+)\}/)[1]
  const expected = [...(shared + categories).matchAll(/"([a-z_]+)"/g)].map((match) => match[1]).sort()
  assert.deepEqual(memoryCategories.map(([id]) => id).sort(), expected)
  assert.match(source('src/pages/MemoryPage.tsx'), /memoryCategories\.map\(/)
})

test('settings distinguish historical chat RAG, project data and legacy compatibility fields', () => {
  const settings = source('src/components/shared/MemorySettings.tsx')
  assert.match(settings, /历史聊天 RAG/)
  assert.match(settings, /不是项目资料检索/)
  assert.match(settings, /summarize_every_turns.*semantic_max_chunks.*不再控制新聊天/)
  assert.doesNotMatch(settings, /\['summarize_every_turns'|\['semantic_max_chunks'/)
  assert.match(settings, /settingsPayload\(value,memorySettingFields\)/)
  assert.match(settings, /blockedShare/)
  assert.doesNotMatch(settings, /项目检索最多片段|项目检索最低相关度|仅检索当前绑定项目/)
})
