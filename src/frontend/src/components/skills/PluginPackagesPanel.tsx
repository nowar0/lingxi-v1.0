import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { AlertTriangle, Archive, CheckCircle2, FileJson, FolderSearch, History, KeyRound, Package, Play, RefreshCw, ShieldAlert, Wrench } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input, Textarea } from '@/components/ui/input'
import { PackageMcpPanel } from './PackageMcpPanel'
import { PackageUiPanel } from './PackageUiPanel'
import { PluginDataPanel } from './PluginDataPanel'
import { PluginHostSecurity, StrictSandboxSnapshot } from './PluginHostSecurity'
import { PluginModelPending } from './PluginModelPending'
import { PluginBrokerPreparation } from './PluginBrokerPreparation'
import {
  PluginRequestError, approvalProblem, blockMessage, canPreviewCandidate,
  createPluginPackagesClient, parsePluginArgs, parsePluginValue, pluginErrorMessage, signatureLabel,
} from '@/lib/pluginPackages'
import type {
  AuditEntry, LifecycleAction, PluginAction, PluginExecution, PluginListing, PluginPackage,
  PluginPackagesClient, PluginPreview, PluginScan, PluginSnapshot, ValueKind,
} from '@/lib/pluginPackages'

export type PluginPackagesPanelProps = {
  /** Same-origin API prefix; the default is the independent package API, not /v6/plugins. */
  apiBase?: string
  /** Host-supplied paths. No hard-coded user directory and no automatic scanning. */
  suggestedRoots?: readonly string[]
  /** Optional native host selector. Return an absolute path visible to the BACKEND, never File bytes. */
  selectLocalPath?: (kind: 'folder' | 'zip') => Promise<string | null | undefined>
  /** Gate actions while the host is disconnected; reads also stop while disabled. */
  disabled?: boolean
  /** Optional host transport/client with the same v1 contract and CSRF boundary. */
  client?: PluginPackagesClient
  onChanged?: () => void
}
type Workspace =
  | { mode: 'source'; action: 'install' | 'update' | 'restore'; item?: PluginPackage; path: string }
  | { mode: 'lifecycle'; action: LifecycleAction; item: PluginPackage }
  | { mode: 'value'; kind: ValueKind; item: PluginPackage; text: string; revision?: number }
  | { mode: 'call'; item: PluginPackage; tool: string; text: string }
  | { mode: 'inspect'; item: PluginPackage }
  | { mode: 'audit'; item: PluginPackage; entries?: AuditEntry[] }
type Attempt = { status: 'success' | 'failed' | 'unknown'; message: string }
const actionNames: Record<PluginAction, string> = {
  install: '安装', update: '更新', restore: '从本地备份恢复', enable: '启用', disable: '停用并撤销后续调用授权',
  rollback: '回滚版本', uninstall: '卸载代码', purge: '独立清除数据', config: '保存配置', state: '保存状态', credentials: '保存凭据引用', call: '执行工具',
}
const valueNames: Record<ValueKind, string> = { config: '配置 JSON', state: '状态 JSON', credentials: '凭据引用 JSON' }
const time = (seconds: number) => new Date(seconds * 1000).toLocaleString('zh-CN')
const jsonText = (value: unknown) => JSON.stringify(value, null, 2) ?? 'null'
const STRICT_PLUGIN_RISK = '严格 OS 隔离须以真实宿主普通 / MCP 状态和每次完整 snapshot 为准；不可用时阻止执行，不回退旧普通进程。权限声明本身不是 OS 限制；公开可读的 Windows 系统资源仍可能可读，不宣称绝对 sandbox。仅运行已审阅并信任的代码。'

function JsonView({ title, value, open = false }: { title: string; value: unknown; open?: boolean }) {
  return <details className="rounded-xl border border-border/60 bg-card/40 p-3" open={open || undefined}>
    <summary className="cursor-pointer text-xs font-medium">{title}</summary>
    <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all text-[11px] leading-5 text-muted-foreground">{jsonText(value)}</pre>
  </details>
}
function Notice({ children, danger = false }: { children: ReactNode; danger?: boolean }) {
  return <div className={`flex gap-2 rounded-xl border p-3 text-xs leading-6 ${danger ? 'border-red-500/20 bg-red-500/5 text-red-700 dark:text-red-300' : 'border-amber-500/20 bg-amber-500/5 text-amber-800 dark:text-amber-300'}`}>
    <AlertTriangle className="mt-1 size-4 shrink-0" /><div className="min-w-0">{children}</div>
  </div>
}
function Permissions({ snapshot }: { snapshot: PluginSnapshot }) {
  return <div className="grid gap-3 sm:grid-cols-2">
    <div className="rounded-xl border border-border/60 p-3"><h4 className="text-xs font-medium">本次完整权限</h4><div className="mt-2 flex flex-wrap gap-1">{snapshot.permissions.length ? snapshot.permissions.map((permission) => <Badge key={permission} variant={permission === 'python.execute' ? 'warning' : 'outline'} className="max-w-full break-all whitespace-normal">{permission}</Badge>) : <span className="text-xs text-muted-foreground">本次没有声明权限（不代表代码安全）</span>}</div></div>
    <div className="rounded-xl border border-border/60 p-3"><h4 className="text-xs font-medium">精确目标</h4>{snapshot.targets.length ? <ul className="mt-2 space-y-2 text-[11px] leading-5">{snapshot.targets.map((target, index) => <li key={index} className="break-all"><span className="text-muted-foreground">{target.permission ? `${target.permission} · ` : ''}{target.kind}</span><pre className="whitespace-pre-wrap">{typeof target.value === 'string' ? target.value : jsonText(target.value)}</pre></li>)}</ul> : <p className="mt-2 text-xs text-muted-foreground">没有声明目标不等于代码安全；是否具备严格 OS 隔离须核对宿主真实状态与本次精确快照。严格模式也不承诺隐藏公开可读系统资源。</p>}</div>
  </div>
}
function ManifestDetails({ item }: { item: PluginPackage }) {
  return <div className="space-y-3">
    <dl className="grid gap-3 text-xs sm:grid-cols-2">
      <div><dt className="text-muted-foreground">插件 ID / 版本</dt><dd className="mt-1 break-all">{item.id} · {item.version} · revision {item.revision}</dd></div>
      <div><dt className="text-muted-foreground">来源签名状态</dt><dd className="mt-1">{signatureLabel(item.signature_status)}</dd></div>
      <div className="sm:col-span-2"><dt className="text-muted-foreground">来源（本机路径不是可信证明）</dt><dd className="mt-1 break-all">{item.source || '来源未知'}</dd></div>
      <div className="sm:col-span-2"><dt className="text-muted-foreground">当前 SHA-256 / 上一版 SHA-256</dt><dd className="mt-1 break-all font-mono text-[11px]">{item.digest || '无'}<br />{item.previous_digest || '无上一版'}</dd></div>
      <div><dt className="text-muted-foreground">保留的数据目录</dt><dd className="mt-1 break-all">{item.data_dir}</dd></div>
      <div><dt className="text-muted-foreground">配置目录</dt><dd className="mt-1 break-all">{item.config_dir}</dd></div>
    </dl>
    <JsonView title={`完整文件与 SHA-256（${Object.keys(item.manifest.files).length} 项，不截断）`} value={item.manifest.files} />
    <JsonView title="包权限 / 各工具权限与目标声明" value={{ package_permissions: item.manifest.permissions || [], implicit_python_permission: item.manifest.entrypoint ? ['python.execute'] : [], tools: item.manifest.tools }} />
    <JsonView title="插件与 Python 依赖声明" value={{ host_api: item.manifest.host_api, dependencies: item.manifest.dependencies || [], python_dependencies: item.manifest.python_dependencies || [] }} />
    <JsonView title="完整 manifest（包技能只读投影；包 MCP 独立授权适配）" value={item.manifest} />
    <p className="text-xs leading-6 text-muted-foreground">包技能由技能目录提供只读详情，启停、版本和删除跟随所属插件包；不会写入旧技能配置。包 MCP 连接和调用分别需要独立预览确认，不自动执行。</p>
  </div>
}
function SnapshotView({ preview, context }: { preview: PluginPreview; context?: PluginPackage }) {
  const snapshot = preview.snapshot
  return <section aria-label="本次操作精确预览" className="space-y-3 border-t border-border/60 pt-4">
    <div className="flex flex-wrap items-center gap-2"><h3 className="text-sm font-semibold">本次精确快照</h3><Badge variant={preview.blocked.length ? 'danger' : 'outline'}>{actionNames[snapshot.action]}</Badge><Badge variant="secondary">revision {snapshot.expected_revision}</Badge></div>
    <dl className="space-y-2 text-xs"><div><dt className="text-muted-foreground">ID / 名称 / 版本</dt><dd className="mt-1 break-all">{snapshot.id}{snapshot.manifest ? ` · ${snapshot.manifest.name} · ${snapshot.manifest.version}` : ''}</dd></div>
      {snapshot.source_path && <div><dt className="text-muted-foreground">后端本机来源路径</dt><dd className="mt-1 break-all">{snapshot.source_path}</dd></div>}
      {snapshot.signature_status && <div><dt className="text-muted-foreground">来源签名</dt><dd className="mt-1">{signatureLabel(snapshot.signature_status)}；哈希匹配不等于来源可信</dd></div>}
      <div><dt className="text-muted-foreground">代码 SHA-256</dt><dd className="mt-1 break-all font-mono text-[11px]">{snapshot.digest || '无'}</dd></div>
    </dl>
    <Permissions snapshot={snapshot} />
    <StrictSandboxSnapshot snapshot={snapshot} />
    {context && !snapshot.manifest && <div className="space-y-2 rounded-xl border border-border/60 p-3"><p className="text-xs leading-6 text-muted-foreground">关联的列表记录（revision {context.revision}，非额外授权字段）：{context.name} · {context.version} · {signatureLabel(context.signature_status)}。来源：<span className="break-all">{context.source || '未知'}</span></p><JsonView title="当前版本完整声明（权限 / 文件 / 工具 / 依赖）" value={context.manifest} />{snapshot.action === 'rollback' && <Notice>列表记录的上一版 SHA-256：<span className="break-all font-mono">{context.previous_digest || '无'}</span>。当前后端预览若未提供目标 manifest / 签名，它们不可从当前版本推断；完整授权范围以下方服务端 snapshot 为准，回滚后仍默认停用，首次执行前需再次检查。</Notice>}</div>}
    {snapshot.manifest && <>
      <JsonView title={`完整文件与 SHA-256（${Object.keys(snapshot.manifest.files).length} 项）`} value={snapshot.manifest.files} open />
      <JsonView title="全部包 / 工具权限、目标和依赖" value={{ permissions: snapshot.manifest.permissions || [], tools: snapshot.manifest.tools, host_api: snapshot.manifest.host_api, dependencies: snapshot.manifest.dependencies || [], python_dependencies: snapshot.manifest.python_dependencies || [] }} open />
      <JsonView title="完整 manifest / 签名声明" value={snapshot.manifest} />
    </>}
    {snapshot.action === 'call' && <><div className="text-xs"><strong>工具：{snapshot.tool}</strong><p className="mt-1 text-muted-foreground">每次执行仅批准下列原样参数与目标。不存在通用主机 RPC 授权。</p></div><JsonView title="本次精确 args（完整）" value={snapshot.args} open /></>}
    {['config', 'state', 'credentials'].includes(snapshot.action) && <JsonView title={snapshot.action === 'credentials' ? '将完整替换为以下凭据 reference 列表（不是 Key）' : '将完整替换为以下 JSON 对象'} value={snapshot.value} open />}
    {snapshot.retained && <p className="rounded-xl bg-primary/5 p-3 text-xs leading-6">保留：{snapshot.retained.join('、')}。卸载仅删除活动与历史代码；恢复须重新选择本地备份包。</p>}
    {['install', 'update', 'restore', 'rollback'].includes(snapshot.action) && <p className="rounded-xl bg-primary/5 p-3 text-xs leading-6">提交后默认停用，不自动启用或执行。数据、配置、状态、凭据引用默认保留。{snapshot.action === 'update' ? '上一版代码保留用于独立回退预览；更新后调用失败时仅停用并保留当前代码和数据，不自动切回旧代码或覆盖数据。涉及数据 schema／代际时，需联合代码与数据的独立预览。' : ''}</p>}
    {snapshot.action === 'enable' && <p className="text-xs leading-6 text-muted-foreground">启用不会运行 Python；每次工具调用仍须重新预览并逐次批准。</p>}
    {snapshot.action === 'disable' && <Notice>停用会阻止宿主后续调用，不等于 OS 级权限撤销，也不能撤回已发生的外部副作用。当前 API 没有独立 grants 删除端点；不虚构权限开关。</Notice>}
    {snapshot.warning && <Notice danger>{snapshot.warning}。清除的是数据、配置、状态与凭据引用；系统凭据撤销依赖宿主适配，不能声称仅删除引用便删除了系统 Key。</Notice>}
    <Notice>{STRICT_PLUGIN_RISK}</Notice>
    {snapshot.risk && <JsonView title="服务端本次风险原文（完整；不代替严格隔离状态与精确意图核验）" value={snapshot.risk} open />}
    <div className="rounded-xl border border-border/60 p-3"><h4 className="text-xs font-medium">依赖 / 前置条件检查</h4>{preview.blocked.length ? <ul className="mt-2 space-y-1 text-xs leading-6 text-red-600 dark:text-red-300">{preview.blocked.map((block, index) => <li key={index}>{blockMessage(block)}</li>)}</ul> : <p className="mt-2 text-xs text-muted-foreground">本次预览没有 blocked 原因；提交时仍会重新校验来源与版本。不会自动安装依赖或运行 pip。</p>}</div>
    <JsonView title="完整准确 snapshot（含所有字段）" value={snapshot} open />
    <div className="text-[11px] leading-5 text-muted-foreground"><p className="break-all">快照指纹：{preview.fingerprint}</p><p>{preview.expires_at ? `授权到期：${time(preview.expires_at)}` : '没有可提交 token（操作被阻止）'}；token 单次使用，服务重启后失效。取消不提交。</p></div>
  </section>
}

/** Standalone package manager. Existing skills and MCP records remain owned by SkillsPanel. */
export function PluginPackagesPanel({ apiBase, suggestedRoots = [], selectLocalPath, disabled = false, client: suppliedClient, onChanged }: PluginPackagesPanelProps) {
  const client = useMemo(() => suppliedClient || createPluginPackagesClient({ apiBase }), [apiBase, suppliedClient])
  const [listing, setListing] = useState<PluginListing>()
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [busy, setBusy] = useState(false)
  const [scanRoot, setScanRoot] = useState('')
  const [scan, setScan] = useState<PluginScan>()
  const [scanError, setScanError] = useState('')
  const [workspace, setWorkspace] = useState<Workspace>()
  const [preview, setPreview] = useState<PluginPreview>()
  const [operationError, setOperationError] = useState('')
  const [attempt, setAttempt] = useState<Attempt>()
  const [execution, setExecution] = useState<PluginExecution>()
  const [acknowledged, setAcknowledged] = useState(false)
  const [purgeText, setPurgeText] = useState('')
  const [now, setNow] = useState(Date.now())
  const [filter, setFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const mounted = useRef(true)
  const clientContext = useRef({ client, disabled })
  clientContext.current = { client, disabled }
  const lock = useRef(false)
  const generation = useRef(0)
  const loadSequence = useRef(0)
  const scanSequence = useRef(0)
  const ready = !disabled && !loading && !loadError && Boolean(listing)

  const refresh = useCallback(async () => {
    if (disabled) return
    const sequence = ++loadSequence.current
    setLoading(true)
    try {
      const value = await client.list()
      if (!mounted.current || sequence !== loadSequence.current) return
      setListing(value); setLoadError('')
    } catch (error) {
      if (mounted.current && sequence === loadSequence.current) setLoadError(pluginErrorMessage(error))
    } finally {
      if (mounted.current && sequence === loadSequence.current) setLoading(false)
    }
  }, [client, disabled])
  useEffect(() => {
    mounted.current = true
    generation.current++; scanSequence.current++; setWorkspace(undefined); setPreview(undefined); setListing(undefined); setScan(undefined); setLoadError(''); setLoading(false); setScanError(''); setAttempt(undefined); setExecution(undefined); setOperationError('')
    void refresh()
    return () => { mounted.current = false; loadSequence.current++; generation.current++; scanSequence.current++ }
  }, [refresh])
  useEffect(() => {
    if (!preview?.expires_at) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [preview])

  const clearPreview = () => { generation.current++; setPreview(undefined); setAttempt(undefined); setExecution(undefined); setOperationError(''); setAcknowledged(false); setPurgeText('') }
  const openWorkspace = (value: Workspace) => { if (lock.current || disabled) return; clearPreview(); setWorkspace(value) }
  const updateWorkspace = (value: Workspace) => { if (lock.current || disabled) return; clearPreview(); setWorkspace(value) }
  const run = async (task: () => Promise<void>) => {
    if (lock.current || disabled) return
    lock.current = true; setBusy(true)
    const context = clientContext.current
    try { await task() }
    catch (error) {
      if (mounted.current && clientContext.current.client === context.client && clientContext.current.disabled === context.disabled) {
        setOperationError(pluginErrorMessage(error))
        if (error instanceof PluginRequestError && ['revision_conflict', 'source_changed', 'files_mismatch', 'invalid_token', 'blocked'].includes(error.code)) await refresh()
      }
    }
    finally { lock.current = false; if (mounted.current) setBusy(false) }
  }
  const latestItem = (id: string) => listing?.items.find((item) => item.id === id)
  const openAction = (item: PluginPackage, action: LifecycleAction) => {
    if (!ready) return
    openWorkspace({ mode: 'lifecycle', item, action })
  }
  const openValue = (item: PluginPackage, kind: ValueKind) => {
    if (lock.current || !ready) return
    openWorkspace({ mode: 'value', kind, item, text: '', revision: undefined })
    void run(async () => {
      const sequence = generation.current
      const value = await client.readValue(item.id, kind)
      if (mounted.current && sequence === generation.current) setWorkspace({ mode: 'value', kind, item, text: jsonText(value.value), revision: value.revision })
    })
  }
  const readAudit = (item: PluginPackage) => {
    if (lock.current || !ready) return
    openWorkspace({ mode: 'audit', item })
    void run(async () => {
      const sequence = generation.current
      const value = await client.audit(item.id)
      if (mounted.current && sequence === generation.current) setWorkspace({ mode: 'audit', item, entries: value.items })
    })
  }
  const reReadValue = () => {
    if (lock.current || !ready || workspace?.mode !== 'value') return
    const current = workspace
    clearPreview()
    setWorkspace({ ...current, revision: undefined })
    void run(async () => {
      const sequence = generation.current
      const value = await client.readValue(current.item.id, current.kind)
      if (mounted.current && sequence === generation.current) setWorkspace({ ...current, text: jsonText(value.value), revision: value.revision })
    })
  }
  const makePreview = () => {
    if (lock.current || !ready || !workspace || workspace.mode === 'inspect' || workspace.mode === 'audit') return
    const current = workspace
    clearPreview()
    void run(async () => {
      const sequence = generation.current
      let value: PluginPreview
      if (current.mode === 'source') {
        const item = current.item && latestItem(current.item.id)
        if (current.action !== 'install' && !item) throw new Error('插件记录已变化，请刷新列表后重新选择目标')
        value = await client.previewSource(current.action, current.path, item?.revision || 0, item?.id)
      } else {
        const item = latestItem(current.item.id)
        if (!item) throw new Error('插件记录已变化，请刷新列表后重新选择目标')
        if (current.mode === 'lifecycle') value = await client.previewAction(item.id, current.action, item.revision)
        else if (current.mode === 'value') {
          if (current.revision === undefined) throw new Error('请先读取配置与版本快照')
          if (current.revision !== item.revision) throw new Error('记录版本已变化，请重新读取配置后编辑，不能覆盖旧快照')
          value = await client.previewValue(item.id, current.kind, current.revision, current.text)
        } else value = await client.previewCall(item.id, current.tool, current.text, item.revision)
      }
      if (mounted.current && sequence === generation.current) { setPreview(value); setNow(Date.now()) }
    })
  }
  const approve = () => {
    if (!ready || !preview || attempt || !acknowledged || approvalProblem(preview) || (preview.snapshot.action === 'purge' && purgeText !== `清除 ${preview.snapshot.id}`)) return
    const current = preview
    const sequence = generation.current
    const currentClient = client
    void run(async () => {
      // Consumed in UI BEFORE awaiting; the contract client separately rejects token reuse.
      setAttempt({ status: 'unknown', message: '请求已发送；不要重复操作，正在等待本次结果。' })
      try {
        if (current.snapshot.action === 'call') {
          const result = await client.execute(current)
          if (mounted.current && sequence === generation.current) { setExecution(result); setAttempt({ status: 'success', message: '本次工具调用已完成。再次执行必须重新预览并重新授权。' }) }
        } else {
          await client.commit(current)
          if (mounted.current && sequence === generation.current) setAttempt({ status: 'success', message: '本次操作已提交。界面不会自动启用插件或执行工具。' })
        }
        try { onChanged?.() } catch { /* Host notification must never trigger an operation retry. */ }
      } catch (error) {
        if (mounted.current && sequence === generation.current) setAttempt({ status: error instanceof PluginRequestError && error.unknownOutcome ? 'unknown' : 'failed', message: pluginErrorMessage(error) })
      } finally {
        // Safe read after both success and failure; NEVER replay the commit/execute.
        if (mounted.current && clientContext.current.client === currentClient && !clientContext.current.disabled) await refresh()
      }
    })
  }
  const choosePath = (kind: 'folder' | 'zip') => {
    if (lock.current || !ready || !selectLocalPath || workspace?.mode !== 'source') return
    const current = workspace
    clearPreview()
    void run(async () => {
      const sequence = generation.current
      const path = await selectLocalPath(kind)
      if (path && mounted.current && sequence === generation.current) setWorkspace({ ...current, path })
    })
  }
  const scanLocal = () => {
    if (lock.current || !ready || !scanRoot.trim()) return
    const root = scanRoot
    const sequence = ++scanSequence.current
    setScan(undefined); setScanError('')
    void run(async () => {
      try {
        const value = await client.scan(root)
        if (mounted.current && sequence === scanSequence.current) setScan(value)
      } catch (error) {
        if (mounted.current && sequence === scanSequence.current) setScanError(pluginErrorMessage(error))
      }
    })
  }

  let validation = ''
  if (workspace?.mode === 'value' && workspace.revision !== undefined) {
    try { parsePluginValue(workspace.text, workspace.kind) } catch (error) { validation = pluginErrorMessage(error) }
  } else if (workspace?.mode === 'call') {
    try { parsePluginArgs(workspace.text) } catch (error) { validation = pluginErrorMessage(error) }
  }
  const previewable = workspace && !['inspect', 'audit'].includes(workspace.mode)
  const callItem = workspace?.mode === 'call' ? latestItem(workspace.item.id) : undefined
  const invalidInput = Boolean(validation) || (workspace?.mode === 'source' && !workspace.path.trim()) || (workspace?.mode === 'value' && (workspace.revision === undefined || workspace.revision !== latestItem(workspace.item.id)?.revision)) || (workspace?.mode === 'call' && (!workspace.tool || !callItem?.enabled || !callItem.installed || !callItem.manifest.entrypoint || callItem.missing || callItem.files_mismatch || callItem.blocked.length > 0))
  const approvalError = preview ? approvalProblem(preview, now) : null
  const displayed = (listing?.items || []).filter((item) => {
    const textMatch = `${item.id} ${item.name} ${item.version}`.toLocaleLowerCase().includes(filter.toLocaleLowerCase())
    return textMatch && (statusFilter === 'all' || (statusFilter === 'installed' && item.installed) || (statusFilter === 'uninstalled' && !item.installed) || (statusFilter === 'enabled' && item.enabled) || (statusFilter === 'attention' && (item.missing || item.files_mismatch || item.needs_restart || Boolean(item.error) || item.blocked.length > 0)))
  })
  const target = workspace && 'item' in workspace ? workspace.item : undefined
  const dialogTitle = workspace?.mode === 'source' ? `${actionNames[workspace.action]}本地插件包` : workspace?.mode === 'lifecycle' ? actionNames[workspace.action] : workspace?.mode === 'value' ? `编辑${valueNames[workspace.kind]}` : workspace?.mode === 'call' ? '逐次授权工具调用' : workspace?.mode === 'audit' ? '插件操作审计' : '插件包详情'

  return <div className="space-y-4" aria-label="本地插件包管理" aria-busy={busy || loading}>
    <details className="personal-disclosure"><summary>高级维护：插件宿主隔离与目录</summary><PluginHostSecurity disabled={disabled || busy} onChanged={() => { clearPreview(); setWorkspace(undefined); setScan(undefined); void refresh(); onChanged?.() }} /></details>
    <Card>
      <CardHeader><div className="flex flex-wrap items-start justify-between gap-3"><div className="flex gap-3"><div className="service-icon size-10"><Package className="size-5" /></div><div><CardTitle>本地插件包</CardTitle><CardDescription className="mt-1">独立于技能与 MCP 连接。预览 → 明确确认 → 单次提交，安装与更新默认停用。</CardDescription></div></div><div className="flex gap-2"><Button size="sm" variant="outline" disabled={busy || loading || disabled} onClick={() => { clearPreview(); void refresh() }}><RefreshCw className={loading ? 'animate-spin' : ''} />刷新</Button><Button size="sm" disabled={busy || !ready} onClick={() => openWorkspace({ mode: 'source', action: 'install', path: '' })}><Archive />选择本地包</Button></div></div></CardHeader>
      <CardContent className="space-y-4">
        {disabled && <Notice>宿主当前不可用，插件包读取与操作已暂停。不会使用演示数据代替真实安装状态。</Notice>}
        {loadError && <div role="alert"><Notice danger><p>列表刷新失败：{loadError}</p><p>下列内容如仍显示，只是上次快照；所有变更已禁用，请刷新恢复连接。</p></Notice></div>}
        <Notice>{STRICT_PLUGIN_RISK}</Notice>
        {listing?.isolation_warning && <JsonView title="后端原始隔离风险说明（原文，不作为严格 OS 可用性证据）" value={listing.isolation_warning} />}
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"><Badge variant="secondary">{listing ? `${listing.items.length} 条本地记录` : loading ? '正在读取' : '尚未读取'}</Badge><Badge variant="outline">远程市场仅规划 / deferred，不可安装</Badge><span>不自动上传 ZIP，不联网下载依赖。</span></div>
        <div className="flex flex-col gap-2 sm:flex-row"><Input aria-label="搜索插件包 ID 或名称" placeholder="搜索名称、ID 或版本" value={filter} onChange={(event) => setFilter(event.target.value)} className="sm:max-w-md" /><select aria-label="按插件状态筛选" className="h-9 rounded-lg border border-border bg-input px-3 text-xs" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">全部状态</option><option value="installed">已安装</option><option value="enabled">已启用</option><option value="uninstalled">已卸载 / 数据保留</option><option value="attention">需要处理</option></select></div>
        {loading && !listing && <p role="status" className="py-6 text-center text-xs text-muted-foreground">正在读取插件包生命周期清单…</p>}
        {!loading && !loadError && listing && !displayed.length && <div className="rounded-xl border border-dashed border-border p-7 text-center"><Package className="mx-auto size-6 text-muted-foreground" /><p className="mt-3 text-sm">{listing.items.length ? '没有匹配的插件包' : '尚无本地插件包记录'}</p><p className="mt-2 text-xs leading-6 text-muted-foreground">选择后端本机目录或 ZIP 路径先预览。扫描结果只是候选，不能跳过确认安装。</p></div>}
        <div className="grid gap-3 xl:grid-cols-2">{displayed.map((item) => <Card key={item.id} className="border border-border/60 bg-card/40 shadow-none">
          <CardHeader className="pb-3"><div className="flex items-start justify-between gap-2"><div className="min-w-0"><h3 className="break-all text-sm font-semibold">{item.name}<span className="ml-2 text-[11px] font-normal text-muted-foreground">v{item.version}</span></h3><p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">{item.id} · revision {item.revision}</p></div><Badge variant={item.signature_status === 'verified_trusted' ? 'success' : item.signature_status === 'invalid' ? 'danger' : 'outline'}>{signatureLabel(item.signature_status)}</Badge></div><p className="pt-1 text-xs leading-5 text-muted-foreground">{item.manifest.description || '此包未提供描述'}</p></CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap gap-1.5"><Badge variant={item.installed ? 'default' : 'secondary'}>{item.installed ? 'installed · 已安装' : '未安装 · 保留记录'}</Badge><Badge variant={item.enabled ? 'success' : 'secondary'}>{item.enabled ? 'enabled · 已启用' : 'disabled · 已停用'}</Badge><Badge variant={item.loaded ? 'warning' : 'outline'}>{item.loaded ? 'loaded · 运行中' : '未加载'}</Badge><Badge variant={item.needs_restart ? 'warning' : 'outline'}>{item.needs_restart ? 'needs_restart · 需要重启' : '无需重启'}</Badge><Badge variant={item.missing ? 'danger' : 'outline'}>{item.missing ? 'missing · 代码缺失' : '无代码缺失标记'}</Badge><Badge variant={item.files_mismatch ? 'danger' : !item.installed || item.missing ? 'secondary' : 'success'}>{!item.installed ? '完整性未检查（已卸载）' : item.files_mismatch ? 'integrity · 文件不匹配' : item.missing ? '完整性不可用' : '完整性检查通过'}</Badge>{item.pending_update && <Badge variant="warning">更新后首次调用待验证</Badge>}</div>
            <p className="break-all text-[11px] leading-5 text-muted-foreground">来源：{item.source || '未知'}<br />SHA-256：<span className="font-mono">{item.digest || '无'}</span></p>
            <div className="rounded-lg border border-border/60 p-2.5 text-xs leading-6"><p className="font-medium">dependencies · {item.blocked.length ? `${item.blocked.length} 项阻止条件` : item.installed ? '未发现阻止条件' : '未安装，不评估启用条件'}</p>{item.blocked.length > 0 && <ul className="mt-1 text-amber-700 dark:text-amber-300">{item.blocked.map((block, index) => <li key={index}>{blockMessage(block)}</li>)}</ul>}<p className="text-[11px] text-muted-foreground">声明：{item.manifest.dependencies?.length || 0} 个插件依赖 · {item.manifest.python_dependencies?.length || 0} 个 Python 依赖</p></div>
            {item.error && <p role="alert" className="break-words text-xs text-red-600 dark:text-red-300">后端报告：{item.error}</p>}
            <div className="flex flex-wrap gap-2"><Button size="xs" variant="outline" disabled={busy || !ready} onClick={() => openWorkspace({ mode: 'inspect', item })}>包详情 / 权限 / 文件</Button><Button size="xs" variant="outline" disabled={busy || !ready} onClick={() => readAudit(item)}><History />审计</Button>
              {item.installed ? <><Button size="xs" variant="soft" disabled={busy || !ready || (!item.enabled && (item.missing || item.files_mismatch || item.blocked.length > 0))} onClick={() => openAction(item, item.enabled ? 'disable' : 'enable')}>{item.enabled ? '停用 / 撤销后续授权' : '预览启用'}</Button><Button size="xs" variant="outline" disabled={busy || !ready} onClick={() => openWorkspace({ mode: 'source', action: 'update', item, path: '' })}>更新</Button><Button size="xs" variant="outline" disabled={busy || !ready || !item.previous_digest} onClick={() => openAction(item, 'rollback')}>回滚</Button><Button size="xs" variant="outline" disabled={busy || !ready} onClick={() => openAction(item, 'uninstall')}>卸载（保留数据）</Button></> : <><Button size="xs" variant="soft" disabled={busy || !ready} onClick={() => openWorkspace({ mode: 'source', action: 'restore', item, path: '' })}>从本地包恢复</Button><Button size="xs" variant="outline" className="text-red-600 dark:text-red-300" disabled={busy || !ready} onClick={() => openAction(item, 'purge')}>单独清除数据…</Button></>}
            </div>
            {'ui' in item.manifest && item.manifest.ui !== null && typeof item.manifest.ui === 'object' && <PackageUiPanel key={`ui:${item.id}:${item.revision}:${item.digest}:${item.enabled}`} pluginId={item.id} revision={item.revision} digest={item.digest} installed={item.installed} enabled={item.enabled} disabled={disabled || busy || Boolean(loadError) || item.missing || item.files_mismatch || item.blocked.length > 0} apiBase={apiBase} onChanged={() => { clearPreview(); void refresh(); onChanged?.() }} />}
            {Array.isArray(item.manifest.mcp) && item.manifest.mcp.length > 0 && <PackageMcpPanel key={`${item.id}:${item.revision}:${item.digest}:${item.enabled}`} pluginId={item.id} revision={item.revision} digest={item.digest} installed={item.installed} enabled={item.enabled} disabled={disabled || busy || !ready} apiBase={apiBase} onChanged={onChanged} />}
            {item.installed && <PluginDataPanel key={`data:${item.id}:${item.revision}`} pluginId={item.id} revision={item.revision} installed={item.installed} disabled={disabled || busy || Boolean(loadError) || loading} onChanged={() => { clearPreview(); void refresh(); onChanged?.() }} />}
            <div className="flex flex-wrap gap-2 border-t border-border/60 pt-3"><Button size="xs" variant="ghost" disabled={busy || !ready} onClick={() => openValue(item, 'config')}><FileJson />配置</Button><Button size="xs" variant="ghost" disabled={busy || !ready} onClick={() => openValue(item, 'state')}>状态 JSON</Button><Button size="xs" variant="ghost" disabled={busy || !ready} onClick={() => openValue(item, 'credentials')}><KeyRound />凭据引用</Button><Button size="xs" variant="outline" disabled={busy || !ready || !item.manifest.tools.length} onClick={() => openWorkspace({ mode: 'call', item, tool: item.manifest.tools[0]?.name || '', text: '{}' })}><Wrench />工具列表（{item.manifest.tools.length}）</Button></div>
          </CardContent>
        </Card>)}</div>
      </CardContent>
    </Card>

    <Card><CardHeader><CardTitle className="flex items-center gap-2"><FolderSearch className="size-4 text-primary" />扫描本机目录</CardTitle><CardDescription>仅扫描后端所在本机的直接子目录 / ZIP（上限 100 项）。结果是规划候选，不会安装、启用或执行。</CardDescription></CardHeader><CardContent className="space-y-3">
      <div className="flex flex-col gap-2 sm:flex-row"><Input aria-label="扫描插件包的后端本机目录" placeholder="输入后端可访问的本机绝对目录路径" maxLength={4000} value={scanRoot} disabled={busy || disabled} onChange={(event) => { scanSequence.current++; setScanRoot(event.target.value); setScan(undefined); setScanError('') }} /><Button variant="outline" size="sm" disabled={busy || !ready || !scanRoot.trim()} onClick={scanLocal}><FolderSearch />仅扫描候选</Button></div>
      {suggestedRoots.length > 0 && <div className="flex flex-wrap gap-2">{suggestedRoots.map((root) => <Button key={root} size="xs" variant="ghost" disabled={busy || disabled} className="h-auto max-w-full whitespace-normal break-all text-left" onClick={() => { scanSequence.current++; setScanRoot(root); setScan(undefined); setScanError('') }}>{root}</Button>)}</div>}
      {!suggestedRoots.length && <p className="text-[11px] leading-5 text-muted-foreground">宿主未提供建议扫描目录；请输入路径。不会猜测用户目录或自动扫描正式数据。</p>}
      {scanError && <p role="alert" className="text-xs text-red-600 dark:text-red-300">扫描失败：{scanError}</p>}
      {scan && <div className="space-y-2"><p className="text-[11px] text-muted-foreground">{scan.items.length} 个扫描结果 · planning_only={String(scan.planning_only)} · auto_install={String(scan.auto_install)} · 上限 {scan.limit}。此界面始终不自动安装。</p>{!scan.items.length && <p className="rounded-xl border border-dashed border-border p-4 text-center text-xs text-muted-foreground">目录中没有可识别候选包</p>}{scan.items.map((candidate, index) => <div key={`${candidate.path}-${index}`} className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border/60 p-3"><div className="min-w-0 flex-1"><p className="break-all text-xs font-medium">{candidate.id || '无法识别包'} {candidate.version || ''}</p><p className="mt-1 break-all text-[11px] text-muted-foreground">{candidate.path}</p>{candidate.error && <p className="mt-1 text-xs text-red-600 dark:text-red-300">{candidate.error}</p>}</div><Badge variant={canPreviewCandidate(candidate) ? 'outline' : 'warning'}>{canPreviewCandidate(candidate) ? '仅候选，尚未预览' : candidate.status === 'invalid' ? '无效包' : 'deferred / 仅规划'}</Badge><Button size="xs" variant="outline" disabled={busy || !ready || !canPreviewCandidate(candidate)} onClick={() => { const item = candidate.id && latestItem(candidate.id); openWorkspace(item ? { mode: 'source', action: item.installed ? 'update' : 'restore', item, path: candidate.path } : { mode: 'source', action: 'install', path: candidate.path }) }}>选择路径并预览…</Button></div>)}</div>}
    </CardContent></Card>

    <Dialog open={Boolean(workspace)} onOpenChange={(open) => { if (!open && !lock.current) { clearPreview(); setWorkspace(undefined) } }}>
      <DialogContent className="max-w-3xl" onEscapeKeyDown={(event) => { if (busy) event.preventDefault() }} onInteractOutside={(event) => { if (busy) event.preventDefault() }}>
        <DialogHeader><DialogTitle>{dialogTitle}</DialogTitle><DialogDescription>{target ? `${target.name} · ${target.id}。` : ''}预览本身不是授权；只在核对快照后手动提交。失败或结果未知不会自动重试。</DialogDescription></DialogHeader>
        <div className="space-y-4">
          {workspace?.mode === 'source' && <div className="space-y-3"><Notice>当前后端只接受 source_path：后端本机目录或 ZIP 的路径。浏览器文件选择不会上传 ZIP，也不能提供后端真实路径，因此此处不使用上传控件。</Notice><label className="block space-y-2 text-xs"><span>本地{workspace.action === 'restore' ? '备份' : ''}包路径（目录 / ZIP）</span><Input aria-label="插件包本地目录或ZIP路径" value={workspace.path} maxLength={4000} placeholder="例如后端本机的绝对路径，不是浏览器下载路径" disabled={busy || disabled} onChange={(event) => updateWorkspace({ ...workspace, path: event.target.value })} /></label>{selectLocalPath && <div className="flex gap-2"><Button size="sm" variant="outline" disabled={busy || disabled} onClick={() => choosePath('folder')}>宿主选择目录</Button><Button size="sm" variant="outline" disabled={busy || disabled} onClick={() => choosePath('zip')}>宿主选择 ZIP 路径</Button></div>}<p className="text-xs leading-6 text-muted-foreground">首次安装使用 revision 0；已卸载记录应通过「恢复」选择同 ID 备份；更新必须为同 ID 的更高版本。提交前后端重新扫描来源与哈希，默认停用。</p></div>}
          {workspace?.mode === 'lifecycle' && <div className="space-y-3"><p className="text-xs leading-6 text-muted-foreground">当前记录 revision {latestItem(workspace.item.id)?.revision ?? workspace.item.revision}。点击下方「生成操作预览」读取后端精确快照；不会直接{actionNames[workspace.action]}。</p>{workspace.action === 'uninstall' && <Notice>仅移除活动与历史代码，保留数据、配置、状态与凭据引用。恢复需要本地备份包；清除必须走独立强确认。</Notice>}{workspace.action === 'purge' && <Notice danger>此操作仅在卸载后可用，会不可撤销地清除数据、配置、状态与凭据引用。有引用时需要宿主撤销适配；未接入则后端 blocked。不能保证撤销外部副作用，也不能声称引用删除等于系统凭据删除。</Notice>}{workspace.action === 'rollback' && <Notice>回滚后默认停用，不会回滚用户数据或网络 / 进程等外部副作用。目标版本及依赖由后端提交时校验。</Notice>}</div>}
          {workspace?.mode === 'value' && <div className="space-y-3"><Notice>{workspace.kind === 'credentials' ? '这里只能编辑宿主已有凭据的 reference 字符串列表，例如 ["vault:plugin-ref"]，不是 Key。不接收、不上传真实密钥。删除 reference 不表示删除系统凭据。' : '配置 / 状态是完整 JSON 对象，不是 Key 编辑器。禁止保存 api_key / password / secret 等密钥字段与真实 Key；凭据只填 reference。'}</Notice><div className="flex flex-wrap items-center justify-between gap-2 text-xs"><span>{workspace.revision === undefined ? '配置尚未读取' : `读取快照 revision ${workspace.revision}`}</span><Button size="xs" variant="outline" disabled={busy || !ready} onClick={reReadValue}>重新读取（放弃未提交编辑）</Button></div><Textarea aria-label={valueNames[workspace.kind]} className="min-h-52 font-mono text-xs" spellCheck={false} autoComplete="off" maxLength={65536} value={workspace.text} disabled={busy || disabled || workspace.revision === undefined} onChange={(event) => updateWorkspace({ ...workspace, text: event.target.value })} />{workspace.revision !== undefined && workspace.revision !== latestItem(workspace.item.id)?.revision && <Notice danger>记录版本已变化，请重新读取后再编辑，避免覆盖旧快照。</Notice>}</div>}
          {workspace?.mode === 'call' && <div className="space-y-3"><Notice>{STRICT_PLUGIN_RISK} 失败、超时或结果未知后可能已经有外部副作用，绝不自动重试。</Notice><div className="space-y-2">{workspace.item.manifest.tools.map((tool) => <button key={tool.name} type="button" disabled={busy || disabled} onClick={() => updateWorkspace({ ...workspace, tool: tool.name, text: '{}' })} className={`w-full rounded-xl border p-3 text-left disabled:opacity-50 ${workspace.tool === tool.name ? 'border-primary/40 bg-primary/5' : 'border-border/60 bg-card/40'}`}><span className="text-xs font-semibold">{tool.name}</span><p className="mt-1 text-xs leading-5 text-muted-foreground">{tool.description || '无工具描述'}</p><span className="mt-2 block text-[11px] text-muted-foreground">声明权限：{['python.execute', ...(tool.permissions || [])].join('、')}</span></button>)}</div>{!workspace.item.manifest.entrypoint && <Notice>此包仅声明式工具，宿主适配尚未集成；可以查看元数据，但不能直接执行。</Notice>}{!latestItem(workspace.item.id)?.enabled && <Notice>当前插件未启用。可查看工具，不能生成可执行调用授权。</Notice>}{(workspace.item.missing || workspace.item.files_mismatch || workspace.item.blocked.length > 0) && <Notice danger>代码完整性或依赖不可用，后端会拒绝调用；请先处理列表中的阻止条件。</Notice>}<JsonView title="所选工具完整 input_schema / 权限 / 目标声明" value={workspace.item.manifest.tools.find((tool) => tool.name === workspace.tool)} /><label className="block space-y-2 text-xs"><span>本次 args JSON 对象（不得包含真实 Key）</span><Textarea aria-label="本次工具args JSON" className="min-h-40 font-mono text-xs" spellCheck={false} autoComplete="off" maxLength={65536} value={workspace.text} disabled={busy || disabled} onChange={(event) => updateWorkspace({ ...workspace, text: event.target.value })} /></label></div>}
          {workspace?.mode === 'inspect' && <ManifestDetails item={workspace.item} />}
          {workspace?.mode === 'audit' && <div className="space-y-3"><p className="text-xs leading-6 text-muted-foreground">最近 100 条。后端仅记录动作、结果、插件与快照指纹，不记录原始参数 / 配置 / 凭据。审计没有结果时，不代表外部副作用没有发生。</p>{workspace.entries === undefined ? <p role="status" className="text-xs text-muted-foreground">{busy ? '正在读取审计…' : '审计未读到，请刷新重试'}</p> : !workspace.entries.length ? <p className="py-6 text-center text-xs text-muted-foreground">没有审计记录</p> : workspace.entries.map((entry) => <div key={entry.seq} className="rounded-xl border border-border/60 p-3 text-xs"><div className="flex flex-wrap items-center gap-2"><strong>{entry.action}</strong><Badge variant={['failed'].includes(entry.result) ? 'danger' : entry.result === 'started' ? 'warning' : 'outline'}>{entry.result}</Badge><span className="ml-auto text-[11px] text-muted-foreground">{time(entry.at)}</span></div><p className="mt-2 break-all font-mono text-[10px] text-muted-foreground">{entry.fingerprint || '无指纹'}</p></div>)}</div>}
          {validation && <p role="alert" className="text-xs text-red-600 dark:text-red-300">{validation}</p>}
          {operationError && <div role="alert"><Notice danger>{operationError}。请核对状态后重新预览，不会后台重试。</Notice></div>}
          {preview && <SnapshotView preview={preview} context={latestItem(preview.snapshot.id)} />}
          {preview && !attempt && approvalError && <Notice danger>{approvalError}</Notice>}
          {preview && !attempt && !approvalError && <div className="space-y-3 rounded-xl border border-primary/20 bg-primary/5 p-4">
            <label className="flex cursor-pointer items-start gap-2 text-xs leading-6"><input type="checkbox" className="mt-1.5 accent-[var(--primary)]" checked={acknowledged} disabled={busy || !ready} onChange={(event) => setAcknowledged(event.target.checked)} /><span>我已核对完整 snapshot、来源、权限、目标与风险，确认只批准本次{actionNames[preview.snapshot.action]}。我已区分宿主严格 OS 隔离可用性与权限声明，理解公开系统资源仍可能可读；缺少隔离证据不回退，失败或 unknown 不自动重试。</span></label>
            {preview.snapshot.action === 'purge' && <label className="block space-y-2 text-xs leading-6"><span className="font-semibold text-red-600 dark:text-red-300">不可撤销清除。请输入「清除 {preview.snapshot.id}」以二次确认：</span><Input aria-label="不可撤销数据清除确认" value={purgeText} autoComplete="off" disabled={busy || !ready} onChange={(event) => setPurgeText(event.target.value)} /></label>}
          </div>}
          {attempt && <div role="status" className={`rounded-xl border p-3 text-xs leading-6 ${attempt.status === 'success' ? 'border-emerald-500/20 bg-emerald-500/5' : 'border-amber-500/20 bg-amber-500/5'}`}><div className="flex items-start gap-2">{attempt.status === 'success' ? <CheckCircle2 className="mt-1 size-4 shrink-0 text-emerald-600" /> : <ShieldAlert className="mt-1 size-4 shrink-0 text-amber-600" />}<div><p>{attempt.message}</p><p>本次 token 已尝试使用，不能再次提交。{attempt.status !== 'success' ? '请刷新列表并查看审计；再次操作必须重新预览和明确确认。' : ''}</p></div></div></div>}
          {workspace?.mode === 'call' && <PluginBrokerPreparation key={`broker-preparation:${workspace.item.id}:${workspace.tool}:${workspace.item.revision}:${workspace.item.digest}`} item={callItem || workspace.item} tool={workspace.tool} argsText={workspace.text} apiBase={apiBase} disabled={disabled || Boolean(loadError)} callRunning={busy} onUseArgs={text => {
            if (lock.current || disabled || !workspace || workspace.mode !== 'call') return
            // Component requires a second explicit confirmation; draft replacement
            // clears old fullspec/token and NEVER executes or approves a call.
            updateWorkspace({ ...workspace, text })
          }} />}
          {workspace?.mode === 'call' && <PluginModelPending key={`plugin-model:${workspace.item.id}:${workspace.tool}:${workspace.item.revision}`} result={execution?.result} disabled={disabled || busy} preparation={(workspace.item.manifest.permissions?.includes('model.generate') || workspace.item.manifest.tools.find(tool => tool.name === workspace.tool)?.permissions?.includes('model.generate')) ? { pluginId: workspace.item.id, tool: workspace.tool, revision: workspace.item.revision } : undefined} onUsePrepared={prepared => {
            if (lock.current || disabled || !workspace || workspace.mode !== 'call' || prepared.plugin_id !== workspace.item.id || prepared.tool !== workspace.tool || prepared.revision !== workspace.item.revision) return
            try {
              const args = parsePluginArgs(workspace.text)
              const declaration = workspace.item.manifest.tools.find(tool => tool.name === workspace.tool)
              const targets = (declaration?.targets || []).filter(target => target.permission === 'model.generate' && target.kind === 'string')
              if (targets.length !== 1) throw new Error('所选工具必须精确声明一个 model.generate 字符串目标；不会猜测 args 字段或自动执行')
              const requests = args.broker_requests
              if (requests !== undefined && !Array.isArray(requests)) throw new Error('broker_requests 必须是数组；不会覆盖未知参数结构')
              const retained = (requests as unknown[] | undefined || []).filter(request => !(request !== null && typeof request === 'object' && !Array.isArray(request) && (request as Record<string, unknown>).op === 'model.generate'))
              updateWorkspace({ ...workspace, text: jsonText({ ...args, [targets[0].argument]: prepared.params.profile_id, broker_requests: [...retained, { op: 'model.generate', target_argument: targets[0].argument, params: prepared.params }] }) })
            } catch (error) { setOperationError(pluginErrorMessage(error)) }
          }} />}
          {execution && <JsonView title="本次工具结果（仅显示 JSON，不执行内容）" value={execution.result} open />}
          {loadError && workspace && <Notice danger>刷新失败：{loadError}。上次快照不能继续用于操作；关闭对话框并刷新列表。</Notice>}
        </div>
        <DialogFooter className="flex-wrap">
          <Button variant="outline" disabled={busy} onClick={() => { clearPreview(); setWorkspace(undefined) }}>关闭 / 取消</Button>
          {workspace?.mode === 'audit' && <Button variant="outline" disabled={busy || !ready} onClick={() => readAudit(workspace.item)}><RefreshCw />刷新审计</Button>}
          {attempt && target && <Button variant="outline" disabled={busy || !ready} onClick={() => readAudit(latestItem(target.id) || target)}><History />查看审计</Button>}
          {attempt && !target && preview && <Button variant="outline" disabled={busy || !ready || !latestItem(preview.snapshot.id)} onClick={() => { const item = latestItem(preview.snapshot.id); if (item) readAudit(item) }}><History />查看审计</Button>}
          {previewable && <Button variant="outline" disabled={busy || !ready || invalidInput} onClick={makePreview}><RefreshCw />{preview ? '重新生成预览' : '生成操作预览'}</Button>}
          {preview && <Button variant={preview.snapshot.action === 'purge' ? 'destructive' : 'default'} disabled={busy || !ready || Boolean(attempt) || Boolean(approvalError) || !acknowledged || (preview.snapshot.action === 'purge' && purgeText !== `清除 ${preview.snapshot.id}`)} onClick={approve}>{preview.snapshot.action === 'call' ? <Play /> : <CheckCircle2 />}{busy && attempt ? '等待本次结果…' : preview.snapshot.action === 'call' ? '仅执行本次（一次性）' : preview.snapshot.action === 'purge' ? '强确认并不可撤销清除' : '确认提交本次快照'}</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
}
