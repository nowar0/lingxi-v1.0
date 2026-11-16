import { ApiError, api, json } from '@/api/client'
import { createModelMaterialSendClient } from '@/lib/model-material-send'
import type { AvailableModelHostRoute, ModelHostEffort, PreparedModelSubmission } from '@/lib/model-material-send'

/** UI identifiers are opaque handles, never plugin PID/owner/session authority or compiled payloads. */
export type PluginModelParams = {
  profile_id: string; provider: string; model: string; prompt_id: string; prompt_sha256: string;
  max_input_tokens: number; max_output_tokens: number;
}
export type PluginModelPrepared = {
  plugin_id: string; tool: string; revision: number; params: PluginModelParams;
  submission_sha256: string; submission_fingerprint: string; expires_in: number;
  prepared: true; sent: false; approval_required: true; final_send_requires_separate_ui_approval: true;
}
export type PluginModelSource = {
  scope_id: string; request_id: string; submission_sha256: string; submission_fingerprint: string;
  profile_id: string; provider: string; model: string; expires_in: number;
  ready_for_verified_adapter: boolean; estimated_text_tokens: number; media_token_estimate: number | null;
  sent: false; send_approval_created: false;
}
export type PluginModelPendingMetadata = {
  request_id: string; submission_sha256: string; submission_fingerprint: string;
  approval_required: true; sent: false; expires_in: number; original_runtime_deadline: true;
}
export type PluginModelPreview = {
  request_id: string; preview: Record<string, unknown>; token: string | null; fingerprint: string | null;
  approval_required: boolean; sent: false; expires_in: number; original_runtime_deadline: true;
  blocked?: string; host_configured_attempt_supported: boolean; capability_notice: string;
  requires_unverified_capabilities_acceptance: boolean;
  unverified_capabilities_confirmation_field: 'accept_unverified_capabilities';
  final_send_requires_separate_ui_approval: true;
}
export type PluginModelResult = {
  answer: string; usage: { input_tokens: number | null; output_tokens: number | null; total_tokens: number | null };
  finish: 'completed' | 'incomplete' | 'refused' | 'blocked' | 'length' | 'tool_requested';
}
export type PluginModelPreparationInput = {
  plugin_id: string; tool: string; expected_revision: number; scope_id: string; request_id: string;
  expected_submission_sha: string; max_input_tokens: number; user_confirmed: true;
}
const TOKEN = /^[A-Za-z0-9_-]{32,128}$/
const ID = /^[A-Za-z0-9_-]{16,128}$/
const SHA = /^[a-f0-9]{64}$/
const SDK_PROFILE = /^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$/
const SDK_ROUTE = /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$/
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const digest = (value: unknown): value is string => typeof value === 'string' && SHA.test(value)
const identifier = (value: unknown): value is string => typeof value === 'string' && ID.test(value)
const integer = (value: unknown, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum && value <= maximum
const expires = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= 60
const exact = (value: Record<string, unknown>, keys: readonly string[]) => Object.keys(value).length === keys.length && keys.every(key => Object.prototype.hasOwnProperty.call(value, key))
const publicPreviewKeys = ['schema', 'text_sha256', 'text_characters', 'attachments', 'attachment_count', 'part_count', 'processed_text_sha256', 'max_output_tokens', 'timeout_seconds', 'capability_source', 'limits', 'loss_notes', 'text', 'processed_text_parts', 'provider_id', 'protocol', 'model', 'profile_id', 'profile_revision', 'route_fingerprint', 'capability_fingerprint', 'payload_sha256', 'wire_bytes', 'submission_fingerprint', 'ready_for_verified_adapter', 'verification_note', 'estimated_text_tokens', 'media_token_estimate', 'token_counts_are_estimates', 'requires_separate_explicit_approval', 'execution_grant_minted', 'automatic_history_persona_memory_tools', 'endpoint', 'max_response_bytes', 'plain_http'] as const
function fail(): never { throw new Error('插件模型响应不符合真实宿主合同；没有批准、发送、续期或自动重试') }
export function pluginModelError(error: unknown): string {
  if (error instanceof ApiError && object(error.payload) && object(error.payload.detail)) {
    const detail = error.payload.detail
    if (typeof detail.code === 'string' && /^model_[a-z0-9_]+$/.test(detail.code)) {
      const labels: Record<string, string> = {
        model_pending_unavailable: '本次 pending 已结束、过期或不属于当前认证用户。未发送，不自动续期。',
        model_runtime_invocation_changed_or_expired: '实际插件调用已经结束或超过原始截止时间。未延长普通调用 10 秒 / MCP 30 秒预算。',
        model_plugin_requires_verified_provider: '该路由尚无 verified-provider 证据，且没有真实宿主配置尝试能力；不伪造验证。',
        model_host_request_export_not_integrated: '宿主未显式接入 model-host 请求导出能力。默认 None，不能使用浏览器编译数据替代。',
        model_host_source_listing_not_integrated: '宿主没有接入同一真实 model-host 的未发送请求列表；不能伪造来源。',
        model_runtime_broker_pending_not_integrated: '宿主尚未接入真实 runtime broker pending 服务。未创建替代进程或假批准。',
        model_prepared_materials_required: '真实预编译资料、verified-provider 或宿主持有的尝试能力不可用，未发送。',
        model_host_unsent_request_required: '请选同一认证用户持有、尚未预览发送或执行的 model-host request。',
        model_unverified_capabilities_confirmation_required: '必须独立确认模型能力未实测及未知媒体 token 预算风险；普通发送勾选不能替代。',
        model_host_attempt_capability_revoked: '真实宿主来源或运行时尝试能力已撤销；没有续期或重试。',
        model_prepared_descriptor_changed: '资料、完整路由参数或输入 token 预算已变化/超限；不能伪估算未知媒体 token。',
      }
      return labels[detail.code] || `${typeof detail.message === 'string' ? detail.message : '插件模型操作未完成'}（${detail.code}）`
    }
  }
  return error instanceof Error ? error.message : '插件模型操作未确认；没有自动重试'
}

/** Call only with the actual result of an explicitly approved plugin invocation, not Agent intent text. */
export function pluginModelPendingRequestId(result: unknown): string | undefined {
  if (!object(result)) return
  if (identifier(result.pending_request_id)) return result.pending_request_id
  // The current ordinary SDK adapter still names the pending handle request_id.
  if (identifier(result.request_id) && result.approval_required === true && result.sent === false && digest(result.submission_fingerprint)) return result.request_id
}
function metadata(value: unknown, requestId: string): PluginModelPendingMetadata {
  if (!object(value) || !exact(value, ['request_id', 'submission_sha256', 'submission_fingerprint', 'approval_required', 'sent', 'expires_in', 'original_runtime_deadline'])
    || value.request_id !== requestId || !digest(value.submission_sha256) || !digest(value.submission_fingerprint)
    || value.approval_required !== true || value.sent !== false || !expires(value.expires_in) || value.original_runtime_deadline !== true) fail()
  return value as PluginModelPendingMetadata
}
function preview(value: unknown, pending: PluginModelPendingMetadata): PluginModelPreview {
  if (!object(value) || value.request_id !== pending.request_id || !object(value.preview) || !exact(value.preview, publicPreviewKeys) || value.sent !== false
    || !expires(value.expires_in) || value.original_runtime_deadline !== true || typeof value.host_configured_attempt_supported !== 'boolean'
    || typeof value.capability_notice !== 'string' || value.capability_notice.length > 2000 || value.preview.payload_sha256 !== pending.submission_sha256
    || value.preview.submission_fingerprint !== pending.submission_fingerprint || typeof value.requires_unverified_capabilities_acceptance !== 'boolean'
    || value.unverified_capabilities_confirmation_field !== 'accept_unverified_capabilities' || value.final_send_requires_separate_ui_approval !== true
    || typeof value.preview.ready_for_verified_adapter !== 'boolean' || value.requires_unverified_capabilities_acceptance !== !value.preview.ready_for_verified_adapter
    || value.preview.capability_source !== (value.preview.ready_for_verified_adapter ? 'verified_provider' : 'host_configured')
    || !integer(value.preview.estimated_text_tokens) || value.preview.media_token_estimate !== null && !integer(value.preview.media_token_estimate)
    || value.preview.token_counts_are_estimates !== true || value.preview.requires_separate_explicit_approval !== true
    || value.preview.execution_grant_minted !== false || value.preview.automatic_history_persona_memory_tools !== false) fail()
  const keys = ['request_id', 'preview', 'token', 'fingerprint', 'approval_required', 'sent', 'expires_in', 'original_runtime_deadline', 'host_configured_attempt_supported', 'capability_notice', 'requires_unverified_capabilities_acceptance', 'unverified_capabilities_confirmation_field', 'final_send_requires_separate_ui_approval']
  if (value.token === null) {
    if (value.fingerprint !== null || value.approval_required !== false || typeof value.blocked !== 'string' || !/^model_[a-z0-9_]+$/.test(value.blocked)) fail()
    keys.push('blocked')
  } else if (typeof value.token !== 'string' || !TOKEN.test(value.token) || !digest(value.fingerprint) || value.approval_required !== true || 'blocked' in value
    || value.requires_unverified_capabilities_acceptance && !value.host_configured_attempt_supported) fail()
  if (!exact(value, keys) || new TextEncoder().encode(JSON.stringify(value)).length > 8 * 1024 * 1024) fail()
  return value as PluginModelPreview
}
function prepared(value: unknown, input: PluginModelPreparationInput): PluginModelPrepared {
  if (!object(value) || !exact(value, ['plugin_id', 'tool', 'revision', 'params', 'submission_sha256', 'submission_fingerprint', 'expires_in', 'prepared', 'sent', 'approval_required', 'final_send_requires_separate_ui_approval'])
    || value.plugin_id !== input.plugin_id || value.tool !== input.tool || value.revision !== input.expected_revision
    || value.submission_sha256 !== input.expected_submission_sha || !digest(value.submission_fingerprint)
    || !expires(value.expires_in) || value.prepared !== true || value.sent !== false || value.approval_required !== true || value.final_send_requires_separate_ui_approval !== true
    || !object(value.params) || !exact(value.params, ['profile_id', 'provider', 'model', 'prompt_id', 'prompt_sha256', 'max_input_tokens', 'max_output_tokens'])
    || typeof value.params.profile_id !== 'string' || !SDK_PROFILE.test(value.params.profile_id)
    || typeof value.params.provider !== 'string' || !SDK_ROUTE.test(value.params.provider)
    || typeof value.params.model !== 'string' || !SDK_ROUTE.test(value.params.model)
    || typeof value.params.prompt_id !== 'string' || !/^[a-f0-9]{32}$/.test(value.params.prompt_id) || !digest(value.params.prompt_sha256)
    || value.params.max_input_tokens !== input.max_input_tokens || !integer(value.params.max_output_tokens, 1, 32768)) fail()
  return value as PluginModelPrepared
}
function sources(value: unknown): PluginModelSource[] {
  if (!object(value) || !exact(value, ['items', 'server_held_model_host_requests_only', 'automatic_retry', 'capability_notice'])
    || !Array.isArray(value.items) || value.items.length > 64 || value.server_held_model_host_requests_only !== true || value.automatic_retry !== false || typeof value.capability_notice !== 'string') fail()
  const seen = new Set<string>()
  for (const item of value.items) {
    if (!object(item) || !exact(item, ['scope_id', 'request_id', 'submission_sha256', 'submission_fingerprint', 'profile_id', 'provider', 'model', 'expires_in', 'ready_for_verified_adapter', 'estimated_text_tokens', 'media_token_estimate', 'sent', 'send_approval_created'])
      || typeof item.scope_id !== 'string' || !TOKEN.test(item.scope_id) || typeof item.request_id !== 'string' || !TOKEN.test(item.request_id)
      || !digest(item.submission_sha256) || !digest(item.submission_fingerprint) || !expires(item.expires_in)
      || typeof item.profile_id !== 'string' || typeof item.provider !== 'string' || typeof item.model !== 'string'
      || typeof item.ready_for_verified_adapter !== 'boolean' || !integer(item.estimated_text_tokens) || item.media_token_estimate !== null && !integer(item.media_token_estimate)
      || item.sent !== false || item.send_approval_created !== false || seen.has(item.request_id)) fail()
    seen.add(item.request_id)
  }
  return value.items as PluginModelSource[]
}
export function createPluginModelPendingClient({ apiBase = '/v11/plugin-model-proxy' }: { apiBase?: string } = {}) {
  const base = apiBase.replace(/\/+$/, '')
  if (!base.startsWith('/') || base.startsWith('//') || /[?#]/.test(base)) throw new Error('插件模型 API 必须为同源路径')
  const consumed = new Set<string>()
  const sourceClient = createModelMaterialSendClient()
  const ownedScopes = new Set<string>()
  return {
    async pending(requestId: string, signal?: AbortSignal) {
      if (!identifier(requestId)) fail()
      return metadata(await api<unknown>(`${base}/pending/${encodeURIComponent(requestId)}`, { signal }), requestId)
    },
    async preview(pending: PluginModelPendingMetadata, signal?: AbortSignal) {
      return preview(await api<unknown>(`${base}/preview`, { ...json('POST', { request_id: pending.request_id, expected_submission_sha: pending.submission_sha256 }), signal }), pending)
    },
    async execute(proposed: PluginModelPreview, signal?: AbortSignal, acceptUnverifiedCapabilities = false): Promise<PluginModelResult> {
      if (typeof acceptUnverifiedCapabilities !== 'boolean' || proposed.requires_unverified_capabilities_acceptance && acceptUnverifiedCapabilities !== true) throw new Error('未独立确认未实测能力和未知媒体 token 预算；没有发送')
      if (!proposed.token || !proposed.fingerprint || !proposed.approval_required || consumed.has(proposed.token)) throw new Error('本次发送批准不可用或已经尝试使用；不自动重试')
      consumed.add(proposed.token)
      const result = await api<unknown>(`${base}/execute`, { ...json('POST', { request_id: proposed.request_id, token: proposed.token, fingerprint: proposed.fingerprint, user_confirmed: true, accept_unverified_capabilities: acceptUnverifiedCapabilities === true }), signal })
      if (!object(result) || !exact(result, ['answer', 'usage', 'finish']) || typeof result.answer !== 'string' || result.answer.includes('\0') || new TextEncoder().encode(result.answer).length > 1024 * 1024
        || !object(result.usage) || !exact(result.usage, ['input_tokens', 'output_tokens', 'total_tokens']) || !Object.values(result.usage).every(item => item === null || integer(item))
        || typeof result.finish !== 'string' || !['completed', 'incomplete', 'refused', 'blocked', 'length', 'tool_requested'].includes(result.finish)) fail()
      return result as PluginModelResult
    },
    async cancel(requestId: string, proposed?: PluginModelPreview) {
      if (!identifier(requestId) || proposed && proposed.request_id !== requestId) fail()
      const body = proposed?.token && proposed.fingerprint ? { request_id: requestId, token: proposed.token, fingerprint: proposed.fingerprint } : { request_id: requestId }
      if (proposed?.token) consumed.add(proposed.token)
      const result = await api<unknown>(`${base}/cancel`, json('POST', body))
      if (!object(result) || !exact(result, ['cancel_requested', 'active_execution', 'sent_undo_guaranteed', 'automatic_retry', 'request_id', ...(proposed?.token ? ['notice'] : [])])
        || result.request_id !== requestId || result.cancel_requested !== true || typeof result.active_execution !== 'boolean' || result.sent_undo_guaranteed !== false || result.automatic_retry !== false
        || proposed?.token && typeof result.notice !== 'string') fail()
      return result
    },
    async prepare(input: PluginModelPreparationInput) {
      if (!exact(input, ['plugin_id', 'tool', 'expected_revision', 'scope_id', 'request_id', 'expected_submission_sha', 'max_input_tokens', 'user_confirmed'])
        || !TOKEN.test(input.scope_id) || !TOKEN.test(input.request_id) || !SHA.test(input.expected_submission_sha) || !integer(input.max_input_tokens, 1, 1000000) || input.user_confirmed !== true) fail()
      return prepared(await api<unknown>(`${base}/prepared/prepare`, json('POST', input)), input)
    },
    async revokePrepared(promptId: string) {
      if (!/^[a-f0-9]{32}$/.test(promptId)) fail()
      const value = await api<unknown>(`${base}/prepared/revoke`, json('POST', { prompt_id: promptId }))
      if (!object(value) || !exact(value, ['revoked', 'sent_undo_guaranteed', 'automatic_retry']) || value.revoked !== true || value.sent_undo_guaranteed !== false || value.automatic_retry !== false) fail()
    },
    async sources(signal?: AbortSignal) {
      return sources(await api<unknown>(`${base}/prepared/sources`, { signal }))
    },
    async sourceRoutes(signal?: AbortSignal) { return sourceClient.routes(signal) },
    async prepareSource(input: { sessionId: number; route: AvailableModelHostRoute; text: string; maxOutputTokens: number; effort: ModelHostEffort | null }, signal?: AbortSignal): Promise<PreparedModelSubmission> {
      if (!SDK_PROFILE.test(input.route.profile_id) || !SDK_ROUTE.test(input.route.provider_id) || !SDK_ROUTE.test(input.route.model) || !integer(input.maxOutputTokens, 1, 32768)) fail()
      const scopeId = await sourceClient.createScope(input.sessionId, signal)
      ownedScopes.add(scopeId)
      try {
        // Actual model-host compiles; deliberately NEVER previewSubmission (which consumes its source token).
        return await sourceClient.prepare(scopeId, { route: input.route, text: input.text, materials: [], max_output_tokens: input.maxOutputTokens, effort: input.effort }, signal)
      } catch (error) {
        ownedScopes.delete(scopeId)
        await sourceClient.revokeScope(scopeId).catch(() => undefined)
        throw error
      }
    },
    async revokeSource(scopeId: string) {
      if (!ownedScopes.has(scopeId)) fail()
      await sourceClient.revokeScope(scopeId)
      ownedScopes.delete(scopeId)
    },
  }
}
export type PluginModelPendingClient = ReturnType<typeof createPluginModelPendingClient>
