import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/components/settings/speech-package-controller.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const exports = {}
vm.runInNewContext(compiled, { exports, AbortController, AbortSignal, setTimeout, clearTimeout, URL, Audio: class {}, Error, Map, Set, Promise }, { filename: 'speech-package-controller.cjs' })
const { createSpeechPackagesController } = exports
const clone = value => JSON.parse(JSON.stringify(value))
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const settle = async () => { for (let i = 0; i < 30; i++) await Promise.resolve() }
function harness() {
  const h = { calls: [], changes: [], timers: new Map(), next: 0, created: [], revoked: [], audios: [], notices: [], refreshes: 0, handler: undefined, blobHandler: undefined,
    packages: [{ id: 'cosyvoice2', name: 'CosyVoice2', kind: 'tts', installed: true, runtime_ready: true, version: '1', versions: [], pointer_revision: 0 }],
    engines: [{ engine: 'cosyvoice2', name: 'CosyVoice2', kind: 'tts', ready: true, installed: true }], moduleRevision: 3 }
  h.api = async (path, options = {}) => {
    h.calls.push({ path, ...options })
    if (h.handler) { const result = h.handler(path, options); if (result !== undefined) return result }
    if (path === '/v6/model-packages') return { items: clone(h.packages) }
    if (path === '/v6/speech/engines') return { items: clone(h.engines) }
    if (path.endsWith('/settings')) return { revision: 2, device: 'cpu', hotwords: [] }
    if (path === '/v6/modules') return { items: [{ id: 'tts', revision: h.moduleRevision }] }
    if (path === '/v6/model-packages/download') { h.packages[0].job = { id: 'job-1', status: 'downloading' }; return { id: 'job-1' } }
    return {}
  }
  h.controller = createSpeechPackagesController({
    api: h.api, blob: (...args) => h.blobHandler ? h.blobHandler(...args) : Promise.resolve({ blob: true }), changed: value => h.changes.push(value), modelsChanged: async () => { h.refreshes++ }, success: s => h.notices.push(s),
    setTimer: (fn, delay) => { const id = ++h.next; h.timers.set(id, { fn, delay }); return id }, clearTimer: id => h.timers.delete(id),
    createUrl: () => { const url = 'blob:' + (h.created.length + 1); h.created.push(url); return url }, revokeUrl: url => h.revoked.push(url),
    audio: () => { const a = { src: '', pauseCount: 0, loadCount: 0, play: async () => {}, pause() { a.pauseCount++ }, load() { a.loadCount++ }, removeAttribute() { a.src = '' } }; h.audios.push(a); return a },
  })
  h.fire = () => { const [id, entry] = h.timers.entries().next().value; h.timers.delete(id); entry.fn(); return entry.delay }
  return h
}

test('idle polls slow down; active downloads poll quickly only after the previous request finishes', async () => {
  const h = harness(); await h.controller.start()
  assert.equal([...h.timers.values()][0].delay, 15000)
  const slow = deferred(); h.handler = path => path === '/v6/model-packages' ? slow.promise : undefined
  assert.equal(h.fire(), 15000); await settle()
  assert.equal(h.timers.size, 0)
  const count = h.calls.filter(c => c.path === '/v6/model-packages').length
  await settle(); assert.equal(h.calls.filter(c => c.path === '/v6/model-packages').length, count)
  slow.resolve({ items: [{ ...h.packages[0], job: { id: 'j', status: 'downloading' } }] }); await settle()
  assert.equal([...h.timers.values()][0].delay, 2000)
  h.controller.dispose(); assert.equal(h.timers.size, 0)
})

test('dispose aborts both inventory and settings requests; late results cannot publish state', async () => {
  const h = harness(), response = deferred(); h.handler = () => response.promise
  const started = h.controller.start(); await settle(); h.controller.dispose()
  assert.ok(h.calls.length >= 5); assert.ok(h.calls.every(c => c.signal.aborted))
  response.resolve({ items: [], hotwords: [], revision: 0, device: 'cpu' }); await started
  assert.equal(h.changes.length, 0); assert.equal(h.timers.size, 0)
})

test('manual refresh invalidates an older poll snapshot even if transport ignores abort', async () => {
  const h = harness(); await h.controller.start(); const old = deferred()
  h.handler = path => path === '/v6/model-packages' ? old.promise : undefined
  h.fire(); await settle(); const request = h.calls.findLast(c => c.path === '/v6/model-packages')
  h.handler = undefined; h.packages[0].name = 'current'
  await h.controller.refresh(); assert.equal(request.signal.aborted, true)
  old.resolve({ items: [{ id: 'stale', name: 'stale' }] }); await settle()
  assert.equal(h.controller.snapshot().packages[0].name, 'current'); h.controller.dispose()
})

test('manual refresh reads one inventory snapshot instead of issuing a second refresh', async () => {
  const h = harness(); await h.controller.start(); h.calls = []
  await h.controller.refresh()
  assert.equal(h.calls.filter(c => c.path === '/v6/model-packages').length, 1)
  assert.equal(h.calls.filter(c => c.path === '/v6/speech/engines').length, 1)
  h.controller.dispose()
})

test('individual engine setting failures are visible without hiding the package list', async () => {
  const h = harness()
  h.handler = path => { if (path === '/v6/speech/cosyvoice2/settings') throw new Error('offline') }
  await h.controller.start()
  assert.equal(h.controller.snapshot().packages.length, 1)
  assert.match(h.controller.snapshot().optionErrors.cosyvoice2, /读取失败/)
  h.controller.dispose()
})

test('switching, ending, stopping and disposing audio each release their object URL exactly once', async () => {
  const h = harness(); await h.controller.start()
  await h.controller.preview('cosyvoice2'); const first = h.audios[0], oldEnded = first.onended
  await h.controller.preview('cosyvoice2'); assert.deepEqual(h.revoked, ['blob:1']); assert.equal(first.src, '')
  oldEnded(); assert.deepEqual(h.revoked, ['blob:1'])
  h.audios[1].onended(); assert.deepEqual(h.revoked, ['blob:1', 'blob:2'])
  await h.controller.preview('cosyvoice2'); h.controller.stopAudio(); assert.deepEqual(h.revoked, ['blob:1', 'blob:2', 'blob:3'])
  await h.controller.preview('cosyvoice2'); h.controller.dispose(); assert.deepEqual(h.revoked, h.created)
  assert.ok(h.audios.every(a => a.pauseCount === 1 && a.loadCount === 1))
})

test('a synthesis response after leaving the page never creates an audio URL or notification', async () => {
  const h = harness(), response = deferred(); await h.controller.start()
  h.blobHandler = () => response.promise
  const preview = h.controller.preview('cosyvoice2'); await settle(); h.controller.dispose(); const count = h.changes.length
  response.resolve({}); await preview
  assert.equal(h.created.length, 0); assert.equal(h.changes.length, count); assert.equal(h.notices.length, 0)
})

test('playback failure and media error release resources and preserve a readable error', async () => {
  const h = harness(); await h.controller.start()
  await h.controller.preview('cosyvoice2'); h.audios[0].onerror(); assert.equal(h.revoked.length, 1)
  assert.match(h.controller.snapshot().error, /试听音频未能播放/)
  h.controller.dispose()
})

test('download completion during another operation stays queued and activates once after it finishes', async () => {
  const h = harness(); await h.controller.start(); await h.controller.download(h.packages[0])
  const device = deferred(); h.handler = path => path === '/v6/speech/device-check' ? device.promise : undefined
  const operation = h.controller.detectDevice(); h.packages[0].job.status = 'complete'
  await settle(); assert.equal(h.calls.filter(c => c.path.endsWith('/activate')).length, 0)
  device.resolve({ cpu: true, cuda: false }); await operation; await settle()
  const activations = h.calls.filter(c => c.path.endsWith('/activate'))
  assert.equal(activations.length, 1); assert.equal(JSON.parse(activations[0].body).expected_revision, 3)
  assert.equal(h.refreshes, 1); h.controller.dispose()
})

test('auto activation keeps the requested module revision; a later assignment is never overwritten or retried', async () => {
  const h = harness(); await h.controller.start(); await h.controller.download(h.packages[0])
  h.moduleRevision = 8; h.packages[0].job.status = 'complete'
  h.handler = path => { if (path.endsWith('/activate')) throw new Error('模块已变化，请刷新') }
  h.fire(); await settle()
  assert.equal(JSON.parse(h.calls.find(c => c.path.endsWith('/activate')).body).expected_revision, 3)
  assert.match(h.controller.snapshot().error, /模块已变化/)
  h.fire(); await settle(); assert.equal(h.calls.filter(c => c.path.endsWith('/activate')).length, 1)
  h.controller.dispose()
})

test('pausing or replacing a job cancels its activation intent without touching a different job', async () => {
  for (const action of ['pause', 'replace']) {
    const h = harness(); await h.controller.start(); await h.controller.download(h.packages[0])
    if (action === 'pause') await h.controller.pause(h.packages[0]); else h.packages[0].job.id = 'another-job'
    h.packages[0].job.status = 'complete'; h.fire(); await settle()
    assert.equal(h.calls.filter(c => c.path.endsWith('/activate')).length, 0); h.controller.dispose()
  }
})

test('stop cancels in-flight synthesis before releasing the engine; repeated stop is coalesced', async () => {
  const h = harness(); await h.controller.start()
  h.blobHandler = (path, { signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(Object.assign(new Error('abort'), { name: 'AbortError' }))))
  const preview = h.controller.preview('cosyvoice2'); await settle()
  await Promise.all([h.controller.stop('cosyvoice2'), h.controller.stop('cosyvoice2'), preview])
  assert.equal(h.calls.filter(c => c.path.endsWith('/stop')).length, 1)
  assert.equal(h.created.length, 0); assert.equal(h.controller.snapshot().busy, ''); h.controller.dispose()
})

test('stop playback also cancels synthesis that has not returned audio yet', async () => {
  const h = harness(); await h.controller.start()
  h.blobHandler = (path, { signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(Object.assign(new Error('abort'), { name: 'AbortError' }))))
  const preview = h.controller.preview('cosyvoice2'); await settle(); h.controller.stopAudio(); await preview
  assert.equal(h.created.length, 0); assert.equal(h.controller.snapshot().busy, ''); h.controller.dispose()
})
