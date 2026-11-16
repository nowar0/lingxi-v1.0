#!/usr/bin/env node
/** No app startup, browser, fixture memory DB, dependency install, build, or emitted artifacts.
 * TypeScript is the project's existing devDependency. Synthetic IDs exist only inside tests. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const nativeRequire = createRequire(import.meta.url), cache = new Map()
function load(relative) {
  const path = resolve(root, relative.endsWith('.ts') ? relative : `${relative}.ts`)
  if (cache.has(path)) return cache.get(path).exports
  const module = { exports: {} }; cache.set(path, module)
  const source = readFileSync(path, 'utf8')
  const result = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, strict: true }, fileName: path, reportDiagnostics: true })
  assert.equal(result.diagnostics?.length || 0, 0, `${relative} transpiles`)
  const require = specifier => specifier.startsWith('.') ? load(resolve(dirname(path), specifier)) : nativeRequire(specifier)
  new Function('require', 'module', 'exports', result.outputText)(require, module, module.exports)
  return module.exports
}
const core = load('src/lib/nebula.ts')
const { buildNebulaScene, stableHash, nodePosition, cameraMatrix, defaultCamera, projectPoint, filterNebulaNodes, idKey, themeName, placeNebulaLabels, LAYER_RADIUS, POINT_STRIDE } = core
let passed = 0
function test(name, body) { body(); passed++; console.log(`✓ ${name}`) }
const fixture = (id, extra = {}) => ({ id, title: `test-${id}`, layer: 'L1', scope: 'companion', importance: .6, activation: .4, theme: 'test-theme', ...extra })

test('compact density uses actual filtered memory counts, never aggregated sprite counts', () => {
  for (const count of [0, 1, 5]) assert.equal(core.nebulaIsCompact(count), true)
  for (const count of [6, 40, 10000, -1, 1.5, NaN, Infinity]) assert.equal(core.nebulaIsCompact(count), false)
  const nodes = Array.from({ length: 10000 }, (_, i) => fixture(i))
  const scene = buildNebulaScene(nodes, [])
  assert.ok(scene.items.length < 40)
  assert.equal(core.nebulaIsCompact(scene.filteredNodes.length), false, 'a large grouped scene stays standard')
  const filtered = buildNebulaScene(nodes, [], { query: 'test-9999' })
  assert.equal(core.nebulaIsCompact(filtered.filteredNodes.length), true)
  const component = readFileSync(resolve(root, 'src/components/memory/MemoryNebula.tsx'), 'utf8')
  assert.doesNotMatch(component, /<details|memory-nebula__directory|memory-nebula__filters|memory-nebula__heading/)
  assert.match(component, /onClick=\{onOpenList\}/)
  assert.match(component, /aria-describedby=\{helpId\}/)

})

test('hash/layout stable across reorder/filter; all four layers separate from scope', () => {
  assert.equal(stableHash('主题🫧'), stableHash('主题🫧')); assert.notEqual(stableHash('a'), stableHash('b'))
  const node = fixture('layout'), position = nodePosition(node)
  assert.deepEqual(nodePosition({ ...node }), position)
  assert.deepEqual(nodePosition({ ...node, scope: 'work' }), position)
  const radii = ['L1', 'L2', 'L3', 'L4'].map(layer => {
    const p = nodePosition({ ...node, layer }); return Math.hypot(p[0], p[2])
  })
  assert.equal(new Set(radii.map(value=>value.toFixed(4))).size,4); assert.equal(new Set(Object.values(core.LAYER_COLORS)).size,4)
  assert.equal(Object.keys(LAYER_RADIUS).length, 4)
  const ordered = [fixture(1), fixture(2), fixture(3)]
  const sceneA = buildNebulaScene(ordered, []), sceneB = buildNebulaScene([...ordered].reverse(), [])
  assert.deepEqual([...sceneA.points], [...sceneB.points]); assert.equal(sceneA.representedNodes, 3)
})

test('atmosphere is deterministic decoration and never becomes a memory or relation', () => {
  const scene=buildNebulaScene([fixture('only-real-memory')],[])
  const original={items:scene.items.length,represented:scene.representedNodes,edges:scene.edgeCounts.total}
  const dust=core.nebulaAtmosphere(scene)
  assert.deepEqual(dust,core.nebulaAtmosphere(scene))
  assert.ok(dust.length/POINT_STRIDE>=1100&&dust.length/POINT_STRIDE<=8500)
  for(let i=0;i<dust.length;i+=POINT_STRIDE){assert.equal(dust[i+8],-10000);for(let j=0;j<POINT_STRIDE;j++)assert.ok(Number.isFinite(dust[i+j]))}
  assert.deepEqual({items:scene.items.length,represented:scene.representedNodes,edges:scene.edgeCounts.total},original)
  assert.equal(scene.items[0].node.id,'only-real-memory')
})

test('ordinary memory sets stay individually selectable without manual cluster expansion', () => {
  const nodes=Array.from({length:30},(_,i)=>fixture(i))
  const scene=buildNebulaScene(nodes,[])
  assert.equal(scene.items.length,30)
  assert.ok(scene.items.every(item=>item.kind==='node'))
})

test('GPU perspective math: translation, rotation, perspective depth and near/far validity', () => {
  const camera = { ...defaultCamera(), yaw: 0, pitch: 0 }, matrix = cameraMatrix(camera, 2)
  const out = new Float32Array(16)
  projectPoint([0, 0, 0], matrix, 800, 400, out, 0)
  assert.ok(Math.abs(out[0] - 400) < .001); assert.ok(Math.abs(out[1] - 200) < .001)
  projectPoint([1, 0, 1], matrix, 800, 400, out, 4)
  projectPoint([1, 0, -1], matrix, 800, 400, out, 8)
  assert.ok(out[4] > out[8], 'near point is farther from center under perspective')
  assert.ok(out[7] > out[11], 'near point has larger screen radius')
  projectPoint([0, 0, 10], matrix, 800, 400, out, 12)
  assert.equal(out[14], -1, 'behind camera cannot be picked')
  const target = [1.1, -.2, .6]
  projectPoint(target, cameraMatrix({ ...camera, target, yaw: .9, pitch: -.4 }, 2), 800, 400, out)
  assert.ok(Math.abs(out[0] - 400) < .001 && Math.abs(out[1] - 200) < .001)
  const reusable = new Float32Array(16)
  assert.equal(cameraMatrix(camera, 1, reusable), reusable)
})

test('filters use real theme, layer, scope and text; never mutate supplied nodes', () => {
  const input = Object.freeze([Object.freeze(fixture('a')), Object.freeze(fixture('b', { layer: 'L4', scope: 'work', theme: '另一主题' })), Object.freeze(fixture('c', { theme: null }))])
  assert.equal(filterNebulaNodes(input, { layers: new Set(['L4']), scope: 'work' })[0].id, 'b')
  assert.equal(filterNebulaNodes(input, { theme: '另一主题', query: 'TEST-b' })[0].id, 'b')
  assert.equal(filterNebulaNodes(input, { layers: new Set() }).length, 0)
  assert.equal(themeName(input[2]), '未分主题'); assert.equal(input.length, 3)
  assert.equal(buildNebulaScene(input, [], { theme: 'unknown' }).representedNodes, 0)
})

test('node size only importance, brightness only activation; number/string IDs distinct', () => {
  const input = [fixture(1, { importance: 0, activation: 0 }), fixture('1', { importance: 1, activation: 1 })]
  const scene = buildNebulaScene(input, [])
  assert.equal(scene.items.length, 2); assert.notEqual(idKey(1), idKey('1'))
  const a = scene.itemIndex.get(idKey(1)) * POINT_STRIDE, b = scene.itemIndex.get(idKey('1')) * POINT_STRIDE
  assert.ok(scene.points[a + 6] < scene.points[b + 6]); assert.ok(scene.points[a + 7] < scene.points[b + 7])
  assert.equal(scene.points[a + 6], 8); assert.equal(scene.points[b + 6], 22)
  const noEdges = buildNebulaScene(input, []); assert.equal(noEdges.solidLines.length + noEdges.candidateLines.length, 0)
})

test('evidence and candidate relations are never inferred or merged into fake group relations', () => {
  const input = [fixture('a'), fixture('b'), fixture('c')]
  const edges = [
    { source: 'a', target: 'b', kind: 'evidence', confirmed: true },
    { source: 'b', target: 'c', kind: 'similarity', confirmed: false },
    { source: 'a', target: 'c', kind: 'evidence-without-confirmation' },
    { source: 'a', target: 'absent', kind: 'unknown', confirmed: true },
  ]
  const scene = buildNebulaScene(input, edges)
  assert.equal(scene.solidLines.length, 8); assert.equal(scene.candidateLines.length, 16)
  assert.equal(scene.edgeCounts.evidence, 2); assert.equal(scene.edgeCounts.candidates, 2); assert.equal(scene.edgeCounts.unresolved, 1)
  const grouped = buildNebulaScene(Array.from({ length: 500 }, (_, i) => fixture(i)), [{ source: 0, target: 1, kind: 'real', confirmed: true }])
  assert.equal(grouped.solidLines.length, 0); assert.equal(grouped.edgeCounts.hidden, 1)
})

test('10k nodes represented exactly once; bounded progressive expansion and selected path reveal', () => {
  const input = Array.from({ length: 10000 }, (_, i) => fixture(i))
  const original = JSON.stringify(input), initial = buildNebulaScene(input, [])
  assert.ok(initial.items.length <= 16); assert.equal(initial.representedNodes, 10000)
  assert.equal(initial.items.reduce((sum, item) => sum + item.count, 0), 10000)
  const selected = buildNebulaScene(input, [], { selectedId: 7341 })
  assert.ok(selected.itemIndex.has(idKey(7341))); assert.ok(selected.items.length < 160)
  assert.equal(selected.items.reduce((sum, item) => sum + item.count, 0), 10000)
  let expanded = new Set(), scene = initial, rounds = 0
  while (scene.groupCount && rounds++ < 12) {
    for (const item of scene.items) if (item.kind === 'group') expanded.add(item.key)
    scene = buildNebulaScene(input, [], { expanded })
    assert.equal(scene.items.reduce((sum, item) => sum + item.count, 0), 10000)
  }
  assert.equal(scene.groupCount, 0); assert.equal(scene.items.length, 10000)
  assert.equal(JSON.stringify(input), original, 'view never changes data')
})

test('many real themes are also aggregated (not silently truncated); duplicates explicitly counted', () => {
  const input = Array.from({ length: 5000 }, (_, i) => fixture(i, { theme: `theme-${i}`, layer: `L${i % 4 + 1}` }))
  const scene = buildNebulaScene(input, [], { selectedId: 4321 })
  assert.equal(scene.representedNodes, 5000); assert.ok(scene.items.length < 200)
  assert.ok(scene.itemIndex.has(idKey(4321)))
  assert.equal(scene.items.reduce((sum, item) => sum + item.count, 0), 5000)
  assert.equal(buildNebulaScene([fixture('x'), fixture('x')], []).duplicateIds, 1)
})

test('DOM labels are bounded, in bounds and non-overlapping', () => {
  const candidates = Array.from({ length: 50 }, (_, i) => ({ key: String(i), title: '真实主题', x: 180 + i * 3, y: 140 + i * 2, count: i, group: true }))
  const placed = placeNebulaLabels(candidates, 700, 420)
  assert.ok(placed.length > 0 && placed.length <= 6)
  for (let i = 0; i < placed.length; i++) {
    assert.ok(placed[i].x >= 8 && placed[i].y >= 8)
    for (let j = i + 1; j < placed.length; j++) assert.ok(Math.abs(placed[i].x - placed[j].x) >= 76 || Math.abs(placed[i].y - placed[j].y) >= 32)
  }
})

class Target {
  listeners = new Map()
  addEventListener(name, callback) { const set = this.listeners.get(name) || new Set(); set.add(callback); this.listeners.set(name, set) }
  removeEventListener(name, callback) { this.listeners.get(name)?.delete(callback) }
  dispatch(name, extra = {}) { const event = { type: name, preventDefault() { this.defaultPrevented = true }, ...extra }; for (const listener of this.listeners.get(name) || []) listener(event); return event }
  count() { return [...this.listeners.values()].reduce((sum, set) => sum + set.size, 0) }
}
function fakeGl({ shaderFail = false } = {}) {
  let serial = 0
  const liveBuffers = new Set(), livePrograms = new Set(), liveShaders = new Set(), enabledAttributes = new Set(), calls = [], source = [], uploads = []
  const gl = { liveBuffers, livePrograms, liveShaders, enabledAttributes, calls, source, uploads,
    VERTEX_SHADER: 35633, FRAGMENT_SHADER: 35632, COMPILE_STATUS: 35713, LINK_STATUS: 35714, ALIASED_POINT_SIZE_RANGE: 33901,
    ARRAY_BUFFER: 34962, STATIC_DRAW: 35044, FLOAT: 5126, LINES: 1, POINTS: 0, TRIANGLE_STRIP: 5, COLOR_BUFFER_BIT: 16384, DEPTH_BUFFER_BIT: 256, DEPTH_TEST: 2929, BLEND: 3042, SRC_ALPHA: 770, ONE_MINUS_SRC_ALPHA: 771, ONE: 1, LEQUAL: 515,
    createShader: () => { const value = { id: ++serial }; liveShaders.add(value); return value },
    shaderSource: (shader, text) => source.push(text), compileShader() {}, getShaderParameter: () => !shaderFail, deleteShader: shader => liveShaders.delete(shader),
    createProgram: () => { const value = { id: ++serial }; livePrograms.add(value); return value }, attachShader() {}, linkProgram() {}, getProgramParameter: () => true,
    deleteProgram: program => { assert.ok(livePrograms.delete(program), 'program deleted exactly once') },
    getAttribLocation: (program, name) => ['a_position', 'a_color', 'a_size', 'a_light', 'a_index', 'a_distance'].indexOf(name) % 5,
    getUniformLocation: (program, name) => name,
    createBuffer: () => { const value = { id: ++serial }; liveBuffers.add(value); return value }, deleteBuffer: buffer => { assert.ok(liveBuffers.delete(buffer), 'buffer deleted exactly once') },
    bindBuffer() {}, bufferData: (target, array) => { assert.ok(array instanceof Float32Array); uploads.push(array) },
    getParameter: () => new Float32Array([1, 128]), clearColor() {}, viewport() {}, clear() {}, disable() {}, enable() {}, blendFunc() {}, depthFunc() {},
    useProgram() {}, enableVertexAttribArray: location => enabledAttributes.add(location), disableVertexAttribArray: location => enabledAttributes.delete(location), vertexAttribPointer() {}, uniformMatrix4fv: (location, transpose, matrix) => { assert.equal(matrix.length, 16); assert.equal(transpose, false) },
    uniform4f() {}, uniform1f() {}, drawArrays: (mode, start, count) => calls.push({ mode, count }),
  }
  return gl
}
function environment({ available = true, reduced = false, shaderFail = false, webgl2 = true } = {}) {
  const old = new Map(['window', 'document', 'ResizeObserver', 'IntersectionObserver'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const gl = fakeGl({ shaderFail }), window = new Target(), document = new Target(), media = new Target(), canvas = new Target()
  const pending = new Map(), observers = [], contexts = []
  let serial = 0
  Object.assign(window, { devicePixelRatio: 4, requestAnimationFrame: callback => { const id = ++serial; pending.set(id, callback); return id }, cancelAnimationFrame: id => pending.delete(id), matchMedia: () => media })
  Object.assign(document, { hidden: false }); Object.assign(media, { matches: reduced })
  Object.assign(canvas, { width: 0, height: 0, captures: new Set(), getBoundingClientRect: () => ({ width: 900, height: 500, left: 0, top: 0 }), getContext: kind => { contexts.push(kind); return available && (kind === 'webgl' || (webgl2 && kind === 'webgl2')) ? gl : null }, focus() {}, setPointerCapture(id) { this.captures.add(id) }, hasPointerCapture(id) { return this.captures.has(id) }, releasePointerCapture(id) { this.captures.delete(id) } })
  class Observer { constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this) } observe() {} disconnect() { this.disconnected = true } }
  for (const [key, value] of Object.entries({ window, document, ResizeObserver: Observer, IntersectionObserver: Observer })) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  return { gl, window, document, media, canvas, pending, observers, contexts,
    flush(time = 1000) { const callbacks = [...pending.values()]; pending.clear(); callbacks.forEach(callback => callback(time)) },
    restore() { for (const [key, descriptor] of old) if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key] },
  }
}
const { NebulaRenderer } = load('src/components/memory/nebula-renderer.ts')
function withRenderer(options, body) {
  const env = environment(options), statuses = [], labels = [], picked = [], metrics = []
  let renderer
  try {
    renderer = new NebulaRenderer(env.canvas, { onStatus: status => statuses.push(status), onLabels: value => labels.push(value), onPick: value => picked.push(value), onMetrics: value => metrics.push(value) })
    body({ ...env, renderer, statuses, labels, picked, metrics })
  } finally { renderer?.dispose(); env.restore() }
}
test('native WebGL2 shaders transform actual xyz; typed arrays cached; no idle RAF; dispose idempotent', () => withRenderer({}, env => {
  const { renderer, gl, canvas, pending } = env
  assert.deepEqual(env.contexts, ['webgl2']); assert.equal(env.statuses.at(-1), 'ready')
  const scene = buildNebulaScene([fixture('a'), fixture('b')], [{ source: 'a', target: 'b', kind: 'evidence', confirmed: true }])
  renderer.setScene(scene); renderer.setSelected('a'); const uploads = gl.uploads.length
  env.flush()
  assert.ok(gl.source.some(source => source.includes('#version 300 es') && source.includes('u_matrix * vec4(a_position, 1.0)')))
  assert.ok(gl.calls.some(call => call.mode === gl.POINTS && call.count === 2))
  assert.equal(pending.size, 0, 'default is on-demand, not perpetual RAF')
  assert.ok(canvas.width <= 900 * 2); assert.ok(canvas.width * canvas.height <= 2510000)
  renderer.zoomBy(1.2); env.flush(1100); assert.equal(gl.uploads.length, uploads, 'geometry is not rebuilt per frame')
  renderer.setAutoRotate(true); env.flush(1200); assert.equal(pending.size, 1)
  renderer.dispose(); renderer.dispose()
  assert.equal(pending.size, 0); assert.equal(gl.liveBuffers.size, 0); assert.equal(gl.livePrograms.size, 0); assert.equal(gl.liveShaders.size, 0); assert.equal(gl.enabledAttributes.size, 0)
  assert.equal(canvas.count() + env.window.count() + env.document.count() + env.media.count(), 0)
  assert.ok(env.observers.every(observer => observer.disconnected))
}))

test('only a selected memory reveals its real links; clearing selection hides them again',()=>withRenderer({},env=>{
  const nodes=[fixture('a'),fixture('b'),fixture('c')]
  const scene=buildNebulaScene(nodes,[{source:'a',target:'b',confirmed:true,kind:'confirmed'},{source:'b',target:'c',confirmed:true,kind:'confirmed'}])
  env.renderer.setScene(scene);env.flush();assert.ok(!env.gl.calls.some(call=>call.mode===env.gl.LINES))
  env.gl.calls.length=0;env.renderer.setSelected('a');env.flush();assert.equal(env.gl.calls.filter(call=>call.mode===env.gl.LINES).reduce((n,call)=>n+call.count,0),36)
  env.gl.calls.length=0;env.renderer.setSelected(undefined);env.flush();assert.ok(!env.gl.calls.some(call=>call.mode===env.gl.LINES))
}))

test('hidden/offscreen pause RAF; reduced-motion overrides auto rotation; context loss falls back and restores', () => withRenderer({}, env => {
  const { renderer, pending, document, media, canvas, gl } = env
  renderer.setScene(buildNebulaScene([fixture(1)], [])); renderer.setAutoRotate(true); env.flush()
  document.hidden = true; document.dispatch('visibilitychange'); assert.equal(pending.size, 0)
  renderer.zoomBy(1.2); assert.equal(pending.size, 0)
  document.hidden = false; document.dispatch('visibilitychange'); assert.equal(pending.size, 1)
  env.observers[1].callback([{ isIntersecting: false }]); assert.equal(pending.size, 0)
  env.observers[1].callback([{ isIntersecting: true }]); env.flush(1100); assert.equal(pending.size, 1)
  media.matches = true; media.dispatch('change'); env.flush(1200); assert.equal(pending.size, 0)
  renderer.setAutoRotate(true); env.flush(1300); assert.equal(pending.size, 0)
  const lost = canvas.dispatch('webglcontextlost'); assert.equal(lost.defaultPrevented, true); assert.equal(env.statuses.at(-1), 'lost'); assert.equal(pending.size, 0)
  // Browser invalidates these, not deleteBuffer() on a lost context.
  gl.liveBuffers.clear(); gl.livePrograms.clear()
  canvas.dispatch('webglcontextrestored'); assert.equal(env.statuses.at(-1), 'ready'); assert.equal(gl.liveBuffers.size, 7)
  env.flush(1400); renderer.dispose(); assert.equal(gl.liveBuffers.size + gl.livePrograms.size, 0)
}))

test('WebGL1 fallback, no-WebGL fallback and shader failure clean up without a frame loop', () => {
  withRenderer({ webgl2: false }, env => { assert.deepEqual(env.contexts, ['webgl2', 'webgl']); assert.equal(env.statuses.at(-1), 'ready'); assert.ok(env.gl.source.every(source => !source.includes('#version 300'))); env.flush() })
  withRenderer({ available: false }, env => { assert.equal(env.statuses.at(-1), 'unavailable'); assert.equal(env.pending.size, 0); env.renderer.dispose(); assert.equal(env.canvas.count() + env.window.count() + env.document.count() + env.media.count(), 0) })
  withRenderer({ shaderFail: true }, env => { assert.equal(env.statuses.at(-1), 'error'); assert.equal(env.gl.liveShaders.size + env.gl.liveBuffers.size + env.gl.livePrograms.size, 0); assert.equal(env.pending.size, 0) })
})

test('pointer click selects real node, drag does not select; native Tab never trapped; keyboard Enter works', () => withRenderer({}, env => {
  const { renderer, canvas, picked } = env
  const scene = buildNebulaScene([fixture('pick')], []); renderer.setScene(scene); env.flush()
  const projection = new Float32Array(4); projectPoint(scene.items[0].position, renderer.matrix, 900, 500, projection, 0, scene.points[6])
  canvas.dispatch('pointerdown', { pointerId: 1, button: 0, clientX: projection[0], clientY: projection[1] }); canvas.dispatch('pointerup', { pointerId: 1, clientX: projection[0], clientY: projection[1] })
  assert.equal(picked[0].node.id, 'pick'); assert.equal(canvas.captures.size, 0)
  canvas.dispatch('pointerdown', { pointerId: 2, button: 0, clientX: 300, clientY: 250 }); canvas.dispatch('pointermove', { pointerId: 2, clientX: 370, clientY: 270 }); canvas.dispatch('pointerup', { pointerId: 2, clientX: 370, clientY: 270 })
  assert.equal(picked.length, 1)
  const tab = canvas.dispatch('keydown', { key: 'Tab' }); assert.equal(tab.defaultPrevented, undefined)
  canvas.dispatch('keydown', { key: 'ArrowRight', shiftKey: true }); canvas.dispatch('keydown', { key: 'Enter' }); assert.equal(picked.length, 2)
  const matrixBefore = [...renderer.matrix]; canvas.dispatch('keydown', { key: 'ArrowRight' }); env.flush(2000); assert.notDeepEqual([...renderer.matrix], matrixBefore)
  renderer.setSelected('pick'); renderer.reset(); renderer.focusSelected(); env.flush(2100)
  const centered = new Float32Array(4); projectPoint(scene.items[0].position, renderer.matrix, 900, 500, centered)
  assert.ok(Math.abs(centered[0] - 450) < .001 && Math.abs(centered[1] - 250) < .001)
}))

test('strict project TypeScript checks the independent component and renderer without emitting files', () => {
  const config = ts.readConfigFile(resolve(root, 'tsconfig.json'), ts.sys.readFile)
  assert.equal(config.error, undefined)
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, root)
  const program = ts.createProgram([
    resolve(root, 'src/lib/nebula.ts'),
    resolve(root, 'src/components/memory/nebula-renderer.ts'),
    resolve(root, 'src/components/memory/MemoryNebula.tsx'),
  ], { ...parsed.options, noEmit: true, incremental: false, composite: false })
  const errors = ts.getPreEmitDiagnostics(program)
  assert.equal(errors.length, 0, ts.formatDiagnosticsWithColorAndContext(errors, { getCurrentDirectory: () => root, getCanonicalFileName: path => path, getNewLine: () => '\n' }))
})

console.log(`\n${passed} nebula tests passed. No application/server or production storage was started.`)
