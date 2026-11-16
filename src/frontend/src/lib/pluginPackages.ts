/** Local plugin-package v1 contract. No application imports, uploads or automatic retries. */
export type JsonObject = { [key: string]: unknown }
export type PluginAction = 'install' | 'update' | 'restore' | 'enable' | 'disable' | 'rollback' | 'uninstall' | 'purge' | 'config' | 'state' | 'credentials' | 'call'
export type ValueKind = 'config' | 'state' | 'credentials'
export type LifecycleAction = 'enable' | 'disable' | 'rollback' | 'uninstall' | 'purge'
export type PluginBlock = { code: string; [key: string]: unknown }
export type PluginTool = { name: string; description?: string; input_schema?: JsonObject; permissions?: string[]; targets?: Array<{ permission: string; argument: string; kind: 'url' | 'path' | 'string' }> }
export type PluginManifest = {
  schema_version: number; id: string; name: string; version: string; host_api: string
  description?: string; files: Record<string, string>; tools: PluginTool[]
  entrypoint?: { type: 'python'; path: string; callable: string }
  permissions?: string[]; dependencies?: Array<{ id: string; version: string }>; python_dependencies?: string[]
  signature?: { algorithm: string; key_id: string; signature: string }; skills?: unknown; mcp?: unknown; ui?: unknown; config_schema?: unknown
}
export type PluginPackage = {
  id: string; revision: number; name: string; version: string
  installed: boolean; enabled: boolean; loaded: boolean; needs_restart: boolean
  error: string; missing: boolean; files_mismatch: boolean; digest: string; previous_digest: string
  pending_update: boolean; signature_status: string; source: string | null; manifest: PluginManifest
  data_dir: string; config_dir: string; blocked: PluginBlock[]
}
export type PluginListing = { items: PluginPackage[]; marketplace: { status: string; network_requests: boolean }; isolation_warning: string }
export type PluginSnapshot = {
  action: PluginAction; id: string; expected_revision: number; digest: string
  permissions: string[]; targets: Array<{ kind: string; value: unknown; permission?: string }>
  source_path?: string; manifest?: PluginManifest; signature_status?: string; total_bytes?: number
  risk?: string; warning?: string; retained?: string[]; value?: unknown; tool?: string; args?: JsonObject
  config_fingerprint?: string; state_fingerprint?: string; host_rpc_allowlist?: unknown[]
  delta?:{reused_files:number;changed_files:number;removed_files:number;note:string}
}
export type PluginPreview = { snapshot: PluginSnapshot; fingerprint: string; blocked: PluginBlock[]; token: string | null; expires_at: number | null }
export type ScanCandidate = { path: string; status: string; id?: string; version?: string; error?: string; deferred?: boolean }
export type PluginScan = { items: ScanCandidate[]; planning_only: boolean; auto_install: boolean; limit: number }
export type PluginValue = { id: string; revision: number; value: unknown }
export type AuditEntry = { seq: number; at: number; plugin_id: string; action: string; result: string; fingerprint: string }
export type PluginAudit = { items: AuditEntry[] }
export type PluginExecution = { ok: true; id: string; fingerprint: string; result: unknown }
export type ApprovalBody = { token: string; fingerprint: string }
export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>

export const PLUGIN_PYTHON_RISK = '个人自用插件，无需发布者信任或沙箱设置。'
const actions = new Set<PluginAction>(['install', 'update', 'restore', 'enable', 'disable', 'rollback', 'uninstall', 'purge', 'config', 'state', 'credentials', 'call'])
const object = (value: unknown): value is JsonObject => value !== null && typeof value === 'object' && !Array.isArray(value)
const revision = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every((item) => typeof item === 'string')
const digest = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
function malformed(): never { throw new Error('插件服务返回格式无效，请刷新后重试预览') }

/** Only a human-readable message is exposed; never stringify details, payloads or stacks. */
export function pluginErrorMessage(error: unknown, fallback = '插件操作未完成'): string {
  if (error instanceof Error && error.message.trim()) return error.message
  if (typeof error === 'string' && error.trim()) return error
  if (object(error)) {
    if (typeof error.message === 'string' && error.message.trim()) return error.message
    if (typeof error.detail === 'string' && error.detail.trim()) return error.detail
    if (object(error.detail) && typeof error.detail.message === 'string' && error.detail.message.trim()) return error.detail.message
    if (object(error.error) && typeof error.error.message === 'string' && error.error.message.trim()) return error.error.message
    if (Array.isArray(error.detail)) {
      const messages = error.detail.flatMap((item) => object(item) && typeof item.msg === 'string' ? [item.msg] : [])
      if (messages.length) return messages.join('；')
    }
  }
  return fallback
}
export class PluginRequestError extends Error {
  status: number
  code: string
  unknownOutcome: boolean
  constructor(message: string, status = 0, code = '', unknownOutcome = false) {
    super(message); this.name = 'PluginRequestError'; this.status = status; this.code = code; this.unknownOutcome = unknownOutcome
  }
}
function blocks(value: unknown): value is PluginBlock[] { return Array.isArray(value) && value.every((item) => object(item) && typeof item.code === 'string') }
function manifest(value: unknown): value is PluginManifest {
  return object(value) && typeof value.id === 'string' && typeof value.name === 'string' && typeof value.version === 'string'
    && typeof value.host_api === 'string' && object(value.files) && Object.values(value.files).every(digest)
    && (value.permissions === undefined || strings(value.permissions))
    && (value.dependencies === undefined || Array.isArray(value.dependencies) && value.dependencies.every((dep) => object(dep) && typeof dep.id === 'string' && typeof dep.version === 'string'))
    && (value.python_dependencies === undefined || strings(value.python_dependencies))
    && Array.isArray(value.tools) && value.tools.every((tool) => object(tool) && typeof tool.name === 'string' && (tool.permissions === undefined || strings(tool.permissions)))
}
export function parsePluginListing(value: unknown): PluginListing {
  if (!object(value) || !Array.isArray(value.items) || !object(value.marketplace) || typeof value.marketplace.status !== 'string'
    || typeof value.marketplace.network_requests !== 'boolean' || typeof value.isolation_warning !== 'string') malformed()
  for (const item of value.items) {
    if (!object(item) || !revision(item.revision) || !manifest(item.manifest) || !blocks(item.blocked)
      || !['id', 'name', 'version', 'digest', 'previous_digest', 'signature_status', 'error', 'data_dir', 'config_dir'].every((key) => typeof item[key] === 'string')
      || !['installed', 'enabled', 'loaded', 'needs_restart', 'missing', 'files_mismatch', 'pending_update'].every((key) => typeof item[key] === 'boolean')
      || !(item.source === null || typeof item.source === 'string')) malformed()
  }
  return value as PluginListing
}
export function parsePluginPreview(value: unknown): PluginPreview {
  if (!object(value) || !object(value.snapshot) || !digest(value.fingerprint) || !blocks(value.blocked)) malformed()
  const snapshot = value.snapshot
  if (!actions.has(snapshot.action as PluginAction) || typeof snapshot.id !== 'string' || !revision(snapshot.expected_revision)
    || typeof snapshot.digest !== 'string' || !strings(snapshot.permissions) || !Array.isArray(snapshot.targets)
    || !snapshot.targets.every((item) => object(item) && typeof item.kind === 'string' && 'value' in item)) malformed()
  if (!(value.token === null || typeof value.token === 'string' && value.token.length >= 20 && value.token.length <= 200)
    || !(value.expires_at === null || typeof value.expires_at === 'number' && Number.isFinite(value.expires_at) && value.expires_at > 0)) malformed()
  if (!value.blocked.length && (!value.token || !value.expires_at)) malformed()
  if (['install', 'update', 'restore'].includes(String(snapshot.action)) && (!manifest(snapshot.manifest) || typeof snapshot.source_path !== 'string' || typeof snapshot.signature_status !== 'string')) malformed()
  if (snapshot.action === 'call' && (typeof snapshot.tool !== 'string' || !object(snapshot.args) || typeof snapshot.risk !== 'string')) malformed()
  return value as PluginPreview
}
export function parsePluginScan(value: unknown): PluginScan {
  if (!object(value) || !Array.isArray(value.items) || typeof value.planning_only !== 'boolean' || typeof value.auto_install !== 'boolean' || !Number.isSafeInteger(value.limit)) malformed()
  if (!value.items.every((item) => object(item) && typeof item.path === 'string' && typeof item.status === 'string' && (item.error === undefined || typeof item.error === 'string'))) malformed()
  return value as PluginScan
}
/** A scan candidate may only populate the preview path; it is never an installation approval. */
export function canPreviewCandidate(item: ScanCandidate): boolean { return item.status === 'candidate_only' && item.deferred !== true }
export function approvalProblem(preview: PluginPreview, now = Date.now()): string | null {
  if (preview.blocked.length) return '操作被阻止，请先解决下列依赖或前置条件'
  if (!preview.token || !digest(preview.fingerprint)) return '没有有效授权，请重新预览'
  if (!preview.expires_at || !Number.isFinite(preview.expires_at) || preview.expires_at * 1000 <= now) return '预览已过期，请重新预览；不会自动续期或提交'
  return null
}
const blockLabels: Record<string, string> = {
  host_api: '宿主 API 版本不兼容', missing_dependency: '缺少插件依赖', dependency_version: '插件依赖版本不匹配', dependency_disabled: '插件依赖未启用',
  dependency_cycle: '存在循环依赖', reverse_dependency_version: '将破坏其他插件的版本要求', python_dependency_invalid: 'Python 依赖声明无效',
  python_dependency_version: 'Python 依赖版本不匹配', python_dependency_missing: '缺少 Python 依赖（不会自动 pip）', invalid_signature: '来源签名无效', revoked_signature: '发布者信任已撤销',
  update_requires_newer_version: '更新包必须使用更高版本', enabled_dependents: '存在启用中的依赖方', not_installed: '尚未安装',
  no_previous_version: '没有可回滚版本', previous_version_missing: '上一版代码记录缺失', uninstall_required: '须先卸载，数据清除不能与卸载合并',
  credential_revoker_not_integrated: '宿主尚未集成系统凭据引用撤销，清除被阻止',
}
export function blockMessage(block: PluginBlock): string {
  const fields = ['id', 'requirement', 'required', 'actual'].flatMap((key) => typeof block[key] === 'string' ? [`${key}: ${block[key]}`] : [])
  return `${blockLabels[block.code] || block.code}${fields.length ? `（${fields.join('；')}）` : ''}`
}
export function signatureLabel(status: string): string {
  return ({ not_required: '个人插件', absent: '无签名', present_unverified: '有签名但未验证', verified_trusted: '宿主可信验证通过', invalid: '签名无效', revoked: '发布者信任已撤销', unknown: '签名状态未知' } as Record<string, string>)[status] || `未知签名状态：${status}`
}
export function containsCredentialField(value: unknown): boolean {
  if (Array.isArray(value)) return value.some(containsCredentialField)
  return object(value) && Object.entries(value).some(([key, child]) => /(api[_-]?key|password|secret|access[_-]?token|credential)/i.test(key) || containsCredentialField(child))
}
function containsLikelyKey(value: unknown): boolean {
  if (typeof value === 'string') return /^(sk[-_]|gsk_|AIza|AKIA|gh[pousr]_)/.test(value) || /^Bearer\s+\S+/i.test(value)
  if (Array.isArray(value)) return value.some(containsLikelyKey)
  return object(value) && Object.values(value).some(containsLikelyKey)
}
function parseJson(text: string): unknown {
  if (new TextEncoder().encode(text).length > 65536) throw new Error('JSON 最大 64 KiB')
  try { return JSON.parse(text) } catch { throw new Error('JSON 格式无效，请检查引号、逗号与括号') }
}
export function parsePluginValue(text: string, kind: ValueKind): unknown {
  const value = parseJson(text)
  if (kind === 'credentials') {
    if (!Array.isArray(value) || value.length > 30 || !value.every((ref) => typeof ref === 'string' && /^[a-zA-Z0-9_.:-]{1,120}$/.test(ref))) throw new Error('凭据仅接受最多 30 个 reference 字符串的 JSON 数组，不接受 Key')
    if (containsLikelyKey(value)) throw new Error('疑似真实 Key；这里只能填写宿主已有凭据的 reference，不能提交真实 Key')
    return value
  }
  if (!object(value)) throw new Error('配置与状态必须是 JSON 对象，不是 Key 或 JSON 字符串')
  if (containsCredentialField(value) || containsLikelyKey(value)) throw new Error('配置与状态禁止密钥字段或疑似真实 Key；凭据只能在凭据引用中填写 reference，不能提交真实 Key')
  return value
}
export function parsePluginArgs(text: string): JsonObject {
  const value = parseJson(text)
  if (!object(value)) throw new Error('工具参数必须是 JSON 对象')
  if (containsCredentialField(value) || containsLikelyKey(value)) throw new Error('工具参数不能包含密钥字段或疑似真实凭据；请通过宿主凭据引用机制配置')
  return value
}
function pathId(id: string) { if (!/^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$/.test(id)) throw new Error('插件 ID 无效'); return encodeURIComponent(id) }
function expectedRevision(value: number) { if (!revision(value)) throw new Error('插件版本记录无效，请刷新'); return value }
function sourcePath(value: string) { const path = value.trim(); if (!path || path.length > 4000 || path.includes('\0')) throw new Error('请输入有效的后端本机目录或 ZIP 路径'); return path }

export type PluginPackagesClient = ReturnType<typeof createPluginPackagesClient>
export function createPluginPackagesClient({ apiBase = '/v1/plugin-packages', fetcher = (path, init) => fetch(path, init) }: { apiBase?: string; fetcher?: FetchLike } = {}) {
  const base = apiBase.replace(/\/+$/, '')
  const consumed = new Set<string>()
  async function request(path: string, body?: unknown, signal?: AbortSignal, mutation = false): Promise<unknown> {
    let response: Response
    try {
      response = await fetcher(`${base}${path}`, { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body),
        signal: signal ?? (body === undefined ? AbortSignal.timeout(15000) : undefined) })
    } catch {
      throw new PluginRequestError(mutation ? '连接中断，提交或执行结果未知；不会自动重试。请刷新状态并查看审计后重新预览。' : '无法连接插件服务，请检查连接后刷新', 0, '', mutation)
    }
    let payload: unknown
    try { payload = await response.json() } catch {
      throw new PluginRequestError(mutation ? '服务响应无法读取，操作结果未知；不会自动重试。请刷新并查看审计。' : '插件服务未返回有效 JSON', response.status, '', mutation)
    }
    if (!response.ok) {
      const detail = object(payload) && object(payload.detail) ? payload.detail : payload
      throw new PluginRequestError(pluginErrorMessage(payload, `插件请求失败（HTTP ${response.status}）`), response.status, object(detail) && typeof detail.code === 'string' ? detail.code : '', mutation && response.status >= 500)
    }
    return payload
  }
  async function approve(preview: PluginPreview, call: boolean) {
    parsePluginPreview(preview)
    const problem = approvalProblem(preview)
    if (problem) throw new Error(problem)
    if ((preview.snapshot.action === 'call') !== call) throw new Error('授权不能跨操作使用，请重新预览')
    if (consumed.has(preview.token!)) throw new Error('本次授权已尝试使用；失败或结果未知也不能重试，请重新预览')
    // Mark before the request. A rejected response, lost response or double-click must not resend.
    consumed.add(preview.token!)
    const result = await request(call ? '/calls/execute' : '/commit', { token: preview.token!, fingerprint: preview.fingerprint } satisfies ApprovalBody, undefined, true)
    const valid = object(result) && (call
      ? result.ok === true && result.id === preview.snapshot.id && result.fingerprint === preview.fingerprint && 'result' in result
      : result.id === preview.snapshot.id && revision(result.revision) && typeof result.installed === 'boolean' && typeof result.enabled === 'boolean')
    if (!valid) throw new PluginRequestError('操作响应格式无效，结果未知；不会自动重试。请刷新并查看审计后重新预览。', 200, '', true)
    return result
  }
  return {
    async list(signal?: AbortSignal) { return parsePluginListing(await request('', undefined, signal)) },
    async scan(directory: string) { return parsePluginScan(await request('/scan', { directory: sourcePath(directory) })) },
    async previewSource(action: 'install' | 'update' | 'restore', path: string, expected = 0, id?: string) {
      if (action !== 'install' && !id) throw new Error('请选择操作目标插件')
      return parsePluginPreview(await request(action === 'install' ? '/install/preview' : `/${pathId(id!)}/${action}/preview`, { source_path: sourcePath(path), expected_revision: expectedRevision(expected) }))
    },
    async previewAction(id: string, action: LifecycleAction, expected: number) {
      if (!['enable', 'disable', 'rollback', 'uninstall', 'purge'].includes(action)) throw new Error('不支持的生命周期操作')
      return parsePluginPreview(await request(`/${pathId(id)}/${action}/preview`, { expected_revision: expectedRevision(expected) }))
    },
    async readValue(id: string, kind: ValueKind): Promise<PluginValue> {
      if (!['config', 'state', 'credentials'].includes(kind)) throw new Error('未知配置类型')
      const value = await request(`/${pathId(id)}/${kind}`)
      if (!object(value) || value.id !== id || !revision(value.revision) || !('value' in value)) malformed()
      return value as PluginValue
    },
    async previewValue(id: string, kind: ValueKind, expected: number, text: string) {
      if (!['config', 'state', 'credentials'].includes(kind)) throw new Error('未知配置类型')
      return parsePluginPreview(await request(`/${pathId(id)}/${kind}/preview`, { expected_revision: expectedRevision(expected), value: parsePluginValue(text, kind) }))
    },
    async previewCall(id: string, tool: string, text: string, expected: number) {
      if (!tool || tool.length > 101) throw new Error('请选择工具')
      return parsePluginPreview(await request(`/${pathId(id)}/calls/preview`, { tool, args: parsePluginArgs(text), expected_revision: expectedRevision(expected) }))
    },
    async commit(preview: PluginPreview): Promise<PluginPackage> { return await approve(preview, false) as PluginPackage },
    async execute(preview: PluginPreview): Promise<PluginExecution> { return await approve(preview, true) as PluginExecution },
    async audit(id: string): Promise<PluginAudit> {
      const value = await request(`/${pathId(id)}/audit?limit=100`)
      if (!object(value) || !Array.isArray(value.items) || !value.items.every((item) => object(item) && typeof item.at === 'number' && typeof item.action === 'string' && typeof item.result === 'string' && typeof item.fingerprint === 'string')) malformed()
      return value as PluginAudit
    },
  }
}
