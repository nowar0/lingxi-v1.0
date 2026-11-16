import { api } from '../api/client'

/** Wire types for app/memory/v2_api.py. Layer, scope, pinning and activation are independent. */
export type MemoryV2Scope = 'companion' | 'work' | 'shared'
export type MemoryV2Layer = 1 | 2 | 3 | 4
export interface MemoryV2Source {
  kind: 'manual' | 'message' | 'legacy' | 'memory' | 'unknown'
  actor?: string
  known?: boolean
  origin?: string
  ref?: string
  character_id?: string | number | null
  session_id?: string | number | null
  message_ids?: Array<string | number>
  parent_ids?: string[]
  [key: string]: unknown
}
export interface MemoryV2Relation {
  id: string
  from_id: string
  to_id: string
  kind: 'confirmed' | 'similarity'
  label: string
  score?: number | null
  revision: number
  asserted?: boolean
}
export interface MemoryV2Reinforcement { id: string; action: string; at: string }
export interface MemoryV2Item {
  id: string
  scope: MemoryV2Scope
  owner_id: string
  key: string
  value: string
  layer: MemoryV2Layer
  category: string
  importance: number
  tags?: string[]
  valence?: number | null
  arousal?: number | null
  pinned: boolean
  revision: number
  current_revision: number
  version_id: string
  is_current: boolean
  status: string
  eligible: boolean
  source: MemoryV2Source
  source_character_id?: string | number | null
  created_at: string
  updated_at: string
  occurred_at?: string | null
  confirmed_at?: string | null
  reinforced_at?: string | null
  reinforcement_count: number
  reinforcement_events?: MemoryV2Reinforcement[]
  activation: number
  half_life_days: number | null
  relations: MemoryV2Relation[]
  redacted?: boolean
  core_confirmation?: unknown
}
export interface MemoryV2Page<T = MemoryV2Item> { items: T[]; total: number; offset: number; limit: number }
export interface MemoryV2Filters {
  scope?: MemoryV2Scope
  mode?: MemoryV2Scope
  session_id?: number
  layer?: MemoryV2Layer
  category?: string
  pinned?: boolean
  query?: string
  before?: string
  include_deleted?: boolean
  offset?: number
  limit?: number
}
export interface MemoryV2Catalog {scope:MemoryV2Scope;total:number;pinned:number;layers:Record<string,number>;categories:Record<string,number>;relations:number;first_date:string|null;last_date:string|null;read_only:true}
export interface MemoryV2Graph extends MemoryV2Page { nodes: MemoryV2Item[]; edges: MemoryV2Relation[] }
export interface MemoryV2Context {
  items: Array<MemoryV2Item & { score: number; evidence_kind: string }>
  total: number
  projects: Array<Pick<MemoryV2Item, 'id' | 'key' | 'value' | 'scope' | 'owner_id' | 'revision' | 'source_character_id'>>
  project_roster_complete: boolean
  history: boolean
  read_only: boolean
  method: string
  facts_policy: string
}
export interface MemoryV2Migration {
  source_ref: string
  batch_id: string
  fingerprint: string
  status: string
  document: string
  entity_id: string | null
  error: string
  attempts: number
  revision: number
  updated_at: string
}
export interface MemoryV2MigrationPage extends MemoryV2Page<MemoryV2Migration> {
  counts: Record<string, number>
  authority: string
  has_more?: boolean
  remaining?: number
}
export interface MemoryV2Batch {
  batch_id: string
  items: Array<{ source_ref: string; status: string; entity_id?: string | null; error?: string }>
  processed: number
  has_more: boolean
  chat_modified: false
  writes_legacy: false
}
export interface MemoryV2ForgetPreview {
  operation_id: string
  token: string
  entity_ids: string[]
  derived_count: number
  source_message_ids: string[]
  scope: string[]
  raw_chat_deleted: false
  backups_deleted: false
  warning: string
}
export interface MemoryV2ForgetResult {
  operation_id: string
  status: string
  forgotten_entity_ids: string[]
  raw_chat_deleted: false
  backups_deleted: false
  external_backups_erased: false
  warning: string
}
export interface MemoryV2Create {
  scope: MemoryV2Scope
  session_id?: number
  key: string
  value: string
  layer: 1 | 2 | 3
  category: string
  importance: number
  tags?: string[]
  valence?: number | null
  arousal?: number | null
  pinned: boolean
  occurred_at?: string | null
  source?: MemoryV2Source
}
export interface MemoryV2Patch {
  expected_revision: number
  key?: string
  value?: string
  layer?: MemoryV2Layer
  category?: string
  importance?: number
  tags?: string[]
  valence?: number | null
  arousal?: number | null
  pinned?: boolean
  occurred_at?: string | null
  source?: MemoryV2Source
}
export interface MemoryV2RestoreConfirmation { user_confirmed: boolean; operation_id: string }
/** Content IDs, not entity revisions, identify a historical document. */
export function memoryV2ContentRevision(item: Pick<MemoryV2Item, 'id' | 'version_id'>): number | undefined {
  const prefix = `${item.id}:`
  if (typeof item.version_id !== 'string' || !item.version_id.startsWith(prefix)) return undefined
  const tail = item.version_id.slice(prefix.length)
  if (!/^[1-9]\d*$/.test(tail)) return undefined
  const revision = Number(tail)
  return Number.isSafeInteger(revision) && revision <= 2147483647 ? revision : undefined
}
export function memoryV2RestoreVersionProblem(current?: MemoryV2Item, historical?: MemoryV2Item): string {
  if (!current || !historical) return '请先读取当前记忆与目标历史内容。'
  if (current.id !== historical.id || current.scope !== historical.scope || current.owner_id !== historical.owner_id) return '历史内容与当前记忆归属不一致，不能跨范围恢复。'
  if (current.is_current !== true) return '必须重新读取当前记忆，不能把历史内容作为当前修订。'
  if (current.status !== 'active' || current.redacted || historical.redacted) return '仅有效记忆可恢复历史内容；普通删除须先恢复，彻底忘记不可恢复。'
  if (current.scope === 'shared') return '共享投影须从来源重新明确授权，不能恢复历史内容。'
  if (current.layer === 4 || historical.layer === 4 || current.core_confirmation != null || historical.core_confirmation != null) return '核心历史或当前核心必须通过独立核心确认，不能直接恢复。'
  if (!current.eligible || !historical.eligible || historical.source?.known !== true) return '当前或历史来源不可验证、已撤回或失效，不能恢复。'
  const revision = memoryV2ContentRevision(historical)
  if (revision === undefined || memoryV2ContentRevision(current) === undefined) return '内容版本标识无效，不能用实体修订号代替。'
  if (historical.is_current || historical.version_id === current.version_id) return '目标是当前内容，不是历史版本。'
  return ''
}
export type MemoryV2Transport = <T>(path: string, options?: RequestInit) => Promise<T>
export const MEMORY_V2_PAGE_SIZE = 40
export const memoryV2Layers = [
  { id: 1, name: 'L1 · 即时', note: '短时线索，不强制转为长期；活跃度半衰期 1 天' },
  { id: 2, name: 'L2 · 经历', note: '半衰期 30 天；重要性 ≥ 70% 时为 180 天，衰减不等于删除' },
  { id: 3, name: 'L3 · 长期', note: '不会因时间衰减自动删除' },
  { id: 4, name: 'L4 · 核心', note: '必须由你逐条手动确认；置顶不等于核心' },
] as const
export const memoryV2ScopeNames: Record<MemoryV2Scope, string> = { companion: '陪伴', work: '工作', shared: '共享' }
export function memoryV2Query(filters: object = {}): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) if (value !== undefined && value !== null && value !== '') query.set(key, String(value))
  return query.size ? `?${query.toString()}` : ''
}
const mutation = (method: string, body?: unknown): RequestInit => ({ method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
const encoded = (id: string) => encodeURIComponent(id)
export function createMemoryV2Client(request: MemoryV2Transport = api) {
  const root = '/v10/memory'
  return {
    catalog:(scope:MemoryV2Scope,session_id?:number)=>request<MemoryV2Catalog>(root+'/catalog'+memoryV2Query({scope,session_id})),
    exportData:(scope:MemoryV2Scope,session_id?:number)=>request<{version:number;memories:MemoryV2Item[]}>(root+'/export'+memoryV2Query({scope,session_id})),
    relate:(from:MemoryV2Item,to:MemoryV2Item,label:string,confirmed:boolean)=>request<MemoryV2Relation>(root+'/relations',mutation('POST',{from_id:from.id,to_id:to.id,kind:confirmed?'confirmed':'similarity',label,user_confirmed:confirmed,source:{kind:'manual',actor:'user'},expected_from_revision:from.revision,expected_to_revision:to.revision})),
    list: (filters: MemoryV2Filters = {}) => request<MemoryV2Page>(root + memoryV2Query(filters)),
    graph: (filters: MemoryV2Filters = {}, signal?: AbortSignal) => request<MemoryV2Graph>(root + '/graph' + memoryV2Query(filters), signal ? { signal } : undefined),
    context: (mode: MemoryV2Scope, session_id: number | undefined, query: string, history = false, limit = 40) => request<MemoryV2Context>(root + '/context' + memoryV2Query({ mode, session_id, query, history, limit })),
    migrations: (status?: string, offset = 0, limit = MEMORY_V2_PAGE_SIZE) => request<MemoryV2MigrationPage>(root + '/migrations' + memoryV2Query({ status, offset, limit })),
    migrateBatch: (batch_id = 'legacy-v2', limit = 100) => request<MemoryV2Batch>(root + '/migrations/batch', mutation('POST', { batch_id, limit })),
    resolve: (item: MemoryV2Migration, action: 'confirm' | 'skip' | 'retry', corrected?: Record<string, unknown>) => request<{ source_ref: string; status: string }>(root + '/migrations/resolve', mutation('POST', { source_ref: item.source_ref, expected_revision: item.revision, action, ...(action === 'confirm' ? { user_confirmed: true } : {}), ...(corrected === undefined ? {} : { corrected }) })),
    activate: () => request<{ authority: string }>(root + '/activate', mutation('POST', { user_confirmed: true })),
    create: (body: MemoryV2Create) => request<MemoryV2Item>(root, mutation('POST', body)),
    get: (id: string, revision?: number) => request<MemoryV2Item>(root + '/' + encoded(id) + memoryV2Query({ revision })),
    patch: (id: string, body: MemoryV2Patch) => request<MemoryV2Item>(root + '/' + encoded(id), mutation('PATCH', body)),
    delete: (item: MemoryV2Item) => request<MemoryV2Item>(root + '/' + encoded(item.id) + memoryV2Query({ expected_revision: item.revision }), mutation('DELETE')),
    restore: (item: MemoryV2Item) => request<MemoryV2Item>(root + '/' + encoded(item.id) + '/restore', mutation('POST', { expected_revision: item.revision })),
    restoreContentVersion: (current: MemoryV2Item, historical: MemoryV2Item, confirmation: MemoryV2RestoreConfirmation) => {
      const problem = memoryV2RestoreVersionProblem(current, historical)
      if (problem) throw new Error(problem)
      if (!Number.isSafeInteger(current.revision) || current.revision < 1 || current.revision > 2147483647) throw new Error('当前修订号无效。')
      if (confirmation?.user_confirmed !== true || typeof confirmation.operation_id !== 'string' || !/^[A-Za-z0-9_-]{8,128}$/.test(confirmation.operation_id)) throw new Error('恢复历史内容需要独立确认与有效操作编号。')
      return request<MemoryV2Item>(root + '/' + encoded(current.id) + '/restore-version', mutation('POST', { expected_revision: current.revision, version_revision: memoryV2ContentRevision(historical)!, user_confirmed: true, operation_id: confirmation.operation_id }))
    },
    share: (item: MemoryV2Item, target_key: string) => request<MemoryV2Item>(root + '/' + encoded(item.id) + '/share', mutation('POST', { expected_revision: item.revision, user_confirmed: true, target_key })),
    confirmCore: (item: MemoryV2Item, confirmation_id: string, value?: string) => request<MemoryV2Item>(root + '/' + encoded(item.id) + '/core-confirm', mutation('POST', { expected_revision: item.revision, user_confirmed: true, confirmation_id, ...(value === undefined ? {} : { value }) })),
    reinforce: (item: MemoryV2Item, event_id: string, action: 'user_confirm' | 'user_remention' = 'user_confirm') => request<MemoryV2Item>(root + '/' + encoded(item.id) + '/reinforce', mutation('POST', { expected_revision: item.revision, action, event_id, user_initiated: true })),
    forgetPreview: (id: string) => request<MemoryV2ForgetPreview>(root + '/' + encoded(id) + '/forget-preview', mutation('POST')),
    forgetConfirm: (preview: MemoryV2ForgetPreview) => request<MemoryV2ForgetResult>(root + '/forget/confirm', mutation('POST', { operation_id: preview.operation_id, token: preview.token, user_confirmed: true })),
    versions: (id: string, offset = 0, limit = MEMORY_V2_PAGE_SIZE) => request<MemoryV2Page>(root + '/' + encoded(id) + '/versions' + memoryV2Query({ offset, limit })),
  }
}
export const memoryV2 = createMemoryV2Client()

/** Aggregate every graph page; reject unstable scans rather than silently truncate.
 * Each projection carries all its actual relations, including cross-page edges.
 */
export async function loadCompleteMemoryV2Graph(client: Pick<ReturnType<typeof createMemoryV2Client>, 'graph'>, filters: MemoryV2Filters, onProgress?: (loaded: number, total: number) => void, signal?: AbortSignal): Promise<MemoryV2Graph> {
  const nodes: MemoryV2Item[] = [], ids = new Set<string>(), relations = new Map<string, MemoryV2Relation>()
  let offset = 0, total: number | undefined
  do {
    signal?.throwIfAborted()
    const page = await client.graph({ ...filters, offset, limit: 200 }, signal)
    signal?.throwIfAborted()
    if (total !== undefined && total !== page.total) throw new Error('读取星图时总数已变化，请重新载入，避免遗漏记忆。')
    total = page.total
    if (page.offset !== offset || !Number.isInteger(total) || total < 0 || (offset < total && !page.nodes.length)) throw new Error('星图分页契约异常，未将不完整结果当作全量数据。')
    for (const node of page.nodes) {
      if (ids.has(node.id)) throw new Error('读取星图时排序或版本已变化，请重新载入。')
      ids.add(node.id); nodes.push(node)
      for (const edge of node.relations || []) relations.set(edge.id, edge)
    }
    for (const edge of page.edges) relations.set(edge.id, edge)
    offset += page.nodes.length
    onProgress?.(offset, total)
  } while (offset < (total ?? 0))
  if (nodes.length !== total) throw new Error('星图总数与分页条目不一致，请重新载入。')
  return { items: nodes, nodes, edges: [...relations.values()].filter(edge => ids.has(edge.from_id) && ids.has(edge.to_id)), total, offset: 0, limit: 200 }
}
export function memoryV2NebulaData(graph: Pick<MemoryV2Graph, 'nodes' | 'edges'>) {
  return {
    nodes: graph.nodes.map(item => ({ id: item.id, key: item.key, title: item.key, layer: `L${item.layer}` as 'L1' | 'L2' | 'L3' | 'L4', scope: item.scope, importance: item.importance, activation: item.activation, theme: item.category, status: item.status, pinned:item.pinned })),
    edges: graph.edges.map(edge => ({ source: edge.from_id, target: edge.to_id, kind: edge.kind, confirmed: edge.kind === 'confirmed' && edge.asserted === true })),
  }
}
export function unresolvedMemoryV2Migrations(counts: Record<string, number>): number {
  return (counts.pending || 0) + (counts.anomaly || 0) + (counts.blocked || 0)
}
export function canActivateMemoryV2(page: MemoryV2MigrationPage | undefined, exhausted: boolean): boolean {
  return Boolean(page && page.authority !== 'v2' && exhausted && unresolvedMemoryV2Migrations(page.counts) === 0)
}
export function memoryV2Conflict(error: unknown): boolean { return typeof error === 'object' && error !== null && 'status' in error && error.status === 409 }
/** Only real timestamps become markers. Never invent reinforcement history from count. */
export function memoryV2ReinforcementPoints(item: MemoryV2Item): MemoryV2Reinforcement[] {
  if (item.reinforcement_events) return item.reinforcement_events.filter(event => Number.isFinite(Date.parse(event.at)))
  return item.reinforced_at && Number.isFinite(Date.parse(item.reinforced_at)) ? [{ id: 'last-known-reinforcement', action: '最后一次真实强化（完整历史未提供）', at: item.reinforced_at }] : []
}
export function memoryV2Curve(item: MemoryV2Item, at = Date.now()) {
  const base = Date.parse(item.reinforced_at || item.confirmed_at || item.created_at)
  if (!Number.isFinite(base)) return []
  const horizon = item.half_life_days ? item.half_life_days * 3 : 180
  const elapsed = Math.max(0, (at - base) / 86400000)
  const days = Math.max(horizon, elapsed)
  return Array.from({ length: 61 }, (_, index) => ({ at: base + days * index / 60 * 86400000, activation: item.half_life_days === null ? 1 : Math.exp(-Math.LN2 * days * index / 60 / item.half_life_days) }))
}
export function memoryV2OperationId(prefix: string): string { return `${prefix}-${globalThis.crypto.randomUUID()}` }
