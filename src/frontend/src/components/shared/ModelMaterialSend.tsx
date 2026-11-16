import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, LoaderCircle, Send, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ModelVideoSelection } from '@/components/shared/ModelVideoSelection'
import { videoSource } from '@/lib/modelVideo'
import type { VideoConversionPreview, VideoSelection, VideoSource } from '@/lib/modelVideo'
import { documentConversionKind, ModelDocumentError, parseDocumentIndices, parseDocumentSheets, parsePdfPages } from '@/lib/modelDocuments'
import {
  createModelMaterialSendClient, emptyModelMaterialSelection, isAvailableModelHostRoute,
  modelMaterialInputProblem, ModelMaterialSendError, validateModelMaterialAttachments,
} from '@/lib/model-material-send'
import type {
  ApprovedModelMaterial, FinalModelSubmissionPreview, MaterialDelivery, MaterialInspection,
  AvailableModelHostRoute, MaterialPart, MaterialPreview, ModelHostEffort, ModelHostRoute, ModelMaterialAttachment,
  ModelMaterialSendResult, ModelMaterialSourceHandoff, PreparedModelMaterialSource, PreparedModelSubmission,
} from '@/lib/model-material-send'

export type ModelMaterialSendProps = {
  sessionId: number
  /** Current Attachment metadata. SHA may be absent until host inspect; never use placeholder hashes. */
  attachments: readonly ModelMaterialAttachment[]
  /** Read-only snapshot. This component never changes or clears the composer's draft. */
  text: string
  connected: boolean
  onResult?: (result: ModelMaterialSendResult) => void
  /** Trusted handoff receipt only; this callback never binds a plugin or approves execution/sending. */
  onHandoff?: (source: ModelMaterialSourceHandoff) => void
  onCancel?: () => void
}
const deliveryNames: Record<MaterialDelivery, string> = {
  native: '原件直传（不转换）', extracted_text: '明确选区提取文字', video_frame: '视频明确取帧',
}
const protocolNames: Record<string, string> = {
  openai_chat: 'OpenAI Chat Completions', openai_responses: 'OpenAI Responses',
  anthropic_messages: 'Anthropic Messages', gemini_content: 'Gemini GenerateContent',
}
const effortNames: Record<ModelHostEffort, string> = {
  none: 'none · 无思考参数强度', minimal: 'minimal · 最低', low: 'low · 低',
  medium: 'medium · 中', high: 'high · 高', xhigh: 'xhigh · 最高',
}
const panel = 'space-y-3 rounded-xl border border-border bg-card/65 p-4'
const selectStyle = 'mt-1 h-9 w-full rounded-md border border-border bg-background px-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/30 disabled:opacity-50'
const preStyle = 'max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-md border border-border bg-background/70 p-3 text-xs leading-6'
const hashStyle = 'break-all font-mono text-[11px] text-muted-foreground'

function SourceDetails({ source }: { source: MaterialInspection['source'] }) {
  return <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs leading-6">
    <dt className="text-muted-foreground">原件</dt><dd className="break-all">{source.filename}</dd>
    <dt className="text-muted-foreground">媒体类型</dt><dd className="break-all">{source.mime}</dd>
    <dt className="text-muted-foreground">实际大小</dt><dd>{source.size.toLocaleString()} bytes · 不可变原件版本 {source.original_version}</dd>
    <dt className="text-muted-foreground">会话 / fid</dt><dd className="break-all">{source.session_id} / {source.fid}</dd>
    <dt className="text-muted-foreground">SHA256</dt><dd className={hashStyle}>{source.sha256}</dd>
    <dt className="text-muted-foreground">不可变身份指纹</dt><dd className={hashStyle}>{source.identity_fingerprint}（不是家族历史版本号）</dd>
  </dl>
}
function Notes({ notes }: { notes: readonly string[] }) {
  return notes.length ? <ul className="list-disc space-y-1 pl-5 text-xs leading-6">{notes.map((note, index) => <li className="whitespace-pre-wrap break-all" key={index}>{note}</li>)}</ul> : <p className="text-xs text-muted-foreground">宿主未列出额外损失说明；不代表已实测模型能力。</p>
}
function PartsDetails({ parts, texts }: { parts: readonly MaterialPart[]; texts: readonly string[] }) {
  let textIndex = 0
  return <div className="space-y-2">{parts.map((part, index) => {
    const content = part.kind === 'text' ? texts[textIndex++] : undefined
    return <details className="rounded-lg border border-border p-3 text-xs leading-6" key={`${index}:${part.part_sha256}`}>
      <summary className="cursor-pointer break-all font-medium">片段 {index + 1} · {deliveryNames[part.delivery]} · {part.mime} · {part.part_size.toLocaleString()} bytes</summary>
      <p className="break-all">原件 {part.opaque_id} · 版本 {part.original_version} · {part.original_size.toLocaleString()} bytes</p>
      <p className={hashStyle}>原件 SHA256：{part.original_sha256}</p>
      <p className={hashStyle}>本片段 SHA256：{part.part_sha256}</p>
      <p className="break-all">方法：{part.conversion_method} / {part.conversion_version}</p>
      <p className={hashStyle}>转换设置 SHA256：{part.conversion_settings_sha256}</p>
      <p className="break-all">定位：{Object.keys(part.locator).length ? Object.entries(part.locator).map(([key, value]) => `${key}=${value}`).join(' · ') : '完整原件，无局部选区'}</p>
      <p className={hashStyle}>真实检查凭据指纹：{part.inspection_fingerprint}</p>
      <details><summary className="cursor-pointer">完整精确 manifest（纯元数据）</summary><pre tabIndex={0} className={preStyle}>{JSON.stringify(part, null, 2)}</pre></details>
      {content !== undefined ? <pre tabIndex={0} className={preStyle}>{content || '该片段是空文字。'}</pre> : <p className="text-muted-foreground">二进制不以 HTML、provider 内部块、base64 或外部 URL 渲染；以上精确字节与 SHA256 绑定最终发送内容。</p>}
      <Notes notes={part.loss_notes}/>
    </details>
  })}</div>
}
function RouteDetails({ route }: { route: AvailableModelHostRoute }) {
  return <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs leading-6">
    <dt className="text-muted-foreground">路由 / 修订</dt><dd className="break-all">{route.profile_id} / {route.profile_revision}</dd>
    <dt className="text-muted-foreground">服务商 / 模型</dt><dd className="break-all">{route.provider_id} / {route.model}</dd>
    <dt className="text-muted-foreground">协议</dt><dd>{protocolNames[route.protocol]}</dd>
    <dt className="text-muted-foreground">准确发送目标</dt><dd className="break-all font-mono">{route.endpoint}</dd>
    <dt className="text-muted-foreground">安全凭据引用</dt><dd className="break-all font-mono">{route.credential_ref}（仅系统凭据条目引用，不是 Key 或 Key 摘要）</dd>
    <dt className="text-muted-foreground">凭据 authority / 代际</dt><dd className="break-all">{route.credential_authority} · generation {route.credential_generation}（整数安全代际；不包含 Key）</dd>
    <dt className="text-muted-foreground">能力来源</dt><dd>{route.capability_source === 'verified_provider' ? '宿主提供此精确路由的已验证证据' : '仅宿主配置声明；未对服务端实际调用验证'}</dd>
    <dt className="text-muted-foreground">声明输入</dt><dd className="break-all">{route.supported_kinds.join('、') || '无'}</dd>
    <dt className="text-muted-foreground">声明原生类型</dt><dd className="break-all">{route.native_mimes.join('、') || '无'}</dd>
  </dl>
}

type MaterialClient = ReturnType<typeof createModelMaterialSendClient>
function VideoFrameGallery({ material, client, onReady }: { material: ApprovedModelMaterial; client: MaterialClient; onReady: (materialId: string, fingerprint: string, ready: boolean) => void }) {
  const [images, setImages] = useState<{ frame: number; timestamp: string; url: string }[]>([])
  const [problem, setProblem] = useState('')
  const loaded = useRef(new Set<number>())
  const displayFailed = useRef(false)
  useEffect(() => {
    const abort = new AbortController(), urls: string[] = []
    let active = true
    displayFailed.current = false; loaded.current.clear(); setImages([]); setProblem(''); onReady(material.material_id, material.fingerprint, false)
    void (async () => {
      const frames = material.parts.filter(part => part.delivery === 'video_frame')
      if (!frames.length || frames.length > 16) throw new ModelMaterialSendError('实际帧数量无效；不截断或伪造展示')
      const next: { frame: number; timestamp: string; url: string }[] = []
      // Bounded sequential reads avoid retaining a second unbounded response set.
      for (const part of frames) {
        const frame = Number(part.locator.frame)
        const blob = await client.videoFrameBlob(material, frame, abort.signal)
        if (!active || abort.signal.aborted) return
        const url = URL.createObjectURL(blob); urls.push(url)
        next.push({ frame, timestamp: part.locator.timestamp_ms, url })
      }
      if (active) setImages(next)
    })().catch(() => {
      for (const url of urls.splice(0)) URL.revokeObjectURL(url)
      if (active) { setImages([]); setProblem('实际PNG帧读取未获可信回执；没有自动重试或发送。可撤销登记后重新明确处理。') }
    })
    return () => { active = false; abort.abort(); for (const url of urls) URL.revokeObjectURL(url); onReady(material.material_id, material.fingerprint, false) }
  }, [client, material, onReady])
  return <section aria-label="已批准转换的实际视频PNG帧" className="space-y-2">
    <h5 className="text-xs font-medium">实际宿主持有帧 · 仅同源鉴权PNG Blob（非原件播放、非外部URL）</h5>
    {problem ? <p role="alert" className="text-xs text-destructive">{problem}</p> : images.length ? <div className="grid gap-3 sm:grid-cols-2">{images.map(image => <figure key={image.frame} className="rounded border border-border p-2"><img src={image.url} alt={`实际抽帧 ${image.frame}，明确请求时间点 ${image.timestamp} ms`} className="h-auto max-w-full" onLoad={() => { loaded.current.add(image.frame); if (!displayFailed.current && loaded.current.size === material.frame_count) onReady(material.material_id, material.fingerprint, true) }} onError={() => { displayFailed.current = true; loaded.current.delete(image.frame); for (const item of images) URL.revokeObjectURL(item.url); setProblem('实际PNG帧未能完整显示；禁止采用未展示结果发送，不自动重试。'); onReady(material.material_id, material.fingerprint, false) }}/><figcaption className="text-xs">帧 {image.frame} · 请求时间点 {image.timestamp} ms</figcaption></figure>)}</div> : <p role="status" className="text-xs">正在读取并核验全部实际PNG帧（最多16），不自动发送模型…</p>}
  </section>
}

/** Separate final-send gate. No legacy chat API, automatic history, draft mutation or automatic send. */
export function ModelMaterialSend({ sessionId, attachments, text, connected, onResult, onHandoff, onCancel }: ModelMaterialSendProps) {
  const clientRef = useRef<ReturnType<typeof createModelMaterialSendClient> | null>(null)
  if (!clientRef.current) clientRef.current = createModelMaterialSendClient()
  const client = clientRef.current
  const [scopeId, setScopeId] = useState('')
  const [routes, setRoutes] = useState<ModelHostRoute[]>([])
  const [profileId, setProfileId] = useState('')
  const [effort, setEffort] = useState('')
  const [maxOutput, setMaxOutput] = useState('')
  const [inspection, setInspection] = useState<MaterialInspection>()
  const [delivery, setDelivery] = useState<MaterialDelivery | ''>('')
  const [indices, setIndices] = useState(''), [sheets, setSheets] = useState('')
  const [includeHidden, setIncludeHidden] = useState(false), [characterLimit, setCharacterLimit] = useState(100000)
  const [materialPreview, setMaterialPreview] = useState<MaterialPreview>()
  const [materialAck, setMaterialAck] = useState(false)
  const [materials, setMaterials] = useState<ApprovedModelMaterial[]>([])
  const [readyVideoFrames, setReadyVideoFrames] = useState<Record<string, string>>({})
  const videoFramesReady = useCallback((materialId: string, fingerprint: string, ready: boolean) => {
    setReadyVideoFrames(current => {
      if (ready && current[materialId] === fingerprint || !ready && !(materialId in current)) return current
      const next = { ...current }
      if (ready) next[materialId] = fingerprint
      else delete next[materialId]
      return next
    })
  }, [])
  const [prepared, setPrepared] = useState<PreparedModelSubmission>()
  const [pluginSource, setPluginSource] = useState<PreparedModelMaterialSource>()
  const [handoff, setHandoff] = useState<ModelMaterialSourceHandoff>()
  const [handoffAck, setHandoffAck] = useState(false), [handoffAttempted, setHandoffAttempted] = useState(false)
  const [sourceRevokeAck, setSourceRevokeAck] = useState(false), [sourceRevokeAttempted, setSourceRevokeAttempted] = useState(false)
  const [sourceRevocation, setSourceRevocation] = useState('')
  const [finalPreview, setFinalPreview] = useState<FinalModelSubmissionPreview>()
  const [sendAck, setSendAck] = useState(false), [unverifiedAck, setUnverifiedAck] = useState(false), [httpAck, setHttpAck] = useState(false)
  const [result, setResult] = useState<ModelMaterialSendResult>()
  const [busy, setBusy] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [outcomeUnknown, setOutcomeUnknown] = useState(false), [closed, setClosed] = useState(false)
  const [, setTick] = useState(0)
  const generation = useRef(0), sequence = useRef(0), lock = useRef(false)
  const controller = useRef<AbortController | null>(null)
  const liveScope = useRef(''), scopeIdentity = useRef(''), livePrepared = useRef<PreparedModelSubmission | undefined>(undefined)
  const executing = useRef(false), uncertain = useRef(false), cancelling = useRef(false)
  const liveHandoff = useRef<ModelMaterialSourceHandoff | undefined>(undefined), transferAttempted = useRef(false)
  const transferSource = useRef<PreparedModelMaterialSource | undefined>(undefined), sourceRevokeConsumed = useRef(false)
  const liveMaterialPreview = useRef<MaterialPreview | undefined>(undefined)
  liveMaterialPreview.current = materialPreview
  const callbacks = useRef({ onResult, onHandoff, onCancel })
  callbacks.current = { onResult, onHandoff, onCancel }
  const identity = JSON.stringify([sessionId, connected, text, attachments.map(v => [v.fid, v.name, v.mime, v.size, v.sha256])])
  const liveIdentity = useRef(identity)
  liveIdentity.current = identity
  livePrepared.current = prepared
  let sourceProblem = ''
  try { validateModelMaterialAttachments(sessionId, attachments) } catch (cause) { sourceProblem = cause instanceof Error ? cause.message : '原件元数据不可信' }
  const selectedRoute = routes.filter(isAvailableModelHostRoute).find(v => v.profile_id === profileId)

  const clearApprovalChecks = () => { setSendAck(false); setUnverifiedAck(false); setHttpAck(false) }
  const revokeDetached = (id: string) => {
    void client.revokeScope(id).catch(() => {
      // No stale async response may publish into a new session. A later user action must establish a new scope.
    })
  }
  useEffect(() => {
    generation.current++; sequence.current++; lock.current = false
    controller.current?.abort(); controller.current = null
    client.invalidate()
    const oldScope = liveScope.current
    liveScope.current = ''
    if (oldScope) revokeDetached(oldScope)
    setScopeId(''); setRoutes([]); setProfileId(''); setEffort(''); setMaxOutput('')
    setInspection(undefined); setDelivery(''); setIndices(''); setSheets(''); setIncludeHidden(false); setCharacterLimit(100000)
    setMaterialPreview(undefined); setMaterialAck(false); setMaterials([]); setReadyVideoFrames({})
    const transferPending = transferAttempted.current
    setPrepared(undefined); livePrepared.current = undefined; setPluginSource(transferPending ? transferSource.current : undefined); setFinalPreview(undefined); clearApprovalChecks()
    setHandoffAck(false); setSourceRevokeAck(false); setHandoffAttempted(transferPending)
    if (!transferPending) {
      setHandoff(undefined); liveHandoff.current = undefined; transferSource.current = undefined
      setSourceRevokeAttempted(false); sourceRevokeConsumed.current = false; setSourceRevocation('')
    }
    setResult(undefined); setBusy(''); setError(''); setClosed(false)
    const unresolved = executing.current || uncertain.current
    uncertain.current = unresolved
    setOutcomeUnknown(unresolved)
    setNotice(transferPending ? liveHandoff.current ? '本地草稿、附件或会话已变化；已交接的 payload 不会随之修改或自动取消。本轮保持冻结，来源只受原到期时间与宿主真实身份／路由／资料检查约束；请在同一认证用户的插件页重新读取并明确选择。' : '交接期间本地来源或会话已变化，完成结果未知；本轮保持冻结，不声称来源仍有效，不自动重试，也不因本地变化再次取消已尝试交接的 request。可单独明确请求撤销。' : unresolved ? '会话、正文或原件在发送期间变化，或旧请求仍未确认。结果未知，可能已经外发或计费；旧 scope 已请求撤销，不自动重试。' : '')
    executing.current = false
    return () => {
      generation.current++; sequence.current++; lock.current = false
      controller.current?.abort(); controller.current = null
      client.invalidate()
      const previous = liveScope.current
      liveScope.current = ''
      if (previous) revokeDetached(previous)
    }
    // Before transfer, exact draft/attachment/session changes revoke the whole scope.
    // After transfer, cleanup still asks revokeScope, but never cancels or rewrites the handed-off request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity, client])
  useEffect(() => {
    if (closed || !materialPreview && !finalPreview && !pluginSource && !handoff) return
    const timer = window.setInterval(() => setTick(v => v + 1), 1000)
    return () => window.clearInterval(timer)
  }, [materialPreview, finalPreview, pluginSource, handoff, closed])

  const run = (label: string, action: (signal: AbortSignal, publish: (change: () => void) => void) => Promise<void>) => {
    if (lock.current || !connected || closed || outcomeUnknown || sourceProblem || transferAttempted.current || liveScope.current && scopeIdentity.current !== identity) return
    lock.current = true; setBusy(label); setError(''); setNotice('')
    const owner = generation.current, request = ++sequence.current, startedIdentity = identity
    const abort = new AbortController(); controller.current = abort
    const current = () => generation.current === owner && sequence.current === request && liveIdentity.current === startedIdentity && !abort.signal.aborted
    const publish = (change: () => void) => { if (current()) change() }
    void action(abort.signal, publish).catch(cause => {
      publish(() => {
        setError(cause instanceof ModelMaterialSendError || cause instanceof ModelDocumentError ? cause.message : '操作未获可信回执；没有自动发送或重试')
        if (cause instanceof ModelMaterialSendError && cause.outcomeUnknown) { uncertain.current = true; setOutcomeUnknown(true) }
      })
    }).finally(() => {
      if (generation.current === owner && sequence.current === request) {
        lock.current = false; controller.current = null; setBusy('')
      }
    })
  }
  const clearFinal = () => {
    if (transferAttempted.current) return
    const old = livePrepared.current
    setPrepared(undefined); livePrepared.current = undefined; setPluginSource(undefined); setHandoffAck(false); setFinalPreview(undefined); clearApprovalChecks(); setResult(undefined)
    if (old) {
      const owner = generation.current, startedIdentity = identity
      void client.cancelSubmission(old.scope_id, old.request_id).catch(() => {
        if (generation.current === owner && liveIdentity.current === startedIdentity) setError('旧准备请求的宿主取消回执未确认；旧 token 已在本组件丢弃，不会沿用或自动发送。可取消整轮以撤销 scope。')
      })
    }
  }
  const clearMaterialPreview = () => {
    if (liveMaterialPreview.current) client.discardMaterialPreview(liveMaterialPreview.current)
    liveMaterialPreview.current = undefined
    setMaterialPreview(undefined); setMaterialAck(false); clearFinal()
  }
  const openScope = () => run('正在读取真实模型路由与会话 scope…', async (signal, publish) => {
    if (liveScope.current) return
    const nextRoutes = await client.routes(signal)
    const id = await client.createScope(sessionId, signal)
    // Store before publish, so unmount always knows which real scope to revoke.
    if (signal.aborted || liveIdentity.current !== identity) { revokeDetached(id); return }
    liveScope.current = id; scopeIdentity.current = identity
    publish(() => { setScopeId(id); setRoutes(nextRoutes); setNotice(nextRoutes.some(isAvailableModelHostRoute) ? '请选择准确模型路由。尚未预检资料、批准转换或发送模型。' : '没有可用的明确模型路由；不可用配置保留诊断并禁止选择。请先在模型配置中处理；不会选择默认模型或自动降级。') })
  })
  const inspect = (attachment: ModelMaterialAttachment) => run('正在核验原件与可用处理方式（不发送模型）…', async (signal, publish) => {
    if (!scopeId) return
    clearMaterialPreview(); setInspection(undefined); setDelivery('')
    setIndices(''); setSheets(''); setIncludeHidden(false); setCharacterLimit(100000)
    const next = await client.inspect(scopeId, attachment, signal)
    publish(() => { setInspection(next); setNotice('只完成原件结构预检；请选择明确交付方式并核对选区，不自动转换或发送。') })
  })
  const previewMaterial = () => run('正在生成原件、选区和信息损失预览…', async (signal, publish) => {
    if (!scopeId || !inspection || !delivery) return
    const kind = documentConversionKind(inspection.source.filename)
    const selection = delivery === 'extracted_text' ? {
      paragraphs: kind === 'docx' ? parseDocumentIndices(indices, 50000) : [],
      slides: kind === 'pptx' ? parseDocumentIndices(indices, 200) : [],
      sheets: kind === 'xlsx' ? parseDocumentSheets(sheets) : [],
      pages: kind === 'pdf' ? parsePdfPages(indices) : [],
      include_hidden: ['xlsx', 'pptx'].includes(kind) && includeHidden,
      max_characters: characterLimit,
    } : emptyModelMaterialSelection()
    clearMaterialPreview()
    const next = await client.previewMaterial(scopeId, inspection, delivery, selection, signal)
    publish(() => { setMaterialPreview(next); setMaterialAck(false); setNotice('转换尚未执行；请完整核对原件、选区、方法及损失，再单独批准本地转换／登记。此批准不包含模型发送。') })
  })
  const approveMaterial = () => run('正在执行已批准的本地处理并登记准确结果（不发送模型）…', async (signal, publish) => {
    if (!materialPreview || !materialAck) return
    const snapshot = materialPreview
    setMaterialPreview(undefined); setMaterialAck(false)
    const value = await client.approveMaterial(snapshot, true, signal)
    const problem = modelMaterialInputProblem(text, [...materials, value])
    if (problem) { await client.revokeMaterial(scopeId, value.material_id, signal); throw new ModelMaterialSendError(problem) }
    publish(() => { setMaterials(values => [...values, value]); setInspection(undefined); setDelivery(''); clearFinal(); setNotice('本地处理与登记完成；下方是准确结果。尚未发送给模型，下一步必须独立预览完整发送内容。') })
  })
  // Stable callbacks are essential: ModelVideoSelection invalidates an old
  // approval when its integration changes, not on every parent status render.
  const videoBridge = useRef<{
    preview: (selection: VideoSelection, signal: AbortSignal) => Promise<VideoConversionPreview>
    approve: (preview: VideoConversionPreview, confirmed: true, signal: AbortSignal) => Promise<void>
  } | null>(null)
  const videoOperation = async <T,>(signal: AbortSignal, action: (active: AbortSignal) => Promise<T>): Promise<T> => {
    if (lock.current || !connected || closed || outcomeUnknown || sourceProblem || transferAttempted.current || scopeIdentity.current !== liveIdentity.current) throw new ModelMaterialSendError('当前会话忙碌、已变化、已交接或不可用；不尝试旧视频计划')
    lock.current = true; setBusy('正在执行一次明确的视频本地操作（不发送模型）…'); setError(''); clearFinal()
    const owner = generation.current, abort = new AbortController()
    const stop = () => abort.abort()
    controller.current = abort
    signal.addEventListener('abort', stop, { once: true })
    if (signal.aborted) abort.abort()
    try { return await action(abort.signal) }
    finally {
      signal.removeEventListener('abort', stop)
      if (generation.current === owner) { lock.current = false; controller.current = null; setBusy('') }
    }
  }
  videoBridge.current = {
    preview: (selection, signal) => videoOperation(signal, active => {
      if (!inspection || delivery !== 'video_frame') throw new ModelMaterialSendError('必须明确选择当前视频的抽帧交付')
      return client.previewVideo(scopeId, inspection, selection, active)
    }),
    approve: (preview, confirmed, signal) => videoOperation(signal, async active => {
      if (!inspection || delivery !== 'video_frame' || preview.source.fid !== inspection.source.fid) throw new ModelMaterialSendError('原件或交付已变化，旧抽帧批准不能使用')
      const owner = generation.current, identityAtStart = liveIdentity.current
      const value = await client.approveVideo(preview, confirmed, active)
      const problem = modelMaterialInputProblem(text, [...materials, value])
      if (problem || owner !== generation.current || identityAtStart !== liveIdentity.current || active.aborted) {
        await client.revokeMaterial(value.scope_id, value.material_id).catch(() => undefined)
        throw new ModelMaterialSendError(problem || '会话或选区变化；旧帧登记已请求撤销，没有批准模型发送')
      }
      setMaterials(values => [...values, value]); setInspection(undefined); setDelivery('')
      setNotice('已登记真实转换帧；下方通过鉴权二进制接口逐帧读取并核验PNG。原生视频与抽帧交付没有互换，模型发送仍须独立预览和批准。')
    }),
  }
  const previewVideo = useCallback((selection: VideoSelection, signal: AbortSignal) => videoBridge.current!.preview(selection, signal), [])
  const approveVideo = useCallback((preview: VideoConversionPreview, confirmed: true, signal: AbortSignal) => videoBridge.current!.approve(preview, confirmed, signal), [])
  const cancelVideoPreview = useCallback(async (preview: VideoConversionPreview) => { client.discardVideoPreview(preview) }, [client])
  const removeMaterial = (material: ApprovedModelMaterial) => run('正在撤销此资料的登记许可…', async (signal, publish) => {
    clearFinal()
    setMaterials(values => values.filter(v => v.material_id !== material.material_id))
    await client.revokeMaterial(scopeId, material.material_id, signal)
    publish(() => setNotice('资料登记已撤销；没有删除原件，也没有修改草稿。'))
  })
  const inputProblem = modelMaterialInputProblem(text, materials) || (materials.some(material => material.delivery === 'video_frame' && readyVideoFrames[material.material_id] !== material.fingerprint) ? '必须先成功读取并完整展示全部实际PNG帧，才能预览／批准模型发送；未加载或失败不会当作已经预览。' : '')
  const routeProblem = !selectedRoute ? '请主动选择一个准确模型路由' : materials.some(material => material.parts.some(part => part.kind !== 'text' && (!selectedRoute.supported_kinds.includes(part.kind) || !selectedRoute.native_mimes.includes(part.mime)))) ? '此准确路由未声明支持已登记资料的实际交付；原生视频仅Gemini明确video配置可选，静态帧需要image/png声明。不会默换路由或交付。' : !effort ? '请主动选择不附带思考参数，或一个明确档位' : !/^\d+$/.test(maxOutput) || Number(maxOutput) < 1 || Number(maxOutput) > selectedRoute.max_output_tokens ? `请明确填写输出 token 硬上限（1–${selectedRoute.max_output_tokens}）` : ''
  const pluginRouteProblem = selectedRoute && (!/^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$/.test(selectedRoute.profile_id) || !/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$/.test(selectedRoute.provider_id) || !/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$/.test(selectedRoute.model)) ? '当前路由标识不符合插件SDK准确描述符合同；不会重命名、代选或编造可绑定来源。' : Number(maxOutput) > 32768 ? '插件SDK最大输出预算至多32,768 tokens；请主动修改，不会自动压低普通发送参数。' : ''
  const prepare = () => run('正在本地编译独立请求与最终发送预览（不发送模型）…', async (signal, publish) => {
    if (!scopeId || !selectedRoute || routeProblem || inputProblem) return
    clearFinal()
    const receipt = await client.prepare(scopeId, { route: selectedRoute, text, materials, max_output_tokens: Number(maxOutput), effort: effort === 'omit' ? null : effort as ModelHostEffort }, signal)
    livePrepared.current = receipt
    publish(() => setPrepared(receipt))
    const preview = await client.previewSubmission(receipt, signal)
    publish(() => { setFinalPreview(preview); clearApprovalChecks(); setNotice('准确请求已经编译，但尚未外发。请逐项核对全文、原件/选区、模型、协议、字节及风险；不会自动发送。') })
  })
  const preparePluginSource = () => run('正在仅准备当前准确资料并核对真实插件 source（不生成外发批准）…', async (signal, publish) => {
    if (!scopeId || !selectedRoute || routeProblem || pluginRouteProblem || inputProblem) return
    clearFinal()
    const receipt = await client.prepare(scopeId, { route: selectedRoute, text, materials, max_output_tokens: Number(maxOutput), effort: effort === 'omit' ? null : effort as ModelHostEffort }, signal)
    livePrepared.current = receipt
    publish(() => setPrepared(receipt))
    // Deliberately never previewSubmission: a send-preview token makes the host record ineligible for plugin export.
    const source = await client.readPluginSource(receipt, signal)
    publish(() => { setPluginSource(source); clearApprovalChecks(); setNotice('当前正文与已批准的实际资料已在宿主编译，并由真实插件来源目录读回核验。未创建外发批准、未发送，也不授权插件执行；请在插件面板另行选择此 source。未完成显式 server-held 交接前必须保持此窗口，离开即撤销。') })
  })
  const handoffPluginSource = () => run('正在显式交接真实 server-held source（不生成外发批准）…', async (signal, publish) => {
    if (!pluginSource || !handoffAck || client.pluginSourceProblem(pluginSource)) return
    const source = pluginSource
    transferSource.current = source; transferAttempted.current = true; setHandoffAttempted(true); setHandoffAck(false)
    setPrepared(undefined); livePrepared.current = undefined; setFinalPreview(undefined); clearApprovalChecks()
    setMaterialPreview(undefined); liveMaterialPreview.current = undefined; setMaterialAck(false)
    setMaterials([]); setReadyVideoFrames({}); setInspection(undefined); setDelivery('')
    try {
      const receipt = await client.handoffPluginSource(source, true, signal)
      publish(() => {
        liveHandoff.current = receipt; setHandoff(receipt)
        setPrepared(undefined); livePrepared.current = undefined; setFinalPreview(undefined); clearApprovalChecks()
        setNotice('交接成功：精确九字段回执与真实 prepared/sources 二次读取均已核验。可关闭此窗口，在同一认证用户的 PluginModelPending 插件页读取并选择以上真实 scope/request。外发另行批准，最多原60秒剩余时间，不续期、不延长插件 child 原始截止时间。')
        try { callbacks.current.onHandoff?.({ ...receipt }) } catch { setError('可信交接回执已保留；调用方交接回调失败，不会重试、绑定插件或发送模型。') }
      })
    } catch (cause) {
      publish(() => setNotice('交接没有同时取得可信完成回执与真实来源二次核验，服务端可能已持有也可能未完成。本轮已冻结并丢弃本地批准；不声称来源仍有效，不自动重试或续期。关闭不会二次取消该 request；如需撤销，请使用下方独立确认的来源撤销操作。'))
      throw cause
    }
  })
  const execute = () => run('正在执行这一次明确批准的模型请求…', async (signal, publish) => {
    if (!finalPreview || !sendAck || inputProblem || finalPreview.requires_unverified_capabilities_acceptance && !unverifiedAck || finalPreview.plain_http && !httpAck) return
    const snapshot = finalPreview
    setFinalPreview(undefined); clearApprovalChecks(); executing.current = true
    try {
      const value = await client.execute(snapshot, { userConfirmed: true, acceptUnverifiedCapabilities: unverifiedAck, allowPlainHttp: httpAck }, signal)
      publish(() => {
        setResult(value); setPrepared(undefined); livePrepared.current = undefined
        setNotice('已获得可信的独立模型完成回执。未修改、清空或发送旧聊天草稿，也未自动加入历史、人格、记忆或工具。')
        try { callbacks.current.onResult?.(value) } catch { setError('结果已完成并保留在此；调用方结果回调失败，不会再次发送。') }
      })
    } finally { executing.current = false }
  })
  const cancelSource = async () => {
    const source = transferSource.current
    if (!source || !sourceRevokeAck || sourceRevokeConsumed.current || lock.current || cancelling.current || !connected || closed) return
    sourceRevokeConsumed.current = true; setSourceRevokeAttempted(true); setSourceRevokeAck(false)
    lock.current = true; setBusy('正在单独请求撤销已尝试交接的准确来源（不发送）…'); setError('')
    const owner = generation.current, request = ++sequence.current, startedIdentity = identity
    const abort = new AbortController(); controller.current = abort
    const current = () => generation.current === owner && sequence.current === request && liveIdentity.current === startedIdentity && !abort.signal.aborted
    try {
      const outcome = await client.cancelSource(source, true, abort.signal)
      if (current()) setSourceRevocation(outcome === 'not_sent' ? '已取得准确来源取消回执：宿主 request 已失效，未通过此 host request 发送。下游已有能力／在途操作仍受其自身撤销与重检约束；不能据此承诺撤回已外发字节。' : '已请求取消准确来源；下游在途外部结果可能未知，不能保证撤回已外发字节，不自动重试。')
    } catch {
      if (current()) setSourceRevocation('来源撤销已尝试但回执未知；不声称来源仍有效或已撤销，不自动重试。请到插件页重新读取真实状态；已有预发送能力可在插件页 prepared/revoke 单独撤销。')
    } finally {
      if (generation.current === owner && sequence.current === request) { lock.current = false; controller.current = null; setBusy('') }
    }
  }
  const cancel = async () => {
    if (cancelling.current || closed) return
    cancelling.current = true
    const owner = generation.current, startedIdentity = identity, oldScope = liveScope.current, old = livePrepared.current
    const transferred = liveHandoff.current, transferPending = transferAttempted.current
    const wasExecuting = executing.current || outcomeUnknown
    sequence.current++; controller.current?.abort(); controller.current = null; lock.current = false
    setBusy(transferPending ? '正在关闭窗口并释放本地许可（不取消已尝试交接的来源）…' : '正在请求取消并撤销当前 scope…'); setError('')
    clearApprovalChecks(); setMaterialAck(false); setMaterialPreview(undefined); setFinalPreview(undefined); setPluginSource(undefined)
    client.invalidate(); liveScope.current = ''; setScopeId(''); setClosed(true)
    let outcome = '', revokeFailed = false, scopeRetained = false
    if (old && !transferPending) {
      try { outcome = await client.cancelSubmission(old.scope_id, old.request_id) } catch { outcome = 'unknown' }
    }
    if (oldScope) { try { scopeRetained = !await client.revokeScope(oldScope) } catch { revokeFailed = true } }
    cancelling.current = false
    if (generation.current !== owner || liveIdentity.current !== startedIdentity) return
    setBusy(''); setMaterials([]); setPrepared(undefined); livePrepared.current = undefined; setInspection(undefined)
    uncertain.current = wasExecuting && outcome !== 'not_sent'
    setOutcomeUnknown(uncertain.current)
    setNotice(transferPending ? sourceRevokeConsumed.current ? '窗口已关闭；来源撤销仅以独立来源撤销回执为准，不重复取消，也不恢复任何本地许可。' : transferred ? '窗口已关闭；已明确交接的准确来源由宿主持有至原到期时间，不续期。这里只释放窗口本地许可，没有取消已交接来源，也没有批准插件执行或模型外发。' : scopeRetained ? '窗口已关闭；宿主明确返回 revoked:false，表示来源已交接并保留至原到期。本窗口缺少完整交接二次核验，不能声称当前可用；没有二次 cancelRequest，不自动重试。' : '窗口已关闭并释放本地许可；交接完成结果未确认，不声称来源仍有效或已取消，没有再次 cancelRequest，不自动重试。' : wasExecuting && outcome !== 'not_sent' ? '已停止采用后续响应并请求取消。取消不能撤回已外发字节；结果可能未知或已计费，不会自动重试。' : scopeRetained ? '宿主返回 revoked:false：来源已交给宿主持有至原到期，不能声称全部来源已撤销；未自动发送。' : '本轮发送许可已丢弃并请求撤销；原件和草稿均未修改。')
    if (revokeFailed) setError(transferred ? '本地窗口已关闭，但宿主 scope 释放回执未确认；已交接来源仍仅受原 TTL 与宿主有效性检查约束，不保证可用。' : '宿主 scope 撤销回执未确认；界面已封锁旧许可，不能据此声称来源已撤销或在途请求已撤回。')
    try { callbacks.current.onCancel?.() } catch { setError('取消已处理；调用方关闭回调失败，不会恢复旧许可或再次发送。') }
  }
  const materialProblem = materialPreview ? client.materialApprovalProblem(materialPreview) : ''
  const finalProblem = finalPreview ? client.sendApprovalProblem(finalPreview) : ''
  const pluginSourceProblem = pluginSource && !handoff && !handoffAttempted ? client.pluginSourceProblem(pluginSource) : ''
  const pluginSourceRemaining = handoff ? client.handoffRemaining(handoff) : pluginSource ? client.pluginSourceRemaining(pluginSource) : 0
  const kind = inspection ? documentConversionKind(inspection.source.filename) : ''
  let pdfSelectionProblem = ''
  const inspectionReport = inspection?.inspection
  if (inspectionReport?.kind === 'pdf' && delivery === 'extracted_text') {
    try {
      const pages = parsePdfPages(indices)
      if (pages.some(page => page > inspectionReport.pages)) pdfSelectionProblem = `PDF只有 ${inspectionReport.pages} 个物理页；请选择范围内的明确页码`
    } catch (cause) { pdfSelectionProblem = cause instanceof ModelDocumentError ? cause.message : 'PDF页选区无效，不能继续' }
  }
  const scopeStale = Boolean(scopeId) && scopeIdentity.current !== identity
  const disabled = Boolean(busy) || !connected || Boolean(sourceProblem) || closed || outcomeUnknown || scopeStale || handoffAttempted

  return <section aria-label="独立模型资料发送" className="space-y-4 text-sm leading-6">
    <header className="space-y-2 border-b border-border pb-4">
      <h2 className="flex items-center gap-2 text-lg font-semibold"><ShieldCheck className="size-5 text-primary"/>独立模型资料 · 最终发送</h2>
      <p className="text-xs text-muted-foreground">会话 {sessionId} 的独立请求。资料转换批准与最终外发批准严格分开；不使用旧聊天接口、不默认模型、不降级、不自动重试，不自动带入历史、人格、记忆或工具。</p>
      <p className="text-xs text-muted-foreground">硬限：每原件 15 MiB · 最多 8 原件 · 合计 16 帧 · 正文与资料合计 100,000 字。超限报错，不静默截断。</p>
    </header>
    {!connected && <p role="status" className="rounded-lg border border-border p-3 text-xs">离线、演示或连接核验中；本组件不会请求独立模型宿主。</p>}
    {sourceProblem && <p role="alert" className="whitespace-pre-wrap break-all text-xs text-destructive">{sourceProblem}</p>}
    {error && <p role="alert" className="whitespace-pre-wrap break-all rounded-lg border border-destructive/30 p-3 text-xs text-destructive">{error}</p>}
    {outcomeUnknown && <p role="alert" className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-xs"><AlertTriangle className="mt-1 size-4 shrink-0"/>旧请求结果未知，可能已经外发或计费。当前界面禁止再次发送；请先处理取消与核查，不会自动恢复或重试。</p>}
    {notice && <p role="status" className="whitespace-pre-wrap break-all text-xs text-muted-foreground">{notice}</p>}
    {!scopeId && !closed && <Button type="button" variant="outline" disabled={disabled} onClick={openScope}>读取明确路由并建立当前会话独立 scope（不发送）</Button>}
    {scopeId && !scopeStale && !handoffAttempted && <>
      <section className={panel} aria-label="准确模型与参数">
        <h3 className="font-semibold">1 · 明确选择模型与参数</h3>
        <p className={hashStyle}>真实 host_chat scope：{scopeId}（未交接前会话或正文/原件变化即请求撤销，不是 BrokerSession/PID）</p>
        <fieldset disabled={disabled || Boolean(result)} className="grid gap-3 sm:grid-cols-2">
          <label className="sm:col-span-2 text-xs">准确模型路由<select aria-label="独立发送模型路由" className={selectStyle} value={profileId} onChange={event => { clearFinal(); setProfileId(event.target.value); setEffort(''); setMaxOutput('') }}>
            <option value="">请选择，不使用默认模型</option>
            {routes.map(v => <option key={v.profile_id} value={v.profile_id} disabled={!v.available}>{v.name} · {v.provider_id} / {v.model || '未配置模型'} · {v.protocol ? protocolNames[v.protocol] : '不可用协议'} · 修订 {v.profile_revision}{v.available ? '' : ' · 不可选'}</option>)}
          </select></label>
          <label className="text-xs">思考参数<select aria-label="独立发送思考参数" className={selectStyle} value={effort} disabled={!selectedRoute} onChange={event => { clearFinal(); setEffort(event.target.value) }}>
            <option value="">请选择，不推断或默认</option><option value="omit">明确不附带思考参数</option>
            {selectedRoute?.supported_efforts.map(value => <option key={value} value={value}>{effortNames[value]}</option>)}
          </select></label>
          <label className="text-xs">最大输出 token<Input aria-label="独立发送最大输出token" className="mt-1" type="number" min={1} max={selectedRoute?.max_output_tokens} value={maxOutput} placeholder={selectedRoute ? `明确填写 1–${selectedRoute.max_output_tokens}` : '先选择模型'} disabled={!selectedRoute} onChange={event => { clearFinal(); setMaxOutput(event.target.value) }}/></label>
        </fieldset>
        {selectedRoute && <RouteDetails route={selectedRoute}/>}
        {routes.filter(v => !v.available).map(v => <p key={v.profile_id} role="status" className="break-all text-xs text-muted-foreground">不可用：{v.name}（{v.profile_id}）· {v.diagnostics.join(' · ')}；保留诊断，禁止选用。</p>)}
        {!routes.some(isAvailableModelHostRoute) && <p className="text-xs">没有可用路由。请先配置具体模型及安全凭据代际；不会代选、改协议或退回旧聊天。</p>}
      </section>
      <section className={panel} aria-label="原件转换与登记">
        <h3 className="font-semibold">2 · 逐原件预检 → 预览选区与损失 → 单独批准转换／登记</h3>
        {!attachments.length && <p className="text-xs text-muted-foreground">没有原件。本轮仅发送明确预览的正文。</p>}
        <ul className="space-y-2">{attachments.map(item => {
          const registered = materials.some(m => m.source.fid === item.fid)
          return <li key={item.fid} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border p-3">
            <div className="min-w-0 flex-1"><p className="break-all text-xs font-medium">{item.name}</p><p className="break-all text-xs text-muted-foreground">{item.mime} · {item.size.toLocaleString()} bytes</p><p className={hashStyle}>{item.sha256 ? `SHA256 ${item.sha256}` : '尚无客户端 SHA256；首次预检核对会话/fid/名称/MIME/大小后，由宿主返回真实不可变原件摘要；不使用占位值。'}</p></div>
            <Button type="button" size="sm" variant="outline" disabled={disabled || registered || Boolean(result)} onClick={() => inspect(item)}>{registered ? '已批准登记' : '预检此原件（不转换、不发送）'}</Button>
          </li>
        })}</ul>
        {inspection && <div className="space-y-3 rounded-lg border border-border p-3">
          <h4 className="font-medium">原件预检 · 未执行转换</h4><SourceDetails source={inspection.source}/>
          <p className={hashStyle}>检查指纹：{inspection.inspection_fingerprint}</p><Notes notes={inspection.inspection.loss_notes}/>
          <p className="break-all text-xs text-muted-foreground">真实结构状态：{inspection.inspection.structural_validation} · 原件类型 {inspection.inspection.kind}；不是模型支持已实测证据。</p>
          {inspection.inspection.kind === 'pdf' && <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs leading-6">
            <dt className="text-muted-foreground">真实物理页数</dt><dd>{inspection.inspection.ready_for_extraction ? inspection.inspection.pages : '未取得可信页数；不可提取'}</dd>
            <dt className="text-muted-foreground">已审核解析版本</dt><dd className="break-all">{inspection.inspection.parser_version}</dd>
            <dt className="text-muted-foreground">结构对象 / 节点</dt><dd>{inspection.inspection.pdf_objects} / {inspection.inspection.pdf_nodes}</dd>
            <dt className="text-muted-foreground">预检解码工作量</dt><dd>{inspection.inspection.pdf_decoded_work_bytes.toLocaleString()} bytes · 图像对象 {inspection.inspection.pdf_image_objects}（未OCR）</dd>
            <dt className="text-muted-foreground">交付 / 诊断</dt><dd className="break-all">{inspection.inspection.delivery} · {inspection.inspection.diagnostic ?? '无失败诊断；仅已有文字层，禁止PDF原件直传'}</dd>
          </dl>}
          <Notes notes={inspection.inspection.unsupported_features}/>
          <fieldset disabled={disabled || Boolean(result)} className="space-y-3">
            <label className="block text-xs">明确交付方式<select aria-label="模型资料交付方式" value={delivery} className={selectStyle} onChange={event => { clearMaterialPreview(); setDelivery(event.target.value as MaterialDelivery | '') }}>
              <option value="">请选择，不自动转换或降级</option>{inspection.deliveries.map(value => <option key={value.delivery} value={value.delivery} disabled={!value.available}>{deliveryNames[value.delivery]}{value.available ? '' : ' · 当前不可用，禁止选择'}</option>)}
            </select></label>
            {delivery === 'extracted_text' && <>
              {(kind === 'docx' || kind === 'pptx') && <label className="block text-xs">{kind === 'docx' ? '正文直接段落编号（表格内段落另行定位）' : '幻灯片顺序编号'}<Input className="mt-1" aria-label="模型资料编号选区" placeholder="留空明确表示全部符合条件项；或 1,2,5" value={indices} maxLength={5000} onChange={event => { clearMaterialPreview(); setIndices(event.target.value) }}/></label>}
              {kind === 'pdf' && <div className="space-y-2">
                <label className="block text-xs">PDF物理页码／页范围（必填，1起始；不会留空默认全PDF）<Input className="mt-1" aria-label="模型资料PDF页范围" placeholder="例如 1,3-5,8（明确选择这些物理页）" value={indices} maxLength={2000} onChange={event => { clearMaterialPreview(); setIndices(event.target.value) }}/></label>
                <p className="text-xs text-muted-foreground">逗号分隔页码或闭区间，按物理页升序提取；拒绝重叠、重复、倒序区间或超出真实页数。仅提取已选页已有文字层，不OCR、不读取未选页正文、不发送PDF原件。</p>
                {pdfSelectionProblem && <p role="status" className="text-xs text-destructive">{pdfSelectionProblem}</p>}
              </div>}
              {kind === 'xlsx' && <label className="block text-xs">工作表与单元格范围（每行一个，留空明确表示全部可见表）<textarea aria-label="模型资料工作表选区" className="mt-1 min-h-24 w-full rounded-md border border-border bg-background p-2" value={sheets} maxLength={20000} placeholder={'Sheet1|A1:C20\n销售记录|B2:D8'} onChange={event => { clearMaterialPreview(); setSheets(event.target.value) }}/></label>}
              {['xlsx', 'pptx'].includes(kind) && <label className="flex items-start gap-2 text-xs"><input type="checkbox" className="mt-1" checked={includeHidden} onChange={event => { clearMaterialPreview(); setIncludeHidden(event.target.checked) }}/>明确包含隐藏工作表／幻灯片（默认排除）</label>}
              <label className="block text-xs">文字提取硬上限（1–100,000 字；超限拒绝而非截断）<Input aria-label="模型资料文字硬上限" className="mt-1" type="number" min={1} max={100000} value={characterLimit} onChange={event => { clearMaterialPreview(); setCharacterLimit(Number(event.target.value)) }}/></label>
              <p className="text-xs text-muted-foreground">不运行 Office、宏、脚本、公式、嵌入对象或外部链接。PDF只有真实只读预检通过后才允许明确选页提取已有文字层，未选页不提取正文；不伪装OCR，不发送PDF原件。图片、图表视觉效果、样式和公式缓存请逐项核对损失说明。</p>
            </>}
            {(inspection.inspection.kind === 'mp4' || inspection.inspection.kind === 'webm') && <p className="text-xs">原生视频：保留完整原件字节，只在明确声明video与对应MIME的Gemini路由尝试；静态帧：仅按明确流／时间点转换PNG，丢音频和连续运动，可交付给声明image/png的路由。两条交付不会自动互换。</p>}
            {delivery === 'video_frame' && (inspection.inspection.kind === 'mp4' || inspection.inspection.kind === 'webm') && <ModelVideoSelection key={`${inspection.source.fid}:${inspection.inspection_fingerprint}`} source={videoSource(inspection.source as VideoSource)} probe={inspection.inspection.video_metadata ?? undefined} connected={connected && !closed && !outcomeUnknown && !scopeStale && !result && !handoffAttempted} onPreview={previewVideo} onApproveConversion={approveVideo} onCancelPreview={cancelVideoPreview}/>}
            {inspection.deliveries.filter(v => !v.available).map(v => <p key={v.delivery} className="break-all text-xs text-muted-foreground">{deliveryNames[v.delivery]}不可用：{v.code}；不会自动转换或降级。</p>)}
            {!inspection.deliveries.some(v => v.available) && <p className="text-xs">宿主没有提供已实现的交付方式；不会伪造转换结果。</p>}
            <Button type="button" size="sm" variant="outline" disabled={!delivery || delivery === 'video_frame' || Boolean(pdfSelectionProblem) || !Number.isInteger(characterLimit) || characterLimit < 1 || characterLimit > 100000} onClick={previewMaterial}>完整预览本地处理（尚不转换、不发送）</Button>
          </fieldset>
        </div>}
        {materialPreview && <div className="space-y-3 rounded-lg border border-primary/30 bg-primary/5 p-3">
          <h4 className="font-semibold">本次转换／原件登记的精确预览</h4><SourceDetails source={materialPreview.source}/>
          <p className="text-xs">方式：{deliveryNames[materialPreview.delivery]} · 方法：{materialPreview.converter}</p>
          <p className="text-xs">原件与选区绑定：{materialPreview.source.mime.startsWith('video/') ? '原生视频为完整原件，不适用文档选区；默认pages等空数组不表示抽帧。视频计划单独绑定真实探测与宿主预算。' : documentConversionKind(materialPreview.source.filename) === 'pdf' ? '只提取以下pages中明确列出的升序物理页；pages不可留空，不会静默扩大为全部PDF。其他格式的空选区不适用于PDF。' : '以下空数组表示按宿主规则处理全部符合条件项，不是仅当前屏幕片段。'}</p>
          <pre tabIndex={0} className={preStyle}>{JSON.stringify(materialPreview.selection, null, 2)}</pre>
          {materialPreview.plan && <details open><summary className="cursor-pointer text-xs font-medium">完整真实视频计划（探测／宿主预算／转换设置与工具）</summary><pre tabIndex={0} className={preStyle}>{JSON.stringify(materialPreview.plan, null, 2)}</pre></details>}
          <h5 className="text-xs font-medium">信息损失／未读取内容（须全部核对）</h5><Notes notes={materialPreview.loss_notes}/>
          <p className={hashStyle}>批准指纹：{materialPreview.fingerprint}</p>
          <p className="text-xs">尚未提取或外发；批准只允许这一次本地处理和准确结果登记。最终模型发送需要下一步单独预览与确认。</p>
          {materialProblem && <p role="alert" className="text-xs text-destructive">{materialProblem}</p>}
          <label className="flex items-start gap-2 text-xs"><input type="checkbox" className="mt-1" checked={materialAck} disabled={disabled || Boolean(materialProblem)} onChange={event => setMaterialAck(event.target.checked)}/>我已完整核对原件、选区、方法与信息损失；只批准这一次本地转换／登记，不批准发送模型。</label>
          <Button type="button" size="sm" disabled={disabled || !materialAck || Boolean(materialProblem)} onClick={approveMaterial}>明确批准转换／登记一次（不发送模型）</Button>
        </div>}
        {materials.map(material => <article className="space-y-3 rounded-lg border border-border p-3" key={material.material_id}>
          <h4 className="flex items-center gap-2 font-medium"><CheckCircle2 className="size-4 text-primary"/>{material.source.filename} · 已批准本地登记，未批准外发</h4>
          <p className="text-xs">{material.parts.length} 片段 · {material.content_bytes.toLocaleString()} bytes · {material.characters.toLocaleString()} 字 · {material.frame_count} 帧</p>
          <p className={hashStyle}>资料 {material.material_id} · {material.fingerprint}</p>
          <h5 className="text-xs font-medium">已批准选区（与结果绑定）</h5><pre tabIndex={0} className={preStyle}>{JSON.stringify(material.video_selection ?? material.selection, null, 2)}</pre><Notes notes={material.loss_notes}/>
          {material.delivery === 'video_frame' && <VideoFrameGallery material={material} client={client} onReady={videoFramesReady}/>}
          <PartsDetails parts={material.parts} texts={material.processed_text_parts}/>
          <Button type="button" size="sm" variant="outline" disabled={disabled || Boolean(result)} onClick={() => removeMaterial(material)}>撤销此资料登记（不删除原件）</Button>
        </article>)}
      </section>
      <section className={panel} aria-label="最终发送精确预览">
        <h3 className="font-semibold">3 · 最终独立发送预览</h3>
        <p className="text-xs text-muted-foreground">正文保持只读，按原样发送（不会 trim、附加旧引用或清空草稿）。只有已明确批准的 {materials.length} 个原件会进入请求；未登记的附件不会静默包含。</p>
        <pre tabIndex={0} className={preStyle}>{text || '（正文为空；仅在存在已批准资料时允许发送）'}</pre>
        {(routeProblem || inputProblem) && <p className="text-xs text-muted-foreground">{routeProblem || inputProblem}</p>}
        {attachments.length !== materials.length && <p className="text-xs text-muted-foreground">还有 {attachments.length - materials.length} 个原件尚未批准登记，本次最终预览不会包含它们。</p>}
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" disabled={disabled || Boolean(routeProblem) || Boolean(inputProblem) || Boolean(result)} onClick={prepare}>编译并预览准确请求（不发送模型）</Button>
          <Button type="button" variant="outline" disabled={disabled || Boolean(routeProblem) || Boolean(pluginRouteProblem) || Boolean(inputProblem) || Boolean(result)} onClick={preparePluginSource}>仅准备给插件选择（不生成外发批准）</Button>
        </div>
        {pluginRouteProblem && <p className="text-xs text-muted-foreground">仅插件来源路径受限：{pluginRouteProblem}</p>}
        <p className="text-xs text-muted-foreground">两条路径独立：插件来源只使用上方已批准的准确图片／PDF文字选页／视频原件或实际PNG帧／OOXML文字选区，不退回仅正文替代。再次编译、改变模型参数或资料会使旧来源失效；不会自动选插件、绑定描述符、调用插件或发送模型。</p>
        {prepared && !finalPreview && !pluginSource && !result && !busy && <p className="text-xs text-muted-foreground">请求已准备但没有有效最终批准或已核验的插件来源。可取消本轮；重新编译会先废弃旧准备，不复用旧 token。</p>}
        {pluginSource && <section aria-label="实际插件模型资料来源" className="space-y-3 rounded-xl border border-primary/40 p-4">
          <h4 className="font-semibold">真实 server-held source · 尚未交接、未外发、没有外发批准</h4>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs leading-6">
            <dt className="text-muted-foreground">scope</dt><dd className={hashStyle}>{pluginSource.scope_id}</dd>
            <dt className="text-muted-foreground">request</dt><dd className={hashStyle}>{pluginSource.request_id}</dd>
            <dt className="text-muted-foreground">payload SHA256</dt><dd className={hashStyle}>{pluginSource.submission_sha256}</dd>
            <dt className="text-muted-foreground">编译内容指纹</dt><dd className={hashStyle}>{pluginSource.submission_fingerprint}</dd>
            <dt className="text-muted-foreground">准确模型</dt><dd className="break-all">{pluginSource.profile_id} · {pluginSource.provider} / {pluginSource.model}</dd>
            <dt className="text-muted-foreground">真实登记资料</dt><dd>{materials.length} 原件 · {materials.reduce((sum, material) => sum + material.parts.length, 0)} 准确片段（完整选区、SHA与损失见上方）</dd>
            <dt className="text-muted-foreground">来源剩余有效期</dt><dd>{pluginSourceProblem ? '已失效；不可继续选择或交接' : pluginSourceRemaining > 0 ? `保守剩余 ${pluginSourceRemaining} 整秒（按宿主回执扣除耗时，不自动续期）` : '不足1秒或已到期；请以插件面板实时读取为准，不自动续期'}</dd>
            <dt className="text-muted-foreground">token 估算</dt><dd>文字 {pluginSource.estimated_text_tokens} · 媒体 {pluginSource.media_token_estimate === null ? '未知，不能当作0或声称已满足总输入预算' : pluginSource.media_token_estimate}（非精确计数）</dd>
          </dl>
          <p className="text-xs">{pluginSource.ready_for_verified_adapter ? '保留宿主已有 verified-provider 能力证据。' : '仅为 host_configured 能力声明，未实测模型；后续外发仍须独立接受相关风险。'} 来源 TTL 受真实 request 与 scope 共同限制，不等于插件调用的原始运行时截止时间，也不会延长它。</p>
          <p className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-xs">当前来源仍属于此窗口：关闭、取消、会话／正文／附件变化会撤销 scope；改路由、参数或登记资料会废弃旧 request。未明确交接前请保持此窗口，在同一认证用户的另一个插件面板／标签中读取并选择以上句柄；如需关闭本窗口，请先单独明确交接。未经精确回执与真实来源二次核验，不声称关闭后仍可用。</p>
          <div className="space-y-2 rounded-lg border border-border p-3">
            <label className="flex items-start gap-2 text-xs"><input type="checkbox" className="mt-1" checked={handoffAck} disabled={disabled || Boolean(pluginSourceProblem)} onChange={event => setHandoffAck(event.target.checked)}/>我明确只把这份准确未预览 request 交给宿主持有至原始到期时间，允许关闭窗口后在插件面板另行选择；不授权插件执行或模型外发，不续期。</label>
            <Button type="button" variant="outline" disabled={disabled || !handoffAck || Boolean(pluginSourceProblem)} onClick={handoffPluginSource}>交接 server-held source（不发送、不授权插件）</Button>
          </div>
          <p className="text-xs text-muted-foreground">下一步：插件面板读取并选择真实 source → 单独批准绑定具体插件／工具及输入预算 → 单独批准插件调用 → pending/A 面板再预览准确请求并独立批准一次外发；这里不会自动执行任一步。</p>
          {pluginSourceProblem && <p role="alert" className="text-xs text-destructive">{pluginSourceProblem}</p>}
        </section>}
        {finalPreview && <div className="space-y-4 rounded-xl border border-primary/40 p-4">
          <h4 className="font-semibold">最终确认 · 以下内容才是准确外发请求</h4><RouteDetails route={finalPreview.route}/>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs leading-6">
            <dt className="text-muted-foreground">真实请求体</dt><dd>{finalPreview.preview.wire_bytes.toLocaleString()} bytes（含协议封装/base64，不是原件大小估算）</dd>
            <dt className="text-muted-foreground">精确正文</dt><dd>{finalPreview.preview.text_characters.toLocaleString()} 字</dd>
            <dt className="text-muted-foreground">原件 / 片段</dt><dd>{finalPreview.preview.attachment_count} / {finalPreview.preview.part_count}</dd>
            <dt className="text-muted-foreground">输出硬限 / 超时</dt><dd>{finalPreview.preview.max_output_tokens.toLocaleString()} tokens / {finalPreview.preview.timeout_seconds} 秒</dd>
            <dt className="text-muted-foreground">思考参数</dt><dd>{finalPreview.effort === null ? '明确不附带思考参数' : effortNames[finalPreview.effort]}</dd>
            <dt className="text-muted-foreground">token 估算</dt><dd>文字 {finalPreview.preview.estimated_text_tokens.toLocaleString()} · 媒体 {finalPreview.preview.media_token_estimate === null ? '未知，不当作 0' : finalPreview.preview.media_token_estimate.toLocaleString()}（均非精确 token 计数）</dd>
          </dl>
          <p className={hashStyle}>本次 scope / request：{finalPreview.scope_id} / {finalPreview.request_id}</p>
          <p className={hashStyle}>请求 SHA256：{finalPreview.submission_sha256}</p>
          <p className={hashStyle}>编译内容指纹：{finalPreview.preview.submission_fingerprint}</p>
          <p className={hashStyle}>路由指纹：{finalPreview.preview.route_fingerprint}</p>
          <p className={hashStyle}>能力指纹：{finalPreview.preview.capability_fingerprint}</p>
          <p className={hashStyle}>最终批准指纹：{finalPreview.fingerprint}</p>
          <h5 className="text-xs font-semibold">精确发送正文 · 原文（不渲染 HTML）</h5><pre tabIndex={0} className={preStyle}>{finalPreview.preview.text || '（正文为空）'}</pre>
          <h5 className="text-xs font-semibold">这一次引用的原件与批准选区</h5>
          {materials.map(material => <div key={material.material_id} className="space-y-2 rounded-lg border border-border p-3"><SourceDetails source={material.source}/><pre tabIndex={0} className={preStyle}>{JSON.stringify(material.video_selection ?? material.selection, null, 2)}</pre></div>)}
          <PartsDetails parts={finalPreview.preview.attachments} texts={finalPreview.preview.processed_text_parts}/>
          <h5 className="text-xs font-semibold">本次全部转换损失说明</h5><Notes notes={finalPreview.preview.loss_notes}/>
          <h5 className="text-xs font-semibold">本次实际资料引用（与各完整 manifest / 选区绑定）</h5><pre tabIndex={0} className={preStyle}>{JSON.stringify(finalPreview.materials, null, 2)}</pre>
          <p className="whitespace-pre-wrap break-all text-xs">{finalPreview.capability_notice}</p>
          <p className="whitespace-pre-wrap break-all text-xs">{finalPreview.preview.verification_note}</p>
          {finalPreview.requires_unverified_capabilities_acceptance && <label className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-xs"><input type="checkbox" className="mt-1" checked={unverifiedAck} disabled={disabled || Boolean(finalProblem)} onChange={event => setUnverifiedAck(event.target.checked)}/>单独接受：此准确模型／协议的能力仅为配置声明，尚未经过真实服务端调用验证；可能不支持、拒绝或计费，我仍允许本次尝试。不将此勾选视为“已实测”。</label>}
          {finalPreview.plain_http && <label className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs"><input type="checkbox" className="mt-1" checked={httpAck} disabled={disabled || Boolean(finalProblem)} onChange={event => setHttpAck(event.target.checked)}/>单独允许：本次目标使用明文 HTTP，正文、资料及凭据可能被读取或篡改；即使是本机地址也不视为已加密。我明确允许仅这一次明文请求。</label>}
          <label className="flex items-start gap-2 text-xs"><input type="checkbox" className="mt-1" checked={sendAck} disabled={disabled || Boolean(finalProblem)} onChange={event => setSendAck(event.target.checked)}/>我已核对全部精确正文、原件/选区/损失/manifest与资料引用、路由/服务商/模型/协议/目标、思考参数/输出上限、真实请求字节、安全凭据引用/authority/整数代际；只批准这一个指纹对应的请求外发一次，理解可能计费，不批准自动重试、降级或扩大上下文。</label>
          {finalProblem && <p role="alert" className="text-xs text-destructive">{finalProblem}</p>}
          <Button type="button" disabled={disabled || Boolean(inputProblem) || !sendAck || finalPreview.requires_unverified_capabilities_acceptance && !unverifiedAck || finalPreview.plain_http && !httpAck || Boolean(finalProblem)} onClick={execute}><Send/>明确发送一次</Button>
        </div>}
      </section>
    </>}
    {handoffAttempted && pluginSource && <section className={panel} aria-label="插件来源交接回执与显式撤销">
      <h3 className="flex items-center gap-2 font-semibold">{sourceRevokeAttempted ? '来源撤销已尝试 · 本轮保持冻结' : handoff ? <><CheckCircle2 className="size-4 text-primary"/>交接成功 · 准确来源已冻结</> : '交接结果未确认 · 本轮已冻结'}</h3>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs leading-6">
        <dt className="text-muted-foreground">原 scope</dt><dd className={hashStyle}>{pluginSource.scope_id}</dd>
        <dt className="text-muted-foreground">原 request</dt><dd className={hashStyle}>{pluginSource.request_id}</dd>
        <dt className="text-muted-foreground">payload SHA256</dt><dd className={hashStyle}>{pluginSource.submission_sha256}</dd>
        <dt className="text-muted-foreground">编译指纹</dt><dd className={hashStyle}>{pluginSource.submission_fingerprint}</dd>
        <dt className="text-muted-foreground">准确模型</dt><dd className="break-all">{pluginSource.profile_id} · {pluginSource.provider} / {pluginSource.model}</dd>
        <dt className="text-muted-foreground">原有效期剩余</dt><dd>{sourceRevokeAttempted ? '已请求撤销；不再视为有效来源' : !handoff ? '未知；不声称仍有效，不允许重试交接' : pluginSourceRemaining > 0 ? `保守剩余 ${pluginSourceRemaining} 整秒（不超过原 source、handoff 与二次读取回执的期限）` : '不足1秒或已到期；不再承诺可用，请以插件页实时读取为准'}</dd>
      </dl>
      {sourceRevokeAttempted ? <p className="rounded-lg border border-border p-3 text-xs">已单独尝试撤销该来源；以上交接信息仅为历史元数据，不再作为当前有效能力。不要依据旧回执选择或继续外发，撤销结果以下方独立回执为准。</p> : handoff ? <p className="rounded-lg border border-primary/40 bg-primary/5 p-3 text-xs">交接成功，可关闭此窗口，在同一认证用户的 PluginModelPending 插件页读取 prepared/sources 并选择以上真实句柄。外发另行批准；最多原60秒的剩余时间，不续期，不延长插件 child 原始运行时截止时间。保留的是宿主原始准确来源，没有复制 payload、猜测 child scope 或自动绑定插件。</p> : <p role="alert" className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-xs">交接已尝试，但精确回执与真实来源二次核验未同时完成。宿主可能已经持有，不能据此声称来源仍有效；没有重试或恢复旧批准。关闭仅释放本地许可并请求 revokeScope，不二次 cancelRequest；需要撤销时请单独确认下方操作。</p>}
      <p className="text-xs text-muted-foreground">本地草稿或附件再变化不会改写这份已交接 payload。本地批准和帧 object URLs 已释放；路由、凭据、认证及实际原件变化仍由宿主持续重检，失败即 sticky invalidation，不保证可用。已绑定的预发送能力可在插件页 prepared/revoke 单独撤销；宿主来源到原 expiry 清理，不长期保活。</p>
      <p className="text-xs text-muted-foreground">下一步均须用户操作：插件页读取并选择真实 source → 单独批准绑定具体插件／工具及预算 → 单独批准插件调用 → pending 面板预览准确请求并独立批准一次外发。onHandoff 只通知九字段元数据，不自动执行以上任何步骤。</p>
      {!sourceRevokeAttempted && !closed && <div className="space-y-2 rounded-lg border border-border p-3">
        <label className="flex items-start gap-2 text-xs"><input type="checkbox" className="mt-1" checked={sourceRevokeAck} disabled={Boolean(busy) || !connected} onChange={event => setSourceRevokeAck(event.target.checked)}/>我明确请求撤销以上 scope/request 的已尝试交接来源；这不是关闭窗口，也不是外发批准。撤销只尝试一次，不保证撤回下游已经外发的字节。</label>
        <Button type="button" variant="outline" disabled={Boolean(busy) || !connected || !sourceRevokeAck} onClick={() => { void cancelSource() }}>单独撤销此已尝试交接的来源（不发送）</Button>
      </div>}
      {sourceRevocation && <p role="status" className="whitespace-pre-wrap break-all text-xs">{sourceRevocation}</p>}
      {sourceRevokeAttempted && !sourceRevocation && <p role="status" className="text-xs">来源撤销已尝试，完成回执尚未确认；不声称来源有效或已撤销，不自动重试。</p>}
      {handoff && <details><summary className="cursor-pointer text-xs">宿主精确九字段交接回执（仅元数据，不是发送批准）</summary><pre tabIndex={0} className={preStyle}>{JSON.stringify(handoff, null, 2)}</pre></details>}
    </section>}
    {result && <section className={panel} aria-label="独立模型完成回执">
      <h3 className="font-semibold">独立模型结果 · {result.finish === 'completed' ? '完成' : result.finish === 'length' ? '达到输出上限' : result.finish === 'tool_requested' ? '模型请求工具（未执行工具）' : result.finish === 'blocked' ? '模型输出被拦截' : result.finish === 'refused' ? '模型拒绝' : '未完整完成'}</h3>
      <p className="text-xs">宿主规范化结束状态：{result.finish} · {result.finish === 'completed' ? '已获得文字完成状态，不据此保证内容正确' : '未声称输出完整；不自动续写、调用工具或重试'}</p>
      <p className="text-xs">用量：输入 {result.usage.input_tokens ?? '未知'} · 输出 {result.usage.output_tokens ?? '未知'} · 合计 {result.usage.total_tokens ?? '未知'} tokens</p>
      <p className={hashStyle}>请求 {result.request_id} · SHA256 {result.submission_sha256}</p>
      <pre tabIndex={0} className={preStyle}>{result.answer || '模型返回了空文字；不伪造回答。'}</pre>
      <p className="text-xs text-muted-foreground">结果仅作为纯文本展示，不渲染 HTML 或 provider 内部块。草稿与附件队列保持不变；调用方须明确处理结果，不能据此自动触发旧聊天发送。</p>
    </section>}
    {busy && <p role="status" className="flex items-center gap-2 text-xs"><LoaderCircle className="size-4 animate-spin"/>{busy}</p>}
    <footer className="flex flex-wrap items-center gap-3 border-t border-border pt-3">
      <Button type="button" variant="outline" disabled={closed || cancelling.current} onClick={() => { void cancel() }}>{handoffAttempted ? sourceRevokeAttempted ? '关闭窗口（不重复撤销来源）' : handoff ? '关闭窗口（保留已交接来源至原到期）' : '关闭窗口（交接结果未知，不二次取消）' : executing.current || outcomeUnknown ? '请求取消在途模型并撤销 scope' : '取消本轮并撤销全部许可'}</Button>
      <p className="text-xs text-muted-foreground">{handoffAttempted ? '此按钮只关闭窗口并释放本地许可；撤销来源需上方独立确认，交接不等于插件执行或模型发送批准。' : '取消不删除原件；发送后取消不能保证撤回已外发字节。'}</p>
    </footer>
    {closed && <p role="status" className="text-xs text-muted-foreground">本轮已关闭。重新打开组件才可发起新的明确预览；不会自动恢复旧许可。</p>}
  </section>
}
