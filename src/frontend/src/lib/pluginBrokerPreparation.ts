import { ApiError, api, json } from '@/api/client'
import { parsePluginArgs } from '@/lib/pluginPackages'
import type { PluginPackage } from '@/lib/pluginPackages'

export type BrokerRequest = { op: string; target_argument: string; params: Record<string, unknown> }
export type BrokerDraft = { args: Record<string, unknown>; requests: BrokerRequest[] }
export type PreparationSource = {
  plugin_id: string; expected_revision: number; expected_digest: string; tool: string;
  args: Record<string, unknown>; user_confirmed: true;
}
export type PreparationBinding = {
  plugin_id: string; tool: string; revision: number; digest: string; source_sha256: string;
  source_fields: string[]; args_sha256: string; config_fingerprint: string; state_fingerprint: string;
  trust_snapshot: unknown; trust_fingerprint: string; dependency_binding: unknown[];
}
export type ObjectPreparation = {
  binding: PreparationBinding; selected_objects: Array<{
    selection_id: string; target: string; operation: string; identity: Record<string, unknown>;
    scope: 'one_explicit_object_snapshot_only'; execution_authorized: false;
  }>;
  required_requests: Array<BrokerRequest & { request_index: number; target: string }>;
  selection_fingerprint: string; expires_in_upper_bound: number; one_preview_only: true;
  selected_target_content_read: false; sent: false; execution_authorized: false; full_call_approval_required: true;
}
export type HttpParams = {
  request_descriptor: string; url: string; method: string; body_sha256: string;
  max_response_bytes: number; allow_redirects: false;
}
export type HttpPreparation = {
  binding: PreparationBinding; request_index: number; required_request: BrokerRequest & { params: HttpParams };
  descriptor: { source_id: string; params: HttpParams; sent: false; dns_preview_only: true; separate_complete_call_approval_required: true };
  pinned_target: Record<string, unknown>; target_fingerprint: string; body_text_utf8: string;
  body_bytes: number; body_sha256: string; args: Record<string, unknown>; expires_in: number;
  dns_preview_only: true; sent: false; execution_authorized: false; full_call_approval_required: true;
  object_selection_required_after_args_change: true;
}
export type ActiveModelPending = {
  request_id: string; plugin_id: string; revision: number; digest: string; tool: string;
  submission_sha256: string; submission_fingerprint: string; owner_session_id_sha256: string;
  current_deadline_monotonic: number; expires_in: number; original_runtime_deadline: true;
  approval_required: true; sent: false;
}
export type ActiveModelPendingList = {
  items: ActiveModelPending[]; owner_from_authenticated_principal: true;
  original_runtime_deadline: true; ttl_extended: false; child_kept_alive: false; automatic_refresh: false;
}
export const objectBrokerOperation = (op: string) => ['file.read', 'file.write', 'data.read', 'data.write'].includes(op)
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const sha = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const integer = (value: unknown, low = 0, high = Number.MAX_SAFE_INTEGER): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= low && value <= high
const exact = (value: Record<string, unknown>, keys: readonly string[]) => Object.keys(value).length === keys.length && keys.every(key => key in value)
const sourceFields = ['manifest', 'tool', 'args', 'config', 'state', 'plugin_id', 'revision', 'digest', 'trust_fingerprint', 'dependency_binding']
const canonical = (value: unknown): unknown => Array.isArray(value) ? value.map(canonical) : object(value) ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value
const jsonText = (value: unknown) => JSON.stringify(canonical(value))
function invalid(): never { throw new Error('具体目标准备响应不符合真实宿主合同；未执行、未发送，也不会自动重试') }
function basePath(value: string) {
  const base = value.replace(/\/+$/, '')
  if (!base.startsWith('/') || base.startsWith('//') || /[?#\\]/.test(base) || base.split('/').some(part => part === '.' || part === '..')) throw new Error('目标准备 API 必须为同源绝对路径')
  return base
}
export function brokerPreparationError(error: unknown): string {
  if (error instanceof ApiError && object(error.payload) && object(error.payload.detail) && typeof error.payload.detail.code === 'string') {
    const code = error.payload.detail.code
    if (/^[a-z][a-z0-9_]{1,100}$/.test(code)) {
      const messages: Record<string, string> = {
        preparation_select_exact_all_declared_objects_required: '必须逐项明确选中当前 args 内全部 file/data 请求；若只要部分目标，请先删改本次 args，不会自动扩大选择。',
        preparation_http_secret_body_forbidden: 'HTTP body 含敏感字段或疑似真实 Key；严格拒绝。',
        preparation_http_binary_body_forbidden: 'HTTP body 只接受可完整审阅的 UTF-8 文本；二进制与控制字节拒绝。',
        preparation_exact_declared_http_target_required: 'URL 必须与当前 broker_requests 对应的 manifest URL 目标参数完全一致。',
        preparation_http_explicit_parameters_changed: '显式 method / 响应上限与现有 broker_requests 不一致；请先明确编辑本次 args。',
        preparation_source_changed_during_dns: 'DNS 期间 revision/config/state/dependency/trust 源已变化，准备不可用，请重新核对。',
        preparation_real_broker_composition_required: '宿主没有提供当前 manager 的真实 broker composition；不会构造替代 runtime。',
        composition_plain_http_requires_separate_user_approval: '本入口严格拒绝明文 HTTP；不存在可绕过的 allow_plain_http 开关。',
        http_private_target_not_approved: '宿主 DNS 指向非公网地址；本入口严格拒绝 private/loopback 目标。',
        broker_explicit_operation_declaration_required: '请先在 args.broker_requests 明确声明具体操作和精确 params；权限/路径不推导操作。',
        broker_request_input_schema_declaration_required: '工具 input_schema 没有显式声明 broker_requests 数组，不能准备。',
        invalid_args: '当前 args 或宿主生成的 descriptor 不符合实际工具 input_schema；不会绕过 schema。',
      }
      return `${messages[code] || '具体目标准备未完成；请核对完整声明与参数。'}（${code}）`
    }
  }
  return error instanceof Error ? error.message : '具体目标准备未确认；不自动重试'
}

/** Read explicit declarations only; never invent an operation, object_id, offset, method or URL. */
export function brokerPreparationDraft(item: PluginPackage, toolName: string, text: string): BrokerDraft {
  const args = parsePluginArgs(text)
  const tools = item.manifest.tools.filter(tool => tool.name === toolName)
  if (tools.length !== 1) throw new Error('请明确选择一个真实 manifest 工具')
  const declaration = tools[0]
  const properties = declaration.input_schema?.properties
  if (!object(properties) || !object(properties.broker_requests) || properties.broker_requests.type !== 'array') throw new Error('所选工具 input_schema 必须明确声明 broker_requests 数组')
  const requests = args.broker_requests
  if (!Array.isArray(requests) || !requests.length || requests.length > 32) throw new Error('请先在 args.broker_requests 明确填写 1–32 个具体请求；这里不猜操作或生成占位参数')
  const permissions = new Set([...(item.manifest.permissions || []), ...(declaration.permissions || [])])
  const operationPermission: Record<string, string> = { 'file.read': 'file.read', 'file.write': 'file.write', 'data.read': 'plugin.data.read', 'data.write': 'plugin.data.write', 'http.request': 'network.http', 'model.generate': 'model.generate' }
  const used = new Set<number>()
  const parsed = requests.map((request): BrokerRequest => {
    if (!object(request) || !exact(request, ['op', 'target_argument', 'params']) || typeof request.op !== 'string'
      || typeof request.target_argument !== 'string' || !object(request.params) || !(request.op in operationPermission)) throw new Error('broker_requests 项必须原样包含 op / target_argument / params，且为已支持的显式操作')
    const permission = operationPermission[request.op]
    const kind = objectBrokerOperation(request.op) ? 'path' : request.op === 'http.request' ? 'url' : 'string'
    const targets = (declaration.targets || []).map((target, index) => ({ target, index })).filter(({ target }) => target.permission === permission && target.argument === request.target_argument && target.kind === kind)
    if (!permissions.has(permission) || targets.length !== 1 || typeof args[request.target_argument] !== 'string') throw new Error('请求必须精确匹配 manifest 权限、target_argument、kind 与本次字符串目标')
    used.add(targets[0].index)
    if (request.op === 'http.request' && request.params.url !== args[request.target_argument]) throw new Error('HTTP params.url 必须先与本次 URL 目标完全一致；准备不会猜 URL')
    if (request.op === 'model.generate' && request.params.profile_id !== args[request.target_argument]) throw new Error('model params.profile_id 必须与本次目标完全一致')
    return { op: request.op, target_argument: request.target_argument, params: request.params }
  })
  if (used.size !== (declaration.targets || []).length) throw new Error('每个 manifest 工具目标必须有本次精确 broker request；不会自动注册未选择的清单路径')
  return { args, requests: parsed }
}
function binding(value: unknown, input: PreparationSource): value is PreparationBinding {
  return object(value) && exact(value, ['plugin_id', 'tool', 'revision', 'digest', 'source_sha256', 'source_fields', 'args_sha256', 'config_fingerprint', 'state_fingerprint', 'trust_snapshot', 'trust_fingerprint', 'dependency_binding'])
    && value.plugin_id === input.plugin_id && value.tool === input.tool && value.revision === input.expected_revision && value.digest === input.expected_digest
    && sha(value.source_sha256) && jsonText(value.source_fields) === jsonText(sourceFields) && sha(value.args_sha256)
    && sha(value.config_fingerprint) && sha(value.state_fingerprint) && sha(value.trust_fingerprint) && Array.isArray(value.dependency_binding)
}
function noExecution(value: Record<string, unknown>) {
  return value.sent === false && value.execution_authorized === false && value.full_call_approval_required === true
}
function httpParams(value: unknown): value is HttpParams {
  return object(value) && exact(value, ['request_descriptor', 'url', 'method', 'body_sha256', 'max_response_bytes', 'allow_redirects'])
    && typeof value.request_descriptor === 'string' && /^http_[a-f0-9]{32}$/.test(value.request_descriptor)
    && typeof value.url === 'string' && typeof value.method === 'string' && ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'].includes(value.method)
    && sha(value.body_sha256) && integer(value.max_response_bytes, 1, 12000) && value.allow_redirects === false
}
export function createPluginBrokerPreparationClient({ apiBase = '/v1/plugin-packages' }: { apiBase?: string } = {}) {
  const base = `${basePath(apiBase)}/preparations`
  return {
    async objects(input: PreparationSource & { selected_request_indices: number[] }): Promise<ObjectPreparation> {
      const value = await api<unknown>(`${base}/objects`, json('POST', input))
      const requests = input.args.broker_requests
      if (!object(value) || !binding(value.binding, input) || !noExecution(value) || value.selected_target_content_read !== false
        || value.one_preview_only !== true || value.expires_in_upper_bound !== 120 || !sha(value.selection_fingerprint)
        || !Array.isArray(value.selected_objects) || !Array.isArray(value.required_requests) || !Array.isArray(requests)
        || value.selected_objects.length !== input.selected_request_indices.length || value.required_requests.length !== input.selected_request_indices.length) invalid()
      for (const [position, index] of input.selected_request_indices.entries()) {
        const selected = value.selected_objects[position]
        const required = value.required_requests[position]
        const declared = requests[index]
        if (!object(selected) || !object(required) || !object(declared) || required.request_index !== index
          || required.op !== declared.op || required.target_argument !== declared.target_argument || jsonText(required.params) !== jsonText(declared.params)
          || required.target !== input.args[String(declared.target_argument)] || !sha(selected.selection_id)
          || selected.target !== required.target || selected.operation !== required.op || !object(selected.identity)
          || selected.scope !== 'one_explicit_object_snapshot_only' || selected.execution_authorized !== false) invalid()
      }
      return value as ObjectPreparation
    },
    async http(input: PreparationSource & { request_index: number; url: string; method: string; body_text: string; max_response_bytes: number }): Promise<HttpPreparation> {
      const value = await api<unknown>(`${base}/http`, json('POST', input))
      const declared = Array.isArray(input.args.broker_requests) ? input.args.broker_requests[input.request_index] : undefined
      if (!object(value) || !binding(value.binding, input) || !noExecution(value) || value.dns_preview_only !== true
        || value.object_selection_required_after_args_change !== true || value.request_index !== input.request_index
        || !object(value.required_request) || value.required_request.op !== 'http.request' || !object(declared)
        || value.required_request.target_argument !== declared.target_argument || !httpParams(value.required_request.params)
        || value.required_request.params.url !== input.url || value.required_request.params.method !== input.method
        || value.required_request.params.max_response_bytes !== input.max_response_bytes || !object(value.descriptor)
        || value.descriptor.source_id !== value.required_request.params.request_descriptor || value.descriptor.sent !== false
        || value.descriptor.dns_preview_only !== true || value.descriptor.separate_complete_call_approval_required !== true
        || jsonText(value.descriptor.params) !== jsonText(value.required_request.params)
        || !object(value.pinned_target) || value.pinned_target.request_descriptor !== value.descriptor.source_id
        || value.pinned_target.url !== input.url || value.pinned_target.method !== input.method
        || value.pinned_target.body_sha256 !== value.required_request.params.body_sha256 || value.pinned_target.max_response_bytes !== input.max_response_bytes
        || !Array.isArray(value.pinned_target.headers) || value.pinned_target.headers.length !== 0
        || value.pinned_target.allow_private !== false || value.pinned_target.allow_plain_http !== false || value.pinned_target.allow_redirects !== false
        || !Array.isArray(value.pinned_target.approved_ips) || !value.pinned_target.approved_ips.length || !value.pinned_target.approved_ips.every(ip => typeof ip === 'string')
        || !sha(value.target_fingerprint) || value.body_text_utf8 !== input.body_text || !sha(value.body_sha256)
        || value.body_sha256 !== value.required_request.params.body_sha256 || value.body_bytes !== new TextEncoder().encode(input.body_text).length
        || typeof value.expires_in !== 'number' || !Number.isFinite(value.expires_in) || value.expires_in <= 0 || value.expires_in > 120 || !object(value.args)) invalid()
      const expected = structuredClone(input.args)
      const requests = expected.broker_requests
      if (!Array.isArray(requests)) invalid()
      const request = requests[input.request_index]
      if (!object(request)) invalid()
      request.params = value.required_request.params
      if (jsonText(expected) !== jsonText(value.args)) invalid()
      return value as HttpPreparation
    },
    async pending(signal?: AbortSignal): Promise<ActiveModelPendingList> {
      const value = await api<unknown>(`${base}/pending`, { signal })
      if (!object(value) || !exact(value, ['items', 'owner_from_authenticated_principal', 'original_runtime_deadline', 'ttl_extended', 'child_kept_alive', 'automatic_refresh'])
        || value.owner_from_authenticated_principal !== true || value.original_runtime_deadline !== true || value.ttl_extended !== false
        || value.child_kept_alive !== false || value.automatic_refresh !== false || !Array.isArray(value.items) || value.items.length > 128) invalid()
      const seen = new Set<string>()
      for (const item of value.items) {
        if (!object(item) || !exact(item, ['request_id', 'plugin_id', 'revision', 'digest', 'tool', 'submission_sha256', 'submission_fingerprint', 'owner_session_id_sha256', 'current_deadline_monotonic', 'expires_in', 'original_runtime_deadline', 'approval_required', 'sent'])
          || typeof item.request_id !== 'string' || !/^[A-Za-z0-9_-]{16,128}$/.test(item.request_id) || seen.has(item.request_id)
          || typeof item.plugin_id !== 'string' || !/^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$/.test(item.plugin_id)
          || !integer(item.revision, 1) || !sha(item.digest) || typeof item.tool !== 'string' || !sha(item.submission_sha256)
          || !sha(item.submission_fingerprint) || !sha(item.owner_session_id_sha256) || typeof item.current_deadline_monotonic !== 'number'
          || !Number.isFinite(item.current_deadline_monotonic) || typeof item.expires_in !== 'number' || !Number.isFinite(item.expires_in) || item.expires_in <= 0 || item.expires_in > 10
          || item.original_runtime_deadline !== true || item.approval_required !== true || item.sent !== false) invalid()
        seen.add(item.request_id)
      }
      return value as ActiveModelPendingList
    },
  }
}
export type PluginBrokerPreparationClient = ReturnType<typeof createPluginBrokerPreparationClient>
