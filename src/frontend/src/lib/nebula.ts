/** Read-only visualization types. Supply V2 values; this module never fetches or persists memory. */
export type NebulaId = string | number
export type NebulaLayer = 'L1' | 'L2' | 'L3' | 'L4'
export interface NebulaNode {
  id: NebulaId
  title?: string
  key?: string
  layer: NebulaLayer
  scope: string
  /** Normalized V2 metrics, 0..1. Clamping affects drawing only, never source data. */
  importance: number
  activation: number
  theme?: string | null
  status?: string
  pinned?: boolean
}
export interface NebulaEdge {
  source: NebulaId
  target: NebulaId
  kind: string
  /** Only true means evidence-backed. Missing/false is always a candidate, never a fact. */
  confirmed?: boolean
}
export interface MemoryNebulaProps {
  nodes: readonly NebulaNode[]
  edges: readonly NebulaEdge[]
  selectedId?: NebulaId | null
  onSelect: (id: NebulaId) => void
  onClearSelection?: () => void
  loading?: boolean
}
export type Vec3 = readonly [number, number, number]
export const NEBULA_LAYERS: readonly NebulaLayer[] = ['L1', 'L2', 'L3', 'L4']
export const LAYER_RADIUS: Record<NebulaLayer, number> = { L1: .78, L2: 1.3, L3: 1.84, L4: 2.4 }
export const LAYER_COLORS: Record<NebulaLayer,string> = {L1:'#79b9dc',L2:'#b298e6',L3:'#79c3b4',L4:'#e0c693'}
export const LAYER_NAMES: Record<NebulaLayer,string> = {L1:'即时记忆',L2:'经历记忆',L3:'长期记忆',L4:'核心记忆'}
const LAYER_CENTERS: Record<NebulaLayer,Vec3> = {L1:[-1.55,-.65,.15],L2:[1.0,.95,-.2],L3:[-1.35,1.1,.2],L4:[1.25,-.9,.3]}
export const SCOPE_COLORS: Record<string, string> = { companion: '#a0a0a0', shared: '#d0d0d0', work: '#787878' }
export const SCOPE_NAMES: Record<string, string> = { companion: '陪伴', shared: '共享', work: '工作' }
export const LIST_PAGE_SIZE = 40
/** View density follows real filtered memories, never the number of aggregated sprites. */
export const NEBULA_COMPACT_LIMIT = 5
export function nebulaIsCompact(filteredCount: number): boolean {
  return Number.isSafeInteger(filteredCount) && filteredCount >= 0 && filteredCount <= NEBULA_COMPACT_LIMIT
}
export const GROUP_LEAF_LIMIT = 48
export const MAX_SCENE_DPR = 2
export const idKey = (id: NebulaId): string => `${typeof id}:${String(id)}`
export const nodeTitle = (node: NebulaNode): string => node.title?.trim() || node.key?.trim() || `记忆 ${node.id}`
export const themeName = (node: NebulaNode): string => node.theme?.trim() || '未分主题'
export const clamp = (value: number, min: number, max: number): number => Math.max(min, Math.min(max, Number.isFinite(value) ? value : min))
export const unit = (value: number): number => clamp(value, 0, 1)
export function stableHash(text: string): number {
  let hash = 2166136261
  for (let index = 0; index < text.length; index++) hash = Math.imul(hash ^ text.charCodeAt(index), 16777619)
  return hash >>> 0
}
const fraction = (key: string, salt: string): number => {let n=stableHash(`${salt}:${key}`);n=Math.imul(n^(n>>>16),0x85ebca6b);n=Math.imul(n^(n>>>13),0xc2b2ae35);return ((n^(n>>>16))>>>0)/4294967296}
export function scopeColor(scope: string): string {
  return SCOPE_COLORS[scope] || '#a0a0a0'
}
export function hexRgb(hex: string): Vec3 {
  return [parseInt(hex.slice(1, 3), 16) / 255, parseInt(hex.slice(3, 5), 16) / 255, parseInt(hex.slice(5, 7), 16) / 255]
}
export function themePosition(layer: NebulaLayer, theme: string): Vec3 {
  const center=LAYER_CENTERS[layer]
  return [center[0]+(fraction(theme,'theme-x')-.5)*.12,center[1]+(fraction(theme,'theme-y')-.5)*.12,center[2]]
}
/** Independent of array order, filtering, and group expansion. No force simulation / randomness. */
export function nodePosition(node: NebulaNode): Vec3 {
  const theme = themeName(node), center = themePosition(node.layer, theme), key = idKey(node.id)
  const angle = fraction(key, 'node-angle') * Math.PI * 2
  const spread = .12 + Math.sqrt(fraction(key, 'node-radius')) * .72
  return [center[0] + Math.cos(angle) * spread, center[1] + Math.sin(angle) * spread * .73, center[2] + (fraction(key, 'node-height') - .5) * 1.05]
}
export interface NebulaFilter { layers?: ReadonlySet<NebulaLayer>; scope?: string; theme?: string; query?: string }
export function filterNebulaNodes(nodes: readonly NebulaNode[], filter: NebulaFilter = {}): NebulaNode[] {
  const query = filter.query?.trim().toLocaleLowerCase()
  return nodes.filter(node => (!filter.layers || filter.layers.has(node.layer)) && (!filter.scope || node.scope === filter.scope) && (!filter.theme || themeName(node) === filter.theme) && (!query || `${nodeTitle(node)} ${node.id} ${themeName(node)}`.toLocaleLowerCase().includes(query)))
}
export interface NebulaItem {
  key: string
  kind: 'node' | 'group'
  title: string
  layer: NebulaLayer
  position: Vec3
  importance: number
  activation: number
  color: string
  count: number
  scopeCounts: Readonly<Record<string, number>>
  node?: NebulaNode
}
export interface NebulaGroup extends NebulaItem { kind: 'group'; children: NebulaTree[] }
export type NebulaTree = NebulaItem | NebulaGroup
export interface NebulaScene {
  items: NebulaItem[]
  /** Retained for accessible drill-down / counting. Never used as invented relationship edges. */
  roots: NebulaGroup[]
  filteredNodes: NebulaNode[]
  points: Float32Array
  solidLines: Float32Array
  candidateLines: Float32Array
  itemIndex: Map<string, number>
  representedNodes: number
  groupedNodes: number
  groupCount: number
  edgeCounts: { total: number; evidence: number; candidates: number; drawnEvidence: number; drawnCandidates: number; hidden: number; unresolved: number }
  duplicateIds: number
}
export const POINT_STRIDE = 9 // xyz, rgb, importance-based size, activation-based brightness, item index
function leaf(node: NebulaNode): NebulaItem {
  return { key: idKey(node.id), kind: 'node', node, title: nodeTitle(node), layer: node.layer, position: nodePosition(node), importance: unit(node.importance), activation: unit(node.activation), color: LAYER_COLORS[node.layer], count: 1, scopeCounts: { [node.scope]: 1 } }
}
function group(key: string, title: string, layer: NebulaLayer, children: NebulaTree[]): NebulaGroup {
  let count = 0, importance = 0, activation = 0, x = 0, y = 0, z = 0
  const scopes: Record<string, number> = Object.create(null)
  for (const child of children) {
    count += child.count; importance += child.importance * child.count; activation += child.activation * child.count
    x += child.position[0] * child.count; y += child.position[1] * child.count; z += child.position[2] * child.count
    for (const [scope, n] of Object.entries(child.scopeCounts)) scopes[scope] = (scopes[scope] || 0) + n
  }
  // A many-theme bucket must remain on its layer's orbit, rather than averaging a whole
  // ring into the origin. Within a real theme, centroid aggregation preserves its location.
  const position: Vec3 = key.includes('/themes/') ? themePosition(layer, key) : count ? [x / count, y / count, z / count] : [0, 0, 0]
  return { key, kind: 'group', title, layer, children, count, position, importance: count ? importance / count : 0, activation: count ? activation / count : 0, color: LAYER_COLORS[layer], scopeCounts: scopes }
}
/** Bounded fan-out hash trie, including a deterministic collision fallback. All entries survive. */
function partition<T>(values: readonly T[], identity: (value: T) => string, makeLeaf: (value: T) => NebulaTree, layer: NebulaLayer, prefix: string, label: string, depth = 0): NebulaTree[] {
  if (values.length <= GROUP_LEAF_LIMIT) return values.map(makeLeaf)
  const bins = new Map<string, T[]>()
  const sorted = [...values].sort((a, b) => identity(a).localeCompare(identity(b)))
  for (let index = 0; index < sorted.length; index++) {
    const value = sorted[index]
    const slot = depth < 8 ? ((stableHash(identity(value)) >>> (depth * 4)) & 15).toString(16) : String(Math.floor(index / GROUP_LEAF_LIMIT))
    const bin = bins.get(slot) || []; bin.push(value); bins.set(slot, bin)
  }
  return [...bins].sort(([a], [b]) => a.localeCompare(b)).map(([slot, bin]) => {
    const key = `${prefix}/${slot}`
    return group(key, `${label} · 分组 ${slot.toUpperCase()}`, layer, partition(bin, identity, makeLeaf, layer, key, label, depth + 1))
  })
}
function rootsFor(nodes: readonly NebulaNode[]): NebulaGroup[] {
  return NEBULA_LAYERS.map(layer => {
    const themes = new Map<string, NebulaNode[]>()
    for (const node of nodes) if (node.layer === layer) { const name = themeName(node), items = themes.get(name) || []; items.push(node); themes.set(name, items) }
    const sortedThemes = [...themes.keys()].sort((a, b) => a.localeCompare(b))
    const children = partition(sortedThemes, value => value, theme => {
      const key = `group/${layer}/theme/${encodeURIComponent(theme)}`
      const members = themes.get(theme)!.sort((a, b) => idKey(a.id).localeCompare(idKey(b.id)))
      return group(key, theme, layer, partition(members, value => idKey(value.id), leaf, layer, key, theme))
    }, layer, `group/${layer}/themes`, `${layer} 主题`)
    return group(`group/${layer}`, layer, layer, children)
  }).filter(root => root.count > 0)
}
function selectedPath(tree: NebulaTree, key: string, result: Set<string>): boolean {
  if (tree.kind === 'node') return tree.key === key
  for (const child of (tree as NebulaGroup).children) if (selectedPath(child, key, result)) { result.add(tree.key); return true }
  return false
}
export function buildNebulaScene(nodes: readonly NebulaNode[], edges: readonly NebulaEdge[], options: NebulaFilter & { expanded?: ReadonlySet<string>; selectedId?: NebulaId | null } = {}): NebulaScene {
  // Bad duplicate IDs cannot be picked unambiguously. Report them explicitly rather than silently merging.
  const seen = new Set<string>(); let duplicateIds = 0
  const unique = nodes.filter(node => { const key = idKey(node.id); if (seen.has(key)) { duplicateIds++; return false }; seen.add(key); return true })
  const filteredNodes = filterNebulaNodes(unique, options), roots = rootsFor(filteredNodes)
  const expanded = new Set(options.expanded)
  if (options.selectedId != null) for (const root of roots) selectedPath(root, idKey(options.selectedId), expanded)
  const items: NebulaItem[] = []
  const automatic = filteredNodes.length <= 1200
  function visit(tree: NebulaTree): void {
    if (tree.kind === 'group' && (expanded.has(tree.key) || (automatic && tree.count <= GROUP_LEAF_LIMIT))) for (const child of (tree as NebulaGroup).children) visit(child)
    else items.push(tree)
  }
  // Layer roots are orbit containers, not memory nodes. Always expose their bounded children.
  for (const root of roots) for (const child of root.children) visit(child)
  const points = new Float32Array(items.length * POINT_STRIDE), itemIndex = new Map<string, number>()
  let groupedNodes = 0, groupCount = 0
  items.forEach((item, index) => {
    itemIndex.set(item.key, index)
    const offset = index * POINT_STRIDE, color = hexRgb(item.color)
    points.set(item.position, offset); points.set(color, offset + 3)
    // Individual size is exclusively importance. Aggregates are visibly distinct and carry counts.
    points[offset + 6] = item.kind === 'group' ? 23 + Math.min(12, Math.log2(item.count + 1)) : 8 + unit(item.importance) * 14
    points[offset + 7] = .2 + unit(item.activation) * .8
    points[offset + 8] = index
    if (item.kind === 'group') { groupedNodes += item.count; groupCount++ }
  })
  const solid: number[] = [], candidates: number[] = []
  const edgeCounts = { total: edges.length, evidence: 0, candidates: 0, drawnEvidence: 0, drawnCandidates: 0, hidden: 0, unresolved: 0 }
  for (const edge of edges) {
    if (edge.confirmed === true) edgeCounts.evidence++; else edgeCounts.candidates++
    const sourceKey = idKey(edge.source), targetKey = idKey(edge.target)
    if (!seen.has(sourceKey) || !seen.has(targetKey) || sourceKey === targetKey) { edgeCounts.unresolved++; continue }
    const sourceIndex = itemIndex.get(sourceKey), targetIndex = itemIndex.get(targetKey)
    if (sourceIndex === undefined || targetIndex === undefined) { edgeCounts.hidden++; continue }
    const start = items[sourceIndex].position, end = items[targetIndex].position
    const length = Math.hypot(end[0] - start[0], end[1] - start[1], end[2] - start[2])
    const target = edge.confirmed === true ? solid : candidates
    target.push(...start, 0, ...end, length)
    if (edge.confirmed === true) edgeCounts.drawnEvidence++; else edgeCounts.drawnCandidates++
  }
  return { items, roots, filteredNodes, points, solidLines: new Float32Array(solid), candidateLines: new Float32Array(candidates), itemIndex, representedNodes: filteredNodes.length, groupedNodes, groupCount, edgeCounts, duplicateIds }
}
export interface NebulaCamera { yaw: number; pitch: number; zoom: number; target: Vec3 }
export function defaultCamera(): NebulaCamera { return { yaw: .12, pitch: .12, zoom: 1.45, target: [0, 0, 0] } }
/** Column-major perspective * view; this same matrix is uploaded to the WebGL vertex shaders. */
export function cameraMatrix(camera: NebulaCamera, aspect: number, out = new Float32Array(16)): Float32Array {
  const cy = Math.cos(camera.yaw), sy = Math.sin(camera.yaw), cx = Math.cos(camera.pitch), sx = Math.sin(camera.pitch)
  const f = 1 / Math.tan(Math.PI / 7), near = .1, far = 100
  const distance = 7.2 / clamp(camera.zoom, .45, 3.8) * Math.max(1,1.25/Math.max(.01,aspect))
  const r00 = cy, r01 = 0, r02 = sy, r10 = sx * sy, r11 = cx, r12 = -sx * cy, r20 = -cx * sy, r21 = sx, r22 = cx * cy
  const [tx, ty, tz] = camera.target
  const vx = -(r00 * tx + r01 * ty + r02 * tz), vy = -(r10 * tx + r11 * ty + r12 * tz), vz = -(r20 * tx + r21 * ty + r22 * tz) - distance
  const a = f / Math.max(.01, aspect), c = (far + near) / (near - far), d = 2 * far * near / (near - far)
  out.set([a * r00, f * r10, c * r20, -r20, a * r01, f * r11, c * r21, -r21, a * r02, f * r12, c * r22, -r22, a * vx, f * vy, c * vz + d, -vz])
  return out
}
/** Writes CSS-pixel x/y, depth, radius into a reusable projection buffer (picking/DOM only). */
export function projectPoint(position: Vec3, matrix: Float32Array, width: number, height: number, out: Float32Array, offset = 0, size = 12): void {
  const [x, y, z] = position
  const w = matrix[3] * x + matrix[7] * y + matrix[11] * z + matrix[15]
  const px = matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12]
  const py = matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13]
  const pz = matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14]
  out[offset] = (px / w + 1) * width / 2; out[offset + 1] = (1 - py / w) * height / 2
  out[offset + 2] = w > .1 && pz / w >= -1 && pz / w <= 1 ? w : -1
  out[offset + 3] = clamp(size * 4 / Math.max(.4, w), 4, 120) / 2
}
export function makeOrbitLines(): Float32Array {
  const values: number[] = [], segments = 160
  for (const layer of NEBULA_LAYERS) for (let index = 0; index < segments; index++) {
    for (const step of [index, index + 1]) { const a = step / segments * Math.PI * 2; values.push(Math.cos(a) * LAYER_RADIUS[layer], 0, Math.sin(a) * LAYER_RADIUS[layer], 0) }
  }
  return new Float32Array(values)
}
export interface NebulaLabel { key: string; title: string; x: number; y: number; group: boolean; count: number; color?:string; pinned?:boolean }
/** Bounded, greedy collision avoidance. Label candidates are already priority ordered. */
export function placeNebulaLabels(candidates: readonly NebulaLabel[], width: number, height: number, max = 6): NebulaLabel[] {
  const result: NebulaLabel[] = [], boxes: Array<{ x: number; y: number; w: number; h: number }> = []
  for (const label of candidates) {
    if (result.length >= max) break
    const w = Math.min(170, Math.max(86, label.title.length * 12 + 28)), h = 32
    for (const [dx, dy] of [[14, -28], [14, 10], [-w - 14, -28], [-w - 14, 10]]) {
      const x = label.x + dx, y = label.y + dy
      if (x < 8 || y < 8 || x + w > width - 8 || y + h > height - 8) continue
      if (boxes.some(box => x < box.x + box.w + 6 && x + w + 6 > box.x && y < box.y + box.h + 6 && y + h + 6 > box.y)) continue
      boxes.push({ x, y, w, h }); result.push({ ...label, x, y }); break
    }
  }
  return result
}

/** Atmospheric particles are decoration only: no IDs, picking, counts, edges or persistence. */
export function nebulaAtmosphere(scene: NebulaScene | null): Float32Array {
  const values:number[]=[]
  const noise=(key:string,salt:string)=>fraction(key,salt)
  const add=(x:number,y:number,z:number,size:number,light:number,color:Vec3=[.55,.64,.83])=>values.push(x,y,z,...color,size,light,-10000)
  // Distant star field, then a soft galactic spiral. Decoration never carries a memory ID.
  for(let i=0;i<1100;i++){
    const key=String(i)
    add((noise(key,'sky-x')-.5)*13,(noise(key,'sky-y')-.5)*8,(noise(key,'sky-z')-.5)*6,noise(key,'sky-size')>.975?4:1.1+noise(key,'sky-size')*1.8,.2+noise(key,'sky-light')*.48)
  }
  if(!scene?.roots.length)return new Float32Array(values)
  for(let i=0;i<1800;i++){
    const key=String(i),t=noise(key,'spiral-turn')*Math.PI*4,r=.3+t*.14
    add(Math.cos(t)*r+(noise(key,'spiral-x')-.5)*.5,Math.sin(t)*r*.64+(noise(key,'spiral-y')-.5)*.5,(noise(key,'spiral-z')-.5)*1.1,1.4+noise(key,'spiral-size')*2.8,.2+noise(key,'spiral-light')*.4,hexRgb(LAYER_COLORS[NEBULA_LAYERS[i%4]]))
  }
  for(const cluster of scene.roots)for(let i=0;i<1400;i++){
    const key=`${cluster.key}:${i}`,angle=noise(key,'dust-angle')*Math.PI*2,r=Math.pow(noise(key,'dust-radius'),.65)*1.25,spiral=angle+r*1.8
    const haze=i<36
    add(cluster.position[0]+Math.cos(spiral)*r,cluster.position[1]+Math.sin(spiral)*r*.69,cluster.position[2]+(noise(key,'dust-z')+noise(key,'dust-z2')+noise(key,'dust-z3')-1.5)*.6,haze?150+noise(key,'dust-size')*100:1.4+noise(key,'dust-size')*3.8,haze?.022+noise(key,'dust-light')*.03:.22+noise(key,'dust-light')*.6,hexRgb(LAYER_COLORS[cluster.layer]))
  }
  return new Float32Array(values)
}

export function nebulaThemeLabel(value: string): string {
  return ({experience:'经历',fact:'事实',habit:'操作习惯',project:'项目记录',preference:'偏好',legacy:'旧记录'} as Record<string,string>)[value] || value
}
