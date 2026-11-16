import { useCallback, useEffect, useRef, useState } from 'react'
import {CurrentPermissions} from './ManagementExtras'
import {collectBrowserPreferences,setPreferenceRevision} from '@/lib/browserPreferences'
import { Database, Download, LoaderCircle, RefreshCw, ShieldCheck, Trash2, Upload } from 'lucide-react'
import { api, json } from '@/api/client'
import { lingxi } from '@/api/lingxi'
import { useApp } from '@/context/AppContext'
import { useTheme } from '@/context/ThemeContext'
import { downloadBlob } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { SettingRow } from '@/components/shared/SettingRow'

// Kept here so the transport contract can be tested without a browser or a live data directory.
export type DataRequest = <T>(path: string, options?: RequestInit) => Promise<T>
type Coverage = { id: string; label: string; coverage: string; paths: string[]; present_paths: string[]; limitations: string }
type Lifecycle = {
  data_dir: string; program_dir: string; data_dir_source: string; independent_of_program: boolean
  copy_program_copies_data: boolean; copy_notice: string; uninstall_reinstall: string; downgrade: string
  installer_complete: boolean; restart_required_after_import: boolean
}
export type StorageOverview = {
  lifecycle: Lifecycle
  inventory: {
    format: string; version: number; categories: Coverage[]
    credentials: { coverage: string; secret_values_exported: boolean; limitation: string }
    excluded: string[]; plugin_restore_policy: string; backup_kinds: Record<string, string>
  }
  groups: { name: string; bytes: number; files: number; cleanable: boolean; limitation?: string }[]
  bytes: number; files: number; truncated: boolean; cleanup_categories: string[]; cleanup_notice: string
}
export type RuntimeFeature = { id: string; name: string; state: string; implemented: boolean; installed: boolean; enabled: boolean; note: string }
export type RuntimeFeatures = { source: 'runtime'; data_root: string; items: RuntimeFeature[]; verification: Record<string, string> }
export type CleanupFile = { path: string; size: number; sha256: string; identity: (number | string)[] }
export type CleanupPreview = { token: string; expires_at: number; files: CleanupFile[]; bytes: number; skipped: string[]; confirmation_required: true }
export type ImportResult = { path: string; files: number; active_data_dir: string; activated: false; restart_required: true; steps: string[]; notice: string }
export const cleanupChoices = [
  { id: 'rebuildable_cache', label: '可重建缓存' },
  { id: 'logs', label: '日志' },
  { id: 'temporary', label: '临时文件' },
] as const
const cleanupIds = new Set<string>(cleanupChoices.map((item) => item.id))

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label}合同无效`)
  return value as Record<string, unknown>
}
function text(value: unknown): value is string { return typeof value === 'string' }
function strings(value: unknown): value is string[] { return Array.isArray(value) && value.every(text) }
function count(value: unknown): value is number { return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 }
function requireContract(valid: boolean, label: string): asserts valid { if (!valid) throw new Error(`${label}合同无效；请刷新或更新本地服务`) }

export function parseStorage(value: unknown): StorageOverview {
  const data = record(value, '数据占用'), lifecycle = record(data.lifecycle, '数据路径'), inventory = record(data.inventory, '业务范围')
  const credentials = record(inventory.credentials, '凭据范围'), kinds = record(inventory.backup_kinds, '备份类型')
  requireContract(['data_dir', 'program_dir', 'data_dir_source', 'copy_notice', 'uninstall_reinstall', 'downgrade'].every((key) => text(lifecycle[key])), '数据路径')
  requireContract(['independent_of_program', 'copy_program_copies_data', 'installer_complete', 'restart_required_after_import'].every((key) => typeof lifecycle[key] === 'boolean'), '数据生命周期')
  requireContract(inventory.format === 'lingxi-data-inventory' && count(inventory.version) && Array.isArray(inventory.categories), '业务范围')
  for (const raw of inventory.categories as unknown[]) {
    const item = record(raw, '业务类别')
    requireContract(['id', 'label', 'coverage', 'limitations'].every((key) => text(item[key])) && strings(item.paths) && strings(item.present_paths), '业务类别')
  }
  requireContract(credentials.coverage === 'excluded' && credentials.secret_values_exported === false && text(credentials.limitation), '凭据范围')
  requireContract(strings(inventory.excluded) && text(inventory.plugin_restore_policy) && Object.values(kinds).every(text), '备份范围')
  requireContract(Array.isArray(data.groups) && count(data.bytes) && count(data.files) && typeof data.truncated === 'boolean' && strings(data.cleanup_categories) && text(data.cleanup_notice), '数据占用')
  for (const raw of data.groups as unknown[]) {
    const group = record(raw, '占用分组')
    requireContract(text(group.name) && count(group.bytes) && count(group.files) && typeof group.cleanable === 'boolean' && (group.limitation === undefined || text(group.limitation)), '占用分组')
  }
  return value as StorageOverview
}

export function formatDataBytes(bytes: number): string {
  if (!count(bytes)) return '未知'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB', 'PiB']
  let size = bytes / 1024, unit = 0
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++ }
  return `${size.toFixed(2)} ${units[unit]}`
}

export function createRequestSequence() {
  let sequence = 0
  return { begin: () => ++sequence, isCurrent: (request: number) => request === sequence, invalidate: () => { sequence++ } }
}

export async function loadDataStorage(request: DataRequest, signal?: AbortSignal) {
  return parseStorage(await request('/v6/data/storage', { signal }))
}

export function parseRuntimeFeatures(value: unknown): RuntimeFeatures {
  const data = record(value, '机器功能清单'), verification = record(data.verification, '验收状态')
  requireContract(data.source === 'runtime' && text(data.data_root) && Array.isArray(data.items) && Object.values(verification).every(text), '机器功能清单')
  const seen = new Set<string>()
  for (const raw of data.items as unknown[]) {
    const item = record(raw, '功能状态')
    requireContract(['id', 'name', 'state', 'note'].every((key) => text(item[key])) && ['implemented', 'installed', 'enabled'].every((key) => typeof item[key] === 'boolean'), '功能状态')
    requireContract(!seen.has(item.id as string) && (!item.enabled || (item.installed === true && item.implemented === true)), '功能状态')
    seen.add(item.id as string)
  }
  return value as RuntimeFeatures
}

export async function loadRuntimeFeatures(request: DataRequest, signal?: AbortSignal) {
  return parseRuntimeFeatures(await request('/v11/features', { signal }))
}

export function runtimeFeatureLabel(item: RuntimeFeature) {
  if (!item.implemented || !item.installed) return item.state === 'deferred' ? '本轮延期 · 未安装' : item.state === 'planned' ? '规划中 · 未安装' : '不可用 / 未安装'
  return item.enabled ? '已安装 · 已启用' : '已安装 · 未启用'
}

export function isCleanablePreviewPath(path: string) {
  const parts = path.split('/'), name = parts.at(-1) || ''
  if (parts.length < 2 || parts.some((part) => !part || part === '.' || part === '..' || /[\\:\x00-\x1f]/.test(part))) return false
  const protectedParts = new Set(['lingxi.db', 'attachments', 'characters', 'artifacts', 'workspaces', 'skill-assets', 'plugin-data', 'plugin-packages', 'plugins', 'plugin-manager', 'backups', 'imports', 'persona.json', 'user_settings.json', 'browser-preferences.json'])
  if (parts.some((part) => protectedParts.has(part.toLowerCase())) || /\.(db|sqlite|sqlite3)(-(wal|shm|journal))?$/i.test(name)) return false
  return (parts[0] === 'logs' && /\.log(\.\d+)?$/i.test(name))
    || (['tmp', 'temp'].includes(parts[0]) && /\.(tmp|temp|part)$/i.test(name))
    || (path.startsWith('cache/rebuildable/') && /\.(cache|idx)$/i.test(name))
    || (path.startsWith('cache/thumbnails/') && /\.(png|jpg|jpeg|webp)$/i.test(name))
    || (path.startsWith('cache/embeddings/') && /\.(cache|npy|npz)$/i.test(name))
}

export function parseCleanupPreview(value: unknown): CleanupPreview {
  const data = record(value, '清理预览')
  requireContract(text(data.token) && data.token.length >= 20 && data.token.length <= 200 && typeof data.expires_at === 'number' && Number.isFinite(data.expires_at), '清理令牌')
  requireContract(data.confirmation_required === true && Array.isArray(data.files) && data.files.length <= 1000 && count(data.bytes) && data.bytes <= 256 * 1024 ** 2 && strings(data.skipped), '清理预览')
  const seen = new Set<string>()
  let total = 0
  for (const raw of data.files as unknown[]) {
    const file = record(raw, '清理文件')
    requireContract(text(file.path) && isCleanablePreviewPath(file.path) && !seen.has(file.path) && count(file.size) && text(file.sha256) && /^[a-f\d]{64}$/i.test(file.sha256), '清理文件')
    requireContract(Array.isArray(file.identity) && file.identity.length === 6 && file.identity.every((value) => text(value) || (typeof value === 'number' && Number.isFinite(value))), '文件身份指纹')
    seen.add(file.path); total += file.size
  }
  requireContract(total === data.bytes, '清理总大小')
  return value as CleanupPreview
}

export async function previewDataCleanup(request: DataRequest, categories: string[]) {
  if (!categories.length || categories.some((category) => !cleanupIds.has(category))) throw new Error('请选择已登记的缓存、日志或临时文件类别')
  return parseCleanupPreview(await request('/v6/data/cleanup/preview', json('POST', { categories: [...new Set(categories)] })))
}

export async function confirmDataCleanup(request: DataRequest, preview: CleanupPreview, acknowledged: boolean, now = Date.now()) {
  parseCleanupPreview(preview)
  if (!acknowledged || !preview.files.length) throw new Error('请先核对完整文件清单并明确确认')
  if (preview.expires_at * 1000 <= now) throw new Error('清理预览已过期，请重新预览')
  const value = await request('/v6/data/cleanup/confirm', json('POST', { token: preview.token }))
  const result = record(value, '清理结果')
  requireContract(strings(result.deleted) && count(result.files) && count(result.bytes) && result.business_data_deleted === false, '清理结果')
  const deleted = result.deleted as string[]
  const candidates = new Map(preview.files.map((file) => [file.path, file.size]))
  requireContract(result.files === preview.files.length && result.bytes === preview.bytes && deleted.length === result.files && new Set(deleted).size === deleted.length && deleted.every((path) => candidates.has(path)) && deleted.reduce((sum, path) => sum + candidates.get(path)!, 0) === result.bytes, '清理结果')
  return result as { deleted: string[]; files: number; bytes: number; business_data_deleted: false }
}

export function parseImportResult(value: unknown): ImportResult {
  const result = record(value, '导入结果')
  requireContract(text(result.path) && Boolean(result.path) && text(result.active_data_dir) && result.path !== result.active_data_dir && count(result.files) && result.activated === false && result.restart_required === true && strings(result.steps) && text(result.notice), '受控导入结果')
  return value as ImportResult
}

export async function importDataBackup(request: DataRequest, file: Blob) {
  if (!file.size) throw new Error('不能导入空备份')
  if (file.size > 2 * 1024 ** 3 + 64 * 1024 ** 2) throw new Error('备份上传超过服务端容量限制')
  // Raw ZIP only: no multipart, arbitrary destination, online activation or environment writes.
  return parseImportResult(await request('/v6/data/import', { method: 'POST', headers: { 'Content-Type': 'application/zip' }, body: file }))
}

const coverageLabels: Record<string, string> = { included: '纳入备份', included_rebuildable: '纳入 · 可重建', excluded: '不纳入' }
const excludedLabels: Record<string, string> = { program_source: '程序源码', development_backups: '开发备份', migration_backups: '迁移回滚快照', logs: '日志', temporary_files: '临时文件', external_models: '外部模型', external_workspaces: '外部工作区' }
function errorText(error: unknown) { return error instanceof Error ? error.message : '未知错误，请检查本地服务' }
function Notice({ error, children }: { error?: boolean; children: React.ReactNode }) {
  return <div role={error ? 'alert' : 'status'} className={`rounded-xl border p-3 text-xs leading-6 ${error ? 'border-red-500/25 bg-red-500/5 text-red-600 dark:text-red-400' : 'border-border/70 bg-secondary/35 text-muted-foreground'}`}>{children}</div>
}

function DataDirectorySwitch({path}:{path?:string}){
 const shell=(window as unknown as {LingxiShell?:{dataLocation?:{state:()=>Promise<{canRevert:boolean}>;switch:(body:unknown)=>Promise<unknown>}}}).LingxiShell?.dataLocation
 const [confirm,setConfirm]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[canRevert,setCanRevert]=useState(false)
 useEffect(()=>{let live=true;if(shell)void shell.state().then(v=>{if(live)setCanRevert(v.canRevert)}).catch(()=>{});return()=>{live=false}},[shell])
 if(!shell||!path&&!canRevert)return null
 return <><Button variant="outline" onClick={()=>setConfirm(true)}>{path?'启用恢复数据并重启':'撤回上次数据切换'}</Button><Dialog open={confirm} onOpenChange={v=>{if(!busy)setConfirm(v)}}><DialogContent><DialogHeader><DialogTitle>{path?'切换到恢复数据？':'撤回上次数据切换？'}</DialogTitle><DialogDescription>零汐将校核数据并重新启动，原目录保留。请先结束正在运行的任务。</DialogDescription></DialogHeader>{path&&<code className="break-all text-xs">{path}</code>}{error&&<p role="alert">{error}</p>}<DialogFooter><Button variant="outline" disabled={busy} onClick={()=>setConfirm(false)}>取消</Button><Button disabled={busy} onClick={()=>{setBusy(true);void shell.switch({path,revert:!path,confirmed:true}).catch(e=>setError(e.message)).finally(()=>setBusy(false))}}>确认并重启</Button></DialogFooter></DialogContent></Dialog></>
}

export function DataManagementPanel({section="all"}:{section?:string}) {
  const { connection } = useApp()
  const { theme } = useTheme()
  const connected = connection === 'connected'
  const [overview, setOverview] = useState<StorageOverview>()
  const [refreshing, setRefreshing] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [busy, setBusy] = useState('')
  const [actionError, setActionError] = useState('')
  const [resultNotice, setResultNotice] = useState('')
  const [categories, setCategories] = useState<string[]>(cleanupChoices.map((item) => item.id))
  const [preview, setPreview] = useState<CleanupPreview>()
  const [acknowledged, setAcknowledged] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [backupFile, setBackupFile] = useState<File>()
  const [importOpen, setImportOpen] = useState(false)
  const [importAcknowledged, setImportAcknowledged] = useState(false)
  const [imported, setImported] = useState<ImportResult>()
  const readSequence = useRef(createRequestSequence())
  const actionSequence = useRef(createRequestSequence())
  const refreshLock = useRef(false)
  const readAbort = useRef<AbortController | undefined>(undefined)
  const mutationLock = useRef(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const ready = connected && Boolean(overview) && !refreshing && !loadError
  const disabled = !ready || Boolean(busy)

  const refresh = useCallback(async () => {
    const attempt = readSequence.current.begin()
    readAbort.current?.abort()
    setPreview(undefined); setAcknowledged(false); setConfirmOpen(false)
    if (!connected) { setOverview(undefined); setLoadError(''); refreshLock.current = false; setRefreshing(false); return }
    const controller = new AbortController(); readAbort.current = controller
    refreshLock.current = true; setRefreshing(true); setLoadError('')
    try {
      const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(20000)])
      const storage = await loadDataStorage(api, signal)
      if (readSequence.current.isCurrent(attempt)) setOverview(storage)
    } catch (error) {
      if (readSequence.current.isCurrent(attempt)) setLoadError(errorText(error))
    } finally {
      if (readSequence.current.isCurrent(attempt)) { refreshLock.current = false; setRefreshing(false) }
    }
  }, [connected])

  useEffect(() => {
    actionSequence.current.invalidate(); mutationLock.current = false; setBusy('')
    setActionError(''); setResultNotice(''); setImported(undefined); setBackupFile(undefined); setImportOpen(false); setImportAcknowledged(false)
    void refresh()
    return () => { readSequence.current.invalidate(); readAbort.current?.abort(); actionSequence.current.invalidate() }
  }, [refresh])

  const run = async (name: string, action: (isCurrent: () => boolean) => Promise<void>) => {
    if (!ready || refreshLock.current || mutationLock.current) return
    mutationLock.current = true
    const attempt = actionSequence.current.begin(), isCurrent = () => actionSequence.current.isCurrent(attempt)
    setBusy(name); setActionError(''); setResultNotice('')
    try { await action(isCurrent) }
    catch (error) { if (isCurrent()) setActionError(errorText(error)) }
    finally { if (isCurrent()) { mutationLock.current = false; setBusy('') } }
  }
  const exportBackup = () => run('export', async (isCurrent) => {
    const blob = await lingxi.backup({ appearance: theme, browser:collectBrowserPreferences() })
    const preferences=await api<{revision:string}>('/v6/data/preferences');setPreferenceRevision(preferences.revision)
    if (!isCurrent()) return
    downloadBlob(blob, `零汐备份-${new Date().toISOString().slice(0, 10)}.zip`)
    setResultNotice('完整用户备份已生成并发起下载，请在下载目录确认 ZIP 已保存。Key 本体不在备份中。')
  })
  const previewCleanup = () => run('preview', async (isCurrent) => {
    setPreview(undefined); setAcknowledged(false); setConfirmOpen(false)
    const fresh = await previewDataCleanup(api, categories)
    if (isCurrent()) { setPreview(fresh); setResultNotice(fresh.files.length ? '预览完成：尚未删除任何文件。请核对下方完整清单。' : '预览完成：没有符合白名单的可清理文件，未删除任何数据。') }
  })
  const cleanup = () => run('cleanup', async (isCurrent) => {
    if (!preview) return
    const snapshot = preview
    // One-shot tokens are never reused after success, failure or an uncertain network result.
    setPreview(undefined); setAcknowledged(false); setConfirmOpen(false)
    try {
      const result = await confirmDataCleanup(api, snapshot, acknowledged)
      if (isCurrent()) { setResultNotice(`已清理 ${result.files} 个预览文件，释放 ${formatDataBytes(result.bytes)}。业务数据未删除。`); await refresh() }
    } catch (error) {
      throw new Error(`清理失败或结果未确认：${errorText(error)}。可能已部分清理；请刷新占用并重新预览，不会自动重试。`)
    }
  })
  const restore = () => run('import', async (isCurrent) => {
    if (!backupFile || !importAcknowledged) return
    setImported(undefined)
    try {
      const result = await importDataBackup(api, backupFile)
      if (isCurrent()) { setImported(result); setImportOpen(false); setBackupFile(undefined); setImportAcknowledged(false); setResultNotice('备份已导入新目录，当前数据未切换。桌面版可在下方确认并重启以启用恢复数据。'); await refresh() }
    } catch (error) {
      throw new Error(`导入失败或结果未确认：${errorText(error)}。界面未切换数据路径；若网络中断，服务端可能已生成新目录，请先核对，不要盲目重复导入。`)
    }
  })

  return <>
    <Card hidden={section!=="all" && section!=="permissions"} className="settings-section data-section"><div className="settings-section-heading"><div><h2>权限与审批</h2><p>高风险操作必须由你确认，不因界面切换而自动执行</p></div><ShieldCheck className="size-4 text-muted-foreground" /></div><div className="p-5">
      <CurrentPermissions/><SettingRow title="屏幕读取" description="手动选择并预览画面后才可识别"><Badge variant="secondary">手动提供画面</Badge></SettingRow>
      <SettingRow title="外部浏览器" description="个人浏览器需要单独配置 MCP，不与专用浏览器混用"><Badge variant="secondary">按插件授权</Badge></SettingRow>
    </div></Card>
    <Card hidden={section!=="all" && section!=="storage"} className="settings-section data-section"><div className="settings-section-heading"><div><h2>本地数据与占用</h2><p>查看数据位置和存储占用</p></div><Button size="sm" variant="outline" disabled={!connected || Boolean(busy)} onClick={() => void refresh()}>{refreshing ? <LoaderCircle className="animate-spin" /> : <RefreshCw />}刷新</Button></div><div className="space-y-4 p-5" aria-busy={refreshing}>
      {!connected && <Notice>{connection === 'demo' ? '演示模式：不读取、导出、导入或清理真实数据；路径、占用和功能状态均未查询。' : '本地服务未连接，无法读取真实路径、业务范围或占用；所有数据操作不可用。'}</Notice>}
      {refreshing && <Notice>正在重新读取路径、范围与占用；旧结果仅供参考，刷新期间不能导入或清理。</Notice>}
      {loadError && <Notice error>刷新失败：{loadError}。{overview ? '以下为上次结果，当前数据操作已禁用。' : '未取得数据，不会显示为 0 或已启用。'}</Notice>}
      {overview && <>
        <div className="space-y-2 rounded-xl border border-border/70 p-4 text-xs leading-6"><div className="flex flex-wrap items-center gap-2"><strong>当前用户数据目录</strong><Badge variant={overview.lifecycle.independent_of_program ? 'success' : 'warning'}>{overview.lifecycle.independent_of_program ? '独立于程序' : '未独立于程序'}</Badge><Badge variant="outline">来源：{overview.lifecycle.data_dir_source}</Badge></div><code className="block break-all select-text">{overview.lifecycle.data_dir}</code><details className="personal-disclosure"><summary>位置与迁移说明</summary><p className="text-muted-foreground">程序目录：<code className="break-all select-text">{overview.lifecycle.program_dir}</code></p><p>{overview.lifecycle.copy_notice}</p><p className="text-muted-foreground">Windows 默认使用 %APPDATA%\LingxiV1；LINGXI_DATA_DIR 可覆盖默认值。上方显示的是服务端实际路径，不是默认路径猜测。</p><p className="text-muted-foreground">{overview.lifecycle.uninstall_reinstall}</p><p className="text-muted-foreground">{overview.lifecycle.downgrade}</p><p className="text-muted-foreground">安装器完成状态：{overview.lifecycle.installer_complete ? '服务端报告已完成' : '未完成；本页不代表安装器已实现保留/卸载流程'}</p></details></div>
        <div className="flex flex-wrap gap-2"><Badge variant="secondary">{overview.truncated || overview.groups.some((group) => group.limitation) ? '已扫描占用（不完整）' : '扫描占用'}：{formatDataBytes(overview.bytes)}</Badge><Badge variant="outline">{overview.files} 个已扫描文件</Badge></div>
        {(overview.truncated || overview.groups.some((group) => group.limitation)) && <Notice>扫描存在截断或受限项目，以下数值不是完整总量；未知项目不会被当作可清理数据。</Notice>}
        <div className="overflow-x-auto"><table className="w-full text-left text-xs"><caption className="sr-only">数据根目录下的真实文件与目录占用，数据库内各业务共用空间不可重复计数</caption><thead className="border-b border-border text-muted-foreground"><tr><th className="py-2 pr-3">实际文件 / 目录</th><th className="pr-3">占用</th><th className="pr-3">文件</th><th>边界</th></tr></thead><tbody>{overview.groups.map((group) => <tr key={group.name} className="border-b border-border/40"><td className="break-all py-3 pr-3 font-mono">{group.name}</td><td className="whitespace-nowrap pr-3">{formatDataBytes(group.bytes)}</td><td className="pr-3">{group.files}</td><td>{group.limitation || (group.cleanable ? '仅白名单文件可预览清理' : '不据此判定可删；以清理预览白名单为准')}</td></tr>)}</tbody></table></div>
        {!overview.groups.length && <p className="text-xs text-muted-foreground">本次扫描未发现根目录条目；不等于聊天或记忆服务已启用。</p>}
        <details className="personal-disclosure"><summary>备份范围与限制</summary><div className="space-y-3"><h3 className="text-sm font-medium">备份范围</h3><p className="text-xs leading-6 text-muted-foreground">聊天、记忆、草稿等共用数据库，不能把同一个数据库大小重复分摊成独立业务占用。下方“纳入”指备份策略，不表示数据已存在或功能已启用。</p>{overview.inventory.categories.map((item) => <div key={item.id} className="space-y-1 rounded-xl border border-border/60 p-3 text-xs leading-6"><div className="flex flex-wrap gap-2"><strong>{item.label}</strong><Badge variant="outline">{coverageLabels[item.coverage] || `服务端范围：${item.coverage}`}</Badge></div><p className="break-all font-mono">范围：{item.paths.join(' · ') || '未声明'}</p><p className="break-all text-muted-foreground">实际存在：{item.present_paths.join(' · ') || '无已确认路径'}（存在不等于功能启用）</p><p className="text-muted-foreground">{item.limitations}</p></div>)}</div>
        <Notice><p>{overview.inventory.credentials.limitation}</p><p>备份只保留凭据引用，跨电脑必须重新授权并保存 Key；本页不读取或展示 Key 本体。</p><p>{overview.inventory.plugin_restore_policy}</p><p>不纳入：{overview.inventory.excluded.map((item) => excludedLabels[item] || item).join('、')}。</p>{Object.entries(overview.inventory.backup_kinds).map(([kind, description]) => <p key={kind}>{({ user: '用户备份', migration: '迁移回滚快照', source_development: '源码 / 开发备份' } as Record<string, string>)[kind] || kind}：{description}</p>)}</Notice></details>
      </>}
    </div></Card>
    <Card hidden={section!=="all" && section!=="backup"} className="settings-section data-section"><div className="settings-section-heading"><div><h2>备份与导入</h2><p>先停止聊天、队列与工作任务；业务备份不包含 Key、外部模型或插件程序</p></div></div><div className="space-y-4 p-5">
      <DataDirectorySwitch/><SettingRow title="导出备份" description="下载用户数据 ZIP，不含 Key 和外部模型"><Button variant="outline" disabled={disabled} onClick={() => void exportBackup()}>{busy === 'export' ? <LoaderCircle className="animate-spin" /> : <Download />}导出备份</Button></SettingRow>
      <SettingRow title="导入备份" description="恢复到新目录，保留当前数据；导入后可确认并重启启用"><Button variant="outline" disabled={disabled} onClick={() => { setActionError(''); setImportOpen(true); setBackupFile(undefined); setImportAcknowledged(false) }}><Upload />选择备份并确认</Button></SettingRow>
      {imported && <Notice><strong className="text-foreground">已恢复，尚未启用</strong><p>新目录：<code className="break-all select-text">{imported.path}</code></p><p>仍在运行：<code className="break-all select-text">{imported.active_data_dir}</code></p><p>{imported.files} 个恢复文件 · 重新启动后生效</p><p>{imported.notice}</p><DataDirectorySwitch path={imported.path}/><ol className="list-inside list-decimal">{imported.steps.map((step, index) => <li className="break-all" key={index}>{step}</li>)}</ol></Notice>}
      <details className="rounded-xl border border-border/70 p-4 text-xs leading-6"><summary className="cursor-pointer font-medium">离线 CLI 恢复、路径切换与跨电脑授权</summary><div className="mt-3 space-y-2 text-muted-foreground"><p>先退出零汐及全部任务，保留原数据目录与备份。离线命令只恢复到不存在的 NEW 新目录，不覆盖旧目录。</p><p>在程序目录使用打包的后端：</p><pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-lg bg-secondary/50 p-3">{'resources\\backend\\LingxiServer.exe --restore "备份.zip" --destination "D:\\Lingxi-Restored-NEW"'}</pre><p>源码环境也可在源码目录使用：</p><pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-lg bg-secondary/50 p-3">{'python -m app.release restore "备份.zip" --destination "D:\\Lingxi-Restored-NEW"'}</pre><p>桌面版导入后可点击“启用恢复数据并重启”，原目录保留，也可撤回上次切换。浏览器独立使用时，可在服务退出后手动设置 LINGXI_DATA_DIR，再启动核对数据。插件仍需按原有规则授权启用。</p><p>跨电脑只迁移配置里的 Key 引用，不迁移系统凭据；在新电脑重新授权、保存 Key 后再测试对应功能。旧版本不应打开更高 schema，只能使用匹配版本快照与新目录。</p></div></details>
    </div></Card>
    <Card hidden={section!=="all" && section!=="cleanup"} className="settings-section data-section"><div className="settings-section-heading"><div><h2>安全清理</h2><p>仅清理缓存、日志和临时文件，先预览再确认</p></div><Trash2 className="size-4 text-muted-foreground" /></div><div className="space-y-4 p-5">
      <Notice>{overview?.cleanup_notice || '只清理明确登记的可重建缓存、日志和临时文件；聊天、记忆、数据库、附件、人物、插件与未知文件一律不动。'}</Notice>
      <div className="flex flex-wrap gap-4 text-xs">{cleanupChoices.map((choice) => <label className="flex items-center gap-2" key={choice.id}><input type="checkbox" checked={categories.includes(choice.id)} disabled={disabled || !overview?.cleanup_categories.includes(choice.id)} onChange={(event) => { setCategories((current) => event.target.checked ? [...current, choice.id] : current.filter((item) => item !== choice.id)); setPreview(undefined); setAcknowledged(false); setConfirmOpen(false) }} />{choice.label}{overview && !overview.cleanup_categories.includes(choice.id) ? '（服务端未登记）' : ''}</label>)}</div>
      <Button variant="outline" disabled={disabled || !categories.length || categories.some((category) => !overview?.cleanup_categories.includes(category))} onClick={() => void previewCleanup()}>{busy === 'preview' ? <LoaderCircle className="animate-spin" /> : <Database />}生成清理预览（不删除）</Button>
      {preview && <div className="space-y-3 rounded-xl border border-border/70 p-4 text-xs leading-6"><h3 className="font-medium">完整候选清单 · {preview.files.length} 个文件 · {formatDataBytes(preview.bytes)}（{preview.bytes} B）</h3><p className="text-muted-foreground">有效至 {new Date(preview.expires_at * 1000).toLocaleString()}；服务端确认时重新核对全部大小、SHA-256 与身份指纹。文件变化、令牌过期或已使用均需重新预览。</p><div className="max-h-96 space-y-3 overflow-y-auto">{preview.files.map((file) => <div key={file.path} className="space-y-1 rounded-lg bg-secondary/40 p-3"><code className="block break-all select-text">{file.path}</code><p>{formatDataBytes(file.size)} · {file.size} B</p><p className="break-all font-mono select-text">SHA-256: {file.sha256}</p><p className="break-all font-mono select-text">identity: {JSON.stringify(file.identity)}</p>{file.identity.some((value) => typeof value === 'number' && !Number.isSafeInteger(value)) && <p className="text-muted-foreground">服务端 identity 含超出浏览器安全整数范围的数值，数值展示可能失真；SHA-256 完整，最终身份由服务端保存的原始令牌快照精确复核。</p>}</div>)}</div>{preview.skipped.length > 0 && <details><summary className="cursor-pointer">跳过 {preview.skipped.length} 个未知 / 受保护文件（不会删除）</summary><ul>{preview.skipped.map((path, index) => <li className="break-all font-mono" key={`${path}-${index}`}>{path}</li>)}</ul></details>}<Button variant="outline" disabled={disabled || !preview.files.length} onClick={() => { setAcknowledged(false); setConfirmOpen(true) }}>核对清单并确认清理</Button></div>}
    </div></Card>
    {busy && <Notice>{({ export: '正在生成完整用户备份并等待下载响应', import: '正在上传、校验并恢复到受控 NEW 新目录', preview: '正在生成完整清理预览，尚未删除文件', cleanup: '正在核验指纹并清理已确认文件' } as Record<string, string>)[busy]}，请勿重复提交；不会自动重试。</Notice>}
    {actionError && <Notice error>{actionError}</Notice>}
    {resultNotice && <Notice>{resultNotice}</Notice>}
    <Dialog open={importOpen} onOpenChange={(open) => { if (!busy) { setImportOpen(open); if (!open) { setBackupFile(undefined); setImportAcknowledged(false) } } }}><DialogContent><DialogHeader><DialogTitle>导入备份</DialogTitle><DialogDescription>备份会先恢复到新目录，当前数据保留。导入完成后，桌面版可再确认并重启以启用。</DialogDescription></DialogHeader>{actionError && <Notice error>{actionError}</Notice>}<input ref={fileInput} type="file" accept=".zip,application/zip" aria-label="用户备份 ZIP" disabled={Boolean(busy)} onChange={(event) => { setBackupFile(event.target.files?.[0]); setImportAcknowledged(false) }} />{backupFile && <p className="break-all text-xs">{backupFile.name} · {formatDataBytes(backupFile.size)}（{backupFile.size} B）</p>}<label className="flex items-start gap-2 text-xs leading-6"><input type="checkbox" checked={importAcknowledged} disabled={Boolean(busy)} onChange={(event) => setImportAcknowledged(event.target.checked)} />我已停止聊天与任务，理解此次只恢复到新目录，原数据保留；启用时需再次确认并重启，跨电脑 Key 需重新授权。</label><DialogFooter><Button variant="ghost" disabled={Boolean(busy)} onClick={() => { setImportOpen(false); setBackupFile(undefined); setImportAcknowledged(false) }}>取消</Button><Button disabled={disabled || !backupFile || !importAcknowledged} onClick={() => void restore()}>{busy === 'import' ? <LoaderCircle className="animate-spin" /> : <Upload />}校验并导入新目录</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={confirmOpen} onOpenChange={(open) => { if (!busy) { setConfirmOpen(open); if (!open) setAcknowledged(false) } }}><DialogContent><DialogHeader><DialogTitle>确认仅删除预览文件</DialogTitle><DialogDescription>将删除刚才完整清单中的 {preview?.files.length || 0} 个白名单文件（{formatDataBytes(preview?.bytes || 0)}）。令牌单次有效；未知文件、业务数据与后来新增文件不在本次候选中。</DialogDescription></DialogHeader><label className="flex items-start gap-2 text-xs leading-6"><input type="checkbox" checked={acknowledged} disabled={Boolean(busy)} onChange={(event) => setAcknowledged(event.target.checked)} />我已核对每个路径、文件大小与指纹，确认只清理所列缓存 / 日志 / 临时文件。</label><DialogFooter><Button variant="ghost" disabled={Boolean(busy)} onClick={() => setConfirmOpen(false)}>取消</Button><Button disabled={disabled || !preview || !acknowledged} onClick={() => void cleanup()}><Trash2 />确认清理</Button></DialogFooter></DialogContent></Dialog>
  </>
}
