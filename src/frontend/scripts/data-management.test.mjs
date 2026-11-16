import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

// Compile in memory only. No React renderer, service, APPDATA access or emitted files.
const panelUrl = new URL('../src/components/settings/DataManagementPanel.tsx', import.meta.url)
const source = readFileSync(panelUrl, 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } }).outputText
const exports = {}
const json = (method, body) => ({ method, body: body === undefined ? undefined : JSON.stringify(body) })
vm.runInNewContext(compiled, { exports, Blob, AbortController, AbortSignal, Date, Set, Map, require: (name) => {
  if (name === '@/api/client') return { json }
  if (name === 'react') return {}
  if (name === 'react/jsx-runtime') return { jsx: () => { throw new Error('测试不得渲染真实组件') }, jsxs: () => { throw new Error('测试不得渲染真实组件') } }
  return {}
} }, { filename: 'DataManagementPanel.contract.cjs' })
const { parseStorage, formatDataBytes, createRequestSequence, loadDataStorage, parseRuntimeFeatures, loadRuntimeFeatures, runtimeFeatureLabel, isCleanablePreviewPath, parseCleanupPreview, previewDataCleanup, confirmDataCleanup, parseImportResult, importDataBackup } = exports
const storage = () => ({
  lifecycle: { data_dir: 'SYNTHETIC/APPDATA/Lingxi', program_dir: 'SYNTHETIC/program', data_dir_source: 'APPDATA', independent_of_program: true, copy_program_copies_data: false, copy_notice: '只复制程序不带走数据', uninstall_reinstall: '保留数据', downgrade: '匹配版本新目录', installer_complete: false, restart_required_after_import: true },
  inventory: { format: 'lingxi-data-inventory', version: 1, categories: [{ id: 'memory', label: '记忆', coverage: 'included', paths: ['lingxi.db'], present_paths: ['lingxi.db'], limitations: '同库占用不能重复计算' }], credentials: { coverage: 'excluded', secret_values_exported: false, limitation: '只有引用，跨电脑重新授权' }, excluded: ['program_source'], plugin_restore_policy: '不加载插件', backup_kinds: { user: 'ZIP', migration: 'snapshot', source_development: 'source' } },
  groups: [{ name: 'lingxi.db', bytes: 2048, files: 1, cleanable: false }], bytes: 2048, files: 1, truncated: false, cleanup_categories: ['rebuildable_cache', 'logs', 'temporary'], cleanup_notice: '不动业务或未知文件',
})
const preview = () => ({ token: 'synthetic-one-shot-token-123456789', expires_at: Date.now() / 1000 + 300, files: [{ path: 'logs/synthetic.log', size: 12, sha256: 'a'.repeat(64), identity: [0, 1, 33188, 12, 123, 456] }], bytes: 12, skipped: ['logs/unknown.txt'], confirmation_required: true })
const imported = () => ({ path: 'SYNTHETIC/imports/restored-new', files: 1, active_data_dir: 'SYNTHETIC/APPDATA/Lingxi', activated: false, restart_required: true, steps: ['退出后设置路径', '新电脑重新保存 Key'], notice: '未切换数据' })
const transport = (value) => {
  const calls = []
  return { calls, request: async (path, options) => { calls.push({ path, options }); return value } }
}

test('storage reads the real overview once with cancellation and no write', async () => {
  const fixture = storage(), { request, calls } = transport(fixture), controller = new AbortController()
  assert.equal(await loadDataStorage(request, controller.signal), fixture)
  assert.equal(calls[0].path, '/v6/data/storage')
  assert.equal(calls[0].options.signal, controller.signal)
  assert.equal(calls[0].options.method, undefined)
  assert.equal(calls[0].options.body, undefined)
  assert.equal(calls.length, 1)
})
test('missing or malformed storage never becomes invented zero, path or enabled state', async () => {
  for (const corrupt of [null, {}, { ...storage(), bytes: -1 }, { ...storage(), files: NaN }, { ...storage(), lifecycle: {} }, { ...storage(), inventory: {} }, { ...storage(), groups: [{ name: 'x', bytes: -1, files: 0, cleanable: false }] }]) assert.throws(() => parseStorage(corrupt), /合同无效/)
  await assert.rejects(loadDataStorage(async () => { throw new Error('synthetic offline') }), /synthetic offline/)
})
test('path source, partial scan and unknown groups are preserved rather than declared independent or cleanable', () => {
  const value = storage()
  value.lifecycle.data_dir_source = 'LINGXI_DATA_DIR'; value.lifecycle.independent_of_program = false
  value.truncated = true; value.groups.push({ name: 'unknown-business', bytes: 99, files: 1, cleanable: false, limitation: '扫描受限' })
  assert.equal(parseStorage(value), value)
  assert.equal(value.groups[1].cleanable, false)
})
test('machine inventory reads runtime source with cancellation, never static enabled flags', async () => {
  const fixture = { source: 'runtime', data_root: 'SYNTHETIC/APPDATA/Lingxi', items: [{ id: 'memory-v2', name: 'V2', state: 'unavailable', implemented: false, installed: false, enabled: false, note: '未安装，旧记忆仍是主库' }], verification: { formal_user_data: 'not_tested' } }
  const { request, calls } = transport(fixture), controller = new AbortController()
  assert.equal(await loadRuntimeFeatures(request, controller.signal), fixture)
  assert.equal(calls[0].path, '/v11/features')
  assert.equal(calls[0].options.signal, controller.signal)
  assert.equal(calls[0].options.body, undefined)
  assert.equal(runtimeFeatureLabel(fixture.items[0]), '不可用 / 未安装')
  assert.equal(runtimeFeatureLabel({ ...fixture.items[0], implemented: true, installed: true, state: 'installed_disabled' }), '已安装 · 未启用')
  assert.equal(runtimeFeatureLabel({ ...fixture.items[0], implemented: true, installed: true, enabled: true, state: 'installed_enabled' }), '已安装 · 已启用')
})
test('machine feature flags reject fake sources or enabled-but-uninstalled and keep deferred state truthful', () => {
  const feature = { id: 'memory-v2', name: 'V2', state: 'unavailable', implemented: false, installed: false, enabled: false, note: 'unknown' }
  const fixture = { source: 'runtime', data_root: 'SYNTHETIC', items: [feature], verification: { external_services: 'not_tested' } }
  assert.equal(parseRuntimeFeatures(fixture), fixture)
  for (const value of [{ ...fixture, source: 'demo' }, { ...fixture, items: [{ ...feature, enabled: true }] }, { ...fixture, items: [{ ...feature, installed: 'true' }] }, { ...fixture, verification: null }]) assert.throws(() => parseRuntimeFeatures(value), /合同无效/)
  assert.equal(runtimeFeatureLabel({ ...feature, state: 'deferred' }), '本轮延期 · 未安装')
  assert.equal(runtimeFeatureLabel({ ...feature, state: 'planned' }), '规划中 · 未安装')
})
test('byte formatting preserves zero and refuses unknown or non-finite values', () => {
  assert.equal(formatDataBytes(0), '0 B')
  assert.equal(formatDataBytes(1024), '1.00 KiB')
  assert.equal(formatDataBytes(2 * 1024 ** 3), '2.00 GiB')
  for (const value of [-1, NaN, Infinity, undefined, '1024']) assert.equal(formatDataBytes(value), '未知')
})
test('request sequence suppresses late success, failure and finally after refresh or unmount', () => {
  const sequence = createRequestSequence(), first = sequence.begin(), second = sequence.begin()
  assert.equal(sequence.isCurrent(first), false)
  assert.equal(sequence.isCurrent(second), true)
  sequence.invalidate()
  assert.equal(sequence.isCurrent(second), false)
  assert.equal(sequence.isCurrent(sequence.begin()), true)
})
test('cleanup preview sends categories only, never paths, and does not confirm or delete', async () => {
  const fixture = preview(), { request, calls } = transport(fixture)
  assert.equal(await previewDataCleanup(request, ['logs', 'temporary', 'logs']), fixture)
  assert.equal(calls[0].path, '/v6/data/cleanup/preview')
  assert.equal(calls[0].options.method, 'POST')
  assert.deepEqual(JSON.parse(calls[0].options.body), { categories: ['logs', 'temporary'] })
  assert.equal(calls.length, 1)
})
test('unknown or empty cleanup categories are refused before transport', async () => {
  const { request, calls } = transport(preview())
  for (const categories of [[], ['attachments'], ['plugin-packages'], ['logs', 'cache'], ['../temp']]) await assert.rejects(previewDataCleanup(request, categories), /已登记/)
  assert.equal(calls.length, 0)
})
test('preview requires full file size, SHA-256, identity, confirmation and correct total', () => {
  for (const mutate of [v => { v.files[0].sha256 = 'partial' }, v => { v.files[0].size = -1 }, v => { v.files[0].identity = [] }, v => { delete v.files[0].identity }, v => { v.bytes = 13 }, v => { v.confirmation_required = false }, v => { v.token = 'short' }, v => { v.files.push({ ...v.files[0] }); v.bytes *= 2 }]) {
    const fixture = preview(); mutate(fixture); assert.throws(() => parseCleanupPreview(fixture), /合同无效/)
  }
})
test('only registered cache/log/temp file types are eligible; business, databases, links and traversal are refused', () => {
  for (const path of ['logs/a.log', 'logs/a.log.2', 'tmp/a.tmp', 'temp/a.part', 'cache/rebuildable/a.idx', 'cache/thumbnails/a.webp', 'cache/embeddings/a.npy']) assert.equal(isCleanablePreviewPath(path), true, path)
  for (const path of ['lingxi.db', 'logs/lingxi.db-wal', 'logs/a.sqlite3', 'attachments/a.tmp', 'characters/a.log', 'plugin-packages/data/a.tmp', 'logs/plugin-packages/a.log', 'logs/unknown.txt', 'cache/unknown/a.cache', 'temp/../a.tmp', '/temp/a.tmp', 'C:/temp/a.tmp', 'temp\\a.tmp', 'temp//a.tmp', 'logs/characters/a.log']) {
    assert.equal(isCleanablePreviewPath(path), false, path)
    const fixture = preview(); fixture.files[0].path = path; assert.throws(() => parseCleanupPreview(fixture), /合同无效/)
  }
})
test('cleanup confirm requires separate acknowledgement and a live nonempty preview before transport', async () => {
  const { request, calls } = transport({})
  await assert.rejects(confirmDataCleanup(request, preview(), false), /明确确认/)
  await assert.rejects(confirmDataCleanup(request, { ...preview(), expires_at: 1 }, true), /过期/)
  await assert.rejects(confirmDataCleanup(request, { ...preview(), files: [], bytes: 0 }, true), /明确确认/)
  assert.equal(calls.length, 0)
})
test('cleanup confirm sends a one-shot token only and checks actual deleted candidates', async () => {
  const fixture = preview(), result = { deleted: ['logs/synthetic.log'], files: 1, bytes: 12, business_data_deleted: false }, { request, calls } = transport(result)
  assert.equal(await confirmDataCleanup(request, fixture, true), result)
  assert.equal(calls[0].path, '/v6/data/cleanup/confirm')
  assert.deepEqual(JSON.parse(calls[0].options.body), { token: fixture.token })
  assert.equal(calls.length, 1)
})
test('cleanup failures and unexpected deletion results never claim success or auto-retry', async () => {
  let calls = 0
  await assert.rejects(confirmDataCleanup(async () => { calls++; throw new Error('fingerprint changed') }, preview(), true), /fingerprint changed/)
  assert.equal(calls, 1)
  for (const result of [{ deleted: ['attachments/a'], files: 1, bytes: 12, business_data_deleted: false }, { deleted: ['logs/synthetic.log'], files: 1, bytes: 12, business_data_deleted: true }, { deleted: ['logs/synthetic.log'], files: 1, bytes: 99, business_data_deleted: false }]) await assert.rejects(confirmDataCleanup(async () => result, preview(), true), /合同无效/)
})
test('controlled import streams raw ZIP with no client path, form, activation or settings write', async () => {
  const fixture = imported(), { request, calls } = transport(fixture), file = new Blob(['synthetic zip bytes'])
  assert.equal(await importDataBackup(request, file), fixture)
  assert.equal(calls[0].path, '/v6/data/import')
  assert.equal(calls[0].options.method, 'POST')
  assert.equal(calls[0].options.headers['Content-Type'], 'application/zip')
  assert.equal(calls[0].options.body, file)
  assert.equal(calls.length, 1)
  assert.equal(fixture.activated, false)
})
test('empty or oversized imports are refused locally and backend failures propagate', async () => {
  const { request, calls } = transport(imported())
  await assert.rejects(importDataBackup(request, new Blob([])), /空备份/)
  await assert.rejects(importDataBackup(request, { size: 2 * 1024 ** 3 + 64 * 1024 ** 2 + 1 }), /容量限制/)
  assert.equal(calls.length, 0)
  await assert.rejects(importDataBackup(async () => { throw new Error('schema mismatch') }, new Blob(['zip'])), /schema mismatch/)
})
test('import refuses activated, overwritten or malformed results and never auto-switches data', () => {
  for (const mutate of [v => { v.activated = true }, v => { v.restart_required = false }, v => { v.path = v.active_data_dir }, v => { delete v.steps }, v => { v.files = -1 }]) {
    const value = imported(); mutate(value); assert.throws(() => parseImportResult(value), /合同无效/)
  }
})
test('settings connects the data tab only and removes the obsolete no-HTTP-restore claim', () => {
  const settings = readFileSync(new URL('../src/pages/SettingsPage.tsx', import.meta.url), 'utf8')
  assert.match(settings, /<TabsContent value="data"[\s\S]*<DataManagementPanel section=\{dataSection\}/)
  const connections = readFileSync(new URL('../src/pages/ConnectionsPage.tsx', import.meta.url), 'utf8')
  assert.match(connections, /<ModelSettings section="modules"/)
  assert.match(connections, /<ModelSettings section="profiles"/)
  assert.match(settings, /navigate\('\/connections\?tab=assignments'/)
  assert.doesNotMatch(settings, /<TabsContent value="models"/)
  assert.doesNotMatch(settings, /完整备份没有 HTTP 恢复接口|const backup = async/)
})
test('backend route aliases and raw ZIP fields match the read-only source contract', () => {
  const backend = readFileSync(new URL('../../app/data_api.py', import.meta.url), 'utf8')
  for (const path of ['/data/storage', '/data/cleanup/preview', '/data/cleanup/confirm', '/data/import', '/data/backup']) assert.ok(backend.includes(`"${path}"`) || backend.includes(`'${path}'`), path)
  assert.match(backend, /application\/zip/)
  assert.match(backend, /request\.stream\(\)/)
  const features = readFileSync(new URL('../../app/feature_status.py', import.meta.url), 'utf8')
  assert.match(features, /\/v11\/features/)
  assert.match(features, /authority/)
})
test('existing download and safety UI remain wired with truthful offline, key and race explanations', () => {
  assert.match(source, /lingxi\.backup\(\{ appearance: theme, browser:collectBrowserPreferences\(\) \}\)/)
  assert.match(source, /downloadBlob\(blob,/)
  assert.match(source, /preview\.files\.map/)
  assert.match(source, /file\.sha256/)
  assert.match(source, /JSON\.stringify\(file\.identity\)/)
  assert.match(source, /readSequence\.current\.isCurrent\(attempt\)/)
  assert.match(source, /actionSequence\.current\.isCurrent\(attempt\)/)
  assert.match(source, /setRefreshing\(false\)/)
  assert.match(source, /mutationLock\.current/)
  assert.match(source, /python -m app\.release restore/)
  assert.match(source, /LingxiServer\.exe --restore/)
  assert.match(source, /跨电脑必须重新授权/)
  assert.match(source, /可能已部分清理/)
  assert.match(source, /不会自动重试/)
  assert.doesNotMatch(source, /localStorage\.setItem|window\.location|\/data\/activate|\/data\/switch|fetch\(/)
})
