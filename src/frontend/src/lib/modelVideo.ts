/** Pure video selection/preview contracts; no fetch, decode, model call or trust minting. */
export const VIDEO_LIMITS = Object.freeze({
  originalBytes: 15 * 1024 * 1024,
  originals: 8,
  framesPerRequest: 16,
  durationMs: 6 * 60 * 60 * 1000,
  outputDimension: 2048,
  outputFrameBytes: 13 * 1024 * 1024,
  resultBytes: 64 * 1024 * 1024,
})

export type VideoSource = {
  fid: string
  session_id: number
  original_version: number
  filename: string
  mime: 'video/mp4' | 'video/webm'
  size: number
  sha256: string
}
export type VideoSelection = {
  timestamps_ms: number[]
  frame_count: number
  max_width: number
  max_height: number
  stream_index: number
  output_format: 'png'
}
export type MediaToolIdentity = {
  name: 'ffprobe' | 'ffmpeg'
  version: string
  sha256: string
  dependency_manifest_sha256: string
}
export type VideoStream = {
  index: number
  codec: string
  width: number
  height: number
  duration_ms: number | null
  average_frame_rate: string
}
export type VideoProbe = {
  original_sha256: string
  byte_length: number
  kind: 'mp4' | 'webm'
  mime: 'video/mp4' | 'video/webm'
  container: string
  duration_ms: number
  video_streams: VideoStream[]
  audio_codecs: string[]
  tool: MediaToolIdentity
  actual_probe_version: string
  fingerprint: string
  cleanup_status: string
  structural_validation: 'bounded_ffprobe_container_streams_validated'
  full_video_decoded: false
  native_provider_support_verified: false
}
export type VideoConversionPlan = {
  original_id: string
  original_version: number
  original_sha256: string
  filename: string
  mime: 'video/mp4' | 'video/webm'
  byte_length: number
  inspection_fingerprint: string
  selection: VideoSelection
  output_width: number
  output_height: number
  frame_count: number
  request_original_count: number
  request_total_video_frames: number
  probe_tool: MediaToolIdentity
  conversion_tool: MediaToolIdentity
  pillow_version: string
  conversion_settings_sha256: string
  fingerprint: string
  loss_notes: string[]
  converter: 'bounded-local-video-v1'
  approval_required: true
  sending_approved: false
}
/** Opaque host approval, NOT a native process owner or final model-send approval. */
export type VideoConversionPreview = {
  source: VideoSource
  probe: VideoProbe
  plan: VideoConversionPlan
  token: string
  fingerprint: string
  expires_at_ms: number
  conversion_performed: false
  model_sent: false
  sending_requires_separate_approval: true
}
export type VideoFrameDescriptor = {
  sha256: string
  mime: 'image/png'
  bytes: number
  width: number
  height: number
  locator: { timestamp_ms: string; frame: string }
  original_id: string
  original_version: number
  original_sha256: string
  conversion_settings_sha256: string
  converter: 'bounded-local-video-v1'
}
/** Frame content remains host-held; any UI preview URL needs a separately scoped host endpoint. */
export type VideoExtractionDescriptor = {
  input_id: string
  fingerprint: string
  source: VideoSource
  conversion_plan_fingerprint: string
  conversion_settings_sha256: string
  frames: VideoFrameDescriptor[]
  frame_count: number
  content_bytes: number
  loss_notes: string[]
  sending_approved: false
  requires_final_content_preview_and_send_approval: true
}

const SHA = /^[0-9a-f]{64}$/
const ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/
const obj = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v)
const integer = (v: unknown, low: number, high: number): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= low && v <= high
const text = (v: unknown, maximum = 500): v is string => typeof v === 'string' && v.length > 0 && v.length <= maximum && !/[\u0000-\u001f\u007f]/.test(v)
const sha = (v: unknown): v is string => typeof v === 'string' && SHA.test(v)
const exact = (v: Record<string, unknown>, keys: string[]) => Object.keys(v).length === keys.length && Object.keys(v).every(k => keys.includes(k))
export class ModelVideoError extends Error {
  constructor(message: string, public code = 'invalid_video_selection') { super(message); this.name = 'ModelVideoError' }
}
function refuse(message: string): never { throw new ModelVideoError(message) }

export function videoSource(value: VideoSource): VideoSource {
  if (!obj(value) || typeof value.fid !== 'string' || !ID.test(value.fid) || !integer(value.session_id, 1, Number.MAX_SAFE_INTEGER)
    || !integer(value.original_version, 1, Number.MAX_SAFE_INTEGER) || !text(value.filename, 255)
    || /[\\/:]/.test(value.filename) || /[ .]$/.test(value.filename)
    || !integer(value.size, 1, VIDEO_LIMITS.originalBytes) || !sha(value.sha256)) refuse('视频原件身份、版本或大小无效；每原件最多15 MiB')
  const extension = value.filename.split('.').pop()?.toLowerCase()
  if (!(extension === 'mp4' && value.mime === 'video/mp4' || extension === 'webm' && value.mime === 'video/webm')) refuse('只允许扩展名和MIME一致的MP4/WebM；容器仍必须由宿主真实探测')
  return { fid: value.fid, session_id: value.session_id, original_version: value.original_version, filename: value.filename, mime: value.mime, size: value.size, sha256: value.sha256 }
}

export function parseVideoTimestamps(raw: string): number[] {
  if (typeof raw !== 'string' || raw.length > 256 || !raw.trim()) refuse('必须明确填写毫秒时间点；留空不表示全部帧')
  const fields = raw.split(',').map(v => v.trim())
  if (fields.length > VIDEO_LIMITS.framesPerRequest || fields.some(v => !/^(0|[1-9][0-9]{0,7})$/.test(v))) refuse('用英文逗号填写0起始的整数毫秒时间点；不接受范围、NaN、Infinity或小数')
  const values = fields.map(Number)
  if (values.some((v, i) => !integer(v, 0, VIDEO_LIMITS.durationMs - 1) || i > 0 && values[i - 1] >= v)) refuse('时间点必须递增、不重复并处于视频时长范围内')
  return values
}

/** Display-only arithmetic for trusted host receipts, never browser submission authority. */
export function videoRequestBudget(originalCount: number, otherFrames: number, selectedFrames: number): number {
  if (!integer(originalCount, 1, VIDEO_LIMITS.originals)) refuse('整次提交最多8个原件；不能由每个组件分别计算')
  if (!integer(otherFrames, 0, VIDEO_LIMITS.framesPerRequest) || !integer(selectedFrames, 1, VIDEO_LIMITS.framesPerRequest)
    || otherFrames + selectedFrames > VIDEO_LIMITS.framesPerRequest) refuse('整次提交所有视频合计最多16帧，超限拒绝而非截断')
  return otherFrames + selectedFrames
}

export function videoSelection(value: VideoSelection, probe: VideoProbe): VideoSelection {
  if (!obj(value) || !exact(value, ['timestamps_ms', 'frame_count', 'max_width', 'max_height', 'stream_index', 'output_format'])
    || !Array.isArray(value.timestamps_ms) || !integer(value.frame_count, 1, VIDEO_LIMITS.framesPerRequest)
    || value.timestamps_ms.length !== value.frame_count || !integer(value.max_width, 1, VIDEO_LIMITS.outputDimension)
    || !integer(value.max_height, 1, VIDEO_LIMITS.outputDimension) || !integer(value.stream_index, 0, 7)
    || value.output_format !== 'png') refuse('视频选区、帧数或输出尺寸无效')
  const stream = probe.video_streams.find(v => v.index === value.stream_index)
  if (!stream) refuse('必须明确选择真实探测得到的视频流')
  const duration = Math.min(probe.duration_ms, stream.duration_ms ?? probe.duration_ms)
  if (value.timestamps_ms.some((v, i) => !integer(v, 0, duration - 1) || i > 0 && value.timestamps_ms[i - 1] >= v)) refuse('时间点必须递增且小于所选视频流时长；不自动补帧或选择全部')
  return { ...value, timestamps_ms: [...value.timestamps_ms] }
}

export function validateVideoProbe(value: unknown, source: VideoSource): VideoProbe {
  videoSource(source)
  if (!obj(value) || !exact(value, ['original_sha256', 'byte_length', 'kind', 'mime', 'container', 'duration_ms', 'video_streams', 'audio_codecs', 'tool', 'actual_probe_version', 'fingerprint', 'cleanup_status', 'structural_validation', 'full_video_decoded', 'native_provider_support_verified']) || value.original_sha256 !== source.sha256 || value.byte_length !== source.size
    || value.mime !== source.mime || value.kind !== (source.mime === 'video/mp4' ? 'mp4' : 'webm')
    || value.container !== (source.mime === 'video/mp4' ? 'mov,mp4,m4a,3gp,3g2,mj2' : 'matroska,webm')
    || !integer(value.duration_ms, 1, VIDEO_LIMITS.durationMs) || !sha(value.fingerprint)
    || !Array.isArray(value.video_streams) || !value.video_streams.length || value.video_streams.length > 4
    || !Array.isArray(value.audio_codecs) || value.audio_codecs.length > 4
    || !text(value.actual_probe_version, 200) || !text(value.cleanup_status, 100)
    || value.structural_validation !== 'bounded_ffprobe_container_streams_validated'
    || value.full_video_decoded !== false || value.native_provider_support_verified !== false) refuse('没有收到绑定当前原件的真实容器探测报告；magic-only不能批准原生视频')
  const codecs = source.mime === 'video/mp4' ? ['h264', 'hevc', 'av1'] : ['vp8', 'vp9', 'av1']
  const audio = source.mime === 'video/mp4' ? ['aac', 'mp3', 'opus'] : ['opus', 'vorbis']
  const seen = new Set<number>()
  for (const stream of value.video_streams) {
    if (!obj(stream) || !exact(stream, ['index', 'codec', 'width', 'height', 'duration_ms', 'average_frame_rate']) || !integer(stream.index, 0, 7) || seen.has(stream.index) || !codecs.includes(String(stream.codec))
      || !integer(stream.width, 1, 8192) || !integer(stream.height, 1, 8192) || stream.width * stream.height > 16777216
      || stream.duration_ms !== null && !integer(stream.duration_ms, 1, VIDEO_LIMITS.durationMs)
      || !text(stream.average_frame_rate, 24)) refuse('视频流编号、编码、尺寸或时长无效')
    const fps = /^([0-9]{1,9})\/([0-9]{1,9})$/.exec(stream.average_frame_rate)
    if (!fps || Number(fps[2]) === 0 || Number(fps[1]) / Number(fps[2]) <= 0 || Number(fps[1]) / Number(fps[2]) > 240) refuse('视频流帧率未知或超出硬限')
    seen.add(stream.index)
  }
  if (value.audio_codecs.some(v => !audio.includes(String(v)))) refuse('音频编码不在受控视频探测白名单内')
  validateTool(value.tool, 'ffprobe')
  return structuredClone(value) as unknown as VideoProbe
}

function validateTool(value: unknown, name: MediaToolIdentity['name']): asserts value is MediaToolIdentity {
  if (!obj(value) || !exact(value, ['name', 'version', 'sha256', 'dependency_manifest_sha256']) || value.name !== name
    || !text(value.version, 200) || !sha(value.sha256) || !sha(value.dependency_manifest_sha256)) refuse('预览缺少已审工具版本、二进制SHA或依赖清单指纹')
}
const equal = (a: unknown, b: unknown): boolean => {
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => equal(v, b[i]))
  if (obj(a) && obj(b)) return Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(k => Object.prototype.hasOwnProperty.call(b, k) && equal(a[k], b[k]))
  return a === b
}

/** Shape/binding checks protect the UI only. Host-held token/result is still mandatory. */
export function validateVideoPreview(value: unknown, source: VideoSource, selection: VideoSelection, now = Date.now()): VideoConversionPreview {
  if (!obj(value) || !exact(value, ['source', 'probe', 'plan', 'token', 'fingerprint', 'expires_at_ms', 'conversion_performed', 'model_sent', 'sending_requires_separate_approval']) || !obj(value.plan) || !equal(value.source, videoSource(source))
    || typeof value.token !== 'string' || !/^[A-Za-z0-9_-]{32,128}$/.test(value.token)
    || !sha(value.fingerprint) || !integer(value.expires_at_ms, 1, Number.MAX_SAFE_INTEGER)
    || !Number.isFinite(now) || value.expires_at_ms <= now || value.expires_at_ms > now + 300000
    || value.conversion_performed !== false || value.model_sent !== false || value.sending_requires_separate_approval !== true) refuse('视频转换预览未知、过期或不属于当前原件；未批准抽帧')
  const probe = validateVideoProbe(value.probe, source)
  const selected = videoSelection(selection, probe)
  const plan = value.plan
  const stream = probe.video_streams.find(v => v.index === selected.stream_index)!
  let numerator = 1, denominator = 1
  for (const [n, d] of [[selected.max_width, stream.width], [selected.max_height, stream.height]]) {
    if (n * denominator < numerator * d) { numerator = n; denominator = d }
  }
  if (!exact(plan, ['original_id', 'original_version', 'original_sha256', 'filename', 'mime', 'byte_length', 'inspection_fingerprint', 'selection', 'output_width', 'output_height', 'frame_count', 'request_original_count', 'request_total_video_frames', 'probe_tool', 'conversion_tool', 'pillow_version', 'conversion_settings_sha256', 'fingerprint', 'loss_notes', 'converter', 'approval_required', 'sending_approved'])
    || plan.original_id !== source.fid || plan.original_version !== source.original_version || plan.original_sha256 !== source.sha256
    || plan.filename !== source.filename || plan.mime !== source.mime || plan.byte_length !== source.size
    || plan.inspection_fingerprint !== probe.fingerprint || !equal(plan.selection, selected) || plan.frame_count !== selected.frame_count
    || !integer(plan.request_original_count, 1, VIDEO_LIMITS.originals) || !integer(plan.request_total_video_frames, selected.frame_count, VIDEO_LIMITS.framesPerRequest)
    || !integer(plan.output_width, 1, selected.max_width) || !integer(plan.output_height, 1, selected.max_height)
    || plan.output_width !== Math.max(1, Math.floor(stream.width * numerator / denominator)) || plan.output_height !== Math.max(1, Math.floor(stream.height * numerator / denominator))
    || !sha(plan.fingerprint) || !sha(plan.conversion_settings_sha256) || plan.converter !== 'bounded-local-video-v1'
    || plan.approval_required !== true || plan.sending_approved !== false || !text(plan.pillow_version, 100)
    || !Array.isArray(plan.loss_notes) || plan.loss_notes.length < 4 || plan.loss_notes.length > 20
    || plan.loss_notes.some(v => !text(v, 500)) || !equal(plan.probe_tool, probe.tool)) refuse('抽帧计划与原件、时间点、帧数、输出尺寸或整次硬限不一致')
  validateTool(plan.probe_tool, 'ffprobe')
  validateTool(plan.conversion_tool, 'ffmpeg')
  return structuredClone(value) as unknown as VideoConversionPreview
}

export function videoPreviewProblem(preview: VideoConversionPreview, now = Date.now()): string {
  return !Number.isFinite(now) || now >= preview.expires_at_ms ? '本次抽帧批准已过期，请重新探测和预览；不自动重试' : ''
}
