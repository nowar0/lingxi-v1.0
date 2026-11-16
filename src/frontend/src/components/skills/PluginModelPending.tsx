import { useEffect, useMemo, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { isAvailableModelHostRoute } from '@/lib/model-material-send'
import type { AvailableModelHostRoute, ModelHostEffort, ModelHostRoute } from '@/lib/model-material-send'
import {
  createPluginModelPendingClient, pluginModelError, pluginModelPendingRequestId,
} from '@/lib/pluginModelPending'
import type {
  PluginModelPendingClient, PluginModelPendingMetadata, PluginModelPrepared, PluginModelPreview, PluginModelResult, PluginModelSource,
} from '@/lib/pluginModelPending'

export type PluginModelPendingProps = {
  /** Pass only the result of an actually approved/executed plugin call, never Agent intent JSON. */
  result?: unknown
  disabled?: boolean
  client?: PluginModelPendingClient
  preparation?: { pluginId: string; tool: string; revision: number }
  /** This explicit UI action copies only the host-returned SDK descriptor; it never executes. */
  onUsePrepared?: (value: PluginModelPrepared) => void
}
const renderJson = (value: unknown) => JSON.stringify(value, null, 2)
function Json({ title, value }: { title: string; value: unknown }) {
  return <details className="rounded-lg border border-border/60 p-3" open><summary className="cursor-pointer text-xs font-medium">{title}</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all text-[11px] leading-5">{renderJson(value)}</pre></details>
}

/** No polling/renewal/automatic approval. Preview and send are separate human actions. */
export function PluginModelPending({ result, disabled = false, client: suppliedClient, preparation, onUsePrepared }: PluginModelPendingProps) {
  const client = useMemo(() => suppliedClient || createPluginModelPendingClient(), [suppliedClient])
  const requestId = pluginModelPendingRequestId(result)
  const [pending, setPending] = useState<PluginModelPendingMetadata>()
  const [proposed, setProposed] = useState<PluginModelPreview>()
  const [output, setOutput] = useState<PluginModelResult>()
  const [busy, setBusy] = useState<'read' | 'preview' | 'execute' | 'cancel' | ''>('')
  const [message, setMessage] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [unverifiedConfirmed, setUnverifiedConfirmed] = useState(false)
  const [attempted, setAttempted] = useState(false)
  const [closed, setClosed] = useState(false)
  const [deadline, setDeadline] = useState(0)
  const [now, setNow] = useState(Date.now())
  const [sources, setSources] = useState<PluginModelSource[]>([])
  const [sourcesReadAt, setSourcesReadAt] = useState(0)
  const [source, setSource] = useState<PluginModelSource>()
  const [sourceBusy, setSourceBusy] = useState(false)
  const [sourceMessage, setSourceMessage] = useState('')
  const [sourceDeadline, setSourceDeadline] = useState(0)
  const [routes, setRoutes] = useState<ModelHostRoute[]>([])
  const [sourceSession, setSourceSession] = useState('')
  const [sourceProfile, setSourceProfile] = useState('')
  const [sourceText, setSourceText] = useState('')
  const [sourceOutput, setSourceOutput] = useState('4096')
  const [sourceEffort, setSourceEffort] = useState('')
  const [sourceConfirmed, setSourceConfirmed] = useState(false)
  const [inputTokens, setInputTokens] = useState('4096')
  const [prepareConfirmed, setPrepareConfirmed] = useState(false)
  const [prepared, setPrepared] = useState<PluginModelPrepared>()
  const [prepareBusy, setPrepareBusy] = useState(false)
  const [prepareMessage, setPrepareMessage] = useState('')
  const [prepareDeadline, setPrepareDeadline] = useState(0)
  const live = useRef(true)
  const sequence = useRef(0)
  const prepareSequence = useRef(0)
  const operation = useRef(false)
  const preparationLock = useRef(false)
  const sourceSequence = useRef(0)
  const sourceLock = useRef(false)
  const ownedSource = useRef<{ client: PluginModelPendingClient; scopeId: string } | undefined>(undefined)
  const abort = useRef<AbortController | undefined>(undefined)
  const current = useRef({ disabled, client })
  current.current = { disabled, client }

  useEffect(() => {
    live.current = true
    return () => { live.current = false; sequence.current++; prepareSequence.current++; sourceSequence.current++; abort.current?.abort() }
  }, [])
  useEffect(() => {
    const generation = ++sequence.current
    abort.current?.abort()
    setPending(undefined); setProposed(undefined); setOutput(undefined); setMessage(''); setConfirmed(false); setUnverifiedConfirmed(false)
    setAttempted(false); setClosed(false); setDeadline(0); setBusy('')
    if (!requestId || disabled) return
    const controller = new AbortController()
    abort.current = controller
    setBusy('read')
    const started = Date.now()
    void client.pending(requestId, controller.signal).then(value => {
      if (!live.current || generation !== sequence.current || current.current.disabled) return
      setPending(value); setDeadline(started + value.expires_in * 1000); setNow(Date.now())
    }).catch(error => {
      if (live.current && generation === sequence.current && !controller.signal.aborted) setMessage(pluginModelError(error))
    }).finally(() => {
      if (live.current && generation === sequence.current) setBusy('')
    })
    return () => controller.abort()
  }, [client, disabled, requestId])
  useEffect(() => {
    prepareSequence.current++
    setPrepared(undefined); setPrepareMessage(''); setPrepareConfirmed(false); setPrepareDeadline(0)
  }, [client, preparation?.pluginId, preparation?.revision, preparation?.tool, disabled])
  useEffect(() => {
    sourceSequence.current++
    setSources([]); setSource(undefined); setSourceDeadline(0); setSourcesReadAt(0)
    setRoutes([]); setSourceProfile(''); setSourceConfirmed(false); setSourceMessage('')
    return () => {
      sourceSequence.current++
      const owned = ownedSource.current
      ownedSource.current = undefined
      if (owned) void owned.client.revokeSource(owned.scopeId).catch(() => undefined)
    }
  }, [client, preparation?.pluginId, preparation?.revision, preparation?.tool])
  useEffect(() => {
    if (!deadline && !prepareDeadline && !sourceDeadline) return
    const timer = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(timer)
  }, [deadline, prepareDeadline, sourceDeadline])

  const run = async (kind: 'preview' | 'execute' | 'cancel', action: (generation: number) => Promise<void>) => {
    if (operation.current || disabled) return
    operation.current = true; setBusy(kind); setMessage('')
    const generation = sequence.current
    try { await action(generation) }
    catch (error) {
      if (live.current && generation === sequence.current) setMessage(`${pluginModelError(error)}${kind === 'execute' ? ' 发送可能已有外部效果，结果未知时也不自动重试。' : ''}`)
    } finally {
      operation.current = false
      if (live.current && generation === sequence.current) setBusy('')
    }
  }
  const valid = (generation: number) => live.current && generation === sequence.current && !current.current.disabled && current.current.client === client
  const makePreview = () => {
    if (!pending || proposed || closed || attempted || Date.now() >= deadline) return
    const value = pending
    const started = Date.now()
    void run('preview', async generation => {
      const proposal = await client.preview(value)
      if (!valid(generation)) return
      setProposed(proposal); setConfirmed(false); setUnverifiedConfirmed(false)
      setDeadline(old => Math.min(old, started + proposal.expires_in * 1000)); setNow(Date.now())
    })
  }
  const send = () => {
    if (disabled || operation.current || !proposed?.token || !confirmed || proposed.requires_unverified_capabilities_acceptance && !unverifiedConfirmed || closed || attempted || Date.now() >= deadline) return
    const value = proposed
    setAttempted(true)
    void run('execute', async generation => {
      const controller = new AbortController()
      abort.current = controller
      const result = await client.execute(value, controller.signal, unverifiedConfirmed === true)
      if (!valid(generation)) return
      setOutput(result); setClosed(true); setMessage('本次独立发送已返回；没有自动再运行插件或把结果交给另一个模型。')
    })
  }
  const cancel = () => {
    if (!requestId || closed || busy === 'cancel') return
    const value = proposed
    if (busy === 'execute') {
      // Explicit cancel is independent of the in-flight send; it reaches the host Event.
      setClosed(true); setConfirmed(false)
      const generation = sequence.current
      void client.cancel(requestId, value).then(() => {
        if (valid(generation)) setMessage('已请求取消；不能撤回已发送的外部效果，不自动重试。')
      }).catch(error => { if (valid(generation)) setMessage(pluginModelError(error)) })
      return
    }
    void run('cancel', async generation => {
      await client.cancel(requestId, value)
      if (valid(generation)) { setClosed(true); setConfirmed(false); setMessage('已撤销 pending 或发送批准；外部效果不能保证撤回。') }
    })
  }
  const clearPrepared = () => { prepareSequence.current++; setPrepared(undefined); setPrepareConfirmed(false); setPrepareMessage(''); setPrepareDeadline(0) }
  const selectSource = (value: PluginModelSource | undefined, readAt = sourcesReadAt) => {
    clearPrepared(); setSource(value); setSourceDeadline(value ? readAt + value.expires_in * 1000 : 0); setNow(Date.now())
  }
  const sourceRun = (action: (generation: number) => Promise<void>) => {
    if (disabled || sourceLock.current || prepareBusy) return
    sourceLock.current = true; setSourceBusy(true); setSourceMessage('')
    const generation = ++sourceSequence.current
    void action(generation).catch(error => { if (live.current && generation === sourceSequence.current) setSourceMessage(pluginModelError(error)) })
      .finally(() => { sourceLock.current = false; if (live.current) setSourceBusy(false) })
  }
  const sourceValid = (generation: number) => live.current && generation === sourceSequence.current && current.current.client === client
  const readSources = () => sourceRun(async generation => {
    const started = Date.now()
    const values = await client.sources()
    if (!sourceValid(generation) || current.current.disabled) return
    clearPrepared(); setSources(values); setSourcesReadAt(started); setSource(undefined); setSourceDeadline(0)
    setSourceMessage(values.length ? '请选择一份尚未预览、未发送的真实请求；读取列表不创建批准。' : '当前没有可绑定的未预览请求。普通聊天已预览的请求不可复用；可在下方仅创建新的 model-host source。')
  })
  const readRoutes = () => sourceRun(async generation => {
    const values = await client.sourceRoutes()
    if (!sourceValid(generation) || current.current.disabled) return
    setRoutes(values); setSourceProfile(''); setSourceEffort(''); setSourceConfirmed(false)
  })
  const changeSourceDraft = () => setSourceConfirmed(false)
  const selectedRoute: AvailableModelHostRoute | undefined = routes.filter(isAvailableModelHostRoute).find(value => value.profile_id === sourceProfile)
  const createSource = () => {
    if (!selectedRoute || !sourceConfirmed || !sourceText || !sourceSessionValid || !sourceOutputValid) return
    const route = selectedRoute
    sourceRun(async generation => {
      const old = ownedSource.current
      if (old) {
        await old.client.revokeSource(old.scopeId)
        ownedSource.current = undefined
      }
      if (!sourceValid(generation) || current.current.disabled) return
      clearPrepared(); setSource(undefined); setSourceDeadline(0)
      const receipt = await client.prepareSource({ sessionId: Number(sourceSession), route, text: sourceText,
        maxOutputTokens: Number(sourceOutput), effort: sourceEffort ? sourceEffort as ModelHostEffort : null })
      if (!sourceValid(generation)) { await client.revokeSource(receipt.scope_id); return }
      ownedSource.current = { client, scopeId: receipt.scope_id }
      const started = Date.now()
      const values = await client.sources()
      if (!sourceValid(generation)) return
      const actual = values.find(value => value.scope_id === receipt.scope_id && value.request_id === receipt.request_id && value.submission_sha256 === receipt.submission_sha256 && value.submission_fingerprint === receipt.submission_fingerprint)
      if (!actual) throw new Error('model-host 创建的真实未预览 source 已变化或不可导出；没有绑定、发送或重试')
      setSources(values); setSourcesReadAt(started); setSourceConfirmed(false)
      if (!current.current.disabled) selectSource(actual, started)
      setSourceMessage('model-host 已真实编译并保留请求；没有调用发送 preview，没有创建发送 token。仅正文入口不自动添加聊天历史、记忆或附件。')
    })
  }
  const sourceSessionValid = /^\d+$/.test(sourceSession) && Number.isSafeInteger(Number(sourceSession)) && Number(sourceSession) > 0
  const sourceOutputValid = /^\d+$/.test(sourceOutput) && Number.isSafeInteger(Number(sourceOutput)) && Number(sourceOutput) > 0 && Number(sourceOutput) <= Math.min(32768, selectedRoute?.max_output_tokens ?? 0)
  const prepare = () => {
    if (!preparation || !source || disabled || sourceBusy || preparationLock.current || !prepareConfirmed || Date.now() >= sourceDeadline) return
    const generation = ++prepareSequence.current
    preparationLock.current = true; setPrepareBusy(true); setPrepareMessage('')
    const started = Date.now()
    void client.prepare({ plugin_id: preparation.pluginId, tool: preparation.tool, expected_revision: preparation.revision,
      scope_id: source.scope_id, request_id: source.request_id, expected_submission_sha: source.submission_sha256,
      max_input_tokens: Number(inputTokens), user_confirmed: true }).then(value => {
      if (live.current && generation === prepareSequence.current && !current.current.disabled) {
        setPrepared(value); setPrepareDeadline(started + value.expires_in * 1000); setNow(Date.now())
      }
    }).catch(error => { if (live.current && generation === prepareSequence.current) setPrepareMessage(pluginModelError(error)) })
      .finally(() => { preparationLock.current = false; if (live.current) setPrepareBusy(false) })
  }
  const prepareValid = Boolean(source) && now < sourceDeadline && /^\d+$/.test(inputTokens) && Number.isSafeInteger(Number(inputTokens)) && Number(inputTokens) >= 1 && Number(inputTokens) <= 1000000
  if (!requestId && !preparation) return null
  const remaining = Math.max(0, Math.ceil((deadline - now) / 1000))
  return <section aria-label="插件模型独立准备与批准" className="space-y-3 rounded-xl border border-primary/25 bg-primary/5 p-4 text-xs leading-6">
    {preparation && <div className="space-y-3">
      <h4 className="font-semibold">启动插件前：绑定真实 model-host 预编译资料</h4>
      <p className="text-muted-foreground">只选择当前认证用户在真实 model-host 中持有、尚未预览或发送的 request；不粘贴 CompiledSubmission、provider payload、附件字节、PID 或 owner。普通聊天的“准备并预览”会占用 source token，不能拿来复用；下方可仅向 model-host 创建新的正文 source，不创建发送批准。准备不发送、不批准插件执行。</p>
      <Button size="sm" variant="outline" disabled={disabled || sourceBusy || prepareBusy} onClick={readSources}>{sourceBusy ? '正在读取真实来源…' : '读取可选的未发送 model-host 请求'}</Button>
      <label className="block space-y-1"><span>选择真实未预览 request（不自动选择）</span><select aria-label="选择真实model-host未发送request" className="h-9 w-full rounded-md border border-border bg-background px-2" value={source?.request_id ?? ''} disabled={disabled || sourceBusy || prepareBusy} onChange={event => selectSource(sources.find(value => value.request_id === event.target.value))}><option value="">请明确选择…</option>{sources.map(value => <option key={value.request_id} value={value.request_id}>{value.profile_id} / {value.model} · {value.request_id}</option>)}</select></label>
      {source && <><Json title="服务端来源句柄与真实预算状态（不是 compiled/policy）" value={source} /><p className="text-amber-700 dark:text-amber-300">{source.ready_for_verified_adapter ? '来源保留 verified-provider 证据。' : '来源只含 host_configured 声明，不会改为 verified；最终发送必须独立接受未实测能力。'} {source.media_token_estimate === null ? '媒体 token 估算未知（null），不能声称总输入已在 token 预算内；硬字节限仍有效。' : `文字估算 ${source.estimated_text_tokens}，媒体估算 ${source.media_token_estimate} token（均非实测）。`} {now >= sourceDeadline ? '来源已到期，不能绑定；不自动刷新。' : ''}</p></>}
      <details className="space-y-3 rounded-lg border border-border p-3"><summary className="cursor-pointer font-medium">仅创建新 model-host 正文 source（不预览、不发送）</summary>
        <p className="text-muted-foreground">此入口只提交你明确填写的正文；不读取聊天上下文或自动添加附件。真实含图片/视频的既有未预览请求可从上方选择，原件字节硬限与未知媒体 token 不混同。正文、会话、路由或参数变化需明确创建新来源，旧句柄不会被静默更新。</p>
        <Button size="sm" variant="outline" disabled={disabled || sourceBusy || prepareBusy} onClick={readRoutes}>读取真实模型目录（不读取 Key）</Button>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1"><span>已拥有的真实聊天 session_id（不是 owner）</span><Input aria-label="source真实聊天session id" inputMode="numeric" value={sourceSession} disabled={disabled || sourceBusy || prepareBusy} onChange={event => { changeSourceDraft(); setSourceSession(event.target.value) }} /></label>
          <label className="space-y-1"><span>明确选择模型</span><select aria-label="source真实模型路由" className="h-9 w-full rounded-md border border-border bg-background px-2" value={sourceProfile} disabled={disabled || sourceBusy || prepareBusy} onChange={event => { changeSourceDraft(); setSourceProfile(event.target.value); setSourceEffort('') }}><option value="">不使用默认模型…</option>{routes.map(value => <option key={value.profile_id} value={value.profile_id} disabled={!value.available}>{value.name} / {value.model || value.profile_id}{value.available ? '' : `（不可用：${value.diagnostics.join('、')}）`}</option>)}</select></label>
          <label className="space-y-1"><span>最大输出 token（SDK 至多 32768）</span><Input aria-label="source最大输出tokens" inputMode="numeric" value={sourceOutput} maxLength={5} disabled={disabled || sourceBusy || prepareBusy} onChange={event => { changeSourceDraft(); setSourceOutput(event.target.value) }} /></label>
          <label className="space-y-1"><span>思考档位（只显示真实声明）</span><select aria-label="source思考档位" className="h-9 w-full rounded-md border border-border bg-background px-2" value={sourceEffort} disabled={disabled || sourceBusy || prepareBusy || !selectedRoute} onChange={event => { changeSourceDraft(); setSourceEffort(event.target.value) }}><option value="">不传档位参数</option>{selectedRoute?.supported_efforts.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
        </div>
        <label className="block space-y-1"><span>仅本次 source 正文</span><textarea aria-label="source明确正文" className="min-h-28 w-full rounded-md border border-border bg-background p-2" value={sourceText} maxLength={100000} disabled={disabled || sourceBusy || prepareBusy} onChange={event => { changeSourceDraft(); setSourceText(event.target.value) }} /></label>
        <label className="flex items-start gap-2"><input type="checkbox" className="mt-1.5" checked={sourceConfirmed} disabled={disabled || sourceBusy || prepareBusy} onChange={event => setSourceConfirmed(event.target.checked)} /><span>我明确要求由 model-host 使用上面这个会话、模型、正文与输出参数创建一份新的未预览请求；这是创建来源，不是发送批准。重新创建会撤销本界面上次创建的来源，不续 TTL。</span></label>
        <Button size="sm" variant="outline" disabled={disabled || sourceBusy || prepareBusy || !selectedRoute || !sourceSessionValid || !sourceOutputValid || !sourceText || !sourceConfirmed} onClick={createSource}>仅请求 model-host 创建 source handle</Button>
      </details>
      {sourceMessage && <p role="status" className="whitespace-pre-wrap break-words">{sourceMessage}</p>}
      <label className="block space-y-1"><span>本次 SDK 最大输入 token 预算（未知媒体总量不伪估算）</span><Input aria-label="插件模型最大输入tokens" inputMode="numeric" value={inputTokens} maxLength={7} disabled={disabled || prepareBusy || sourceBusy} onChange={event => { clearPrepared(); setInputTokens(event.target.value) }} /></label>
      <label className="flex items-start gap-2"><input type="checkbox" className="mt-1.5" checked={prepareConfirmed} disabled={disabled || prepareBusy || Boolean(prepared)} onChange={event => setPrepareConfirmed(event.target.checked)} /><span>我明确同意仅把这份宿主持有请求绑定给 {preparation.pluginId} / {preparation.tool}（revision {preparation.revision}）。每个 plugin/tool/profile/prompt SHA 和完整参数必须一致；后续仍分别预览插件调用和最终模型发送。</span></label>
      <Button size="sm" variant="outline" disabled={disabled || prepareBusy || !prepareValid || !prepareConfirmed || Boolean(prepared)} onClick={prepare}>{prepareBusy ? '正在核验真实宿主资料…' : '仅准备模型资料能力（不发送）'}</Button>
      <p className="text-amber-700 dark:text-amber-300">仅真实 source owner、payload SHA、完整路由和资料生命周期复核后，宿主才能创建本次 typed policy；浏览器不能构造或序列化它。默认 verified 严格门仍在，host_configured 尝试不更改 ready / capability_source；最终另行勾选未实测及未知媒体预算风险。</p>
      {prepareMessage && <p role="alert" className="text-destructive">{prepareMessage}</p>}
      {prepared && <><Json title="宿主生成的完整 model.generate params（不是发送批准）" value={prepared} /><div className="flex flex-wrap gap-2">{onUsePrepared && <Button size="sm" variant="outline" disabled={disabled || prepareBusy || now >= prepareDeadline} onClick={() => { if (prepared) onUsePrepared?.(prepared) }}>将宿主参数填入本次 args（不执行）</Button>}<Button size="sm" variant="outline" disabled={disabled || prepareBusy} onClick={() => {
        if (preparationLock.current) return
        preparationLock.current = true; setPrepareBusy(true)
        const generation = prepareSequence.current
        void client.revokePrepared(prepared.params.prompt_id).then(() => { if (live.current && generation === prepareSequence.current) { clearPrepared(); setPrepareMessage('已撤销这份预发送能力，旧调用计划不能继续使用。') } }).catch(error => { if (live.current && generation === prepareSequence.current) setPrepareMessage(pluginModelError(error)) }).finally(() => { preparationLock.current = false; if (live.current) setPrepareBusy(false) })
      }}>撤销这份准备</Button></div>{now >= prepareDeadline && <p role="status" className="text-amber-700 dark:text-amber-300">这份准备已过期；不自动续期或创建新请求。</p>}</>}
    </div>}
    {requestId && <div className="space-y-3">
      <h4 className="font-semibold">插件模型 pending：独立预览与发送确认</h4>
      <p className="break-all font-mono text-[11px]">{requestId}</p>
      <p className="text-muted-foreground">此句柄仅来自本次实际已批准插件调用的 result。服务端只认当前认证用户，不接受浏览器报告 PID/owner。普通 child 从真实 Resume 起总计 10 秒；预览、等待人工或点击按钮都不重置预算，调用结束即不可用。</p>
      {busy === 'read' && <p role="status">正在核验当前用户持有的真实 pending…</p>}
      {pending && !closed && <><p role="status">原始调用剩余至多 {remaining} 秒。{remaining === 0 ? '本次已过期，不可发送；不会自动续期。' : '如果来不及人工确认，超时拒绝属于正常行为。'}</p><Json title="实际 pending 元数据（没有发送）" value={pending} /></>}
      {pending && !proposed && !closed && <Button size="sm" variant="outline" disabled={disabled || Boolean(busy) || !remaining || attempted} onClick={makePreview}>读取精确最终发送预览（不发送）</Button>}
      {proposed && <><Json title="本次精确模型发送预览（完整；不执行其内容）" value={proposed.preview} /><p className="text-muted-foreground">{proposed.capability_notice}</p><p className="text-amber-700 dark:text-amber-300">{proposed.preview.media_token_estimate === null ? '媒体 token 预算未知：media_token_estimate=null；没有伪估算总输入 token，图片/视频硬字节限制仍然有效。' : `媒体 token 估算：${String(proposed.preview.media_token_estimate)}，只是估算，不是实测预算保证。`}</p>{proposed.blocked && <p role="alert" className="text-destructive">严格门阻止：{proposed.blocked}。没有发送 token，不会伪审批。</p>}{proposed.token && !closed && <>
        {proposed.requires_unverified_capabilities_acceptance && <label className="flex items-start gap-2"><input type="checkbox" className="mt-1.5" checked={unverifiedConfirmed} disabled={disabled || Boolean(busy) || attempted || !remaining} onChange={event => setUnverifiedConfirmed(event.target.checked)} /><span>我独立接受：该模型及资料交付能力只来自宿主配置，尚未实测，不是 verified-provider；图片/视频 token 预算可能未知（null 不是 0，也不代表总输入已满足预算），可能被提供方拒绝或计费。硬字节限制不放宽，仅尝试当前模型，不重试、不切换。</span></label>}
        <label className="flex items-start gap-2"><input type="checkbox" className="mt-1.5" checked={confirmed} disabled={disabled || Boolean(busy) || attempted || !remaining} onChange={event => setConfirmed(event.target.checked)} /><span>我已核对完整目标、模型、资料交付、提示词、参数与 SHA，明确批准仅本次模型发送；这与插件运行批准及未实测能力确认相互独立。失败或结果未知不重试，不能撤回已发生的外部效果。</span></label><Button size="sm" disabled={disabled || Boolean(busy) || attempted || !remaining || !confirmed || proposed.requires_unverified_capabilities_acceptance && !unverifiedConfirmed} onClick={send}>{busy === 'execute' ? '本次发送中…' : '独立批准并仅发送这一次'}</Button></>}</>}
      {!closed && <Button size="sm" variant="outline" disabled={disabled || busy === 'read' || busy === 'preview' || busy === 'cancel'} onClick={cancel}>拒绝 / 撤销批准 / 请求取消</Button>}
      {attempted && !output && <p className="text-amber-700 dark:text-amber-300">本次 token 已尝试使用；无论成功、失败或结果未知均不可重复发送。</p>}
      {message && <p role="status" className="whitespace-pre-wrap break-words">{message}</p>}
      {output && <Json title="本次独立模型结果（仅显示，不再执行）" value={output} />}
    </div>}
  </section>
}
