import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, Plug, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input, Textarea } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { parsePluginArgs, pluginErrorMessage, signatureLabel } from '@/lib/pluginPackages'
import { establishHostSession } from '@/api/client'

export type PackageMcpDeclaration = { name: string; transport: 'stdio' | 'http'; entrypoint?: string; url?: string }
export type PackageMcpMetadata = { id: string; revision: number; digest: string; installed: boolean; enabled: boolean; items: PackageMcpDeclaration[]; connects: false; credentials: string; risk: string }
export type PackageMcpTool = { name: string; description?: string; inputSchema: Record<string, unknown>; [key: string]: unknown }
export type PackageMcpPreview = { token: string | null; fingerprint: string; expires_at: number | null; blocked: Array<{ code: string; [key: string]: unknown }>; snapshot: {
  strict_mcp_spec?: Record<string,unknown>; adapter: 'package-mcp-v1'; action: 'call'; mcp_operation: 'discover' | 'execute' | 'catalog'; id: string; expected_revision: number; digest: string; mcp_name: string; target: Record<string, unknown>; declaration: PackageMcpDeclaration; package_root: string; signature_status: string; risk: string; note: string; permissions: string[]; tool?: string; args?: Record<string, unknown>; tool_schema?: Record<string, unknown>; tools?: PackageMcpTool[]
} }
export type PackageMcpDiscovery = { id: string; revision: number; digest: string; mcp_name: string; tools: PackageMcpTool[]; catalog: PackageMcpPreview; note: string }
export type PackageMcpResult = { ok: true; id: string; mcp_name: string; fingerprint: string; result: unknown }
export type PackageMcpFetch = (path: string, init?: RequestInit) => Promise<Response>
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const validRevision = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
function invalid(): never { throw new Error('包 MCP 服务返回格式无效；请刷新状态，不会自动重试') }
function canonical(value: unknown): string { if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`; if (object(value)) return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`; return JSON.stringify(value) ?? 'null' }
function declaration(value: unknown): value is PackageMcpDeclaration { return object(value) && typeof value.name === 'string' && !!value.name && (value.transport === 'http' && typeof value.url === 'string' || value.transport === 'stdio' && typeof value.entrypoint === 'string') }
function tools(value: unknown): value is PackageMcpTool[] { return Array.isArray(value) && value.every(item => object(item) && typeof item.name === 'string' && object(item.inputSchema)) && new Set(value.map(item => item.name)).size === value.length }
export function parsePackageMcpMetadata(value: unknown): PackageMcpMetadata {
  if (!object(value) || typeof value.id !== 'string' || !validRevision(value.revision) || !hash(value.digest) || typeof value.installed !== 'boolean' || typeof value.enabled !== 'boolean' || value.connects !== false || !Array.isArray(value.items) || !value.items.every(declaration) || typeof value.risk !== 'string' || typeof value.credentials !== 'string') invalid()
  if (new Set(value.items.map(item => item.name)).size !== value.items.length) throw new Error('包 MCP 声明存在重名，无法确定连接目标；请在插件管理修复包后重试')
  return value as PackageMcpMetadata
}
export function parsePackageMcpPreview(value: unknown, operation: 'discover' | 'execute' | 'catalog'): PackageMcpPreview {
  if (!object(value) || !object(value.snapshot) || !hash(value.fingerprint) || !Array.isArray(value.blocked) || !value.blocked.every(item => object(item) && typeof item.code === 'string')) invalid()
  const s = value.snapshot
  if (s.adapter !== 'package-mcp-v1' || s.action !== 'call' || s.mcp_operation !== operation || typeof s.id !== 'string' || !validRevision(s.expected_revision) || !hash(s.digest) || typeof s.mcp_name !== 'string' || !declaration(s.declaration) || !object(s.target) || typeof s.package_root !== 'string' || typeof s.signature_status !== 'string' || typeof s.risk !== 'string' || typeof s.note !== 'string' || !Array.isArray(s.permissions) || !s.permissions.every(item => typeof item === 'string')) invalid()
  if (s.declaration.transport === 'stdio' && (s.target.kind !== 'local_process' || typeof s.target.entrypoint !== 'string' || !hash(s.target.sha256) || typeof s.target.cwd !== 'string' || s.target.inherits_user_environment !== false || s.target.interpreter !== 'dedicated_frozen_single_server')) invalid()
  if (s.declaration.transport === 'http' && (s.target.kind !== 'network' || s.target.url !== s.declaration.url || s.target.redirects !== false || s.target.credentials !== 'none' || s.target.environment_proxy !== false)) invalid()
  if (!(value.token === null || typeof value.token === 'string' && value.token.length >= 20 && value.token.length <= 200) || !(value.expires_at === null || typeof value.expires_at === 'number' && Number.isFinite(value.expires_at))) invalid()
  if (!value.blocked.length && (!value.token || !value.expires_at)) invalid()
  if (operation !== 'catalog' && value.blocked.length===0 && s.execution_mode !== 'personal') {
    const spec=s.strict_mcp_spec
    if(!object(spec)||spec.v!==1||spec.mode!=='stdio_mcp'||!hash(spec.fingerprint)||!object(spec.policy)||!object(spec.policy.binding)||!object(spec.policy.policy)||spec.policy.policy.mode!=='stdio_mcp'||spec.policy.binding.plugin_id!==s.id||spec.policy.binding.revision!==s.expected_revision||spec.policy.binding.digest!==s.digest||spec.call_id!==spec.policy.binding.call_id||!Array.isArray(spec.policy.ipc_roles)||canonical(spec.policy.ipc_roles)!==canonical(['stdin_read','stdout_write','diagnostics_write','proxy_request_read','proxy_result_write'])||spec.permission_proxy!=='unavailable-deny'||spec.policy.fallback_allowed!==false||spec.policy.actual_verified!==false||spec.policy.os_actions_performed!==false||!Array.isArray(spec.policy.acl_grants)||spec.policy.acl_grants.length!==7)invalid()
  }
  if (operation === 'execute' && (typeof s.tool !== 'string' || !object(s.args) || !object(s.tool_schema))) invalid()
  if (operation === 'catalog' && !tools(s.tools)) invalid()
  return value as PackageMcpPreview
}
export function packageMcpApprovalError(preview: PackageMcpPreview, now = Date.now()): string | null {
  if (preview.blocked.length) return '后端阻止此次操作；请先处理插件依赖或版本状态'
  if (!preview.token || !preview.expires_at || preview.expires_at * 1000 <= now) return '授权或工具目录已过期，请重新发现 / 预览；不会自动续期'
  return null
}
export function createPackageMcpClient({ apiBase = '/v1/plugin-packages', fetcher = async (path, init) => { await establishHostSession(); return fetch(path, {...init,credentials:'same-origin'}) } }: { apiBase?: string; fetcher?: PackageMcpFetch } = {}) {
  if (apiBase.startsWith('//') || !/^\/[A-Za-z0-9/_-]+$/.test(apiBase)) throw new Error('MCP API 必须是同源绝对路径')
  const base = apiBase.replace(/\/+$/, ''), consumed = new Set<string>()
  const id = (value: string) => { if (!/^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$/.test(value)) throw new Error('插件 ID 无效'); return encodeURIComponent(value) }
  const expected = (value: number) => { if (!validRevision(value)) throw new Error('插件版本无效，请刷新'); return value }
  const request = async (path: string, body?: unknown): Promise<unknown> => {
    let response: Response
    try { response = await fetcher(base + path, { method: body === undefined ? 'GET' : 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } }), credentials: 'same-origin', cache: 'no-store' }) }
    catch { throw new Error('MCP 请求失败或结果未知；不会重试已提交的授权，请重读状态和审计') }
    let value: unknown
    try { value = await response.json() } catch { throw new Error('MCP 返回非 JSON，结果未知；不能重试本次 token') }
    if (!response.ok) throw new Error(pluginErrorMessage(value, `MCP 请求失败（HTTP ${response.status}）`))
    return value
  }
  const take = (value: PackageMcpPreview, operation: 'discover' | 'execute') => {
    parsePackageMcpPreview(value, operation)
    const reason = packageMcpApprovalError(value)
    if (reason) throw new Error(reason)
    if (consumed.has(value.token!)) throw new Error('此 token 已提交，不能重试；请重新预览')
    consumed.add(value.token!) // Consume before fetch, even if network/format fails.
    return { token: value.token, fingerprint: value.fingerprint }
  }
  return {
    metadata: async (pluginId: string) => parsePackageMcpMetadata(await request(`/${id(pluginId)}/mcp`)),
    discoverPreview: async (pluginId: string, name: string, revision: number) => {
      if (!name.trim()) throw new Error('请选择包内 MCP 声明')
      const value = parsePackageMcpPreview(await request(`/${id(pluginId)}/mcp/discover-preview`, { name, expected_revision: expected(revision) }), 'discover')
      if (value.snapshot.id !== pluginId || value.snapshot.mcp_name !== name || value.snapshot.expected_revision !== revision) invalid()
      return value
    },
    discover: async (preview: PackageMcpPreview): Promise<PackageMcpDiscovery> => {
      const value = await request('/mcp/discover', take(preview, 'discover'))
      if (!object(value) || value.id !== preview.snapshot.id || value.revision !== preview.snapshot.expected_revision || value.digest !== preview.snapshot.digest || value.mcp_name !== preview.snapshot.mcp_name || !tools(value.tools) || typeof value.note !== 'string') invalid()
      const catalog = parsePackageMcpPreview(value.catalog, 'catalog')
      if (catalog.snapshot.id !== value.id || catalog.snapshot.digest !== value.digest || catalog.snapshot.expected_revision !== value.revision || catalog.snapshot.mcp_name !== value.mcp_name || canonical(catalog.snapshot.tools) !== canonical(value.tools)) invalid()
      return value as PackageMcpDiscovery
    },
    callPreview: async (pluginId: string, name: string, revision: number, discovery: PackageMcpDiscovery, tool: string, text: string) => {
      const catalog = parsePackageMcpPreview(discovery.catalog, 'catalog')
      const reason = packageMcpApprovalError(catalog)
      if (reason) throw new Error(reason)
      if (discovery.id !== pluginId || discovery.mcp_name !== name || discovery.revision !== revision || !discovery.tools.some(item => item.name === tool)) throw new Error('工具目录与当前包 / MCP / 版本不匹配，请重新发现')
      const args = parsePluginArgs(text)
      const value = parsePackageMcpPreview(await request(`/${id(pluginId)}/mcp/call-preview`, { name, expected_revision: expected(revision), catalog_token: catalog.token, catalog_fingerprint: catalog.fingerprint, tool, args }), 'execute')
      const discoveredTool = discovery.tools.find(item => item.name === tool)!
      if (value.snapshot.id !== pluginId || value.snapshot.mcp_name !== name || value.snapshot.expected_revision !== revision || value.snapshot.digest !== discovery.digest || value.snapshot.tool !== tool || canonical(value.snapshot.args) !== canonical(args) || canonical(value.snapshot.tool_schema) !== canonical(discoveredTool.inputSchema) || canonical(value.snapshot.target) !== canonical(catalog.snapshot.target)) invalid()
      return value
    },
    execute: async (preview: PackageMcpPreview): Promise<PackageMcpResult> => {
      const value = await request('/mcp/execute', take(preview, 'execute'))
      if (!object(value) || value.ok !== true || value.id !== preview.snapshot.id || value.mcp_name !== preview.snapshot.mcp_name || value.fingerprint !== preview.fingerprint || !('result' in value)) invalid()
      return value as PackageMcpResult
    },
  }
}
export type PackageMcpClient = ReturnType<typeof createPackageMcpClient>
const pretty = (value: unknown) => JSON.stringify(value, null, 2) ?? 'null'

/** Only metadata is read on mount. Connections and tool calls require independent dialogs. */
export function PackageMcpPanel({ pluginId, revision, digest, installed, enabled, disabled = false, apiBase, onChanged }: { pluginId: string; revision: number; digest: string; installed: boolean; enabled: boolean; disabled?: boolean; apiBase?: string; onChanged?: () => void }) {
  const client = useMemo(() => createPackageMcpClient({ apiBase }), [apiBase])
  const [metadata, setMetadata] = useState<PackageMcpMetadata>(), [loading, setLoading] = useState(false), [busy, setBusy] = useState(false)
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [selected, setSelected] = useState(''), [discovery, setDiscovery] = useState<PackageMcpDiscovery>()
  const [tool, setTool] = useState(''), [args, setArgs] = useState('{}'), [preview, setPreview] = useState<PackageMcpPreview>(), [ack, setAck] = useState(false), [attempted, setAttempted] = useState(false), [result, setResult] = useState<unknown>()
  const [reload, setReload] = useState(0), [now, setNow] = useState(Date.now())
  const alive = useRef(true), sequence = useRef(0), lock = useRef(false)
  useEffect(() => {
    alive.current = true; const current = ++sequence.current
    setMetadata(undefined); setDiscovery(undefined); setSelected(''); setTool(''); setArgs('{}'); setPreview(undefined); setAttempted(false); setResult(undefined); setAck(false); setError(''); setNotice('')
    if (disabled) { setLoading(false); return () => { alive.current = false; sequence.current++ } }
    setLoading(true)
    void client.metadata(pluginId).then(value => { if (alive.current && current === sequence.current) { setMetadata(value); setSelected(value.items[0]?.name || '') } }).catch(reason => { if (alive.current && current === sequence.current) setError(pluginErrorMessage(reason)) }).finally(() => { if (alive.current && current === sequence.current) setLoading(false) })
    return () => { alive.current = false; sequence.current++ }
  }, [client, pluginId, revision, digest, installed, enabled, disabled, reload])
  useEffect(() => { if (!preview && !discovery) return; const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer) }, [preview, discovery])
  const matches = metadata?.id === pluginId && metadata.revision === revision && metadata.digest === digest
  const ready = !disabled && !loading && !!matches && installed && enabled && metadata?.installed && metadata.enabled
  const resetPreview = () => { sequence.current++; setPreview(undefined); setAttempted(false); setAck(false); setResult(undefined); setNotice(''); setError('') }
  const run = async (task: (current: number) => Promise<void>) => {
    if (lock.current || disabled) return
    lock.current = true; setBusy(true); setError('')
    const current = sequence.current
    try { await task(current) } catch (reason) { if (alive.current && current === sequence.current) setError(pluginErrorMessage(reason)) }
    finally { lock.current = false; if (alive.current) setBusy(false) }
  }
  const discoverPreview = () => { if (!ready || busy) return; resetPreview(); void run(async current => { const value = await client.discoverPreview(pluginId, selected, revision); if (alive.current && current === sequence.current) { setPreview(value); setNow(Date.now()) } }) }
  const callPreview = () => { if (!ready || busy || !discovery) return; resetPreview(); void run(async current => { const value = await client.callPreview(pluginId, selected, revision, discovery, tool, args); if (alive.current && current === sequence.current) { setPreview(value); setNow(Date.now()) } }) }
  const approve = () => {
    if (!ready || !preview || attempted || !ack || busy || packageMcpApprovalError(preview, now)) return
    const value = preview
    setAttempted(true) // No response, failure or unknown outcome may reuse this token.
    void run(async current => {
      setNotice('本次授权已提交。等待结果期间不要重复操作。')
      if (value.snapshot.mcp_operation === 'discover') {
        setDiscovery(undefined); setTool('')
        const found = await client.discover(value)
        if (alive.current && current === sequence.current) { setDiscovery(found); setTool(found.tools[0]?.name || ''); setNotice(`已发现 ${found.tools.length} 个真实工具。目录有有效期，每次调用仍需独立预览确认。`) }
      } else {
        const valueResult = await client.execute(value)
        if (alive.current && current === sequence.current) { setResult(valueResult.result); setNotice('本次调用完成；重复执行必须重新生成预览并明确批准。') }
      }
      try { onChanged?.() } catch { /* Do not replay a successful side effect for a host refresh failure. */ }
    })
  }
  const selectedDeclaration = metadata?.items.find(item => item.name === selected)
  const catalogError = discovery ? packageMcpApprovalError(discovery.catalog, now) : null
  return <section className="space-y-3 rounded-xl border border-border/60 p-3" aria-label={`包 MCP ${pluginId}`} aria-busy={loading || busy}>
    <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="flex items-center gap-2 text-xs font-semibold"><Plug className="size-3.5" />包内 MCP 适配</h4><Button size="xs" variant="ghost" disabled={busy || disabled} onClick={() => setReload(value => value + 1)}><RefreshCw />重读声明</Button></div>
    <p className="text-[11px] leading-5 text-muted-foreground">与手工 MCP 配置分开。仅读取声明不会连接服务；发现和调用分别需要一次性确认。当前适配不向服务传递凭据。</p>
    {loading && <p role="status" className="text-xs text-muted-foreground">正在读取包 MCP 元数据…</p>}
    {metadata && <><div className="flex flex-wrap gap-1"><Badge variant={metadata.enabled && metadata.installed ? 'success' : 'secondary'}>{metadata.installed ? metadata.enabled ? '包已启用' : '包已停用' : '包已卸载'}</Badge><Badge variant="outline">revision {metadata.revision}</Badge></div>{!matches && <p role="alert" className="text-xs text-amber-600">元数据与包列表版本不同，请刷新插件管理；不能使用旧快照连接。</p>}
      {!metadata.items.length ? <p className="text-xs text-muted-foreground">此包未声明 MCP 服务。</p> : <div className="space-y-3"><select aria-label={`选择包MCP ${pluginId}`} className="h-9 w-full rounded-lg border border-border bg-input px-3 text-xs" disabled={busy || disabled} value={selected} onChange={event => { resetPreview(); setSelected(event.target.value); setDiscovery(undefined); setTool(''); setArgs('{}') }}>{metadata.items.map(item => <option key={item.name} value={item.name}>{item.name} · {item.transport}</option>)}</select><p className="break-all text-[11px] leading-5 text-muted-foreground">{selectedDeclaration?.transport === 'stdio' ? `包内入口：${selectedDeclaration.entrypoint}` : `HTTP 地址：${selectedDeclaration?.url}`}</p><Button size="xs" variant="outline" disabled={busy || !ready || !selected} onClick={discoverPreview}>预览本次连接与工具发现</Button></div>}
      {discovery && <div className="space-y-3 border-t border-border/60 pt-3"><p className="text-xs">已发现 {discovery.tools.length} 个工具 · 目录到期 {discovery.catalog.expires_at ? new Date(discovery.catalog.expires_at * 1000).toLocaleString('zh-CN') : '未知'}</p>{catalogError && <p role="alert" className="text-xs text-amber-600">{catalogError}</p>}{discovery.tools.length ? <><select aria-label={`选择MCP工具 ${pluginId}`} className="h-9 w-full rounded-lg border border-border bg-input px-3 text-xs" value={tool} disabled={busy || disabled} onChange={event => { resetPreview(); setTool(event.target.value); setArgs('{}') }}>{discovery.tools.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}</select><p className="text-xs text-muted-foreground">{discovery.tools.find(item => item.name === tool)?.description || '工具未提供描述'}</p><details className="text-xs"><summary>真实工具 schema</summary><pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-all">{pretty(discovery.tools.find(item => item.name === tool)?.inputSchema)}</pre></details><Textarea aria-label={`包MCP工具参数 ${pluginId}`} className="min-h-28 font-mono text-xs" maxLength={65536} value={args} spellCheck={false} autoComplete="off" disabled={busy || disabled} onChange={event => { resetPreview(); setArgs(event.target.value) }} /><Button size="xs" variant="outline" disabled={busy || !ready || !!catalogError || !tool} onClick={callPreview}>预览本次工具参数与服务目标</Button></> : <p className="text-xs text-muted-foreground">服务真实返回空工具目录，不使用示例代替。</p>}</div>}
    </>}
    {error && <p role="alert" className="text-xs leading-6 text-red-600 dark:text-red-300">{error}。请求失败或结果未知不会自动重试；请重读状态与审计，再重新预览。</p>}
    {notice && <p role="status" className="text-xs leading-6 text-muted-foreground">{notice}</p>}
    <Dialog open={!!preview} onOpenChange={open => { if (!open && !busy) setPreview(undefined) }}><DialogContent className="max-w-3xl" onEscapeKeyDown={event => { if (busy) event.preventDefault() }} onInteractOutside={event => { if (busy) event.preventDefault() }}><DialogHeader><DialogTitle>{preview?.snapshot.mcp_operation === 'discover' ? '确认包 MCP 连接与发现' : '独立确认本次包 MCP 调用'}</DialogTitle><DialogDescription>核对本次服务和参数后运行。个人版无需发布者信任或沙箱设置。</DialogDescription></DialogHeader>{preview && <div className="space-y-3 text-xs"><div className="flex flex-wrap gap-2"><Badge variant="outline">{preview.snapshot.mcp_name}</Badge><Badge variant="secondary">r{preview.snapshot.expected_revision}</Badge><Badge variant="outline">{signatureLabel(preview.snapshot.signature_status)}</Badge></div><p className="break-all font-mono">包 SHA-256：{preview.snapshot.digest}</p><div className="flex gap-2 rounded-xl border border-amber-500/20 bg-amber-500/5 p-3 leading-6"><AlertTriangle className="mt-1 size-4 shrink-0" /><p>{preview.snapshot.risk}</p></div><p>{preview.snapshot.note}</p><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-xl bg-secondary/40 p-3">{pretty({ operation: preview.snapshot.mcp_operation, server: preview.snapshot.declaration, target: preview.snapshot.target, cwd: preview.snapshot.package_root, permissions: preview.snapshot.permissions, tool: preview.snapshot.tool, args: preview.snapshot.args, tool_schema: preview.snapshot.tool_schema })}</pre><p>有效期：{preview.expires_at ? new Date(preview.expires_at * 1000).toLocaleString('zh-CN') : '无可用授权'}；发现目录只在同一包版本及 TTL 内可用。</p>{packageMcpApprovalError(preview, now) && <p role="alert" className="text-amber-600">{packageMcpApprovalError(preview, now)}</p>}{preview.blocked.length > 0 && <pre className="whitespace-pre-wrap break-all text-red-600">{pretty(preview.blocked)}</pre>}{!attempted && <label className="flex items-start gap-2 leading-6"><input type="checkbox" checked={ack} disabled={busy || !ready} onChange={event => setAck(event.target.checked)} className="mt-1.5" /><span>我已核对服务和参数，确认运行本次操作。</span></label>}{attempted && <p role="status" className="rounded-lg bg-secondary/40 p-3 leading-6">本次 token 已尝试提交，成功、失败或未知结果均不可重试；再次操作须关闭后重新预览。</p>}{error && <p role="alert" className="text-red-600">{error}</p>}{notice && <p role="status">{notice}</p>}{result !== undefined && <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-secondary/40 p-3">{pretty(result)}</pre>}</div>}<DialogFooter><Button variant="outline" disabled={busy} onClick={() => setPreview(undefined)}>关闭 / 取消</Button><Button disabled={busy || !ready || !preview || attempted || !ack || !!packageMcpApprovalError(preview, now)} onClick={approve}>{busy ? '等待本次结果…' : preview?.snapshot.mcp_operation === 'discover' ? '仅批准本次连接与发现' : '仅批准本次工具执行'}</Button></DialogFooter></DialogContent></Dialog>
  </section>
}
