import { api, apiBlob, ApiError, json } from '@/api/client'
import { documentSelection } from '@/lib/modelDocuments'
import type { DocumentSelection } from '@/lib/modelDocuments'
import { validateVideoPreview, validateVideoProbe, videoSelection, videoSource, VIDEO_LIMITS } from '@/lib/modelVideo'
import type { VideoConversionPlan, VideoConversionPreview, VideoProbe, VideoSelection, VideoSource } from '@/lib/modelVideo'

/** Host-chat scopes are host minted. No browser PID, BrokerSession, Key or provider payload is accepted. */
export const MODEL_MATERIAL_LIMITS = {
  originalBytes: 15 * 1024 * 1024, originals: 8, frames: 16, characters: 100000,
  parts: 5000, wireBytes: 192 * 1024 * 1024, responseBytes: 8 * 1024 * 1024,
} as const
export type ModelMaterialAttachment = { fid: string; name: string; mime: string; size: number; sha256?: string }
export type MaterialSource = { fid: string; session_id: number; filename: string; mime: string; size: number; sha256: string; original_version: 1; identity_fingerprint: string; identity_semantics: 'immutable_media_object_not_version_history' }
export type MaterialDelivery = 'native' | 'extracted_text' | 'video_frame'
export type ModelHostProtocol = 'openai_chat' | 'openai_responses' | 'anthropic_messages' | 'gemini_content'
export type ModelHostEffort = 'none' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh'
export type ModelMaterialSelection = DocumentSelection
export type MaterialReference = { material_id: string; fingerprint: string }
export class ModelMaterialSendError extends Error {
  constructor(message: string, public readonly outcomeUnknown = false) { super(message); this.name = 'ModelMaterialSendError' }
}
const SHA = /^[0-9a-f]{64}$/
const ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/
const FID = /^[0-9a-f]{32}$/
const TOKEN = /^[A-Za-z0-9_-]{32,128}$/
const protocols: readonly string[] = ['openai_chat', 'openai_responses', 'anthropic_messages', 'gemini_content']
const efforts: readonly string[] = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh']
const kinds: readonly string[] = ['text', 'image', 'video', 'document']
const deliveries: readonly string[] = ['native', 'extracted_text', 'video_frame']
const kindMimes: Record<string, readonly string[]> = {
  text: ['text/plain', 'text/markdown'], image: ['image/png', 'image/jpeg', 'image/webp', 'image/gif'],
  video: ['video/mp4', 'video/webm', 'video/quicktime'], document: ['application/pdf', 'text/plain', 'text/markdown', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'],
}
function fail(message = '宿主响应不符合精确发送合同，已拒绝采用；没有自动发送或重试'): never { throw new ModelMaterialSendError(message) }
const object = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v)
const integer = (v: unknown, min = 0, max = Number.MAX_SAFE_INTEGER): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= min && v <= max
const digest = (v: unknown): v is string => typeof v === 'string' && SHA.test(v)
const identifier = (v: unknown): v is string => typeof v === 'string' && ID.test(v)
const line = (v: unknown, max = 4000): v is string => typeof v === 'string' && v.length > 0 && v.length <= max && !/[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/.test(v)
const text = (v: unknown, max: number = MODEL_MATERIAL_LIMITS.characters): v is string => typeof v === 'string' && Array.from(v).length <= max && !v.includes('\0') && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(v)
const exact = (v: Record<string, unknown>, keys: readonly string[]) => Object.keys(v).length === keys.length && Object.keys(v).every(k => keys.includes(k))
const list = (v: unknown, max = 100, itemMax = 1000): v is string[] => Array.isArray(v) && v.length <= max && v.every(x => line(x, itemMax))
const canonical = (v: unknown): string => Array.isArray(v) ? `[${v.map(canonical).join(',')}]` : object(v) ? `{${Object.keys(v).sort().map(k => `${JSON.stringify(k)}:${canonical(v[k])}`).join(',')}}` : JSON.stringify(v) ?? 'null'
function finite(value: unknown) {
  const stack: [unknown, number][] = [[value, 0]]; let nodes = 0
  while (stack.length) {
    const [v, depth] = stack.pop()!
    if (++nodes > 300000 || depth > 24) fail('宿主响应结构超限；拒绝，不截断')
    if (Array.isArray(v)) for (const item of v) stack.push([item, depth + 1])
    else if (object(v)) for (const [key, item] of Object.entries(v)) {
      if (['__proto__', 'prototype', 'constructor'].includes(key)) fail()
      stack.push([item, depth + 1])
    }
    else if (v !== null && !(typeof v === 'string' || typeof v === 'boolean' || typeof v === 'number' && Number.isFinite(v))) fail()
  }
  if (new TextEncoder().encode(JSON.stringify(value)).byteLength > MODEL_MATERIAL_LIMITS.responseBytes) fail('宿主响应超过 8 MiB；拒绝，不截断')
}
export function validateModelMaterialAttachments(sessionId: number, values: readonly ModelMaterialAttachment[]): ModelMaterialAttachment[] {
  if (!integer(sessionId, 1)) fail('需要真实的当前会话；不会创建虚构会话或 PID')
  if (!Array.isArray(values) || values.length > MODEL_MATERIAL_LIMITS.originals) fail('一次最多 8 个原件；超限拒绝，不截断')
  const seen = new Set<string>()
  return values.map(v => {
    if (!object(v) || typeof v.fid !== 'string' || !FID.test(v.fid) || seen.has(v.fid) || !line(v.name, 200) || /[\\/:]/.test(v.name) || !line(v.mime, 200) || !integer(v.size, 1, MODEL_MATERIAL_LIMITS.originalBytes) || v.sha256 !== undefined && !digest(v.sha256)) fail('原件身份、SHA256、名称或大小无效；每个原件须为非空且不超过 15 MiB。可由首次宿主预检取得真实摘要，不能用链接或占位摘要代替')
    seen.add(v.fid)
    return { fid: v.fid, name: v.name, mime: v.mime, size: v.size, sha256: v.sha256 }
  })
}
export function emptyModelMaterialSelection(): ModelMaterialSelection {
  return { paragraphs: [], sheets: [], slides: [], pages: [], include_hidden: false, max_characters: MODEL_MATERIAL_LIMITS.characters }
}
function source(value: unknown, attachment: ModelMaterialAttachment, sessionId: number): MaterialSource {
  if (!object(value) || !exact(value, ['fid', 'session_id', 'filename', 'mime', 'size', 'sha256', 'original_version', 'identity_fingerprint', 'identity_semantics']) || !digest(value.identity_fingerprint) || value.identity_semantics !== 'immutable_media_object_not_version_history' || value.fid !== attachment.fid || value.session_id !== sessionId || value.filename !== attachment.name || value.mime !== attachment.mime || value.size !== attachment.size || !digest(value.sha256) || attachment.sha256 !== undefined && value.sha256 !== attachment.sha256 || value.original_version !== 1) fail('宿主原件身份、会话归属、不可变版本或 SHA256 与当前附件不一致')
  return value as MaterialSource
}
function endpoint(value: unknown): string {
  if (!line(value, 4000) || value.includes('\\')) fail('发送目标地址无效')
  let url: URL
  try { url = new URL(value) } catch { return fail('发送目标地址无效') }
  if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash) fail('发送地址含不允许的凭据、查询参数或协议')
  return value
}
async function shaText(value: string) {
  if (!globalThis.crypto?.subtle) fail('当前环境无法核验精确文字 SHA256；不能批准发送')
  const bytes = new TextEncoder().encode(value)
  const hash = await crypto.subtle.digest('SHA-256', bytes)
  return { bytes: bytes.byteLength, sha256: Array.from(new Uint8Array(hash), n => n.toString(16).padStart(2, '0')).join('') }
}

// All response objects below are closed DTOs. Unknown fields (especially provider blocks) fail closed.
export type ModelHostRoute = {
  profile_id: string; profile_revision: number; name: string; provider_id: string; model: string;
  protocol: ModelHostProtocol | null; endpoint: string | null; credential_ref: string | null;
  credential_authority: string | null; credential_generation: number | null;
  credential_ready: boolean; available: boolean; diagnostics: string[];
  capability_source: 'host_configured' | 'verified_provider'; supported_kinds: string[];
  native_mimes: string[]; supported_efforts: ModelHostEffort[]; max_output_tokens: number; timeout_seconds: number;
}
export type AvailableModelHostRoute = ModelHostRoute & {
  available: true; credential_ready: true; protocol: ModelHostProtocol; endpoint: string;
  credential_ref: string; credential_authority: string; credential_generation: number;
}
export const isAvailableModelHostRoute = (value: ModelHostRoute): value is AvailableModelHostRoute => value.available === true
const routeKeys = ['profile_id', 'profile_revision', 'name', 'provider_id', 'model', 'protocol', 'endpoint', 'credential_ref', 'credential_authority', 'credential_generation', 'credential_ready', 'available', 'diagnostics', 'capability_source', 'supported_kinds', 'native_mimes', 'supported_efforts', 'max_output_tokens', 'timeout_seconds'] as const
function route(value: unknown): ModelHostRoute {
  if (!object(value) || !exact(value, routeKeys) || !identifier(value.profile_id) || !integer(value.profile_revision, 1) || !line(value.name, 200) || !identifier(value.provider_id) || !text(value.model, 200) || typeof value.available !== 'boolean' || typeof value.credential_ready !== 'boolean' || !list(value.diagnostics, 20, 120) || value.diagnostics.some(v => !/^[a-z0-9_]+$/.test(v)) || !['host_configured', 'verified_provider'].includes(String(value.capability_source)) || !list(value.supported_kinds, 4, 20) || value.supported_kinds.some(v => !kinds.includes(v)) || !list(value.native_mimes, 30, 200) || !list(value.supported_efforts, 6, 20) || value.supported_efforts.some(v => !efforts.includes(v)) || !integer(value.max_output_tokens, 1, 65536) || typeof value.timeout_seconds !== 'number' || !Number.isFinite(value.timeout_seconds) || value.timeout_seconds <= 0 || value.timeout_seconds > 300) fail('模型路由字段或能力声明无效；不会选择默认路由或降级')
  if (value.available) {
    if (!value.credential_ready || value.diagnostics.length || typeof value.protocol !== 'string' || !protocols.includes(value.protocol) || !line(value.model, 200) || !line(value.credential_ref, 200) || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/.test(value.credential_ref) || /^(?:sk-|AIza|Bearer)/.test(value.credential_ref) || !digest(value.credential_authority) || !integer(value.credential_generation, 1) || !value.supported_kinds.includes('text') || !['openai_chat', 'openai_responses'].includes(value.protocol) && value.supported_efforts.length) fail('可用模型必须有真实安全凭据引用、整数代际和已实现的档位映射')
    endpoint(value.endpoint)
    const supportedKinds=value.supported_kinds as string[]
    if (supportedKinds.includes('video') && value.protocol !== 'gemini_content' || value.native_mimes.some(mime => mime.startsWith('video/') && (value.protocol !== 'gemini_content' || !supportedKinds.includes('video') || !['video/mp4', 'video/webm'].includes(mime)))) fail('原生视频仅接受明确配置的Gemini MP4/WebM映射；其他协议不会伪造支持或默换静态帧')
  } else if (value.credential_ready || !value.diagnostics.length || value.protocol !== null || value.endpoint !== null || value.credential_ref !== null || value.credential_authority !== null || value.credential_generation !== null || value.supported_kinds.length || value.native_mimes.length || value.supported_efforts.length) fail('不可用路由必须保留诊断且不能伪造可发送字段')
  for (const key of ['supported_kinds', 'native_mimes', 'supported_efforts'] as const) {
    const entries=value[key] as string[]
    if (new Set(entries).size !== entries.length) fail()
  }
  return value as ModelHostRoute
}
export type AttachmentInspectionReport = {
  byte_length: number; structural_validation: string; ready_for_extraction: boolean;
  converter_required: boolean; native_provider_support_verified: false; text_characters: number | null;
  archive_entries: number; uncompressed_bytes: number; xml_nodes: number; sheets: number; cells: number;
  formulas: number; slides: number; loss_notes: string[]; unsupported_features: string[];
  limits: Record<string, number>; context_notes: string[];
} & ({
  kind: 'pdf'; pages: number; pdf_objects: number; pdf_nodes: number; pdf_decoded_work_bytes: number;
  pdf_image_objects: number; parser_version: string; delivery: 'extracted_text' | 'unavailable'; diagnostic: string | null;
} | { kind: 'mp4' | 'webm'; delivery: 'native' | 'unavailable'; diagnostic: string | null; video_metadata: VideoProbe | null }
  | { kind: 'txt' | 'md' | 'docx' | 'xlsx' | 'pptx' | 'png' | 'jpeg' | 'webp' })
const pdfInspectionKeys = ['pages', 'pdf_objects', 'pdf_nodes', 'pdf_decoded_work_bytes', 'pdf_image_objects', 'parser_version', 'delivery', 'diagnostic'] as const
const pdfInspectionLimits = {
  max_pdf_pages: 200, max_pdf_objects: 20000, max_pdf_nodes: 100000, max_pdf_depth: 64,
  max_pdf_stream_bytes: 4 * 1024 * 1024, max_pdf_decoded_work_bytes: 32 * 1024 * 1024,
  max_pdf_characters: 100000, max_pdf_memory_bytes: 384 * 1024 * 1024, max_pdf_user_cpu_seconds: 6,
  max_pdf_processes: 2, max_pdf_wall_seconds: 10, max_pdf_output_bytes: 1024 * 1024,
} as const
export type MaterialInspection = {
  scope_id: string; source: MaterialSource; inspection: AttachmentInspectionReport; inspection_fingerprint: string;
  deliveries: { delivery: MaterialDelivery; available: boolean; code: string | null }[]; model_sent: false;
}
function inspectionReport(value: unknown, original: MaterialSource): AttachmentInspectionReport {
  const keys = ['kind', 'byte_length', 'structural_validation', 'ready_for_extraction', 'converter_required', 'native_provider_support_verified', 'text_characters', 'archive_entries', 'uncompressed_bytes', 'xml_nodes', 'sheets', 'cells', 'formulas', 'slides', 'loss_notes', 'unsupported_features', 'limits', 'context_notes']
  const limitKeys = ['max_bytes', 'max_entries', 'max_entry_bytes', 'max_uncompressed_bytes', 'max_ratio', 'max_xml_bytes', 'max_xml_nodes', 'max_xml_depth', 'max_sheets', 'max_cells', 'max_slides', 'max_text_characters']
  const isPdf = object(value) && value.kind === 'pdf'
  const isVideo = object(value) && (value.kind === 'mp4' || value.kind === 'webm')
  if (isPdf) { keys.push(...pdfInspectionKeys); limitKeys.push(...Object.keys(pdfInspectionLimits)) }
  if (isVideo) keys.push('delivery', 'diagnostic', 'video_metadata')
  if (!object(value) || !exact(value, keys) || !line(value.kind, 20) || !['txt', 'md', 'docx', 'xlsx', 'pptx', 'pdf', 'png', 'jpeg', 'webp', 'mp4', 'webm'].includes(value.kind) || value.byte_length !== original.size || !line(value.structural_validation, 100) || typeof value.ready_for_extraction !== 'boolean' || typeof value.converter_required !== 'boolean' || value.native_provider_support_verified !== false || value.text_characters !== null && !integer(value.text_characters, 0, MODEL_MATERIAL_LIMITS.characters) || !['archive_entries', 'uncompressed_bytes', 'xml_nodes', 'sheets', 'cells', 'formulas', 'slides'].every(k => integer(value[k])) || !list(value.loss_notes, 100, 1000) || !list(value.unsupported_features, 100, 1000) || !list(value.context_notes, 100, 1000) || !object(value.limits) || !exact(value.limits, limitKeys) || Object.values(value.limits).some(v => !integer(v, 1)) || value.limits.max_bytes !== MODEL_MATERIAL_LIMITS.originalBytes || value.limits.max_text_characters !== MODEL_MATERIAL_LIMITS.characters) fail('原件结构预检字段、限额或能力状态无效')
  if (isPdf) {
    if (original.mime !== 'application/pdf' || !integer(value.pages, 0, pdfInspectionLimits.max_pdf_pages) || !integer(value.pdf_objects, 0, pdfInspectionLimits.max_pdf_objects) || !integer(value.pdf_nodes, 0, pdfInspectionLimits.max_pdf_nodes) || !integer(value.pdf_decoded_work_bytes, 0, pdfInspectionLimits.max_pdf_decoded_work_bytes) || !integer(value.pdf_image_objects, 0, pdfInspectionLimits.max_pdf_objects) || !line(value.parser_version, 100) || value.converter_required !== true || value.text_characters !== null || !['extracted_text', 'unavailable'].includes(String(value.delivery)) || Object.entries(pdfInspectionLimits).some(([key, expected]) => (value.limits as Record<string, number>)[key] !== expected)) fail('PDF物理页数、真实解析版本或资源硬限无效')
    if (value.delivery === 'extracted_text') {
      if (!value.ready_for_extraction || value.pages < 1 || value.diagnostic !== null || value.structural_validation !== 'bounded_host_job_pdf_metadata_validated') fail('PDF没有通过真实只读结构预检，禁止提取或原件直传')
    } else if (value.ready_for_extraction || value.pages !== 0 || value.pdf_objects !== 0 || value.pdf_nodes !== 0 || value.pdf_decoded_work_bytes !== 0 || value.pdf_image_objects !== 0 || value.structural_validation !== 'bounded_pdf_inspection_failed' || !line(value.diagnostic, 120) || !/^pdf_[a-z0-9_]+$/.test(value.diagnostic)) fail('不可用PDF必须保留真实失败诊断，不能伪造页数或转换能力')
  }
  if (isVideo) {
    const originalVideo = videoSource(original as VideoSource)
    if (value.kind !== (originalVideo.mime === 'video/mp4' ? 'mp4' : 'webm') || value.text_characters !== null) fail('视频类型与真实原件不符')
    if (value.delivery === 'native') {
      if (value.ready_for_extraction !== true || value.converter_required !== false || value.diagnostic !== null || value.structural_validation !== 'bounded_ffprobe_container_streams_validated') fail('视频没有通过真实容器探测')
      validateVideoProbe(value.video_metadata, originalVideo)
    } else if (value.delivery !== 'unavailable' || value.ready_for_extraction !== false || value.converter_required !== true || value.video_metadata !== null || value.structural_validation !== 'bounded_video_probe_failed' || !line(value.diagnostic, 120) || !/^[a-z0-9_]+$/.test(value.diagnostic)) fail('不可用视频必须保留失败诊断，不伪造探测能力')
  }
  return value as AttachmentInspectionReport
}
export type MaterialPart = {
  opaque_id: string; original_sha256: string; original_version: 1; original_kind: string;
  original_mime: string; original_size: number; kind: string; mime: string;
  part_sha256: string; part_size: number; delivery: MaterialDelivery; locator: Record<string, string>;
  conversion_method: string; conversion_version: string; conversion_settings_sha256: string;
  loss_notes: string[]; inspection_fingerprint: string;
}
export type NativeVideoPlan = {
  original_id: string; original_version: number; original_sha256: string; byte_length: number;
  inspection: VideoProbe; request_original_count: number; request_total_video_frames: number;
  delivery: 'native'; loss_notes: string[]; converter: 'none'; approval_required: true;
  sending_approved: false; native_provider_support_verified: false;
}
export type MaterialPreview = {
  scope_id: string; token: string; fingerprint: string; expires_in: number;
  source: MaterialSource; delivery: MaterialDelivery; selection: ModelMaterialSelection;
  converter: string; loss_notes: string[]; conversion_performed: false;
  approval_required: true; model_sent: false; sending_requires_separate_approval: true;
  client_expires_at: number;
  /** Present only for video; document selection always remains its default DTO. */
  video_selection?: VideoSelection | null; plan?: VideoConversionPlan | NativeVideoPlan;
}
export type ApprovedModelMaterial = MaterialReference & {
  scope_id: string; source: MaterialSource; delivery: MaterialDelivery; selection: ModelMaterialSelection;
  parts: MaterialPart[]; processed_text_parts: string[]; loss_notes: string[];
  content_bytes: number; characters: number; frame_count: number;
  model_sent: false; sending_approved: false; sending_requires_separate_approval: true;
  video_selection?: VideoSelection | null;
  frames?: { frame: number; timestamp_ms: number; sha256: string; mime: 'image/png'; bytes: number }[];
  conversion_performed?: boolean;
}
export type PreparedModelSubmission = { scope_id: string; request_id: string; submission_sha256: string; submission_fingerprint: string; scope_revision: number; sent: false; approval_required: true }
/** Actual same-owner host request, read back from the plugin source registry; never browser-built compiled content. */
export type ModelMaterialPluginSource = {
  scope_id: string; request_id: string; submission_sha256: string; submission_fingerprint: string;
  profile_id: string; provider: string; model: string; expires_in: number;
  ready_for_verified_adapter: boolean; estimated_text_tokens: number; media_token_estimate: number | null;
  sent: false; send_approval_created: false;
}
export type PreparedModelMaterialSource = ModelMaterialPluginSource & { client_expires_at: number }
/** Explicit transfer of this exact actual scope/request to bounded server ownership, not a send grant. */
export type ModelMaterialSourceHandoff = {
  scope_id: string; request_id: string; submission_sha256: string; expires_in: number;
  source_kind: 'host_model_request'; handoff_complete: true; sent: false; send_approval_created: false;
  runtime_deadline_extended: false;
}
const pluginSourceKeys = ['scope_id', 'request_id', 'submission_sha256', 'submission_fingerprint', 'profile_id', 'provider', 'model', 'expires_in', 'ready_for_verified_adapter', 'estimated_text_tokens', 'media_token_estimate', 'sent', 'send_approval_created'] as const
function pluginSource(value: unknown): ModelMaterialPluginSource {
  if (!object(value) || !exact(value, pluginSourceKeys) || typeof value.scope_id !== 'string' || !TOKEN.test(value.scope_id) || typeof value.request_id !== 'string' || !TOKEN.test(value.request_id) || !digest(value.submission_sha256) || !digest(value.submission_fingerprint) || !identifier(value.profile_id) || !identifier(value.provider) || !line(value.model, 200) || typeof value.expires_in !== 'number' || !Number.isFinite(value.expires_in) || value.expires_in <= 0 || value.expires_in > 60 || typeof value.ready_for_verified_adapter !== 'boolean' || !integer(value.estimated_text_tokens) || value.media_token_estimate !== null && !integer(value.media_token_estimate) || value.sent !== false || value.send_approval_created !== false) fail('插件来源不是有效的真实未预览 host request；没有生成发送批准')
  return value as ModelMaterialPluginSource
}
export type CompiledModelPreview = {
  schema: 'lingxi.model-submission.v1'; text_sha256: string; text_characters: number;
  attachments: MaterialPart[]; attachment_count: number; part_count: number; processed_text_sha256: string[];
  max_output_tokens: number; timeout_seconds: number; capability_source: 'host_configured' | 'verified_provider';
  limits: { max_original_bytes: number; max_attachments: number; max_video_frames: number; max_document_chars: number; max_wire_bytes: number };
  loss_notes: string[]; text: string; processed_text_parts: string[]; provider_id: string; protocol: ModelHostProtocol;
  model: string; profile_id: string; profile_revision: number; route_fingerprint: string; capability_fingerprint: string;
  payload_sha256: string; wire_bytes: number; submission_fingerprint: string; ready_for_verified_adapter: boolean;
  verification_note: string; estimated_text_tokens: number; media_token_estimate: number | null;
  token_counts_are_estimates: true; requires_separate_explicit_approval: true; execution_grant_minted: false;
  automatic_history_persona_memory_tools: false; endpoint: string; max_response_bytes: number; plain_http: boolean;
}
export type FinalModelSubmissionPreview = {
  scope_id: string; request_id: string; submission_sha256: string; token: string; fingerprint: string; expires_in: number;
  route: AvailableModelHostRoute; effort: ModelHostEffort | null; materials: MaterialReference[]; preview: CompiledModelPreview;
  plain_http: boolean; requires_unverified_capabilities_acceptance: boolean; capability_notice: string;
  approval_required: true; sent: false; client_expires_at: number;
}
export type ModelMaterialSendResult = {
  scope_id: string; request_id: string; submission_sha256: string; answer: string;
  usage: { input_tokens: number | null; output_tokens: number | null; total_tokens: number | null };
  finish: 'completed' | 'length' | 'tool_requested' | 'blocked' | 'refused' | 'incomplete';
}
function references(value: unknown): MaterialReference[] {
  if (!Array.isArray(value) || value.length > MODEL_MATERIAL_LIMITS.originals) fail('资料引用超过 8 个原件限制')
  const seen = new Set<string>()
  for (const v of value) {
    if (!object(v) || !exact(v, ['material_id', 'fingerprint']) || !identifier(v.material_id) || !digest(v.fingerprint) || seen.has(v.material_id)) fail('资料引用无效或重复')
    seen.add(v.material_id)
  }
  return value as MaterialReference[]
}
const partKeys = ['opaque_id', 'original_sha256', 'original_version', 'original_kind', 'original_mime', 'original_size', 'kind', 'mime', 'part_sha256', 'part_size', 'delivery', 'locator', 'conversion_method', 'conversion_version', 'conversion_settings_sha256', 'loss_notes', 'inspection_fingerprint'] as const
function part(value: unknown): MaterialPart {
  if (!object(value) || !exact(value, partKeys) || typeof value.opaque_id !== 'string' || !FID.test(value.opaque_id) || !digest(value.original_sha256) || value.original_version !== 1 || typeof value.original_kind !== 'string' || !kinds.includes(value.original_kind) || !line(value.original_mime, 200) || !integer(value.original_size, 1, MODEL_MATERIAL_LIMITS.originalBytes) || typeof value.kind !== 'string' || !kinds.includes(value.kind) || !line(value.mime, 200) || !digest(value.part_sha256) || !integer(value.part_size, 1, MODEL_MATERIAL_LIMITS.originalBytes) || typeof value.delivery !== 'string' || !deliveries.includes(value.delivery) || !object(value.locator) || Object.keys(value.locator).length > 5 || Object.entries(value.locator).some(([k, v]) => !['page', 'sheet', 'slide', 'timestamp_ms', 'frame', 'document', 'paragraph', 'table', 'row', 'cell'].includes(k) || !line(v, 255)) || !line(value.conversion_method, 100) || !line(value.conversion_version, 100) || !digest(value.conversion_settings_sha256) || !list(value.loss_notes, 20, 500) || !digest(value.inspection_fingerprint)) fail('资料片段来源、字节、定位或损失说明无效')
  if (!kindMimes[value.kind as string].includes(value.mime as string) || !kindMimes[value.original_kind as string].includes(value.original_mime as string)) fail('资料种类与实际媒体类型不匹配')
  for (const [key, v] of Object.entries(value.locator)) {
    if (['page', 'slide', 'paragraph', 'table', 'row'].includes(key) && (!/^[1-9][0-9]*$/.test(v as string) || Number(v) > 1048576) || key === 'document' && v !== 'text' || key === 'cell' && !/^(?:[A-Z]{1,3}[1-9][0-9]{0,6}|[1-9][0-9]{0,4})$/.test(v as string)) fail('资料定位字段无效')
  }
  if (value.original_mime === 'application/pdf' && (value.delivery !== 'extracted_text' || value.original_kind !== 'document' || value.kind !== 'text')) fail('PDF仅允许明确选页后的已有文字层；禁止原件直传或伪造OCR')
  if (value.delivery === 'native' && (value.kind !== value.original_kind || value.mime !== value.original_mime || value.part_sha256 !== value.original_sha256 || value.part_size !== value.original_size || value.conversion_method !== 'none')) fail('原件直传的字节与原件身份不符')
  if (value.delivery === 'extracted_text' && (value.kind !== 'text' || !['text', 'document'].includes(value.original_kind) || value.conversion_method === 'none' || !value.loss_notes.length)) fail('提取文字缺少真实转换或损失说明')
  if (value.delivery === 'video_frame' && (value.kind !== 'image' || value.original_kind !== 'video' || value.conversion_method === 'none' || !value.loss_notes.length || !/^\d+$/.test(String(value.locator.frame)) || !/^\d+$/.test(String(value.locator.timestamp_ms)))) fail('视频帧缺少明确帧序号、时间戳或损失说明')
  return value as MaterialPart
}
function assertSelectedParts(parts: readonly MaterialPart[], preview: MaterialPreview) {
  const kind = preview.source.filename.split('.').pop()?.toLowerCase(), selection = preview.selection
  const cellPosition = (raw: string) => {
    const match = /^([A-Z]{1,3})([1-9][0-9]{0,6})$/.exec(raw)
    if (!match) fail('提取单元格没有有效的明确定位')
    let column = 0
    for (const letter of match[1]) column = column * 26 + letter.charCodeAt(0) - 64
    const row = Number(match[2])
    if (column > 16384 || row > 1048576) fail('提取单元格定位超出工作表边界')
    return [column, row]
  }
  let lastPdfPage = 0
  for (const p of parts) {
    // Native video does not convert bytes. The core's final native receipt has
    // its own exact notes (not the inspection's prose); display both stages and
    // bind final approval to the actual registry manifest, never rewrite it.
    if (!(preview.delivery === 'native' && preview.source.mime.startsWith('video/'))) for (const note of preview.loss_notes) if (!p.loss_notes.includes(note)) fail('资料片段丢失了批准前展示的信息损失说明')
    if (preview.delivery === 'video_frame') {
      const selectedVideo = preview.video_selection, plan = preview.plan as VideoConversionPlan | undefined
      const frame = Number(p.locator.frame)
      if (!selectedVideo || !plan || !exact(p.locator, ['timestamp_ms', 'frame']) || !integer(frame, 1, selectedVideo.frame_count) || p.locator.frame !== String(frame) || p.locator.timestamp_ms !== String(selectedVideo.timestamps_ms[frame - 1]) || p.mime !== 'image/png' || p.conversion_method !== plan.converter || p.conversion_version !== plan.converter || p.conversion_settings_sha256 !== plan.conversion_settings_sha256) fail('实际抽帧的序号、时间点、PNG或转换设置不属于明确批准的计划')
    }
    if (preview.delivery !== 'extracted_text') continue
    if (p.conversion_method !== preview.converter || p.conversion_version !== preview.converter) fail('登记片段的真实转换方法或版本与批准预览不一致')
    const loc = p.locator
    if (kind === 'pdf') {
      const page = Number(loc.page)
      if (!selection.pages.length || selection.pages.some((selected, index) => !integer(selected, 1, 200) || index > 0 && selected <= selection.pages[index - 1]) || !exact(loc, ['page']) || !/^[1-9][0-9]{0,2}$/.test(loc.page) || !selection.pages.includes(page) || page <= lastPdfPage) fail('PDF文字片段必须按升序来自明确批准的物理页；拒绝未选页、重复页或默选全部')
      lastPdfPage = page
    } else if (kind === 'xlsx') {
      if (!loc.sheet || !loc.cell) fail('提取结果缺少工作表与单元格定位')
      const actual = cellPosition(loc.cell)
      if (selection.sheets.length) {
        const sheet = selection.sheets.find(s => s.name === loc.sheet)
        if (!sheet) fail('提取结果含未批准的工作表')
        if (sheet.cell_range) {
          const bounds = sheet.cell_range.split(':'), first = cellPosition(bounds[0]), last = cellPosition(bounds.at(-1)!)
          if (actual[0] < first[0] || actual[0] > last[0] || actual[1] < first[1] || actual[1] > last[1]) fail('提取结果含未批准的单元格范围')
        }
      }
    } else if (kind === 'pptx' && (!loc.slide || selection.slides.length && !selection.slides.includes(Number(loc.slide)))) fail('提取结果幻灯片不属于批准选区')
    else if (kind === 'docx' && (!loc.paragraph && !loc.table || selection.paragraphs.length && loc.paragraph && !selection.paragraphs.includes(Number(loc.paragraph)))) fail('提取结果段落不属于批准选区')
    else if (['txt', 'md', 'markdown'].includes(kind ?? '') && loc.document !== 'text') fail('提取结果缺少明确文档文字定位')
  }
}
async function checkedParts(partsValue: unknown, textValue: unknown) {
  if (!Array.isArray(partsValue) || partsValue.length > MODEL_MATERIAL_LIMITS.parts || !Array.isArray(textValue)) fail('资料片段超限；拒绝，不截断')
  const parts = partsValue.map(part), texts = textValue as unknown[]
  const originals = new Map<string, string>(), native = new Set<string>(), seenFrames = new Set<string>()
  let characters = 0, bytes = 0, frames = 0, index = 0
  for (const p of parts) {
    const identity = canonical([p.original_sha256, p.original_version, p.original_kind, p.original_mime, p.original_size])
    if (originals.has(p.opaque_id) && originals.get(p.opaque_id) !== identity || native.has(p.opaque_id) || p.delivery === 'native' && originals.has(p.opaque_id)) fail('同一原件存在冲突、重复或混合直传')
    originals.set(p.opaque_id, identity)
    if (p.delivery === 'native') native.add(p.opaque_id)
    bytes += p.part_size
    if (p.delivery === 'video_frame') {
      const key = `${p.opaque_id}:${p.locator.frame}:${p.locator.timestamp_ms}`
      if (seenFrames.has(key)) fail('视频帧重复；拒绝发送')
      seenFrames.add(key); frames++
    }
    if (p.kind === 'text') {
      const content = texts[index++]
      if (!text(content)) fail('精确提取文字缺失或超过 100,000 字；拒绝，不截断')
      const actual = await shaText(content)
      if (actual.bytes !== p.part_size || actual.sha256 !== p.part_sha256) fail('提取文字实际字节或 SHA256 不符')
      characters += Array.from(content).length
    }
  }
  if (originals.size > MODEL_MATERIAL_LIMITS.originals || frames > MODEL_MATERIAL_LIMITS.frames || characters > MODEL_MATERIAL_LIMITS.characters || index !== texts.length) fail('资料超出 8 原件／16 帧／100,000 字硬限制，或文字与片段不一一对应；拒绝，不截断')
  return { parts, texts: texts as string[], originals, characters, bytes, frames }
}
const compiledKeys = ['schema', 'text_sha256', 'text_characters', 'attachments', 'attachment_count', 'part_count', 'processed_text_sha256', 'max_output_tokens', 'timeout_seconds', 'capability_source', 'limits', 'loss_notes', 'text', 'processed_text_parts', 'provider_id', 'protocol', 'model', 'profile_id', 'profile_revision', 'route_fingerprint', 'capability_fingerprint', 'payload_sha256', 'wire_bytes', 'submission_fingerprint', 'ready_for_verified_adapter', 'verification_note', 'estimated_text_tokens', 'media_token_estimate', 'token_counts_are_estimates', 'requires_separate_explicit_approval', 'execution_grant_minted', 'automatic_history_persona_memory_tools', 'endpoint', 'max_response_bytes', 'plain_http'] as const
export type ModelSubmissionInput = {
  route: AvailableModelHostRoute; text: string; materials: ApprovedModelMaterial[]; max_output_tokens: number; effort: ModelHostEffort | null;
}
async function compiled(value: unknown, expected: ModelSubmissionInput, submissionSha: string): Promise<CompiledModelPreview> {
  if (!object(value) || !exact(value, compiledKeys) || value.schema !== 'lingxi.model-submission.v1' || !digest(value.text_sha256) || !integer(value.text_characters, 0, MODEL_MATERIAL_LIMITS.characters) || !integer(value.attachment_count, 0, MODEL_MATERIAL_LIMITS.originals) || !integer(value.part_count, 0, MODEL_MATERIAL_LIMITS.parts) || !Array.isArray(value.processed_text_sha256) || value.processed_text_sha256.some(v => !digest(v)) || value.max_output_tokens !== expected.max_output_tokens || value.timeout_seconds !== expected.route.timeout_seconds || value.capability_source !== expected.route.capability_source || !object(value.limits) || !exact(value.limits, ['max_original_bytes', 'max_attachments', 'max_video_frames', 'max_document_chars', 'max_wire_bytes']) || value.limits.max_original_bytes !== MODEL_MATERIAL_LIMITS.originalBytes || value.limits.max_attachments !== MODEL_MATERIAL_LIMITS.originals || value.limits.max_video_frames !== MODEL_MATERIAL_LIMITS.frames || value.limits.max_document_chars !== MODEL_MATERIAL_LIMITS.characters || !integer(value.limits.max_wire_bytes, 1024, MODEL_MATERIAL_LIMITS.wireBytes) || !list(value.loss_notes, 100000, 500) || value.text !== expected.text || value.provider_id !== expected.route.provider_id || value.protocol !== expected.route.protocol || value.model !== expected.route.model || value.profile_id !== expected.route.profile_id || value.profile_revision !== expected.route.profile_revision || !digest(value.route_fingerprint) || !digest(value.capability_fingerprint) || value.payload_sha256 !== submissionSha || !integer(value.wire_bytes, 1, value.limits.max_wire_bytes) || !digest(value.submission_fingerprint) || value.ready_for_verified_adapter !== (expected.route.capability_source === 'verified_provider') || !line(value.verification_note, 2000) || !integer(value.estimated_text_tokens) || value.media_token_estimate !== null && !integer(value.media_token_estimate) || value.token_counts_are_estimates !== true || value.requires_separate_explicit_approval !== true || value.execution_grant_minted !== false || value.automatic_history_persona_memory_tools !== false) fail('最终模型、协议、正文、字节或能力声明与准备发送的快照不一致')
  if (value.endpoint !== expected.route.endpoint || value.max_response_bytes !== 1024 * 1024 || value.plain_http !== (new URL(expected.route.endpoint).protocol === 'http:')) fail('最终服务端目标、响应字节硬限或 HTTP 风险不符')
  const checked = await checkedParts(value.attachments, value.processed_text_parts)
  if (checked.originals.size !== value.attachment_count || checked.parts.length !== value.part_count || Array.from(expected.text).length !== value.text_characters || checked.characters + value.text_characters > MODEL_MATERIAL_LIMITS.characters) fail('最终文字或原件/片段数量不符，或超过 100,000 字硬限；拒绝，不截断')
  if ((await shaText(expected.text)).sha256 !== value.text_sha256) fail('最终草稿 SHA256 不符')
  const expectedParts = expected.materials.flatMap(v => v.parts), expectedTexts = expected.materials.flatMap(v => v.processed_text_parts)
  if (canonical(checked.parts) !== canonical(expectedParts) || canonical(checked.texts) !== canonical(expectedTexts) || canonical(value.loss_notes) !== canonical(expectedParts.flatMap(v => v.loss_notes))) fail('最终发送资料不是逐项批准的准确内容，或损失说明发生变化')
  const hashes = await Promise.all(checked.texts.map(async s => (await shaText(s)).sha256))
  if (canonical(hashes) !== canonical(value.processed_text_sha256)) fail('最终提取文字 SHA256 列表不符')
  return value as CompiledModelPreview
}
export function modelMaterialInputProblem(textValue: string, materials: readonly ApprovedModelMaterial[]): string {
  if (!text(textValue)) return '草稿超过 100,000 字或含无效字符；拒绝，不截断'
  if (!textValue && !materials.length) return '正文和资料不能同时为空'
  if (materials.length > MODEL_MATERIAL_LIMITS.originals || new Set(materials.map(v => v.source.fid)).size !== materials.length) return '最多 8 个不同原件；不能重复登记同一原件'
  if (materials.reduce((sum, v) => sum + v.characters, Array.from(textValue).length) > MODEL_MATERIAL_LIMITS.characters) return '正文与资料文字合计超过 100,000 字；拒绝，不截断'
  if (materials.reduce((sum, v) => sum + v.frame_count, 0) > MODEL_MATERIAL_LIMITS.frames) return '所有资料合计超过 16 帧；拒绝，不截断'
  if (materials.reduce((sum, v) => sum + v.parts.length, 0) > MODEL_MATERIAL_LIMITS.parts) return '处理片段超过 5,000；拒绝，不截断'
  return ''
}

/** Reuses the authenticated same-origin API wrapper. Reads never execute; mutations never retry. */
export function createModelMaterialSendClient() {
  let epoch = 0
  type Issued<T> = { snapshot: string; value: T; wall: number; monotonic: number }
  const materialApprovals = new Map<string, Issued<MaterialPreview>>()
  const videoApprovals = new Map<string, { snapshot: string; material: MaterialPreview }>()
  const sendApprovals = new Map<string, Issued<FinalModelSubmissionPreview>>()
  const approved = new Map<string, string>(), approvedScopes = new Map<string, string>()
  const pending = new Map<string, { scopeId: string; snapshot: ModelSubmissionInput; receipt: PreparedModelSubmission; previewAttempted?: boolean; handoffAttempted?: boolean; source?: { value: PreparedModelMaterialSource; snapshot: string; monotonic: number } }>()
  // These records retain only metadata and conservative deadlines, never local approval or compiled bytes.
  const handedOff = new Map<string, { snapshot: string; wall: number; monotonic: number }>()
  const transfers = new Map<string, { requestId: string; sha256: string; revokeAttempted: boolean }>()
  const clearSubmissionApprovals = (scopeId: string) => {
    for (const [token, held] of sendApprovals) if (held.value.scope_id === scopeId) sendApprovals.delete(token)
    for (const [id, held] of pending) if (held.scopeId === scopeId) pending.delete(id)
  }
  const scopeSessions = new Map<string, number>()
  const inspectedSources = new Map<string, MaterialSource>(), inspectedReceipts = new Map<string, string>()
  const sourceKey = (scopeId: string, fid: string) => `${scopeId}:${fid}`
  const now = () => performance.now()
  const invalidate = () => { epoch++; materialApprovals.clear(); videoApprovals.clear(); sendApprovals.clear(); approved.clear(); approvedScopes.clear(); pending.clear(); scopeSessions.clear(); inspectedSources.clear(); inspectedReceipts.clear() }
  function releaseScopeApprovals(scopeId: string) {
    scopeSessions.delete(scopeId)
    for (const key of inspectedSources.keys()) if (key.startsWith(scopeId + ':')) { inspectedSources.delete(key); inspectedReceipts.delete(key) }
    for (const [id, owner] of approvedScopes) if (owner === scopeId) { approved.delete(id); approvedScopes.delete(id) }
    for (const [token, held] of materialApprovals) if (held.value.scope_id === scopeId) materialApprovals.delete(token)
    for (const [token, held] of videoApprovals) if (held.material.scope_id === scopeId) videoApprovals.delete(token)
    clearSubmissionApprovals(scopeId)
  }
  async function pluginSources(signal?: AbortSignal) {
    const v = await request('/prepared/sources', undefined, signal, { apiBase: '/v11/plugin-model-proxy' })
    if (!object(v) || !exact(v, ['items', 'server_held_model_host_requests_only', 'automatic_retry', 'capability_notice']) || !Array.isArray(v.items) || v.items.length > 64 || v.server_held_model_host_requests_only !== true || v.automatic_retry !== false || !line(v.capability_notice, 2000)) fail('宿主未返回真实插件来源目录；没有绑定或发送')
    const rows = v.items.map(pluginSource)
    if (new Set(rows.map(item => item.request_id)).size !== rows.length) fail('插件来源目录的请求句柄重复')
    return rows
  }
  async function cancelRequest(scopeId: string, requestId: string, signal?: AbortSignal): Promise<'not_sent' | 'cancel_requested' | 'unknown'> {
    if (typeof scopeId !== 'string' || !TOKEN.test(scopeId) || typeof requestId !== 'string' || !TOKEN.test(requestId)) fail()
    pending.delete(requestId)
    for (const [key, held] of sendApprovals) if (held.value.request_id === requestId) sendApprovals.delete(key)
    const v = await request('/submissions/cancel', { scope_id: scopeId, request_id: requestId }, signal, { detached: true })
    if (!object(v) || !exact(v, ['scope_id', 'request_id', 'cancel_requested', 'active_execution', 'outcome', 'sent_undo_guaranteed', 'automatic_retry']) || v.scope_id !== scopeId || v.request_id !== requestId || v.cancel_requested !== true || typeof v.active_execution !== 'boolean' || !['not_sent', 'cancel_requested', 'unknown'].includes(String(v.outcome)) || v.sent_undo_guaranteed !== false || v.automatic_retry !== false) fail('撤销回执未确认；不能据此声称模型未收到请求')
    return v.outcome as 'not_sent' | 'cancel_requested' | 'unknown'
  }
  async function request(path: string, body: unknown, signal?: AbortSignal, options: { detached?: boolean; execution?: boolean; timeout?: number; apiBase?: '/v11/plugin-model-proxy' } = {}) {
    if (signal?.aborted) throw new ModelMaterialSendError('操作在请求前已取消；没有发送请求')
    const generation = epoch, controller = new AbortController()
    const abort = () => controller.abort()
    signal?.addEventListener('abort', abort, { once: true })
    const timer = setTimeout(abort, options.timeout ?? 20000)
    try {
      if (signal?.aborted) controller.abort()
      const value = await api<unknown>((options.apiBase ?? '/v11/model-host') + path, {
        ...(body === undefined ? { method: 'GET' } : json('POST', body)), signal: controller.signal, redirect: 'error',
      })
      finite(value)
      if (!options.detached && generation !== epoch || controller.signal.aborted) throw new ModelMaterialSendError(options.execution ? '请求已中断或会话已变化，发送结果未知；不会自动重试' : '旧会话或旧预览响应已丢弃', Boolean(options.execution))
      return value
    } catch (cause) {
      if (cause instanceof ModelMaterialSendError) {
        if (options.execution && !cause.outcomeUnknown) throw new ModelMaterialSendError('模型请求已尝试，但响应未通过安全验证；结果未知，可能已外发或计费，不自动重试', true)
        throw cause
      }
      // Do not echo arbitrary provider errors, internal payloads or credential-bearing diagnostics.
      throw new ModelMaterialSendError(options.execution
        ? '模型请求未获得可信完成回执，可能已经外发或计费；结果未知，请勿自动重试'
        : cause instanceof ApiError ? `宿主拒绝此操作（HTTP ${cause.status}）；请重新读取或预览，没有自动发送或重试` : '宿主请求中断或响应无效；没有自动重试', Boolean(options.execution))
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
  }
  function live(scopeId: string) {
    if (!identifier(scopeId) || !scopeSessions.has(scopeId) || transfers.has(scopeId)) fail('需要当前真实会话的宿主 scope；不会伪造运行时身份')
    return scopeSessions.get(scopeId)!
  }
  function issue<T extends { token: string; expires_in: number; client_expires_at: number }>(map: Map<string, Issued<T>>, value: T, mono: number) {
    if (map.size >= 128 || map.has(value.token)) fail('批准数量超限或 token 重复；请取消后重新预览')
    map.set(value.token, { snapshot: canonical(value), value, wall: value.client_expires_at, monotonic: mono + value.expires_in * 1000 })
    return value
  }
  function approvalProblem<T extends { token: string }>(map: Map<string, Issued<T>>, value: T) {
    const record = map.get(value.token)
    return !record || record.snapshot !== canonical(value) || Date.now() >= record.wall || now() >= record.monotonic ? '本次批准已失效、已尝试、已过期或不属于当前快照；请重新预览' : ''
  }
  function consume<T extends { token: string }>(map: Map<string, Issued<T>>, value: T) {
    const problem = approvalProblem(map, value)
    map.delete(value.token)
    if (problem) fail(problem)
  }
  function expiry(v: Record<string, unknown>, start: number) {
    if (typeof v.token !== 'string' || !TOKEN.test(v.token) || !digest(v.fingerprint) || typeof v.expires_in !== 'number' || !Number.isFinite(v.expires_in) || v.expires_in <= 0 || v.expires_in > 300) fail('批准 token、指纹或期限无效')
    return start + v.expires_in * 1000
  }
  function validatedInput(scopeId: string, input: ModelSubmissionInput): ModelSubmissionInput {
    live(scopeId); route(input.route)
    if (!isAvailableModelHostRoute(input.route)) fail('此模型路由不可用；保留诊断但禁止选用')
    const problem = modelMaterialInputProblem(input.text, input.materials)
    if (problem) fail(problem)
    if (!integer(input.max_output_tokens, 1, input.route.max_output_tokens) || input.effort !== null && !input.route.supported_efforts.includes(input.effort)) fail('输出上限或思考档位不属于当前模型的明确声明')
    for (const material of input.materials) if (material.scope_id !== scopeId || approved.get(material.material_id) !== canonical(material)) fail('资料未由当前 scope 批准或已发生变化')
    return JSON.parse(JSON.stringify(input)) as ModelSubmissionInput
  }
  const client = {
    invalidate,
    discardMaterialPreview: (value: MaterialPreview) => { materialApprovals.delete(value.token) },
    materialApprovalProblem: (value: MaterialPreview) => approvalProblem(materialApprovals, value),
    sendApprovalProblem: (value: FinalModelSubmissionPreview) => approvalProblem(sendApprovals, value),
    async routes(signal?: AbortSignal): Promise<ModelHostRoute[]> {
      const value = await request('/routes', undefined, signal)
      if (!object(value) || !exact(value, ['items', 'capability_notice', 'automatic_fallback']) || !Array.isArray(value.items) || value.items.length > 256 || !line(value.capability_notice, 2000) || value.automatic_fallback !== false) fail('模型路由清单无效')
      const rows = value.items.map(route)
      if (new Set(rows.map(v => v.profile_id)).size !== rows.length) fail('模型路由身份重复')
      return rows
    },
    async createScope(sessionId: number, signal?: AbortSignal): Promise<string> {
      if (!integer(sessionId, 1)) fail('必须使用真实的当前会话')
      const generation = epoch
      // Parse even a late creation reply so a scope minted after unmount can still be revoked.
      const value = await request('/scopes', { session_id: sessionId }, undefined, { detached: true })
      if (!object(value) || !exact(value, ['scope_id', 'session_id', 'scope_kind', 'expires_in', 'revision']) || typeof value.scope_id !== 'string' || !TOKEN.test(value.scope_id) || value.session_id !== sessionId || value.scope_kind !== 'host_chat' || value.expires_in !== 300 || value.revision !== 1) fail('宿主没有返回绑定当前会话的 host_chat scope')
      if (generation !== epoch || signal?.aborted) {
        await client.revokeScope(value.scope_id).catch(() => undefined)
        fail('旧会话 scope 已撤销或请求撤销，未采用')
      }
      scopeSessions.set(value.scope_id, sessionId)
      return value.scope_id
    },
    async inspect(scopeId: string, attachment: ModelMaterialAttachment, signal?: AbortSignal): Promise<MaterialInspection> {
      const sessionId = live(scopeId), original = validateModelMaterialAttachments(sessionId, [attachment])[0]
      const key = sourceKey(scopeId, original.fid), held = inspectedSources.get(key)
      if (held && (held.filename !== original.name || held.mime !== original.mime || held.size !== original.size || original.sha256 !== undefined && held.sha256 !== original.sha256)) fail('当前 scope 的原件元数据变化；必须撤销并建立新 scope')
      const expectedSha = held?.sha256 ?? original.sha256
      const v = await request('/materials/inspect', { scope_id: scopeId, fid: original.fid, ...(expectedSha === undefined ? {} : { expected_sha256: expectedSha }) }, signal)
      if (!object(v) || !exact(v, ['scope_id', 'source', 'inspection', 'inspection_fingerprint', 'deliveries', 'model_sent']) || v.scope_id !== scopeId || !digest(v.inspection_fingerprint) || !Array.isArray(v.deliveries) || v.deliveries.length > 3 || v.model_sent !== false) fail('原件预检回执无效')
      const actual = source(v.source, { ...original, sha256: expectedSha }, sessionId)
      if (held && canonical(actual) !== canonical(held)) fail('同一 scope 的不可变原件身份发生变化，旧摘要不能继续使用')
      const report = inspectionReport(v.inspection, actual)
      const seen = new Set<string>()
      for (const d of v.deliveries) {
        if (!object(d) || !exact(d, ['delivery', 'available', 'code']) || typeof d.delivery !== 'string' || !deliveries.includes(d.delivery) || seen.has(d.delivery) || typeof d.available !== 'boolean' || d.available && d.code !== null || !d.available && (!line(d.code, 120) || !/^[a-z0-9_]+$/.test(d.code))) fail('资料可用交付或诊断无效')
        seen.add(d.delivery)
      }
      if (report.kind === 'mp4' || report.kind === 'webm') {
        const native = v.deliveries[0], frames = v.deliveries[1]
        const ready = report.delivery === 'native' && report.ready_for_extraction
        if (v.deliveries.length !== 2 || !object(native) || !object(frames) || native.delivery !== 'native' || frames.delivery !== 'video_frame' || native.available !== ready || native.code !== (ready ? null : report.diagnostic) || !ready && (frames.available !== false || frames.code !== report.diagnostic)) fail('视频必须提供明确分离的原生／帧交付，且与真实探测或失败诊断一致')
      }
      if (report.kind === 'pdf') {
        const expected = [{ delivery: 'extracted_text', available: report.delivery === 'extracted_text' && report.ready_for_extraction, code: report.delivery === 'extracted_text' && report.ready_for_extraction ? null : report.diagnostic }]
        if (canonical(v.deliveries) !== canonical(expected)) fail('PDF可用交付必须与真实预检和诊断一致；禁止原件直传或静默降级')
      }
      // Only after fid/session/name/mime/size and immutable source9 pass do we
      // remember the actual host SHA. Every later inspect/preview binds this SHA.
      inspectedSources.set(key, { ...actual }); inspectedReceipts.set(key, canonical(v))
      return v as MaterialInspection
    },
    async previewMaterial(scopeId: string, inspection: MaterialInspection, delivery: MaterialDelivery, selected: ModelMaterialSelection, signal?: AbortSignal, selectedVideo?: VideoSelection): Promise<MaterialPreview> {
      const sessionId = live(scopeId)
      if (inspection.scope_id !== scopeId || inspectedReceipts.get(sourceKey(scopeId, inspection.source.fid)) !== canonical(inspection) || !inspection.deliveries.some(d => d.delivery === delivery && d.available)) fail('该预检不属于当前 scope 或没有明确提供这种交付方式；不自动改为其他方式')
      const selection = delivery === 'extracted_text' ? documentSelection(selected, inspection.source.filename) : emptyModelMaterialSelection()
      if (delivery !== 'extracted_text' && canonical(selected) !== canonical(selection)) fail('此交付方式不接受文档选区，不能静默忽略选区')
      const report = inspection.inspection
      const isVideo = report.kind === 'mp4' || report.kind === 'webm'
      if (isVideo) for (const [token, held] of materialApprovals) if (held.value.scope_id === scopeId && held.value.source.fid === inspection.source.fid) { materialApprovals.delete(token); videoApprovals.delete(token) }
      if (!isVideo && (selectedVideo !== undefined || delivery === 'video_frame') || isVideo && (delivery === 'extracted_text' || delivery === 'native' && selectedVideo !== undefined)) fail('文档选区、原生视频与明确抽帧不可混用或静默切换')
      const probe = report.kind === 'mp4' || report.kind === 'webm' ? validateVideoProbe(report.video_metadata, videoSource(inspection.source as VideoSource)) : undefined
      const videoSelected = delivery === 'video_frame' && probe ? videoSelection(selectedVideo as VideoSelection, probe) : null
      if (report.kind === 'pdf' && (delivery !== 'extracted_text' || report.delivery !== 'extracted_text' || !report.ready_for_extraction || !selection.pages.length || selection.pages.some(page => page > report.pages))) fail('PDF必须明确选取真实物理页数内的页码；不能留空、原件直传或默选全部')
      const start = Date.now(), mono = now()
      clearSubmissionApprovals(scopeId)
      const v = await request('/materials/preview', { scope_id: scopeId, fid: inspection.source.fid, expected_sha256: inspection.source.sha256, delivery, selection, ...(isVideo ? { video_selection: videoSelected } : {}) }, signal, { timeout: isVideo ? 45000 : 20000 })
      const previewKeys = ['scope_id', 'token', 'fingerprint', 'expires_in', 'source', 'delivery', 'selection', 'converter', 'loss_notes', 'conversion_performed', 'approval_required', 'model_sent', 'sending_requires_separate_approval', ...(isVideo ? ['video_selection', 'plan'] : [])]
      if (!object(v) || !exact(v, previewKeys) || v.scope_id !== scopeId || v.delivery !== delivery || canonical(v.selection) !== canonical(selection) || !line(v.converter, 100) || !list(v.loss_notes, 100, 1000) || delivery !== 'native' && !v.loss_notes.length || v.conversion_performed !== false || v.approval_required !== true || v.model_sent !== false || v.sending_requires_separate_approval !== true) fail('资料转换预览的原件、选区、损失或未发送状态无效')
      if (report.kind === 'pdf' && v.converter !== report.parser_version) fail('PDF转换计划的真实版本与已预检解析器不一致')
      const original = { fid: inspection.source.fid, name: inspection.source.filename, mime: inspection.source.mime, size: inspection.source.size, sha256: inspection.source.sha256 }
      source(v.source, original, sessionId)
      if (canonical(v.source) !== canonical(inspection.source)) fail('转换预览的不可变原件身份与首读不同')
      const result = { ...v, client_expires_at: expiry(v, start) } as MaterialPreview
      if (isVideo) {
        if (canonical(v.video_selection) !== canonical(videoSelected) || !object(v.plan) || !probe) fail('视频计划必须独立绑定精确六键选区')
        if (delivery === 'video_frame') {
          const precise = validateVideoPreview({ source: videoSource(inspection.source as VideoSource), probe, plan: v.plan, token: v.token, fingerprint: v.fingerprint, expires_at_ms: result.client_expires_at, conversion_performed: false, model_sent: false, sending_requires_separate_approval: true }, videoSource(inspection.source as VideoSource), videoSelected!)
          if (v.converter !== precise.plan.converter || canonical(v.loss_notes) !== canonical(precise.plan.loss_notes)) fail('抽帧方法或全部损失与真实计划不一致')
        } else {
          const p = v.plan
          if (!exact(p, ['original_id', 'original_version', 'original_sha256', 'byte_length', 'inspection', 'request_original_count', 'request_total_video_frames', 'delivery', 'loss_notes', 'converter', 'approval_required', 'sending_approved', 'native_provider_support_verified']) || p.original_id !== inspection.source.fid || p.original_version !== inspection.source.original_version || p.original_sha256 !== inspection.source.sha256 || p.byte_length !== inspection.source.size || canonical(p.inspection) !== canonical(probe) || !integer(p.request_original_count, 1, 8) || !integer(p.request_total_video_frames, 0, 16) || p.delivery !== 'native' || p.converter !== 'none' || v.converter !== 'none' || canonical(p.loss_notes) !== canonical(v.loss_notes) || p.approval_required !== true || p.sending_approved !== false || p.native_provider_support_verified !== false) fail('原生视频计划没有绑定真实探测、预算和完整原件')
        }
      }
      return issue(materialApprovals, result, mono)
    },
    async approveMaterial(preview: MaterialPreview, userConfirmed: boolean, signal?: AbortSignal): Promise<ApprovedModelMaterial> {
      live(preview.scope_id)
      if (userConfirmed !== true) fail('资料转换与登记必须由用户单独明确批准')
      consume(materialApprovals, preview)
      clearSubmissionApprovals(preview.scope_id)
      const generation = epoch
      const isVideo = preview.source.mime === 'video/mp4' || preview.source.mime === 'video/webm'
      const v = await request('/materials/approve', { scope_id: preview.scope_id, token: preview.token, fingerprint: preview.fingerprint, user_confirmed: true }, signal, { timeout: isVideo ? 180000 : 20000 })
      const approvalKeys = ['scope_id', 'material_id', 'fingerprint', 'source', 'delivery', 'selection', 'parts', 'processed_text_parts', 'loss_notes', 'content_bytes', 'characters', 'frame_count', 'model_sent', 'sending_approved', 'sending_requires_separate_approval', ...(isVideo ? ['video_selection', 'frames', 'conversion_performed'] : [])]
      if (!object(v) || !exact(v, approvalKeys) || v.scope_id !== preview.scope_id || !identifier(v.material_id) || !digest(v.fingerprint) || canonical(v.source) !== canonical(preview.source) || v.delivery !== preview.delivery || canonical(v.selection) !== canonical(preview.selection) || !list(v.loss_notes, 100, 1000) || !(isVideo && preview.delivery === 'native') && preview.loss_notes.some(note => !(v.loss_notes as string[]).includes(note)) || v.model_sent !== false || v.sending_approved !== false || v.sending_requires_separate_approval !== true) fail('资料登记回执与批准的原件、转换或选区不一致')
      const checked = await checkedParts(v.parts, v.processed_text_parts)
      if (!checked.parts.length || checked.originals.size !== 1 || !checked.originals.has(preview.source.fid) || checked.parts.some(p => p.opaque_id !== preview.source.fid || p.original_sha256 !== preview.source.sha256 || p.original_size !== preview.source.size || p.original_mime !== preview.source.mime || p.delivery !== preview.delivery) || v.content_bytes !== checked.bytes || v.characters !== checked.characters || v.frame_count !== checked.frames || checked.characters > preview.selection.max_characters) fail('登记资料实际内容、数量或原件不符；没有批准发送')
      assertSelectedParts(checked.parts, preview)
      if (isVideo) {
        if (!checked.parts[0].loss_notes.length || canonical(v.loss_notes) !== canonical(checked.parts[0].loss_notes)) fail('视频登记损失必须精确来自真实registry片段，不能删除、替换或伪造')
        const frames = checked.parts.filter(p => p.delivery === 'video_frame').map(p => ({ frame: Number(p.locator.frame), timestamp_ms: Number(p.locator.timestamp_ms), sha256: p.part_sha256, mime: p.mime, bytes: p.part_size }))
        if (canonical(v.video_selection) !== canonical(preview.video_selection) || canonical(v.frames) !== canonical(frames) || v.conversion_performed !== (preview.delivery === 'video_frame') || preview.delivery === 'video_frame' && (checked.frames !== preview.video_selection?.frame_count || frames.some((p, index) => p.frame !== index + 1)) || preview.delivery === 'native' && (checked.frames !== 0 || checked.parts.length !== 1)) fail('宿主实际帧结果与已批准的时间点、顺序或交付不一致')
      }
      if (generation !== epoch || signal?.aborted) fail('原件、选区或会话已变化；已丢弃旧登记结果，未批准发送')
      const result = v as ApprovedModelMaterial
      if (approved.has(result.material_id)) fail('资料登记身份重复')
      approved.set(result.material_id, canonical(result)); approvedScopes.set(result.material_id, preview.scope_id)
      return result
    },
    async previewVideo(scopeId: string, inspection: MaterialInspection, selection: VideoSelection, signal?: AbortSignal): Promise<VideoConversionPreview> {
      const material = await client.previewMaterial(scopeId, inspection, 'video_frame', emptyModelMaterialSelection(), signal, selection)
      const report = inspection.inspection
      if ((report.kind !== 'mp4' && report.kind !== 'webm') || !report.video_metadata) fail('必须使用当前原件的真实视频探测报告')
      // Exact explicit adapter, not a guessed tolerant union of the two contracts.
      const value = validateVideoPreview({ source: videoSource(material.source as VideoSource), probe: report.video_metadata, plan: material.plan, token: material.token, fingerprint: material.fingerprint, expires_at_ms: material.client_expires_at, conversion_performed: false, model_sent: false, sending_requires_separate_approval: true }, videoSource(material.source as VideoSource), selection)
      videoApprovals.set(value.token, { snapshot: canonical(value), material })
      return value
    },
    discardVideoPreview(value: VideoConversionPreview) {
      const held = videoApprovals.get(value.token)
      videoApprovals.delete(value.token)
      if (held) materialApprovals.delete(held.material.token)
      // The host-held reservation expires or is replaced for this original. This
      // endpoint contract has no plan-revoke route; do not claim remote removal.
    },
    async approveVideo(value: VideoConversionPreview, userConfirmed: true, signal?: AbortSignal): Promise<ApprovedModelMaterial> {
      const held = videoApprovals.get(value.token)
      videoApprovals.delete(value.token)
      if (!held || held.snapshot !== canonical(value)) fail('抽帧批准不属于当前准确宿主计划')
      return client.approveMaterial(held.material, userConfirmed, signal)
    },
    async videoFrameBlob(material: ApprovedModelMaterial, frame: number, signal?: AbortSignal): Promise<Blob> {
      live(material.scope_id)
      if (approved.get(material.material_id) !== canonical(material) || material.delivery !== 'video_frame' || !integer(frame, 1, 16)) fail('帧预览需要当前 scope 已批准且未变化的真实抽帧资料')
      const p = material.parts.find(part => part.delivery === 'video_frame' && part.locator.frame === String(frame))
      if (!p || p.mime !== 'image/png' || p.part_size > VIDEO_LIMITS.outputFrameBytes) fail('没有绑定该帧的真实PNG片段')
      const owner = epoch, controller = new AbortController()
      const stop = () => controller.abort(), timer = setTimeout(() => controller.abort(), 20000)
      signal?.addEventListener('abort', stop, { once: true })
      if (signal?.aborted) controller.abort()
      try {
        const blob = await apiBlob('/v11/model-host/materials/video-frame', { ...json('POST', { scope_id: material.scope_id, material_id: material.material_id, fingerprint: material.fingerprint, frame, expected_sha256: p.part_sha256 }), headers: { 'Content-Type': 'application/json' }, signal: controller.signal, redirect: 'error' })
        if (owner !== epoch || controller.signal.aborted || approved.get(material.material_id) !== canonical(material) || blob.type !== 'image/png' || blob.size !== p.part_size || blob.size > VIDEO_LIMITS.outputFrameBytes || !globalThis.crypto?.subtle) fail('帧响应已失效，或不是准确的安全PNG字节')
        const hash = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer())
        if (owner !== epoch || controller.signal.aborted || approved.get(material.material_id) !== canonical(material) || Array.from(new Uint8Array(hash), n => n.toString(16).padStart(2, '0')).join('') !== p.part_sha256) fail('实际PNG帧的SHA256与宿主持有片段不符')
        return blob
      } finally { clearTimeout(timer); signal?.removeEventListener('abort', stop) }
    },
    async revokeMaterial(scopeId: string, materialId: string, signal?: AbortSignal) {
      if (transfers.has(scopeId)) fail('已尝试交接的原 scope 已冻结；不能用普通资料清理撤销来源，请独立确认 cancelSource')
      if (!identifier(scopeId) || !identifier(materialId)) fail()
      approved.delete(materialId); approvedScopes.delete(materialId); sendApprovals.clear(); pending.clear()
      const v = await request('/materials/revoke', { scope_id: scopeId, material_id: materialId }, signal, { detached: true })
      if (!object(v) || !exact(v, ['revoked', 'model_sent', 'external_effect_undo_guaranteed']) || v.revoked !== true || v.model_sent !== false || v.external_effect_undo_guaranteed !== false) fail('资料撤销未获可信回执；不能继续沿用旧发送预览')
    },
    async prepare(scopeId: string, input: ModelSubmissionInput, signal?: AbortSignal): Promise<PreparedModelSubmission> {
      const snapshot = validatedInput(scopeId, input)
      clearSubmissionApprovals(scopeId)
      const v = await request('/submissions/prepare', { scope_id: scopeId, profile_id: snapshot.route.profile_id, expected_profile_revision: snapshot.route.profile_revision, text: snapshot.text, materials: snapshot.materials.map(({ material_id, fingerprint }) => ({ material_id, fingerprint })), max_output_tokens: snapshot.max_output_tokens, effort: snapshot.effort }, signal)
      if (!object(v) || !exact(v, ['scope_id', 'request_id', 'submission_sha256', 'submission_fingerprint', 'scope_revision', 'sent', 'approval_required']) || v.scope_id !== scopeId || typeof v.request_id !== 'string' || !TOKEN.test(v.request_id) || !digest(v.submission_sha256) || !digest(v.submission_fingerprint) || !integer(v.scope_revision, 1) || v.sent !== false || v.approval_required !== true || pending.has(v.request_id)) fail('宿主准备回执无效；没有批准发送')
      const receipt = v as PreparedModelSubmission
      pending.set(receipt.request_id, { scopeId, snapshot, receipt })
      return receipt
    },
    async readPluginSource(receipt: PreparedModelSubmission, signal?: AbortSignal): Promise<PreparedModelMaterialSource> {
      live(receipt.scope_id)
      const record = pending.get(receipt.request_id)
      if (!record || record.scopeId !== receipt.scope_id || canonical(record.receipt) !== canonical(receipt) || record.previewAttempted || record.handoffAttempted) fail('只有当前尚未尝试发送预览或交接的准备请求才可作为窗口持有的插件来源')
      const start = Date.now(), mono = now(), generation = epoch
      const rows = await pluginSources(signal)
      const actual = rows.find(item => item.scope_id === receipt.scope_id && item.request_id === receipt.request_id && item.submission_sha256 === receipt.submission_sha256 && item.submission_fingerprint === receipt.submission_fingerprint)
      if (!actual || actual.profile_id !== record.snapshot.route.profile_id || actual.provider !== record.snapshot.route.provider_id || actual.model !== record.snapshot.route.model || actual.ready_for_verified_adapter !== (record.snapshot.route.capability_source === 'verified_provider')) fail('准备请求已过期、已变化或不能导出为真实插件来源；没有绑定、生成外发批准或发送')
      if (generation !== epoch || signal?.aborted || pending.get(receipt.request_id) !== record || record.previewAttempted || record.handoffAttempted) fail('来源准备期间正文、原件、路由或 scope 已变化，旧句柄已丢弃')
      const value = { ...actual, client_expires_at: start + actual.expires_in * 1000 }
      if (Date.now() >= value.client_expires_at || now() >= mono + actual.expires_in * 1000) fail('来源回执抵达时已到期；没有续期或发送')
      record.source = { value, snapshot: canonical(value), monotonic: mono + actual.expires_in * 1000 }
      return value
    },
    pluginSourceProblem(value: PreparedModelMaterialSource): string {
      const record = pending.get(value.request_id), held = record?.source
      return !scopeSessions.has(value.scope_id) || !record || record.scopeId !== value.scope_id || record.previewAttempted || record.handoffAttempted || !held || held.snapshot !== canonical(value) || Date.now() >= value.client_expires_at || now() >= held.monotonic ? '本次来源已过期、已变化、已尝试外发预览／交接或不再属于当前窗口 scope；不能据此选择或再次交接，不自动续期。' : ''
    },
    pluginSourceRemaining(value: PreparedModelMaterialSource): number {
      const held = pending.get(value.request_id)?.source
      if (!held || client.pluginSourceProblem(value)) return 0
      return Math.max(0, Math.floor(Math.min(value.client_expires_at - Date.now(), held.monotonic - now()) / 1000))
    },
    async handoffPluginSource(value: PreparedModelMaterialSource, userConfirmed: boolean, signal?: AbortSignal): Promise<ModelMaterialSourceHandoff> {
      live(value.scope_id)
      const generation = epoch, record = pending.get(value.request_id), source = record?.source
      if (userConfirmed !== true || signal?.aborted || !record || !source || record.handoffAttempted || transfers.has(value.scope_id) || client.pluginSourceProblem(value)) fail('仅可单独明确交接当前真实未预览来源一次；不自动重试')
      // Consume before the first mutation. Lost/invalid replies cannot restore any local approval or source capability.
      record.handoffAttempted = true
      transfers.set(value.scope_id, { requestId: value.request_id, sha256: value.submission_sha256, revokeAttempted: false })
      releaseScopeApprovals(value.scope_id)
      const start = Date.now(), mono = now()
      try {
        const v = await request('/submissions/handoff', { scope_id: value.scope_id, request_id: value.request_id, expected_submission_sha: value.submission_sha256, user_confirmed: true }, signal)
        if (!object(v) || !exact(v, ['scope_id', 'request_id', 'submission_sha256', 'expires_in', 'source_kind', 'handoff_complete', 'sent', 'send_approval_created', 'runtime_deadline_extended']) || v.scope_id !== value.scope_id || v.request_id !== value.request_id || v.submission_sha256 !== value.submission_sha256 || typeof v.expires_in !== 'number' || !Number.isFinite(v.expires_in) || v.expires_in <= 0 || v.expires_in > 60 || v.expires_in > value.expires_in || v.source_kind !== 'host_model_request' || v.handoff_complete !== true || v.sent !== false || v.send_approval_created !== false || v.runtime_deadline_extended !== false) fail('未获得符合精确九字段合同的交接回执')
        const readStart = Date.now(), readMono = now()
        const rows = await pluginSources(signal)
        const actual = rows.find(item => item.scope_id === value.scope_id && item.request_id === value.request_id && item.submission_sha256 === value.submission_sha256)
        if (!actual || actual.submission_fingerprint !== value.submission_fingerprint || actual.profile_id !== value.profile_id || actual.provider !== value.provider || actual.model !== value.model || actual.ready_for_verified_adapter !== value.ready_for_verified_adapter || actual.estimated_text_tokens !== value.estimated_text_tokens || actual.media_token_estimate !== value.media_token_estimate || actual.expires_in > v.expires_in) fail('交接后二次读取未核验到同一准确来源或原有效期')
        const wall = Math.min(value.client_expires_at, start + v.expires_in * 1000, readStart + actual.expires_in * 1000)
        const monotonic = Math.min(source.monotonic, mono + v.expires_in * 1000, readMono + actual.expires_in * 1000)
        if (generation !== epoch || signal?.aborted || Date.now() >= wall || now() >= monotonic || transfers.get(value.scope_id)?.revokeAttempted) fail('交接期间来源、会话或有效期已变化')
        const result = { ...v } as ModelMaterialSourceHandoff
        handedOff.set(value.scope_id, { snapshot: canonical(result), wall, monotonic })
        return result
      } catch {
        fail('交接已尝试，但未同时取得可信九字段回执与真实来源二次核验；结果未知，不声称来源仍有效，不重试或续期。可单独请求撤销准确来源，或到插件页重新读取状态；未批准插件执行或模型外发')
      }
    },
    handoffRemaining(value: ModelMaterialSourceHandoff): number {
      const held = handedOff.get(value.scope_id)
      if (!held || held.snapshot !== canonical(value)) return 0
      return Math.max(0, Math.floor(Math.min(held.wall - Date.now(), held.monotonic - now()) / 1000))
    },
    async cancelSource(value: Pick<ModelMaterialSourceHandoff, 'scope_id' | 'request_id' | 'submission_sha256'>, userConfirmed: boolean, signal?: AbortSignal): Promise<'not_sent' | 'cancel_requested' | 'unknown'> {
      const transfer = transfers.get(value.scope_id)
      if (userConfirmed !== true || signal?.aborted || !transfer || transfer.requestId !== value.request_id || transfer.sha256 !== value.submission_sha256 || transfer.revokeAttempted) fail('撤销已尝试交接的准确来源须独立明确确认一次；不自动重试')
      transfer.revokeAttempted = true
      handedOff.delete(value.scope_id)
      return cancelRequest(value.scope_id, value.request_id, signal)
    },
    async previewSubmission(receipt: PreparedModelSubmission, signal?: AbortSignal): Promise<FinalModelSubmissionPreview> {
      live(receipt.scope_id)
      const record = pending.get(receipt.request_id)
      if (!record || record.scopeId !== receipt.scope_id || canonical(record.receipt) !== canonical(receipt) || record.previewAttempted || record.handoffAttempted) fail('发送准备回执不属于当前快照，或已经尝试预览／交接')
      // A failed/uncertain preview can still mint a host token: never offer this receipt as an unpreviewed source again.
      record.previewAttempted = true; record.source = undefined
      const start = Date.now(), mono = now(), generation = epoch
      const v = await request('/submissions/preview', { scope_id: receipt.scope_id, request_id: receipt.request_id, expected_submission_sha: receipt.submission_sha256 }, signal)
      if (!object(v) || !exact(v, ['scope_id', 'request_id', 'submission_sha256', 'token', 'fingerprint', 'expires_in', 'route', 'effort', 'materials', 'preview', 'plain_http', 'requires_unverified_capabilities_acceptance', 'capability_notice', 'approval_required', 'sent']) || v.scope_id !== receipt.scope_id || v.request_id !== receipt.request_id || v.submission_sha256 !== receipt.submission_sha256 || v.sent !== false || canonical(route(v.route)) !== canonical({ ...record.snapshot.route, max_output_tokens: record.snapshot.max_output_tokens }) || v.effort !== record.snapshot.effort || canonical(references(v.materials)) !== canonical(record.snapshot.materials.map(({ material_id, fingerprint }) => ({ material_id, fingerprint }))) || v.plain_http !== (new URL(record.snapshot.route.endpoint).protocol === 'http:') || v.requires_unverified_capabilities_acceptance !== (record.snapshot.route.capability_source !== 'verified_provider')) fail('最终发送快照、路由、凭据代际、资料或风险状态不一致')
      if (!line(v.capability_notice, 2000) || v.approval_required !== true || !object(v.preview) || v.preview.submission_fingerprint !== receipt.submission_fingerprint) fail('最终能力告知、独立批准或编译指纹无效')
      await compiled(v.preview, record.snapshot, receipt.submission_sha256)
      if (generation !== epoch || signal?.aborted) fail('原件、正文、路由或会话已变化；已丢弃旧发送预览')
      const result = { ...v, client_expires_at: expiry(v, start) } as FinalModelSubmissionPreview
      return issue(sendApprovals, result, mono)
    },
    async execute(preview: FinalModelSubmissionPreview, confirmation: { userConfirmed: boolean; acceptUnverifiedCapabilities: boolean; allowPlainHttp: boolean }, signal?: AbortSignal): Promise<ModelMaterialSendResult> {
      live(preview.scope_id)
      if (confirmation.userConfirmed !== true || preview.requires_unverified_capabilities_acceptance && confirmation.acceptUnverifiedCapabilities !== true || preview.plain_http && confirmation.allowPlainHttp !== true) fail('需要独立勾选最终发送、未实测能力及适用的明文 HTTP 风险')
      consume(sendApprovals, preview)
      pending.delete(preview.request_id)
      const v = await request('/submissions/execute', { scope_id: preview.scope_id, token: preview.token, fingerprint: preview.fingerprint, user_confirmed: true, accept_unverified_capabilities: confirmation.acceptUnverifiedCapabilities === true, allow_plain_http: confirmation.allowPlainHttp === true }, signal, { execution: true, timeout: (preview.route.timeout_seconds + 5) * 1000 })
      if (!object(v) || !exact(v, ['scope_id', 'request_id', 'submission_sha256', 'answer', 'usage', 'finish']) || v.scope_id !== preview.scope_id || v.request_id !== preview.request_id || v.submission_sha256 !== preview.submission_sha256 || !text(v.answer, 1024 * 1024) || !object(v.usage) || !exact(v.usage, ['input_tokens', 'output_tokens', 'total_tokens']) || Object.values(v.usage).some(n => n !== null && !integer(n)) || !['completed', 'length', 'tool_requested', 'blocked', 'refused', 'incomplete'].includes(String(v.finish))) throw new ModelMaterialSendError('模型请求已尝试，但完成回执不符合安全合同；结果未知，可能已计费，不自动重试', true)
      return v as ModelMaterialSendResult
    },
    async cancelSubmission(scopeId: string, requestId: string, signal?: AbortSignal): Promise<'not_sent' | 'cancel_requested' | 'unknown'> {
      if (transfers.has(scopeId)) fail('普通窗口清理不能取消已尝试交接的来源；如需撤销，请单独明确调用 cancelSource')
      return cancelRequest(scopeId, requestId, signal)
    },
    async revokeScope(scopeId: string): Promise<boolean> {
      if (!identifier(scopeId)) fail()
      releaseScopeApprovals(scopeId)
      const v = await request('/scopes/revoke', { scope_id: scopeId }, undefined, { detached: true })
      if (!object(v) || !exact(v, ['revoked', 'sent_undo_guaranteed', 'automatic_retry']) || typeof v.revoked !== 'boolean' || v.sent_undo_guaranteed !== false || v.automatic_retry !== false) fail('scope 撤销未确认；界面已丢弃批准，但不能据此推断来源已撤销或在途请求被撤回')
      if (v.revoked === false) {
        // Explicit frozen handoff: only local permissions were released; the original source lives until its original expiry.
        return false
      }
      handedOff.delete(scopeId)
      return true
    },
  }
  return client
}
