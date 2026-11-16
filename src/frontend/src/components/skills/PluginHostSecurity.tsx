import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, RefreshCw, ShieldCheck } from 'lucide-react'
import { api, ApiError, json } from '@/api/client'
import { useApp } from '@/context/AppContext'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'

const SECURITY_API = '/v11/plugin-host/security'
const APPROVAL_SEMANTICS = 'parent_preparation_only_each_invocation_requires_exact_approval'
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const pretty = (value: unknown) => JSON.stringify(value, null, 2) ?? 'null'
const field = (value: unknown) => value === undefined ? '未提供（不能推断）' : typeof value === 'string' ? value : pretty(value)
const hash = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const requestSignal = (controller: AbortController) => AbortSignal.any([controller.signal, AbortSignal.timeout(15000)])

type HostSettings = {
  schema: 'lingxi.strict-host-settings.v1'
  parent: string
  parent_identity: { volume: string; file_id: string; directory: true }
  host_parent_approved: true
  approval_semantics: typeof APPROVAL_SEMANTICS
}
type RuntimeStatus = {
  available: boolean; reason_code: string; mode: 'ordinary' | 'mcp'
  strict_required: true; fallback_allowed: false; runtime_inventory_sha256: string | null; notice: string
}
type HostStatus = {
  configured: boolean; settings: HostSettings | null; ordinary: RuntimeStatus; mcp: RuntimeStatus
  execution_granted: false; notice: string
}
type SetupPreview = {
  token: string; fingerprint: string; snapshot: HostSettings; expires_in: number
  native_actions_performed: false; execution_granted: false; notice: string
}
type BoundPreview = SetupPreview & { scope: string; input: string; deadline: number; expiresAt: number }

function invalidResponse(): never { throw new Error('严格插件宿主返回格式不符，未取得可用授权；请只读刷新后重新预览') }
function settings(value: unknown): value is HostSettings {
  return object(value) && value.schema === 'lingxi.strict-host-settings.v1' && typeof value.parent === 'string'
    && object(value.parent_identity) && typeof value.parent_identity.volume === 'string'
    && typeof value.parent_identity.file_id === 'string' && value.parent_identity.directory === true
    && value.host_parent_approved === true && value.approval_semantics === APPROVAL_SEMANTICS
}
function runtimeStatus(value: unknown, mode: 'ordinary' | 'mcp'): value is RuntimeStatus {
  return object(value) && typeof value.available === 'boolean' && typeof value.reason_code === 'string'
    && value.mode === mode && value.strict_required === true && value.fallback_allowed === false
    && (value.runtime_inventory_sha256 === null || hash(value.runtime_inventory_sha256))
    && (!value.available || hash(value.runtime_inventory_sha256)) && typeof value.notice === 'string'
}
function parseStatus(value: unknown): HostStatus {
  if (!object(value) || typeof value.configured !== 'boolean' || value.execution_granted !== false
    || typeof value.notice !== 'string' || !runtimeStatus(value.ordinary, 'ordinary') || !runtimeStatus(value.mcp, 'mcp')
    || (value.configured ? !settings(value.settings) : value.settings !== null)) invalidResponse()
  return value as HostStatus
}
function parsePreview(value: unknown, parent: string): SetupPreview {
  if (!object(value) || !settings(value.snapshot) || value.snapshot.parent !== parent
    || typeof value.token !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(value.token) || !hash(value.fingerprint)
    || typeof value.expires_in !== 'number' || !Number.isFinite(value.expires_in) || value.expires_in <= 0 || value.expires_in > 120
    || value.native_actions_performed !== false || value.execution_granted !== false || typeof value.notice !== 'string') invalidResponse()
  return value as SetupPreview
}
function securityErrorCode(cause: unknown): string {
  if (!(cause instanceof ApiError) || !object(cause.payload) || !object(cause.payload.detail)) return ''
  return typeof cause.payload.detail.code === 'string' ? cause.payload.detail.code : ''
}
function securityError(cause: unknown): string {
  if (cause instanceof ApiError) {
    if (cause.status === 401 || cause.status === 403) return '本地 Cookie 会话认证不可用，请从零汐启动器重新打开；旧预览已失效'
    const code = securityErrorCode(cause)
    const messages: Record<string, string> = {
      strict_host_settings_invalid: '已保存的宿主配置无效，或父目录实体身份已变化；不能把它当作已配置，请核查路径后重新设置',
      strict_parent_not_ordinary: '父目录须为后端本机已存在的普通绝对目录，不能包含链接、重解析路径或受保护目录',
      strict_setup_capacity: '待确认预览已达上限，请等待旧预览过期后手动重试',
      strict_setup_confirmation_invalid: '本次预览已过期、失效、已使用或不属于当前 Cookie 会话；必须重新预览',
      strict_parent_identity_changed: '父目录的卷号或文件实体 ID 已变化；未按旧身份保存，请重新检查并预览',
      strict_runtime_busy: '仍有插件调用或待清理资源，不能重组宿主；请待其结束并核查状态后重新预览',
    }
    return messages[code] || cause.message
  }
  return cause instanceof Error ? cause.message : '无法连接严格插件宿主'
}

/** Explicit projection of the exact server intent; never fabricate missing OS evidence or ACLs. */
export function StrictSandboxSnapshot({ snapshot }: { snapshot: unknown }) {
  if (!object(snapshot)) return null
  const specs = ['sandbox_spec', 'strict_mcp_spec'].filter((key) => snapshot[key] !== undefined)
  if (!specs.length) return snapshot.action === 'call'
    ? <p className="rounded-xl border border-amber-500/20 bg-amber-500/5 p-3 text-xs leading-6">本次 snapshot 未提供 sandbox_spec / strict_mcp_spec，不能据此声称具有严格 OS 隔离。请核查宿主真实可用状态与后端阻止原因；不回退到旧普通进程。</p>
    : null
  return <div className="space-y-3">{specs.map((key) => {
    const rawSpec = snapshot[key]
    const spec = object(rawSpec) ? rawSpec : undefined
    const plan = spec && object(spec.policy) ? spec.policy : undefined
    const policy = plan && object(plan.policy) ? plan.policy : undefined
    const paths = plan && object(plan.paths) ? plan.paths : undefined
    const job = plan && object(plan.job) ? plan.job : undefined
    const grants = plan && Array.isArray(plan.acl_grants) ? plan.acl_grants : undefined
    return <section key={key} aria-label={`${key} 严格隔离精确意图`} className="space-y-3 rounded-xl border border-primary/20 bg-primary/5 p-3 text-xs">
      <h4 className="font-semibold">{key} · 本次严格隔离意图（不是已执行 / 验收证据）</h4>
      <dl className="grid gap-3 sm:grid-cols-2">
        {[
          ['独立 root', paths?.root], ['专用 AppContainer profile', spec?.profile_name],
          ['专用 SID', spec?.appcontainer_sid ?? plan?.appcontainer_sid], ['本次 call_id', spec?.call_id],
          ['runtime · 只读代码运行时路径', paths?.runtime], ['code · 插件代码路径', paths?.code],
          ['scratch · 本次临时写入路径', paths?.scratch], ['host_approved_parent', spec?.host_approved_parent],
        ].map(([label, value]) => <div key={String(label)}><dt className="text-muted-foreground">{String(label)}</dt><dd className="mt-1 whitespace-pre-wrap break-all font-mono text-[11px]">{field(value)}</dd></div>)}
      </dl>
      <div className="space-y-2"><h5 className="font-medium">逐路径 ACL · {grants ? `${grants.length} 条（严格策略应为 7 条）` : '未提供，不推断授权'}</h5>
        {grants?.map((grant, index) => <div key={index} className="space-y-1 rounded-lg border border-border/60 bg-card/50 p-2.5">
          {object(grant) ? <><p className="break-all font-mono">{index + 1}. {field(grant.path)}</p><p className="break-all">SID：{field(grant.sid)}</p><p>rights：{field(grant.rights)} · scope：{field(grant.scope)}</p></> : <pre className="whitespace-pre-wrap break-all">{pretty(grant)}</pre>}
        </div>)}
        {grants && grants.length !== 7 && <p className="text-amber-700 dark:text-amber-300">ACL 数量不符严格 7 条策略，不能将此展示当作已核验隔离。</p>}
      </div>
      <dl className="grid gap-3 sm:grid-cols-2">
        {[
          ['墙钟限制（秒，宿主 watchdog）', job?.wall_seconds ?? policy?.wall_seconds],
          ['每进程内存上限（bytes）', job?.process_memory_bytes ?? policy?.memory_bytes],
          ['Job 总内存上限（bytes）', job?.job_memory_bytes],
          ['CPU hard cap（1/100 %；2000 = 20%）', job?.cpu_rate ?? policy?.cpu_rate],
          ['active process 上限', job?.active_processes ?? policy?.active_processes],
          ['普通插件 / MCP 共享并发上限（FIFO）', policy?.global_concurrency],
          ['AppContainer capabilities', plan?.capabilities ?? policy?.capabilities],
          ['network capabilities', plan?.network_capabilities ?? policy?.network_capabilities],
          ['fallback_allowed（只接受 false）', spec?.fallback_allowed ?? plan?.fallback_allowed ?? policy?.fallback_allowed],
          ['actual_verified / os_actions_performed', { actual_verified: plan?.actual_verified, os_actions_performed: plan?.os_actions_performed }],
        ].map(([label, value]) => <div key={String(label)}><dt className="text-muted-foreground">{String(label)}</dt><dd className="mt-1 whitespace-pre-wrap break-all font-mono text-[11px]">{field(value)}</dd></div>)}
      </dl>
      <p className="leading-6 text-muted-foreground">每次仍须批准完整 snapshot 的专用 root / profile / SID、7 条逐路径 ACL、限额和参数，随后由宿主重新核验。严格隔离不是绝对不可读的环境：公开可读的 Windows 系统资源仍可能可读；权限声明本身不构成 OS 限制或外部 I/O 代理授权。</p>
      <details className="rounded-lg border border-border/60 p-2.5"><summary className="cursor-pointer font-medium">完整 {key}（保留所有字段，不截断）</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all text-[11px] leading-5 text-muted-foreground">{pretty(snapshot[key])}</pre></details>
    </section>
  })}</div>
}

function RuntimeStatusView({ title, value, stale }: { title: string; value: RuntimeStatus; stale: boolean }) {
  return <section className="space-y-2 rounded-xl border border-border/60 p-3 text-xs">
    <div className="flex flex-wrap items-center gap-2"><h3 className="font-medium">{title} · {value.mode}</h3><Badge variant={stale ? 'warning' : value.available ? 'success' : 'danger'}>{stale ? '上次状态 · 未核实' : value.available ? '宿主只读可用性检查通过' : '不可用 · 执行阻止'}</Badge></div>
    <p className="leading-6 text-muted-foreground">strict_required={String(value.strict_required)} · fallback_allowed={String(value.fallback_allowed)}；{value.reason_code || '没有不可用原因码'}。</p>
    <p className="break-all font-mono text-[11px]">运行时 inventory SHA-256：{value.runtime_inventory_sha256 || '未取得有效证据'}</p>
    <p className="leading-6 text-muted-foreground">{value.notice}</p>
  </section>
}

/** Independent recovery/setup entry: package-list failures must not hide host security. */
export function PluginHostSecurity({ disabled = false, onChanged }: { disabled?: boolean; onChanged?: () => void }) {
  const { connection, activeSessionId, activeWorkSessionId } = useApp()
  const scope = JSON.stringify([connection, activeSessionId ?? null, activeWorkSessionId ?? null, disabled])
  const liveScope = useRef(scope)
  liveScope.current = scope
  const connected = connection === 'connected' && !disabled
  const [online, setOnline] = useState(() => navigator.onLine)
  const [status, setStatus] = useState<HostStatus>()
  const [stale, setStale] = useState(true)
  const [settingsInvalid, setSettingsInvalid] = useState(false)
  const [busy, setBusy] = useState(false)
  const [parent, setParent] = useState('') // Deliberately empty: no test directory or remembered grant as a default.
  const [preview, setPreview] = useState<BoundPreview>()
  const [acknowledged, setAcknowledged] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [now, setNow] = useState(() => performance.now())
  const mounted = useRef(false)
  const epoch = useRef(0)
  const lock = useRef<AbortController | null>(null)
  const invalidate = useCallback(() => {
    epoch.current++
    lock.current?.abort(); lock.current = null
    setBusy(false); setPreview(undefined); setAcknowledged(false)
  }, [])
  const refresh = useCallback(async () => {
    invalidate(); setStale(true); setSettingsInvalid(false); setError(''); setNotice('')
    if (!connected || !online) return
    const generation = epoch.current, currentScope = scope, controller = new AbortController()
    lock.current = controller; setBusy(true)
    try {
      const value = parseStatus(await api<unknown>(SECURITY_API, { signal: requestSignal(controller) }))
      if (mounted.current && generation === epoch.current && liveScope.current === currentScope && !controller.signal.aborted) {
        setStatus(value); setStale(false); setSettingsInvalid(false)
      }
    } catch (cause) {
      if (mounted.current && generation === epoch.current && liveScope.current === currentScope && !controller.signal.aborted) {
        setError(securityError(cause)); setSettingsInvalid(securityErrorCode(cause) === 'strict_host_settings_invalid')
      }
    } finally {
      if (lock.current === controller) { lock.current = null; if (mounted.current) setBusy(false) }
    }
  }, [connected, online, scope, invalidate])

  useEffect(() => {
    mounted.current = true
    void refresh()
    return () => { mounted.current = false; epoch.current++; lock.current?.abort(); lock.current = null }
  }, [refresh])
  useEffect(() => {
    const loseContext = () => {
      invalidate(); setStale(true); setSettingsInvalid(false)
      setNotice('页面或网络上下文已变化，旧 token 已从界面丢弃；请只读刷新后重新预览。已发送的请求不能借此撤回。')
    }
    const networkChanged = () => { loseContext(); setOnline(navigator.onLine) }
    const visibilityChanged = () => { if (document.visibilityState !== 'visible') loseContext() }
    window.addEventListener('offline', networkChanged); window.addEventListener('online', networkChanged)
    window.addEventListener('pagehide', loseContext); document.addEventListener('visibilitychange', visibilityChanged)
    return () => {
      window.removeEventListener('offline', networkChanged); window.removeEventListener('online', networkChanged)
      window.removeEventListener('pagehide', loseContext); document.removeEventListener('visibilitychange', visibilityChanged)
    }
  }, [invalidate])
  useEffect(() => {
    if (!preview) return
    setNow(performance.now())
    const timer = window.setInterval(() => setNow(performance.now()), 500)
    return () => window.clearInterval(timer)
  }, [preview])

  const ready = connected && online && !busy && ((!stale && Boolean(status)) || settingsInvalid)
  const generatePreview = async () => {
    if (!ready || lock.current || !parent.trim() || parent.length < 4 || parent.includes('\0')) return
    invalidate(); setError(''); setNotice('')
    const generation = epoch.current, currentScope = scope, input = parent, controller = new AbortController()
    const started = performance.now()
    lock.current = controller; setBusy(true)
    try {
      const value = parsePreview(await api<unknown>(`${SECURITY_API}/preview`, { ...json('POST', { parent: input }), signal: requestSignal(controller) }), input)
      if (mounted.current && generation === epoch.current && liveScope.current === currentScope && !controller.signal.aborted) {
        const deadline = started + value.expires_in * 1000
        setPreview({ ...value, scope: currentScope, input, deadline, expiresAt: Date.now() + Math.max(0, deadline - performance.now()) })
        setAcknowledged(false); setNow(performance.now())
      }
    } catch (cause) {
      if (mounted.current && generation === epoch.current && liveScope.current === currentScope && !controller.signal.aborted) {
        setError(securityError(cause)); setStale(true)
        if (!(cause instanceof ApiError) || cause.status === 401 || cause.status === 403) setSettingsInvalid(false)
      }
    } finally {
      if (lock.current === controller) { lock.current = null; if (mounted.current) setBusy(false) }
    }
  }
  const cancelPreview = () => { if (lock.current) return; invalidate(); setNotice('已取消并丢弃本次预览；没有发送 commit，也没有授予插件执行权限。') }
  const commit = async () => {
    if (!ready || lock.current || !preview || !acknowledged || preview.scope !== liveScope.current
      || preview.input !== parent || preview.deadline <= performance.now() || preview.expiresAt <= Date.now()) return
    const current = preview, generation = epoch.current, controller = new AbortController()
    // Consume the UI token before awaiting. A lost response, rejection or double-click never replays it.
    setPreview(undefined); setAcknowledged(false); setStale(true); setSettingsInvalid(false); setBusy(true); setError('')
    setNotice('只发送一次宿主配置提交，正在等待结果；不会自动重试、启用插件或执行调用。')
    lock.current = controller
    try {
      const result = await api<unknown>(`${SECURITY_API}/commit`, { ...json('POST', { token: current.token, fingerprint: current.fingerprint, user_confirmed: true }), signal: requestSignal(controller) })
      if (!object(result) || result.saved !== true || result.execution_granted !== false || result.native_actions_performed !== false) {
        throw new Error('提交响应无法核实，保存结果未知；请只读刷新，不能重试旧 token')
      }
      if (!mounted.current || generation !== epoch.current || liveScope.current !== current.scope || controller.signal.aborted) return
      setNotice('宿主配置已保存，manager 已重组；未创建 profile、未修改父目录 ACL、未启用或执行插件。正在只读刷新实际可用状态。')
      try { onChanged?.() } catch { /* A host listing refresh failure must never replay commit. */ }
      try {
        const updated = parseStatus(await api<unknown>(SECURITY_API, { signal: requestSignal(controller) }))
        if (mounted.current && generation === epoch.current && liveScope.current === current.scope && !controller.signal.aborted) {
          setStatus(updated); setStale(false)
          setNotice('宿主配置已保存并重新读取；父目录许可仅用于未来准备调用预览。没有自动启用、连接或执行插件，每次调用仍须完整精确审批。')
        }
      } catch (cause) {
        if (mounted.current && generation === epoch.current && !controller.signal.aborted) setError(`保存已确认，但状态刷新失败：${securityError(cause)}；不能把旧状态当作当前可用性`)
      }
    } catch (cause) {
      if (mounted.current && generation === epoch.current && !controller.signal.aborted) {
        const unknown = !(cause instanceof ApiError) || cause.status >= 500
        setError(`${unknown ? '提交结果未知' : '提交未获成功确认'}：${securityError(cause)}。本次 token 已丢弃，不会重试；请只读刷新核对实际保存状态`)
        setNotice('未收到可核实的成功状态；不要根据旧快照推断已保存或未保存。')
      }
    } finally {
      if (lock.current === controller) { lock.current = null; if (mounted.current) setBusy(false) }
    }
  }
  const currentPreview = preview && preview.scope === scope && preview.input === parent ? preview : undefined
  const expired = Boolean(currentPreview && (currentPreview.deadline <= now || currentPreview.expiresAt <= Date.now()))

  return <Card aria-label="严格插件宿主安全设置" aria-busy={busy}>
    <CardHeader><div className="flex flex-wrap items-start justify-between gap-3"><div><CardTitle className="flex items-center gap-2"><ShieldCheck className="size-4 text-primary" />严格插件宿主安全设置</CardTitle><CardDescription className="mt-1">普通插件与包 MCP 的独立宿主设置。先显式选择父目录，再预览并单次确认；不继承任何实验的一次性授权。</CardDescription></div><Button size="sm" variant="outline" disabled={!connected || !online || busy} onClick={() => void refresh()}><RefreshCw className={busy ? 'animate-spin' : ''} />只读刷新状态</Button></div></CardHeader>
    <CardContent className="space-y-4">
      <p className="flex items-start gap-2 rounded-xl border border-amber-500/20 bg-amber-500/5 p-3 text-xs leading-6"><AlertTriangle className="mt-1 size-4 shrink-0" /><span>严格 OS 隔离是否可用，以真实宿主返回的普通 / MCP 状态为准；不可用就阻止执行，没有旧运行时 fallback。配置成功和源码存在都不等于 OS 隔离已验收；公开可读的 Windows 系统资源仍可能可读，不宣称绝对 sandbox。</span></p>
      <div className="flex flex-wrap gap-2"><Badge variant="outline">普通插件 + MCP 共享 10 个 FIFO 并发槽位</Badge><Badge variant="outline">不自动 enable / connect / call</Badge><Badge variant="outline">父目录设置 ≠ 单次执行批准</Badge></div>
      <p className="text-xs leading-6 text-muted-foreground">严格策略：普通插件 10 秒 / 256 MiB，stdio MCP 30 秒 / 512 MiB；CPU 20% hard cap、active process 1、零 capabilities。以上为策略限额，不表示当前 OS 观察已通过；实际逐次授权仍以下方调用 snapshot 为准。状态端点不提供实时队列长度，本界面不会伪造占用数据。</p>
      {(!connected || !online) && <p role="status" className="text-xs leading-6 text-muted-foreground">{connection === 'demo' ? '演示模式不读取或提交真实宿主设置。' : !online ? '浏览器报告离线，读取和提交已暂停。' : '本地连接不可用，读取和提交已暂停。'}旧 token 无效；下方如有内容仅是上次状态。</p>}
      {busy && <p role="status" className="text-xs text-muted-foreground">正在处理当前一次请求，不会后台重试或自动授权。</p>}
      {error && <p role="alert" className="rounded-xl border border-red-500/20 bg-red-500/5 p-3 text-xs leading-6 text-red-700 dark:text-red-300">{error}</p>}
      {notice && <p role="status" className="text-xs leading-6 text-muted-foreground">{notice}</p>}
      {settingsInvalid && <p role="status" className="rounded-xl border border-amber-500/20 p-3 text-xs leading-6">后端已认证并报告保存配置失效，当前配置及运行时可用性均不能视为有效。为避免锁死恢复入口，仍可手动输入新父目录并先生成独立预览；修复保存不会沿用旧许可或自动执行。</p>}
      {!status && !busy && !settingsInvalid && <p className="text-xs leading-6 text-muted-foreground">尚未取得已认证的真实宿主状态；不能判断已配置或隔离可用。请连接本地服务后只读刷新。</p>}
      {status && <div className="space-y-3"><div className="flex flex-wrap items-center gap-2"><Badge variant={stale ? 'warning' : status.configured ? 'outline' : 'warning'}>{stale ? '上次配置 · 当前未核实' : status.configured ? '已显式保存父目录' : '未配置严格插件宿主'}</Badge><span className="text-xs text-muted-foreground">execution_granted=false · 没有执行授权</span></div>
        {status.settings ? <dl className="space-y-2 text-xs"><div><dt className="text-muted-foreground">当前保存的父目录（不会自动填入新授权输入）</dt><dd className="mt-1 break-all font-mono">{status.settings.parent}</dd></div><div><dt className="text-muted-foreground">父目录实体身份</dt><dd className="mt-1 break-all font-mono">volume={status.settings.parent_identity.volume} · file_id={status.settings.parent_identity.file_id} · directory=true</dd></div></dl> : <p className="text-xs leading-6 text-muted-foreground">尚无独立父目录许可。请自行准备一个现有的普通专用目录并明确输入；不会使用测试路径、用户数据目录或历史实验批准作为默认授权。</p>}
        <div className="grid gap-3 lg:grid-cols-2"><RuntimeStatusView title="严格普通插件" value={status.ordinary} stale={stale} /><RuntimeStatusView title="严格包 MCP" value={status.mcp} stale={stale} /></div>
        <p className="text-xs leading-6 text-muted-foreground">{status.notice}</p><details className="rounded-xl border border-border/60 p-3"><summary className="cursor-pointer text-xs font-medium">完整宿主只读状态（不删减字段）</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all text-[11px] leading-5 text-muted-foreground">{pretty(status)}</pre></details>
      </div>}
      <section className="space-y-3 border-t border-border/60 pt-4"><h3 className="text-xs font-semibold">独立设置严格调用的父目录</h3><label className="block space-y-2 text-xs"><span>后端本机已存在的普通绝对目录（必须手动输入）</span><Input aria-label="严格插件宿主独立父目录" placeholder="输入专用于严格插件调用的现有父目录绝对路径" value={parent} maxLength={4000} spellCheck={false} autoComplete="off" disabled={!connected || !online || busy} onChange={(event) => { invalidate(); setParent(event.target.value); setError(''); setNotice('输入已变化，旧预览已丢弃；尚未提交任何新设置。') }} /></label>
        <p className="text-xs leading-6 text-muted-foreground">输入和预览均不保存配置，不创建目录 / profile / 进程，不修改父目录 ACL。后端核对无链接 / 重解析路径及现有目录的 volume / file_id；预览和提交间实体变化会被拒绝。取消不提交。更换输入、会话或 connection 后必须重新预览。</p>
        <Button size="sm" variant="outline" disabled={!ready || parent.length < 4 || !parent.trim() || parent.includes('\0')} onClick={() => void generatePreview()}>仅预览父目录设置（不保存）</Button>
      </section>
    </CardContent>
    <Dialog open={Boolean(currentPreview)} onOpenChange={(open) => { if (!open) cancelPreview() }}><DialogContent className="max-w-3xl"><DialogHeader><DialogTitle>明确确认严格插件宿主父目录</DialogTitle><DialogDescription>这是独立的宿主配置许可，不是任何插件或 MCP 的执行许可。只提交下方 token / fingerprint 所绑定的原样 snapshot；取消不发送 commit。</DialogDescription></DialogHeader>
      {currentPreview && <div className="space-y-4 text-xs leading-6"><dl className="space-y-2"><div><dt className="text-muted-foreground">本次精确 parent</dt><dd className="mt-1 break-all font-mono">{currentPreview.snapshot.parent}</dd></div><div><dt className="text-muted-foreground">现有目录实体身份</dt><dd className="mt-1 break-all font-mono">volume={currentPreview.snapshot.parent_identity.volume} · file_id={currentPreview.snapshot.parent_identity.file_id} · directory={String(currentPreview.snapshot.parent_identity.directory)}</dd></div><div><dt className="text-muted-foreground">批准语义（原文）</dt><dd className="mt-1 break-all font-mono">{currentPreview.snapshot.approval_semantics}</dd></div></dl>
        <div className="rounded-xl border border-amber-500/20 bg-amber-500/5 p-3"><h3 className="font-semibold">本次保存的安全副作用与边界</h3><ul className="mt-2 list-disc space-y-1 pl-4"><li>仅持久化本次 host 配置并关闭 / 重组插件 manager；有未结束调用或未清理资源时后端拒绝保存。</li><li>不修改父目录 ACL，不自动创建 AppContainer profile / SID 授权或运行根，不启动插件进程。</li><li>不自动启用插件、连接 MCP、发现工具或调用工具，不授予长期执行权限。</li><li>未来每次调用仍须单独核对完整 snapshot.sandbox_spec / strict_mcp_spec 的新 root、profile、SID、7 条逐路径 ACL、限额、目标和参数，再由人明确批准。</li><li>父目录身份再次校验；旧 token 单次使用且绑定真实 Cookie 会话，失效 / 失败 / unknown 结果不自动重试。</li></ul></div>
        <p>{currentPreview.notice}</p><div><h3 className="mb-2 font-semibold">完整精确 snapshot（全部字段）</h3><pre className="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded-xl border border-border/60 bg-secondary/30 p-3 text-[11px] leading-5">{pretty(currentPreview.snapshot)}</pre></div>
        <p className="break-all font-mono">snapshot fingerprint：{currentPreview.fingerprint}</p><p>本地保守到期时间：{new Date(currentPreview.expiresAt).toLocaleString('zh-CN')}（后端最长 {currentPreview.expires_in} 秒；不会自动续期）。native_actions_performed=false · execution_granted=false。</p>
        {expired && <p role="alert" className="text-red-700 dark:text-red-300">本次预览已过期，不能保存；请取消后重新预览。</p>}
        <label className="flex cursor-pointer items-start gap-2"><input type="checkbox" className="mt-1.5 accent-[var(--primary)]" checked={acknowledged} disabled={!ready || expired} onChange={(event) => setAcknowledged(event.target.checked)} /><span>我已核对原样父目录、现有实体身份、完整 snapshot 和上述安全副作用；只批准此次宿主父目录配置，不批准任何插件或 MCP 执行。</span></label>
      </div>}
      <DialogFooter><Button variant="outline" disabled={busy} onClick={cancelPreview}>取消，不提交</Button><Button disabled={!ready || !currentPreview || !acknowledged || expired} onClick={() => void commit()}><ShieldCheck />确认并只保存此次宿主配置</Button></DialogFooter>
    </DialogContent></Dialog>
  </Card>
}
