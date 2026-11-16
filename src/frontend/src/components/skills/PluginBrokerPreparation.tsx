import { useEffect, useMemo, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input, Textarea } from '@/components/ui/input'
import { PluginModelPending } from './PluginModelPending'
import { brokerPreparationDraft, brokerPreparationError, createPluginBrokerPreparationClient, objectBrokerOperation } from '@/lib/pluginBrokerPreparation'
import type { ActiveModelPending, ActiveModelPendingList, BrokerDraft, HttpPreparation, ObjectPreparation, PluginBrokerPreparationClient, PreparationSource } from '@/lib/pluginBrokerPreparation'
import type { PluginPackage } from '@/lib/pluginPackages'

export type PluginBrokerPreparationProps = {
  item: PluginPackage
  tool: string
  argsText: string
  apiBase?: string
  disabled?: boolean
  /** Preparation is blocked during execute, but active pending refresh stays available. */
  callRunning?: boolean
  client?: PluginBrokerPreparationClient
  /** Called only AFTER a second explicit human confirmation; never executes. */
  onUseArgs: (text: string) => void
}
const showJson = (value: unknown) => JSON.stringify(value, null, 2)
function Json({ title, value }: { title: string; value: unknown }) {
  return <details className="rounded-lg border border-border/60 p-3" open><summary className="cursor-pointer text-xs font-medium">{title}</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all text-[11px] leading-5">{showJson(value)}</pre></details>
}

/** Only explicit clicks issue requests. No polling, TTL renewal, child keepalive or execution. */
export function PluginBrokerPreparation({ item, tool, argsText, apiBase, disabled = false, callRunning = false, client: suppliedClient, onUseArgs }: PluginBrokerPreparationProps) {
  const client = useMemo(() => suppliedClient || createPluginBrokerPreparationClient({ apiBase }), [apiBase, suppliedClient])
  const [selected, setSelected] = useState<number[]>([])
  const [objectConfirmed, setObjectConfirmed] = useState(false)
  const [objects, setObjects] = useState<ObjectPreparation>()
  const [requestIndex, setRequestIndex] = useState<number>()
  const [url, setUrl] = useState('')
  const [method, setMethod] = useState('')
  const [bodyText, setBodyText] = useState('')
  const [responseLimit, setResponseLimit] = useState('')
  const [httpConfirmed, setHttpConfirmed] = useState(false)
  const [useConfirmed, setUseConfirmed] = useState(false)
  const [http, setHttp] = useState<HttpPreparation>()
  const [httpDeadline, setHttpDeadline] = useState(0)
  const [busy, setBusy] = useState<'objects' | 'http' | ''>('')
  const [message, setMessage] = useState('')
  const [active, setActive] = useState<ActiveModelPendingList>()
  const [activePending, setActivePending] = useState<ActiveModelPending>()
  const [pendingBusy, setPendingBusy] = useState(false)
  const [pendingMessage, setPendingMessage] = useState('')
  const [pendingReadAt, setPendingReadAt] = useState(0)
  const mounted = useRef(true)
  const sequence = useRef(0)
  const pendingSequence = useRef(0)
  const operation = useRef(false)
  const pendingOperation = useRef(false)
  const current = useRef({ disabled, callRunning, client })
  current.current = { disabled, callRunning, client }
  const context = `${item.id}:${item.revision}:${item.digest}:${tool}:${argsText}`
  const currentContext = useRef(context)
  currentContext.current = context
  let draft: BrokerDraft | undefined
  let validation = ''
  try { draft = brokerPreparationDraft(item, tool, argsText) } catch (error) { validation = brokerPreparationError(error) }
  const objectRequests = (draft?.requests || []).map((request, index) => ({ request, index })).filter(({ request }) => objectBrokerOperation(request.op))
  const httpRequests = (draft?.requests || []).map((request, index) => ({ request, index })).filter(({ request }) => request.op === 'http.request')
  const blocked = disabled || callRunning || Boolean(busy) || !item.installed || !item.enabled
  const allSelected = objectRequests.length > 0 && selected.length === objectRequests.length && objectRequests.every(({ index }) => selected.includes(index))
  const selectedHttp = requestIndex === undefined ? undefined : draft?.requests[requestIndex]

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; sequence.current++; pendingSequence.current++ }
  }, [])
  useEffect(() => {
    sequence.current++; pendingSequence.current++
    setSelected([]); setObjectConfirmed(false); setObjects(undefined); setRequestIndex(undefined)
    setUrl(''); setMethod(''); setBodyText(''); setResponseLimit(''); setHttpConfirmed(false)
    setUseConfirmed(false); setHttp(undefined); setHttpDeadline(0); setMessage('')
    setActive(undefined); setActivePending(undefined); setPendingMessage(''); setPendingReadAt(0)
  }, [context, client, disabled])
  const source = (): PreparationSource => {
    if (!draft) throw new Error(validation || '当前 broker_requests 不可准备')
    return { plugin_id: item.id, expected_revision: item.revision, expected_digest: item.digest, tool, args: draft.args, user_confirmed: true }
  }
  const prepare = async (kind: 'objects' | 'http', action: () => Promise<void>) => {
    if (operation.current || blocked || !draft) return
    operation.current = true; setBusy(kind); setMessage('')
    try { await action() }
    catch (error) { if (mounted.current && currentContext.current === context) setMessage(brokerPreparationError(error)) }
    finally { operation.current = false; if (mounted.current) setBusy('') }
  }
  const selectObjects = () => {
    if (!allSelected || !objectConfirmed || objects) return
    const generation = sequence.current
    void prepare('objects', async () => {
      const value = await client.objects({ ...source(), selected_request_indices: objectRequests.map(({ index }) => index) })
      if (!mounted.current || generation !== sequence.current || current.current.disabled || current.current.client !== client) return
      setObjects(value)
    })
  }
  const clearHttp = () => { sequence.current++; setHttp(undefined); setHttpDeadline(0); setHttpConfirmed(false); setUseConfirmed(false); setMessage('') }
  const prepareHttp = () => {
    if (requestIndex === undefined || !selectedHttp || selectedHttp.op !== 'http.request' || !httpConfirmed || http) return
    if (!/^\d+$/.test(responseLimit) || !Number.isSafeInteger(Number(responseLimit)) || Number(responseLimit) < 1 || Number(responseLimit) > 12000) { setMessage('请明确填写 1–12000 的最大响应字节数'); return }
    const generation = sequence.current
    const started = Date.now()
    void prepare('http', async () => {
      const value = await client.http({ ...source(), request_index: requestIndex, url, method, body_text: bodyText, max_response_bytes: Number(responseLimit) })
      if (!mounted.current || generation !== sequence.current || current.current.disabled || current.current.client !== client) return
      setHttp(value); setHttpDeadline(started + value.expires_in * 1000); setUseConfirmed(false)
    })
  }
  const refreshPending = () => {
    if (disabled || pendingOperation.current) return
    pendingOperation.current = true; setPendingBusy(true); setPendingMessage('')
    const generation = ++pendingSequence.current
    const started = Date.now()
    void client.pending().then(value => {
      if (!mounted.current || generation !== pendingSequence.current || current.current.disabled || current.current.client !== client) return
      const items = value.items.filter(pending => pending.plugin_id === item.id && pending.revision === item.revision && pending.digest === item.digest && pending.tool === tool)
      setActive({ ...value, items }); setPendingReadAt(started)
      // Only mount the owner's live pending on this explicit user refresh. The
      // existing component then performs its own real pending metadata lookup.
      // A read must not unmount/abort a separately approved send already shown.
      // Expiry is still checked by its own host metadata and original deadline.
      setActivePending(previous => previous || items[0])
      if (!items.length) setPendingMessage('当前刷新时未发现仍活着且属于此用户 / 插件 / 工具 / revision / digest 的 pending。不会等 final result、续 TTL 或保持 child。')
    }).catch(error => {
      if (mounted.current && generation === pendingSequence.current) { setActive(undefined); setPendingMessage(brokerPreparationError(error)) }
    }).finally(() => { pendingOperation.current = false; if (mounted.current) setPendingBusy(false) })
  }

  return <section aria-label="插件具体目标准备与运行中请求" className="space-y-4 rounded-xl border border-border/70 bg-card/40 p-4 text-xs leading-6">
    <div><h4 className="font-semibold">准备具体目标（独立于插件执行批准）</h4><p className="mt-1 text-muted-foreground">先明确填写 args.broker_requests。仅本次明确选中的 file/data 目标登记 stat 身份，不读取选中目标内容、不创建文件、不授予目录权限。HTTP 只登记显式 UTF-8 body 与宿主 DNS pinned 请求，不发送；之后仍须人工核对完整 fullspec 并批准插件执行。</p></div>
    <p className="text-amber-700 dark:text-amber-300">顺序：先准备 HTTP → 核对并明确填回 args → 再选择本次 file/data。任何 args / revision / config / state / dependency / trust 变化都会使旧源选择不可复用。没有根据 call ID 外推或自动注册 manifest 路径。</p>
    {callRunning && <p role="status" className="text-amber-700 dark:text-amber-300">插件调用正在等待返回：目标准备与草稿修改暂停；下方「刷新运行中请求」仍可手动使用，不延长 child 原始预算。</p>}
    {validation && <p className="text-muted-foreground">{validation}</p>}
    {draft && <Json title="当前明确声明的完整 broker_requests（不是执行批准）" value={draft.requests} />}
    {objectRequests.length > 0 && <div className="space-y-3 border-t border-border/60 pt-3">
      <h5 className="font-medium">一次选择当前声明的全部具体 file/data 对象</h5>
      <p className="text-muted-foreground">逐项确认下列 exact target 与精确 params。若只要部分对象，请先修改 args；本入口不会缩小 source 后再假装匹配原始 args，也不会替你补 object_id / offset / length / write 内容。</p>
      {objectRequests.map(({ request, index }) => <label key={index} className="flex items-start gap-2 rounded-lg border border-border/50 p-3"><input type="checkbox" className="mt-1.5" checked={selected.includes(index)} disabled={blocked || Boolean(objects)} onChange={event => { setObjectConfirmed(false); setSelected(old => event.target.checked ? [...old, index] : old.filter(value => value !== index)) }} /><span className="min-w-0 flex-1"><strong>#{index} · {request.op} · {request.target_argument}</strong><span className="block break-all font-mono">{String(draft?.args[request.target_argument])}</span><pre className="whitespace-pre-wrap break-all text-[11px]">{showJson(request.params)}</pre></span></label>)}
      <label className="flex items-start gap-2"><input type="checkbox" className="mt-1.5" checked={objectConfirmed} disabled={blocked || !allSelected || Boolean(objects)} onChange={event => setObjectConfirmed(event.target.checked)} /><span>我明确选择了以上每一个已声明具体对象，仅允许本次身份登记和接下来的一次完整预览；这不是读写执行批准。</span></label>
      <Button size="sm" variant="outline" disabled={blocked || !allSelected || !objectConfirmed || Boolean(objects)} onClick={selectObjects}>{busy === 'objects' ? '正在核对身份…' : '仅登记这些具体目标（不读取内容）'}</Button>
      {objects && <><Json title="完整 selected 对象 summary / identities / fingerprints / required 精确 params" value={objects} /><p className="text-muted-foreground">本次仅选择完成；上限 120 秒且只允许一次后续 preview。点击主面板「生成操作预览」，核对 fullspec 后再独立批准。失败不会重新选择或自动续期。</p></>}
    </div>}
    {httpRequests.length > 0 && <div className="space-y-3 border-t border-border/60 pt-3">
      <h5 className="font-medium">显式 HTTPS 请求预注册</h5>
      <p className="text-muted-foreground">仅 UTF-8 文本；binary / secret / private / 明文 HTTP 严格拒绝。没有自定义 auth headers、approved_ips 或放宽策略输入。点击准备才由宿主查询真实 DNS；不会通过 requests 直接发送网络。</p>
      <label className="block space-y-1"><span>选择当前明确声明的 HTTP request</span><select aria-label="本次HTTP broker request" className="h-9 w-full rounded-lg border border-border bg-input px-3" value={requestIndex ?? ''} disabled={blocked} onChange={event => {
        clearHttp()
        const index = event.target.value === '' ? undefined : Number(event.target.value)
        setRequestIndex(index)
        const request = index === undefined ? undefined : draft?.requests[index]
        setUrl(request && draft ? String(draft.args[request.target_argument]) : '')
        setMethod(request && typeof request.params.method === 'string' ? request.params.method : '')
        setResponseLimit(request && typeof request.params.max_response_bytes === 'number' ? String(request.params.max_response_bytes) : '')
        setBodyText('')
      }}><option value="">请选择一次具体请求，不猜操作</option>{httpRequests.map(({ request, index }) => <option key={index} value={index}>#{index} · {request.target_argument}</option>)}</select></label>
      {selectedHttp?.op === 'http.request' && <>
        <label className="block space-y-1"><span>本次 URL（必须与当前 args 目标完全一致；改变 URL 请先改 args）</span><Input aria-label="准备HTTPS URL" value={url} disabled={blocked} maxLength={4000} autoComplete="off" onChange={event => { clearHttp(); setUrl(event.target.value) }} /></label>
        <div className="grid gap-3 sm:grid-cols-2"><label className="space-y-1"><span>显式 method（不默认 GET）</span><select aria-label="准备HTTP method" className="h-9 w-full rounded-lg border border-border bg-input px-3" value={method} disabled={blocked} onChange={event => { clearHttp(); setMethod(event.target.value) }}><option value="">请明确选择</option>{['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'].map(value => <option key={value}>{value}</option>)}</select></label><label className="space-y-1"><span>最大响应字节数（1–12000，不默认）</span><Input aria-label="HTTP最大响应字节数" value={responseLimit} disabled={blocked} maxLength={5} inputMode="numeric" onChange={event => { clearHttp(); setResponseLimit(event.target.value) }} /></label></div>
        <label className="block space-y-1"><span>本次完整 body UTF-8（GET / HEAD 必须明确保持空文本；不是文件上传 / Base64）</span><Textarea aria-label="HTTP完整body文本" className="min-h-28 font-mono text-xs" value={bodyText} disabled={blocked} maxLength={12000} autoComplete="off" spellCheck={false} onChange={event => { clearHttp(); setBodyText(event.target.value) }} /></label>
        <label className="flex items-start gap-2"><input type="checkbox" className="mt-1.5" checked={httpConfirmed} disabled={blocked || Boolean(http)} onChange={event => setHttpConfirmed(event.target.checked)} /><span>我已明确核对上述 URL、method、完整 body 与响应上限，同意仅登记这些字节并进行一次宿主 DNS 预览；无 HTTP 发送授权。</span></label>
        <Button size="sm" variant="outline" disabled={blocked || !httpConfirmed || !method || !url || !responseLimit || Boolean(http)} onClick={prepareHttp}>{busy === 'http' ? '正在登记并查询 DNS…' : '准备精确 HTTP descriptor（不发送）'}</Button>
      </>}
      {http && <><Json title="完整 HTTP descriptor params / pinned IPs / body / fingerprints / 待填回 args" value={http} /><label className="flex items-start gap-2"><input type="checkbox" className="mt-1.5" checked={useConfirmed} disabled={blocked} onChange={event => setUseConfirmed(event.target.checked)} /><span>我已核对完整返回内容，同意仅把该 request 的宿主 descriptor 填回当前 args；替换会清除旧 fullspec 预览，绝不执行。随后重新选择 file/data。</span></label><Button size="sm" variant="outline" disabled={blocked || !useConfirmed} onClick={() => {
        if (disabled || callRunning || !useConfirmed || currentContext.current !== context) return
        if (Date.now() >= httpDeadline) { setMessage('这份 HTTP 准备已过期；不填回、不自动续期。'); return }
        onUseArgs(showJson(http.args))
      }}>确认填回本次 args（不执行）</Button></>}
    </div>}
    {message && <p role="alert" className="whitespace-pre-wrap break-words text-destructive">{message}</p>}
    <div className="space-y-3 border-t border-border/60 pt-3">
      <h5 className="font-medium">运行中的真实模型 pending（独立于 call final result）</h5>
      <p className="text-muted-foreground">仅手动刷新当前认证用户仍活着的 child 请求。不会定时刷新、续 TTL、保活 child 或悬挂等待人工。普通 child 原始总预算仍为 10 秒；手动选择也不重置 deadline。</p>
      <Button size="sm" variant="outline" disabled={disabled || pendingBusy} onClick={refreshPending}>{pendingBusy ? '正在核验真实 registry…' : '刷新运行中请求（不续期）'}</Button>
      {pendingMessage && <p role="status" className="whitespace-pre-wrap break-words">{pendingMessage}</p>}
      {active && <><p className="text-muted-foreground">本次读取时匹配 {active.items.length} 项；以下剩余值只是读取快照，不代表当前仍有效，发送前宿主再次核验。</p>{active.items.map(pending => <div key={pending.request_id} className="space-y-2 rounded-lg border border-border/60 p-3"><p className="break-all font-mono">{pending.request_id}</p><p>读取时剩余至多 {pending.expires_in.toFixed(2)} 秒 · deadline monotonic {pending.current_deadline_monotonic} · owner SHA {pending.owner_session_id_sha256}</p><Button size="xs" variant="outline" disabled={disabled} onClick={() => {
        if (Date.now() >= pendingReadAt + pending.expires_in * 1000) { setPendingMessage('当前读取快照已过截止时间，请手动刷新；没有续期。'); return }
        setActivePending(pending)
      }}>选择此真实 pending 并核验</Button></div>)}<Json title="完整 owner-filtered active metadata（原始期限，不重置）" value={active} /></>}
      {activePending && <PluginModelPending key={`active:${activePending.request_id}`} result={activePending} disabled={disabled} />}
    </div>
  </section>
}
