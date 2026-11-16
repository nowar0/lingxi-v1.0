import { createPortal } from 'react-dom'
import { useNavigate, useSearchParams } from 'react-router-dom'
import './memory-surface.css'
import { MemoryToolsDrawer } from './MemoryToolsDrawer'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '@/components/ui/dropdown-menu'
import { Select } from '@/components/ui/select'
import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, BookOpen, Check, ChevronDown, History, Layers, List, LockKeyhole, MoreHorizontal, Pin, Plus, RefreshCw, Search, Share2, ShieldCheck, Sparkles, Trash2, Undo2 } from 'lucide-react'
import { Card } from '../ui/card'
import { Button } from '../ui/button'
import { Badge } from '../ui/badge'
import { Input, Textarea } from '../ui/input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../ui/dialog'
import { MemoryExplorer } from './MemoryExplorer'
import { MemorySourceMessages } from './MemorySourceMessages'
import { canActivateMemoryV2, loadCompleteMemoryV2Graph, MEMORY_V2_PAGE_SIZE, memoryV2, memoryV2Conflict, memoryV2Curve, memoryV2Layers, memoryV2OperationId, memoryV2ContentRevision, memoryV2RestoreVersionProblem, memoryV2ReinforcementPoints, memoryV2ScopeNames, unresolvedMemoryV2Migrations } from '../../lib/memoryV2'
import { useApp } from '../../context/AppContext'
import { createMemoryV2Sync } from '../../lib/memoryV2Sync'
import { establishHostSession } from '../../api/client'
import type { MemorySyncState } from '../../lib/memoryV2Sync'
import { memorySyncPanelDecision, memorySyncPanelStatus, memorySyncProtectionActive, memorySyncWritesReady } from '../../lib/memoryV2SyncPanel'
import type { MemoryV2Catalog, MemoryV2Context, MemoryV2Create, MemoryV2Filters, MemoryV2ForgetPreview, MemoryV2Graph, MemoryV2Item, MemoryV2Layer, MemoryV2Migration, MemoryV2MigrationPage, MemoryV2Page, MemoryV2Scope } from '../../lib/memoryV2'

const selectClass = 'h-9 rounded-lg border border-border bg-input px-3 text-sm outline-none focus:ring-2 focus:ring-primary/20'
const date = (value?: string | null) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN') : '未记录'
const message = (error: unknown) => error instanceof Error ? error.message : String(error)
const categoryNames: Record<string, string> = { emotion:'情绪',idea:'想法',promise:'约定',learning:'学习',diary:'日记', experience: '经历', fact: '事实', habit: '操作习惯', project: '项目名册', preference: '偏好', legacy: '旧记录' }
const migrationNames: Record<string, string> = { pending: '待处理', complete: '已完成', anomaly: '需处理异常', skipped: '已明确跳过', blocked: '来源已阻断', forgotten: '已彻底忘记' }
type SyncPanelInfo = { status: string; disabled: boolean; inflight: boolean }

/** Lead mounts this component explicitly. It does not activate, migrate or reinforce on mount. */
export function MemoryV2Panel({ workSessionId, initialScope = 'companion' }: { workSessionId?: number; initialScope?: MemoryV2Scope }) {
  const [params,setParams] = useSearchParams()
  const initialRouteScope = params.get('scope')
  const [scope, setScope] = useState<MemoryV2Scope>(initialRouteScope === 'work' || initialRouteScope === 'shared' || initialRouteScope === 'companion' ? initialRouteScope : initialScope)
  const [view, setView] = useState<'star' | 'list'>(params.get('item') ? 'list' : 'star')
  const [directoryRequest,setDirectoryRequest]=useState(0)
  const scopeTabs=<nav className="memory-scope-tabs" aria-label="记忆隐私范围">{(['companion', 'shared', 'work'] as const).map(value => <button type="button" key={value} aria-pressed={scope === value} onClick={() => {setScope(value);setDirectoryRequest(0);if(params.has('item'))setParams({scope:value},{replace:true})}}><span className="flex items-center gap-2 font-medium">{value === 'work' ? <LockKeyhole className="size-4" /> : value === 'shared' ? <Share2 className="size-4" /> : <BookOpen className="size-4" />}{memoryV2ScopeNames[value]}</span></button>)}</nav>
  const viewSwitch=<button type="button" className="memory-view-switch" aria-label={`切换到${view === 'star' ? '列表' : '星空'}视图`} onClick={() => {setDirectoryRequest(0);setView(current => current === 'star' ? 'list' : 'star');if(params.has('item'))setParams({scope},{replace:true})}}>{view === 'star' ? '列表' : '星空'}</button>
  return <div className="page-container chrome-page personal-page customer-page memory-page space-y-5">
    <header className="personal-heading"><div><h1>记忆</h1></div>{view === 'star' && <Button variant="ghost" size="sm" aria-label="打开记忆目录" disabled={scope==='work'&&!workSessionId} title={scope==='work'&&!workSessionId?'请先选择工作会话':undefined} onClick={()=>{setView('star');setDirectoryRequest(value=>value+1)}}><List/>目录</Button>}</header>
    <div className="memory-scope-bar">{scopeTabs}{viewSwitch}</div>
    <MemoryV2Browser key={`${scope}:${workSessionId ?? 'none'}`} scope={scope} workSessionId={workSessionId} view={view} onViewList={()=>{setDirectoryRequest(0);setView('list')}} directoryRequest={directoryRequest} targetId={params.get('item')||undefined} targetRevision={params.get('revision')||undefined} />
  </div>
}

export function MemorySettingsTools({tab}:{tab:'search'|'sync'|'migration'}) {
  const {activeWorkSessionId}=useApp()
  const [scope,setScope]=useState<MemoryV2Scope>('companion')
  const [searchHost,setSearchHost]=useState<HTMLDivElement|null>(null)
  const [syncInfo,setSyncInfo]=useState<SyncPanelInfo>({status:'正在读取记忆状态…',disabled:true,inflight:false})
  return <div className="memory-settings-tools">
    {tab!=='migration'&&<div className="customer-segment" role="group" aria-label="记忆范围">{(['companion','work','shared'] as const).map(value=><Button key={value} type="button" size="sm" variant={scope===value?'secondary':'ghost'} aria-pressed={scope===value} onClick={()=>setScope(value)}>{memoryV2ScopeNames[value]}</Button>)}</div>}
    {tab==='search'&&<div ref={setSearchHost} className="memory-tools-panel"/>}
    {tab==='sync'&&<div className="memory-tools-panel memory-sync-panel"><p role="status">{syncInfo.status}</p><Button type="button" size="sm" variant="outline" disabled={syncInfo.disabled} onClick={()=>window.dispatchEvent(new Event('lingxi-memory-check-updates'))}><RefreshCw className={syncInfo.inflight?'size-4 animate-spin':'size-4'}/>{syncInfo.inflight?'检查中…':'检查更新'}</Button></div>}
    {tab==='migration'&&<MigrationWorkbench/>}
    {tab!=='migration'&&<MemoryV2Browser key={`${scope}:${activeWorkSessionId??'none'}`} scope={scope} workSessionId={activeWorkSessionId} view="list" onViewList={()=>undefined} settingsOnly searchHost={searchHost} onSyncStatus={setSyncInfo}/>}
  </div>
}

export function MigrationWorkbench() {
  const { connection } = useApp()
  const [page, setPage] = useState<MemoryV2MigrationPage>()
  const [offset, setOffset] = useState(0)
  const [status, setStatus] = useState('anomaly')
  const [expanded, setExpanded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [reading, setReading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [exhausted, setExhausted] = useState(false)
  const [startOpen, setStartOpen] = useState(false)
  const [activateOpen, setActivateOpen] = useState(false)
  const [resolution, setResolution] = useState<{ item: MemoryV2Migration; action: 'confirm' | 'skip' | 'retry' }>()
  const [corrected, setCorrected] = useState('')
  const [latest, setLatest] = useState<MemoryV2Migration>()
  const [batchProcessed, setBatchProcessed] = useState(0)
  const [reload, setReload] = useState(0)
  const stop = useRef(false), alive = useRef(true), lock = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false; stop.current = true } }, [])
  const refresh = () => setReload(value => value + 1)
  useEffect(() => {
    let disposed = false
    if (connection !== 'connected') { setPage(undefined); setReading(false); setError('连接本地服务后查看迁移状态。'); return }
    setReading(true)
    void memoryV2.migrations(status || undefined, offset).then(result => {
      if (disposed) return
      if (offset > 0 && offset >= result.total) { setOffset(Math.max(0, Math.ceil(result.total / MEMORY_V2_PAGE_SIZE) - 1) * MEMORY_V2_PAGE_SIZE); return }
      setPage(result); setError(''); setNotice(current => current === '正在检查迁移状态…' ? '迁移状态已更新。' : current)
      if (result.has_more !== undefined) setExhausted(!result.has_more)
      else if (result.remaining !== undefined) setExhausted(result.remaining === 0)
    }).catch(reason => { if (!disposed) { setError(`迁移状态读取失败：${message(reason)}`); setNotice('') } }).finally(() => { if (!disposed) setReading(false) })
    return () => { disposed = true }
  }, [connection, status, offset, reload])
  const run = async (task: () => Promise<void>) => {
    if (lock.current) return
    lock.current = true; setBusy(true); setError(''); setNotice('')
    try { await task() } catch (reason) { if (alive.current) setError(message(reason)) }
    finally { lock.current = false; if (alive.current) { setBusy(false); refresh() } }
  }
  const migrate = async () => {
    stop.current = false; setStartOpen(false); setBatchProcessed(0)
    let count = 0, more = true
    while (more && !stop.current && alive.current) {
      const batch = await memoryV2.migrateBatch('legacy-v2', 100)
      more = batch.has_more; count += batch.processed
      if (!alive.current) return
      setBatchProcessed(count); setExhausted(!more); refresh()
      if (batch.processed === 0 && more) throw new Error('后端返回仍有剩余但本批未处理记录；已停止，请刷新检查。')
    }
    if (alive.current) {window.dispatchEvent(new Event('lingxi-memory-refresh'));setNotice(more ? '已暂停。已完成批次保存在主库，下次可继续，无需重迁。' : '可识别条目已扫描完毕。请处理异常后再明确激活；原聊天和旧记录均保留。')}
  }
  const resolve = async () => {
    if (!resolution || latest) return
    let patch: Record<string, unknown> | undefined
    if (resolution.action === 'confirm') {
      const parsed: unknown = JSON.parse(corrected)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('修正内容必须是 JSON 对象。')
      patch = parsed as Record<string, unknown>
      if (patch.source_ref !== undefined && patch.source_ref !== resolution.item.source_ref) throw new Error('不能更换真实 source_ref。')
    }
    try {
      const result = await memoryV2.resolve(resolution.item, resolution.action, patch)
      if (!alive.current) return
      setResolution(undefined);window.dispatchEvent(new Event('lingxi-memory-refresh')); setNotice(`异常操作完成：${migrationNames[result.status] || result.status}。${result.status === 'anomaly' ? '仍需根据新错误继续处理。' : ''}`)
    } catch (reason) {
      if (memoryV2Conflict(reason)) {
        // Reread this exact source without dropping the user's correction text.
        let found: MemoryV2Migration | undefined, cursor = 0, total = 0
        do {
          const current = await memoryV2.migrations(undefined, cursor, 200)
          found = current.items.find(item => item.source_ref === resolution.item.source_ref)
          total = current.total; cursor += current.items.length
          if (!current.items.length) break
        } while (!found && cursor < total)
        if (alive.current) { setLatest(found); setError('异常记录版本已变化。修正输入已保留，请比较最新记录后再操作。') }
        refresh(); return
      }
      throw reason
    }
  }
  const counts = page?.counts || {}
  const registered = Object.values(counts).reduce((sum, value) => sum + value, 0)
  const finished = (counts.complete || 0) + (counts.skipped || 0) + (counts.forgotten || 0)
  const unresolved = unresolvedMemoryV2Migrations(counts)
  const content=<Card className="memory-migration-card overflow-hidden">
    <div className="flex flex-wrap items-center justify-between gap-3 p-4"><div className="flex items-start gap-3"><span className="rounded-xl bg-primary/10 p-2.5 text-primary"><Layers className="size-5" /></span><div><h2 className="flex flex-wrap items-center gap-2 font-semibold">旧记忆迁移<Badge variant={page?.authority === 'v2' ? 'success' : 'warning'}>{page ? page.authority === 'v2' ? '已启用' : '待迁移' : '状态未读取'}</Badge></h2><p className="mt-1 text-xs leading-5 text-muted-foreground">{page?.authority==='v2'?(registered===0?'四层记忆已启用，当前没有旧记录需要迁移。':'四层记忆已启用，可查看迁移记录。'):'将旧记忆导入四层记忆，原记录保留。'}</p></div></div><div className="flex flex-wrap gap-2"><Button variant="outline" size="sm" disabled={busy || reading} onClick={() => { setNotice('正在检查迁移状态…'); refresh() }}><RefreshCw className="size-3.5" />刷新</Button>{(page?.authority!=='v2'||registered>0)&&<Button variant="outline" size="sm" onClick={() => { if(page?.authority==='v2'&&!expanded){setStatus('');setOffset(0)} setExpanded(value => !value) }} aria-expanded={expanded}>{page?.authority==='v2'?'迁移记录':'待处理记录'}<ChevronDown className="size-3.5" /></Button>}{page?.authority!=='v2'&&(busy ? <Button variant="outline" size="sm" onClick={() => { stop.current = true; setNotice('将在当前批次结束后暂停，已提交的批次不会回滚。') }}>暂停</Button> : <Button size="sm" disabled={!page || page.authority === 'v2'} onClick={() => setStartOpen(true)}>{registered ? '继续迁移' : '开始迁移'}</Button>)}{page?.authority!=='v2'&&<Button size="sm" disabled={busy || reading || !canActivateMemoryV2(page, exhausted)} onClick={() => setActivateOpen(true)}>启用新记忆</Button>}</div></div>
    {page?.authority!=='v2'&&<div className="border-t border-border bg-muted/20 px-4 py-3"><div className="flex flex-wrap gap-x-5 gap-y-2 text-xs"><span>已登记 <strong>{page ? registered : '—'}</strong></span><span>完成 <strong>{counts.complete || 0}</strong></span><span>明确跳过 <strong>{counts.skipped || 0}</strong></span><span>未完成 / 异常 / 阻断 <strong>{unresolved}</strong></span><span>本次自动处理 <strong>{batchProcessed}</strong></span><span>{exhausted ? '剩余扫描：已完成' : '剩余扫描：未确认，请继续迁移'}</span></div><progress className="mt-3 h-1.5 w-full accent-primary" max={Math.max(1, registered)} value={finished} aria-label="已登记迁移记录处理进度" /><p className="mt-1 text-[11px] text-muted-foreground">扫描完成并处理待办后，可启用新记忆。</p></div>}
    {error && <p role="alert" className="border-t border-border px-4 py-3 text-sm text-destructive">{error}</p>}{notice && <p role="status" className="border-t border-border px-4 py-3 text-sm text-primary">{notice}</p>}
    {expanded && <section className="space-y-3 border-t border-border p-4" aria-label="迁移待处理记录"><div className="flex flex-wrap items-center gap-3"><h3 className="text-sm font-medium">迁移记录</h3><Select className={selectClass} aria-label="迁移状态筛选" value={status} onChange={event => { setStatus(event.target.value); setOffset(0) }}><option value="">全部状态</option>{Object.entries(migrationNames).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</Select><span className="ml-auto text-xs text-muted-foreground">{reading ? '正在重读…' : `当前筛选共 ${page?.total ?? '—'} 条`}</span></div><div className="space-y-2">{page?.items.map(item => <article key={item.source_ref} className="rounded-lg border border-border p-3"><div className="flex flex-wrap items-center gap-2"><Badge variant={item.status === 'anomaly' || item.status === 'blocked' ? 'warning' : 'secondary'}>{migrationNames[item.status] || item.status}</Badge><code className="break-all text-xs">{item.source_ref}</code><span className="ml-auto text-xs text-muted-foreground">r{item.revision} · 尝试 {item.attempts} 次</span></div>{item.error && <p className="mt-2 text-xs leading-5 text-amber-600 dark:text-amber-400">{item.error}</p>}<details className="mt-2 text-xs text-muted-foreground"><summary className="cursor-pointer">原始迁移数据与来源（保留原聊天）</summary><pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap break-all rounded bg-muted/40 p-3">{pretty(item.document)}</pre></details>{page?.authority!=='v2' && ['anomaly', 'blocked', 'skipped'].includes(item.status) && <div className="mt-3 flex flex-wrap gap-2">{(['confirm', 'skip', 'retry'] as const).map(action => <Button key={action} variant="outline" size="sm" disabled={busy} onClick={() => { setResolution({ item, action }); setCorrected(pretty(item.document)); setLatest(undefined); setError('') }}>{action === 'confirm' ? '修正并确认' : action === 'skip' ? '明确跳过' : '重试真实来源'}</Button>)}</div>}</article>)}{!reading && page?.items.length === 0 && <p className="py-4 text-center text-sm text-muted-foreground">暂无迁移记录。</p>}</div><Pager offset={offset} total={page?.total} busy={busy || reading} onPage={setOffset} /></section>}
    <Dialog open={startOpen} onOpenChange={value => { if (!busy) setStartOpen(value) }}><DialogContent data-memory-dialog><DialogHeader><DialogTitle>启动 / 继续迁移到四层记忆？</DialogTitle><DialogDescription>将在本地主库中每批处理 100 条，自动迁移可识别数据，异常保留在操作台。不会调用外部模型，不改原聊天，也不自动激活。你可以在任意批次后暂停。</DialogDescription></DialogHeader><p className="text-sm leading-6">L1 不强制变长期；旧核心不能自动升级为 L4。来源未知的记录会明确标注，不伪造人物、日期或用户确认。</p><DialogFooter><Button variant="outline" onClick={() => setStartOpen(false)}>取消</Button><Button disabled={busy} onClick={() => void run(migrate)}>我确认启动迁移</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={activateOpen} onOpenChange={value => { if (!busy) setActivateOpen(value) }}><DialogContent data-memory-dialog><DialogHeader><DialogTitle>明确切换 V2 为唯一主记忆库</DialogTitle><DialogDescription>仅在剩余扫描完成且异常已处理或明确跳过时允许。旧记录仍保留；激活后由集成层停止旧库写入，不进行永久双写。</DialogDescription></DialogHeader><p className="text-sm">这是主库切换，不是普通浏览操作。请确认你已审阅迁移台账与跳过项。</p><DialogFooter><Button variant="outline" onClick={() => setActivateOpen(false)}>暂不切换</Button><Button disabled={busy || !canActivateMemoryV2(page, exhausted)} onClick={() => void run(async () => { await memoryV2.activate(); if (alive.current) { setActivateOpen(false);window.dispatchEvent(new Event('lingxi-memory-refresh')); setNotice('V2 已明确激活。旧聊天保留，共享仍需逐条授权。') } })}>我启用新记忆</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(resolution)} onOpenChange={value => { if (!value && !busy) { setResolution(undefined); setLatest(undefined) } }}><DialogContent data-memory-dialog className="max-w-2xl"><DialogHeader><DialogTitle>{resolution?.action === 'confirm' ? '修正并确认这条异常' : resolution?.action === 'skip' ? '明确跳过这条异常' : '重试这条真实旧来源'}</DialogTitle><DialogDescription>来源：{resolution?.item.source_ref} · r{resolution?.item.revision}。{resolution?.action === 'skip' ? '跳过记录保留，可稍后重试；不会删除原聊天或旧记录。' : '使用后端校验，不自动把旧数据升级为核心，也不伪造来源。'}</DialogDescription></DialogHeader>{resolution?.action === 'confirm' && <label className="space-y-2 text-sm"><span>修正原始 JSON（不能更换 source_ref）</span><Textarea className="min-h-64 font-mono text-xs" value={corrected} onChange={event => setCorrected(event.target.value)} /></label>}{resolution?.item.error && <p className="text-xs text-amber-600 dark:text-amber-400">后端原因：{resolution.item.error}</p>}{latest && <div className="space-y-2 rounded-lg border border-amber-500/30 p-3"><p className="text-sm">检测到新版本 r{latest.revision}；你的修正输入未丢失。</p><pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all text-xs">{pretty(latest.document)}</pre><Button size="sm" variant="outline" onClick={() => { setResolution(current => current ? { ...current, item: latest } : current); setLatest(undefined); setError('') }}>我已比较，采用最新记录版本（保留修正输入）</Button></div>}{error && <p role="alert" className="text-sm text-destructive">{error}</p>}<DialogFooter><Button variant="outline" disabled={busy} onClick={() => setResolution(undefined)}>取消</Button><Button disabled={busy || Boolean(latest)} onClick={() => void run(resolve)}>{resolution?.action === 'confirm' ? '我确认这些修正' : resolution?.action === 'skip' ? '我确认跳过' : '按当前来源重试'}</Button></DialogFooter></DialogContent></Dialog>
  </Card>
  return content
}

interface MemoryDraft { key: string; value: string; category: string; layer: 1 | 2 | 3; importance: number; pinned: boolean; occurred_at: string;tags:string;valence:number|null;arousal:number|null }
type ItemAction = 'delete' | 'restore' | 'share' | 'core' | 'reinforce' | 'forget'
function MemoryV2Browser({ scope, workSessionId, view, onViewList, directoryRequest, targetId, targetRevision, settingsOnly=false, searchHost, onSyncStatus, onLocated }: { scope: MemoryV2Scope; workSessionId?: number; view:'star'|'list'; onViewList:()=>void; directoryRequest?:number; targetId?:string; targetRevision?:string; settingsOnly?:boolean; searchHost?:HTMLElement|null; onSyncStatus?:(info:SyncPanelInfo)=>void; onLocated?:()=>void }) {
  const navigate=useNavigate()
  const { connection } = useApp()
  const [visible, setVisible] = useState(() => !document.hidden)
  const [syncState, setSyncState] = useState<Readonly<MemorySyncState>>()
  const [externalPending, setExternalPending] = useState(false)
  const [baselinePending, setBaselinePending] = useState(false)
  const unavailable = scope === 'work' && workSessionId === undefined
  const [layer, setLayer] = useState<MemoryV2Layer>()
  const [category, setCategory] = useState('')
  const [includeDeleted, setIncludeDeleted] = useState(false)
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<MemoryV2Page>()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [reload, setReload] = useState(0)
  const [selected, setSelected] = useState<MemoryV2Item>()
  const graphOpen = view === 'star' && !settingsOnly
  const [starDetailOpen, setStarDetailOpen] = useState(false)
  const [advancedDetailOpen,setAdvancedDetailOpen]=useState(false)
  const [catalog,setCatalog]=useState<MemoryV2Catalog>()
  const [catalogError,setCatalogError]=useState('')
  const [browseQuery,setBrowseQuery]=useState(''),[browseFilter,setBrowseFilter]=useState('')
  const [pinnedOnly,setPinnedOnly]=useState(false),[before,setBefore]=useState(''),[beforeFilter,setBeforeFilter]=useState('')
  const [relationOpen,setRelationOpen]=useState(false),[relationTarget,setRelationTarget]=useState(''),[relationLabel,setRelationLabel]=useState('相关记忆'),[relationKind,setRelationKind]=useState<'confirmed'|'similarity'>('similarity'),[relationAcknowledged,setRelationAcknowledged]=useState(false)
  useEffect(()=>{const id=setTimeout(()=>{setBrowseFilter(browseQuery);setBeforeFilter(before)},180);return()=>clearTimeout(id)},[browseQuery,before])
  useEffect(()=>{if(!graphOpen||connection!=='connected'||unavailable)return;let disposed=false;void memoryV2.catalog(scope,scope==='work'?workSessionId:undefined).then(value=>{if(!disposed){setCatalog(value);setCatalogError('')}}).catch(reason=>{if(!disposed)setCatalogError(`目录读取失败：${message(reason)}`)});return()=>{disposed=true}},[scope,workSessionId,reload,graphOpen,connection,unavailable])
  const [graph, setGraph] = useState<MemoryV2Graph>()
  const [graphLoading, setGraphLoading] = useState(false)
  const [graphError, setGraphError] = useState('')
  const [editOpen, setEditOpen] = useState(false)
  const [editing, setEditing] = useState<MemoryV2Item>()
  const [draft, setDraft] = useState<MemoryDraft>(() => newDraft(scope))
  const [conflict, setConflict] = useState<MemoryV2Item>()
  const [action, setAction] = useState<{ kind: ItemAction; item: MemoryV2Item; operationId: string }>()
  const [shareKey, setShareKey] = useState('')
  const [coreValue, setCoreValue] = useState('')
  const [forgetPreview, setForgetPreview] = useState<MemoryV2ForgetPreview>()
  const [forgetPhrase, setForgetPhrase] = useState('')
  const [acknowledged, setAcknowledged] = useState(false)
  const [busy, setBusy] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [versions, setVersions] = useState<MemoryV2Page>()
  const [versionOffset, setVersionOffset] = useState(0)
  const [versionLoading, setVersionLoading] = useState(false)
  const [versionError, setVersionError] = useState('')
  const [historical, setHistorical] = useState<MemoryV2Item>()
  const [restoreVersion, setRestoreVersion] = useState<{ current: MemoryV2Item; historical: MemoryV2Item; operationId: string }>()
  const [restoreVersionAcknowledged, setRestoreVersionAcknowledged] = useState(false)
  const [restoreVersionAttempted, setRestoreVersionAttempted] = useState(false)
  const [restoreVersionLatest, setRestoreVersionLatest] = useState<MemoryV2Item>()
  const [restoreVersionError, setRestoreVersionError] = useState('')
  const historyOwner = useRef(0)
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState<MemoryV2Context>()
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState('')
  const [searchHistory, setSearchHistory] = useState(false)
  const alive = useRef(true), lock = useRef(false), detailRequest = useRef(0)
  const detailView = useRef(view); detailView.current = view
  useEffect(() => {
    detailRequest.current++; historyOwner.current++
    setStarDetailOpen(false); setAdvancedDetailOpen(false); setSelected(undefined)
  }, [view])
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const filters: MemoryV2Filters = useMemo(() => ({ scope, ...(scope === 'work' ? { mode: 'work', session_id: workSessionId } : {}), layer, category: category || undefined, include_deleted: includeDeleted, pinned:pinnedOnly?true:undefined, query:browseFilter||undefined,before:beforeFilter||undefined }), [scope, workSessionId, layer, category, includeDeleted,pinnedOnly,browseFilter,beforeFilter])
  const writesReady = memorySyncWritesReady(syncState, baselinePending) && !loading && (!graphOpen || !graphLoading)
  const refresh = () => { setReload(value => value + 1); setSearch(undefined) }
  const syncController = useRef<ReturnType<typeof createMemoryV2Sync> | undefined>(undefined)
  const searchRequest = useRef(0)
  const syncReadiness = useRef({ writesReady, baselinePending })
  syncReadiness.current = { writesReady, baselinePending }
  const syncGuard = useRef({ externalPending, protection: { editOpen, actionOpen: Boolean(action)||relationOpen, versionsOpen: versionsOpen || starDetailOpen||advancedDetailOpen, restoreOpen: Boolean(restoreVersion), busy } })
  syncGuard.current = { externalPending, protection: { editOpen, actionOpen: Boolean(action)||relationOpen, versionsOpen: versionsOpen || starDetailOpen||advancedDetailOpen, restoreOpen: Boolean(restoreVersion), busy } }
  const clearStaleViews = () => {
    syncReadiness.current.writesReady = false; setLoading(true)
    if (graphOpen) setGraphLoading(true)
    detailRequest.current++; historyOwner.current++; searchRequest.current++
    setSelected(undefined); setSearch(undefined); setSearchLoading(false); setHistorical(undefined); setVersions(undefined)
    setRestoreVersion(undefined); setRestoreVersionLatest(undefined); setRestoreVersionAcknowledged(false)
    setForgetPreview(undefined); setAcknowledged(false); setPage(undefined); setGraph(undefined)
    setReload(value => value + 1)
    // Do not blindly reload an old selected ID: its scope or authorization may
    // have changed. Selection can be made again from the newly filtered list.
  }
  const onExternalChange = useRef<(initial: boolean) => void | Promise<void>>(() => undefined)
  onExternalChange.current = async initial => {
    if(settingsOnly)return
    const decision = memorySyncPanelDecision({ ...syncGuard.current.protection, busy: syncGuard.current.protection.busy || lock.current }, syncGuard.current.externalPending, initial)
    if (decision === 'retain-pending') return
    if (decision === 'verify-baseline') {
      syncReadiness.current = { writesReady: false, baselinePending: true }; setBaselinePending(true)
      setNotice('首次只读基线尚待校核；输入和历史选择保留，这不表示有外部更新。请关闭旧操作后检查更新，再重新核对写入。')
      return
    }
    detailRequest.current++; historyOwner.current++; searchRequest.current++
    setAcknowledged(false);setRelationAcknowledged(false); setForgetPreview(undefined); setRestoreVersionAcknowledged(false); setSearchLoading(false)
    if (decision === 'defer') {
      syncGuard.current.externalPending = true; setExternalPending(true)
      setNotice('检测到外部更新。输入和所选历史版本已保留，旧批准已撤销；关闭旧操作后点击「检查更新」重新比较。')
    } else clearStaleViews()
  }
  useEffect(() => {
    const update = () => setVisible(!document.hidden)
    document.addEventListener('visibilitychange', update)
    return () => document.removeEventListener('visibilitychange', update)
  }, [])
  useEffect(() => {
    setBaselinePending(false); syncReadiness.current = { writesReady: false, baselinePending: false }; setSyncState(undefined)
    const controller = createMemoryV2Sync({
      context: { connected: connection === 'connected', visible, scope, session_id: scope === 'work' ? workSessionId : undefined, layer, category: category || undefined, include_deleted: includeDeleted },
      fetcher: async (path, init) => { await establishHostSession(); return fetch(path, init) },
      onChange: change => { if (alive.current && !change.signal.aborted) return onExternalChange.current(change.initial) },
      onStatus: state => { if (alive.current) { if (!memorySyncWritesReady(state, syncReadiness.current.baselinePending)) syncReadiness.current.writesReady = false; setSyncState(state) } },
    })
    syncController.current = controller; controller.start()
    return () => { controller.stop(); if (syncController.current === controller) syncController.current = undefined }
    // A reload or selection never recreates this owner's polling baseline.
  }, [scope, workSessionId, layer, category, includeDeleted])
  useEffect(() => {
    syncController.current?.updateContext({ connected: connection === 'connected', visible, scope, session_id: scope === 'work' ? workSessionId : undefined, layer, category: category || undefined, include_deleted: includeDeleted })
  }, [connection, visible, scope, workSessionId, layer, category, includeDeleted])
  const manualSyncBlocked=editOpen||Boolean(action)||relationOpen||versionsOpen||Boolean(restoreVersion)||busy
  const syncPanelStatus=unavailable?'请先选择工作会话。':memorySyncPanelStatus(syncState,connection==='connected',visible)
  const syncPanelDisabled=connection!=='connected'||!visible||unavailable||manualSyncBlocked||Boolean(syncState?.inflight)
  useEffect(()=>{onSyncStatus?.({status:syncPanelStatus,disabled:syncPanelDisabled,inflight:Boolean(syncState?.inflight)})},[onSyncStatus,syncPanelStatus,syncPanelDisabled,syncState?.inflight])
  const checkExternalUpdates = () => {
    if (manualSyncBlocked||lock.current) return
    if(settingsOnly){syncController.current?.refreshNow();return}
    setStarDetailOpen(false);setAdvancedDetailOpen(false);syncGuard.current.protection.versionsOpen=false
    const wasPending=syncGuard.current.externalPending
    syncGuard.current.externalPending=false;setExternalPending(false)
    syncReadiness.current={writesReady:false,baselinePending:false};setBaselinePending(false)
    clearStaleViews()
    setNotice(wasPending?'已读取最新记忆，请重新选择并核对内容。':'已重新读取当前记忆，正在检查其他窗口的更新。')
    syncController.current?.refreshNow()
  }
  useEffect(() => {
    window.addEventListener('lingxi-memory-check-updates', checkExternalUpdates)
    return () => window.removeEventListener('lingxi-memory-check-updates', checkExternalUpdates)
  })
  const manualSyncRef=useRef(checkExternalUpdates);manualSyncRef.current=checkExternalUpdates
  useEffect(()=>{const refresh=()=>manualSyncRef.current();window.addEventListener('lingxi-memory-refresh',refresh);return()=>window.removeEventListener('lingxi-memory-refresh',refresh)},[])
  useEffect(() => {
    if (settingsOnly || unavailable || externalPending) return
    let disposed = false
    setLoading(true); setError('')
    void memoryV2.list({ ...filters, offset, limit: MEMORY_V2_PAGE_SIZE }).then(result => {
      if (disposed) return
      if (offset > 0 && offset >= result.total) { setOffset(Math.max(0, Math.ceil(result.total / MEMORY_V2_PAGE_SIZE) - 1) * MEMORY_V2_PAGE_SIZE); return }
      setPage(result)
    }).catch(reason => { if (!disposed) { setPage(undefined); setError(`列表读取失败：${message(reason)}`) } }).finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true }
  }, [filters, offset, reload, unavailable, externalPending, settingsOnly])
  useEffect(() => {
    setGraph(undefined); setGraphError('')
    if (!graphOpen || unavailable || externalPending) return
    const abort = new AbortController()
    setGraphLoading(true)
    void loadCompleteMemoryV2Graph(memoryV2, filters, undefined, abort.signal).then(result => { if (!abort.signal.aborted) setGraph(result) }).catch(reason => { if (!abort.signal.aborted) setGraphError(message(reason)) }).finally(() => { if (!abort.signal.aborted) setGraphLoading(false) })
    return () => abort.abort()
  }, [filters, reload, graphOpen, unavailable, externalPending])
  useEffect(() => {
    if (!versionsOpen || !selected || externalPending) return
    let disposed = false
    setVersionLoading(true); setVersionError('')
    void memoryV2.versions(selected.id, versionOffset).then(result => { if (!disposed) setVersions(result) }).catch(reason => { if (!disposed) setVersionError(message(reason)) }).finally(() => { if (!disposed) setVersionLoading(false) })
    return () => { disposed = true }
  }, [versionsOpen, selected?.id, versionOffset, reload, externalPending])
  const selectItem = async (id: string) => {
    const request = ++detailRequest.current
    const ownerView = view
    setError(''); setSelected(undefined); setVersionsOpen(false); setHistorical(undefined)
    historyOwner.current++; setRestoreVersion(undefined); setRestoreVersionAcknowledged(false); setRestoreVersionLatest(undefined)
    try {
      const item = await memoryV2.get(id)
      if (alive.current && request === detailRequest.current && detailView.current === ownerView) { setSelected(item); return true }
    } catch (reason) { if (alive.current && request === detailRequest.current) setError(`详情读取失败：${message(reason)}`) }
    return false
  }
  // A direct link must not open a protected detail before the first readonly
  // sync baseline; otherwise initial verification is deferred behind that detail.
  const routeBaselineEstablished=syncState?.last_checked!==undefined&&!baselinePending
  useEffect(()=>{
    if(!targetId||settingsOnly||connection!=='connected'||!routeBaselineEstablished)return
    let cancelled=false
    void selectItem(targetId).then(async found=>{
      if(cancelled||!found)return
      setAdvancedDetailOpen(true)
      if(targetRevision){
        const revision=Number(targetRevision)
        if(!Number.isSafeInteger(revision)||revision<1){setError('历史版本无效，请重新检索。');return}
        try{const item=await memoryV2.get(targetId,revision);if(!cancelled){setHistorical(item);setVersionsOpen(true)}}
        catch(reason){if(!cancelled)setError(`历史版本读取失败：${message(reason)}`)}
      }
    })
    return()=>{cancelled=true}
  },[targetId,targetRevision,scope,connection,settingsOnly,routeBaselineEstablished])
  const closeDetail = () => { detailRequest.current++; historyOwner.current++; setStarDetailOpen(false); setSelected(undefined) }
  const changeAdvancedDetail = (open: boolean) => { if (!open) { detailRequest.current++; historyOwner.current++ } setAdvancedDetailOpen(open) }
  const run = async (task: () => Promise<void>) => {
    if (lock.current || syncGuard.current.externalPending || !syncReadiness.current.writesReady) return
    lock.current = true; setBusy(true); setError(''); setNotice('')
    try { await task() } catch (reason) {
      if (!alive.current) return
      setError(message(reason))
      if (syncGuard.current.externalPending) return
      if (memoryV2Conflict(reason)) {
        const id = editing?.id || action?.item.id || selected?.id
        if (id) {
          try { const latest = await memoryV2.get(id); if (alive.current) { setConflict(latest); setSelected(latest); setForgetPreview(undefined); setAcknowledged(false); setError('版本已变化：已重新读取最新记录，原输入保留。请比较后重新明确操作，不会自动覆盖或重放确认。') } }
          catch (readError) { if (alive.current) setError(`版本冲突，重读失败：${message(readError)}。输入保留，请重试读取。`) }
        }
        refresh()
      }
    } finally { lock.current = false; if (alive.current) setBusy(false) }
  }
  const togglePin=(item:MemoryV2Item)=>{if(item.status!=='active')return;void run(async()=>{
    const result=await memoryV2.patch(item.id,{expected_revision:item.revision,pinned:!item.pinned})
    if(alive.current&&!syncGuard.current.externalPending){if(selected?.id===item.id)setSelected(result);refresh();setNotice(result.pinned?'已钉选记忆。':'已取消钉选。')}
  })}
  const openEditor = (item?: MemoryV2Item) => {
    if (syncGuard.current.externalPending || !syncReadiness.current.writesReady) return
    setEditing(item); setDraft(item ? { key: item.key, value: item.value, category: item.category, layer: item.layer === 4 ? 3 : item.layer, importance: item.importance, pinned: item.pinned, occurred_at: item.occurred_at || '',tags:(item.tags||[]).join('，'),valence:item.valence??null,arousal:item.arousal??null } : newDraft(scope)); setConflict(undefined); setError(''); setEditOpen(true)
  }
  const save = async () => {
    if (conflict || syncGuard.current.externalPending || !syncReadiness.current.writesReady) return
    if (!draft.key.trim() || !draft.value.trim()) throw new Error('标题与内容不能为空。')
    if (scope === 'work' && !['habit', 'project'].includes(draft.category)) throw new Error('工作记忆仅允许操作习惯或项目名册。')
    const payload = { ...draft, tags:[...new Set(draft.tags.split(/[,，、;；]/u).map(tag=>tag.trim()).filter(Boolean))], occurred_at: draft.occurred_at.trim() || null }
    const result = editing ? await memoryV2.patch(editing.id, { ...payload, expected_revision: editing.revision }) : await memoryV2.create({ ...payload, scope, ...(scope === 'work' ? { session_id: workSessionId } : {}) } satisfies MemoryV2Create)
    if (alive.current && syncGuard.current.externalPending) { setNotice('保存请求已完成，但同时检测到外部更新。草稿保留为待比较输入，请取消旧编辑后检查更新；不会重复提交。'); return }
    if (alive.current) { setSelected(result); setEditOpen(false); setNotice(editing ? '已保存；共享内容需重新授权。' : '记忆已保存。'); refresh() }
  }
  const openAction = (kind: ItemAction, item: MemoryV2Item) => {
    if (syncGuard.current.externalPending || !syncReadiness.current.writesReady) return
    setAction({ kind, item, operationId: memoryV2OperationId(kind) }); setShareKey(item.key); setCoreValue(item.value); setConflict(undefined); setForgetPreview(undefined); setForgetPhrase(''); setAcknowledged(false); setError('')
  }
  const performAction = async () => {
    if (!action || conflict || !acknowledged || syncGuard.current.externalPending || !syncReadiness.current.writesReady) return
    const item = action.item
    let result: MemoryV2Item | undefined
    if (action.kind === 'delete') result = await memoryV2.delete(item)
    if (action.kind === 'restore') result = await memoryV2.restore(item)
    if (action.kind === 'share') { await memoryV2.share(item, shareKey.trim()); result = await memoryV2.get(item.id) }
    if (action.kind === 'core') result = await memoryV2.confirmCore(item, action.operationId, coreValue)
    if (action.kind === 'reinforce') result = await memoryV2.reinforce(item, action.operationId)
    if (action.kind === 'forget') {
      if (!forgetPreview || forgetPhrase !== '彻底忘记') return
      const forgotten = await memoryV2.forgetConfirm(forgetPreview)
      if (alive.current) { setSelected(undefined); setNotice(forgotten.warning) }
    }
    if (alive.current && syncGuard.current.externalPending) { setNotice('操作请求已结束，同时存在外部更新。旧操作已停用，请取消旧对话后检查更新；不会自动重试。'); return }
    if (alive.current) {
      if (result) setSelected(result)
      setAction(undefined); setVersionsOpen(false); setHistorical(undefined)
      if (action.kind !== 'forget') setNotice(action.kind === 'restore' ? '已恢复普通删除的记忆。原共享授权未恢复，请重新打开共享对话框授权。' : action.kind === 'share' ? '已按本次明确授权建立 / 更新共享投影。以后恢复来源仍需重新授权。' : action.kind === 'delete' ? '已普通删除，可从“已删除 / 失效”列表恢复。原聊天与备份均未删除。' : action.kind === 'core' ? '已由你手动确认核心，置顶状态未被自动改变。' : '已记录这一次用户主动确认的强化；浏览不会强化。')
      refresh()
    }
  }
  useEffect(() => () => { historyOwner.current++ }, [])
  const openRestoreVersion = () => {
    if (!selected || !historical || busy || syncGuard.current.externalPending || !syncReadiness.current.writesReady || memoryV2RestoreVersionProblem(selected, historical)) return
    setRestoreVersion({ current: selected, historical, operationId: memoryV2OperationId('restore-version') })
    setRestoreVersionAcknowledged(false); setRestoreVersionAttempted(false); setRestoreVersionLatest(undefined); setRestoreVersionError('')
  }
  const cancelRestoreVersion = () => {
    if (busy) return
    setRestoreVersion(undefined); setRestoreVersionAcknowledged(false); setRestoreVersionLatest(undefined); setRestoreVersionError('')
  }
  const performRestoreVersion = async () => {
    if (!restoreVersion || !restoreVersionAcknowledged || restoreVersionAttempted || restoreVersionLatest || lock.current || syncGuard.current.externalPending || !syncReadiness.current.writesReady) return
    const pending = restoreVersion, owner = historyOwner.current
    const problem = memoryV2RestoreVersionProblem(pending.current, pending.historical)
    if (problem) { setRestoreVersionError(problem); return }
    lock.current = true; setBusy(true); setRestoreVersionAttempted(true); setRestoreVersionError('')
    const currentOwner = () => alive.current && owner === historyOwner.current
    try {
      const result = await memoryV2.restoreContentVersion(pending.current, pending.historical, { user_confirmed: true, operation_id: pending.operationId })
      if (!currentOwner()) return
      setSelected(result); setHistorical(result); setRestoreVersion(undefined); setRestoreVersionAcknowledged(false)
      // End the protected operation explicitly. The next projection change may
      // include our write AND another window's write, so never skip that cursor.
      setVersionsOpen(false); syncGuard.current.protection.versionsOpen = false; syncGuard.current.protection.restoreOpen = false
      setVersionOffset(0); refresh()
      setNotice('历史内容已创建为新的当前版本；已关闭恢复与历史对话，可点击版本历史查看新版本。当前选择保留，下一次只读检查仍会重读全部变化，不跳过并发更新。未恢复旧确认或共享授权，原聊天未删除。')
    } catch (reason) {
      if (!currentOwner()) return
      setRestoreVersionAcknowledged(false); setRestoreVersionError(`恢复未完成或结果未知：${message(reason)}。不会自动重试。`)
      if (syncGuard.current.externalPending) return
      if (memoryV2Conflict(reason)) {
        try {
          const latest = await memoryV2.get(pending.current.id)
          if (currentOwner()) { setRestoreVersionLatest(latest); setRestoreVersionError('版本已变化：目标历史内容仍保留。请比较最新记录，再单独采用新修订并重新确认；不会自动更新 expected_revision 或重试。') }
        } catch (readError) { if (currentOwner()) setRestoreVersionError(`版本冲突，重读失败：${message(readError)}。目标历史内容仍保留，请取消后重新读取；不会重试恢复。`) }
      }
    } finally { lock.current = false; if (alive.current) setBusy(false) }
  }
  const updateSearch = (nextQuery: string, nextHistory = searchHistory) => {
    searchRequest.current++
    setQuery(nextQuery); setSearchHistory(nextHistory)
    setSearch(undefined); setSearchError(''); setSearchLoading(false)
  }
  const contextSearch = async () => {
    if (connection !== 'connected' || unavailable) return
    const request = ++searchRequest.current
    setSearchLoading(true); setSearch(undefined); setSearchError('')
    try { const result = await memoryV2.context(scope, scope === 'work' ? workSessionId : undefined, query, searchHistory, 40); if (alive.current && request === searchRequest.current) setSearch(result) }
    catch (reason) { if (alive.current && request === searchRequest.current) setSearchError(`检索失败：${message(reason)}`) }
    finally { if (alive.current && request === searchRequest.current) setSearchLoading(false) }
  }
  const locateSearchResult = async (item: MemoryV2Item) => {
    if(settingsOnly){
      const revision=!item.is_current?memoryV2ContentRevision(item):undefined
      if(!item.is_current&&revision===undefined){setSearchError('历史版本无效，请重新检索。');return}
      navigate(`/memory?scope=${encodeURIComponent(scope)}&item=${encodeURIComponent(item.id)}${revision?`&revision=${revision}`:''}`)
      return
    }
    const request = searchRequest.current
    setSearchError('')
    const found = await selectItem(item.id)
    if (!alive.current || request !== searchRequest.current) return
    if (!found) { setSearchError('记忆读取失败，请重试。'); return }
    const owner = historyOwner.current
    try {
      let historicalItem: MemoryV2Item | undefined
      if (!item.is_current) {
        const revision = memoryV2ContentRevision(item)
        if (revision === undefined) throw new Error('历史版本无效，请重新检索。')
        historicalItem = await memoryV2.get(item.id, revision)
      }
      if (!alive.current || request !== searchRequest.current || owner !== historyOwner.current) return
      if (view === 'star') setStarDetailOpen(true)
      else setAdvancedDetailOpen(true)
      onLocated?.()
      if (historicalItem) { setHistorical(historicalItem); setVersionsOpen(true) }
    } catch (reason) { if (alive.current && request === searchRequest.current && owner === historyOwner.current) setSearchError(`记忆读取失败：${message(reason)}`) }
  }
  
  const changeFilter = (callback: () => void) => { callback(); setOffset(0); setSelected(undefined); setVersionsOpen(false); setHistorical(undefined); setRestoreVersion(undefined); setRestoreVersionAcknowledged(false); setRestoreVersionLatest(undefined); historyOwner.current++; setSearch(undefined); detailRequest.current++ }
  const quickActions=selected&&selected.status==='active'?<div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={busy||externalPending||!writesReady} onClick={()=>selected.layer===4?openAction('core',selected):openEditor(selected)}>改一下</Button>{scope!=='shared'&&<Button size="sm" variant="outline" disabled={busy||externalPending||!writesReady||!selected.eligible||selected.layer===4} title={selected.layer===4?'核心记忆不能直接共享':undefined} onClick={()=>openAction('share',selected)}>谁能使用</Button>}<Button size="sm" variant="ghost" disabled={busy||externalPending||!writesReady} onClick={()=>openAction('delete',selected)}>不再使用</Button><p className="w-full text-xs leading-5 text-muted-foreground">{scope==='shared'?'这是已授权的共享记忆，可跨模式使用。':scope==='work'?'当前工作会话使用；仅工作习惯和项目名册可长期保存。':'仅消息陪伴使用；共享须逐条授权。'}“不再使用”可恢复；“彻底忘记”在更多操作中，需预览并确认。</p></div>:null
  const detailContent = <div className="memory-detail-content min-w-0 p-5">{selected ? <div className="space-y-5">{quickActions}<div><div className="flex flex-wrap items-center gap-2"><Badge>L{selected.layer}</Badge><Badge variant={selected.status === 'deleted' ? 'warning' : selected.eligible ? 'outline' : 'secondary'}>{selected.status === 'deleted' ? '已删除' : selected.eligible ? (categoryNames[selected.category] || selected.category) : '来源或授权已失效'}</Badge>{selected.pinned && <Badge variant="secondary"><Pin className="size-3" />已置顶</Badge>}</div><h3 className="mt-3 break-words text-lg font-semibold">{selected.key}</h3><p className="mt-3 whitespace-pre-wrap break-words text-sm leading-7">{selected.value}</p></div><details className="personal-disclosure memory-metadata"><summary>记忆信息</summary><dl className="grid grid-cols-2 gap-3 text-xs"><div><dt className="text-muted-foreground">当前修订 / 内容版本</dt><dd className="mt-1 break-all">r{selected.revision} / {selected.version_id}</dd></div><div><dt className="text-muted-foreground">归属与类别</dt><dd className="mt-1 break-all">{selected.owner_id} · {categoryNames[selected.category] || selected.category}</dd></div><div><dt className="text-muted-foreground">重要性 / 活跃度</dt><dd className="mt-1">{Math.round(selected.importance * 100)}% / {Math.round(selected.activation * 100)}%</dd></div><div><dt className="text-muted-foreground">确认次数</dt><dd className="mt-1">{selected.reinforcement_count} 次</dd></div><div><dt className="text-muted-foreground">创建 / 更新</dt><dd className="mt-1">{date(selected.created_at)}<br />{date(selected.updated_at)}</dd></div><div><dt className="text-muted-foreground">事件发生 / 用户确认</dt><dd className="mt-1">{date(selected.occurred_at)}<br />{date(selected.confirmed_at)}</dd></div></dl></details>{!externalPending && <SourceDetails item={selected} mode={scope === 'work' ? 'work' : 'companion'} workSessionId={workSessionId} />}<details className="personal-disclosure"><summary>活跃度与确认记录</summary><ActivationCurve item={selected} /></details><RelationDetails item={selected} onLocate={id => void selectItem(id)} />
      <div className="flex flex-wrap gap-2 border-t border-border pt-4"><Button size="sm" variant="outline" disabled={busy} onClick={() => { setVersionsOpen(true); setVersionOffset(0); setHistorical(undefined) }}><History className="size-3.5" />版本历史</Button>{selected.status === 'active' && <><Button size="sm" variant="outline" disabled={busy || selected.layer === 4} title={selected.layer === 4 ? '核心内容修改需单独确认' : undefined} onClick={() => openEditor(selected)}>编辑</Button><Button size="sm" variant="outline" disabled={busy} onClick={() => void run(async () => { const result = await memoryV2.patch(selected.id, { expected_revision: selected.revision, pinned: !selected.pinned }); if (alive.current && !syncGuard.current.externalPending) { setSelected(result); refresh() } })}><Pin className="size-3.5" />{selected.pinned ? '取消置顶' : '置顶'}</Button></>}<DropdownMenu><DropdownMenuTrigger asChild><Button size="sm" variant="ghost" disabled={busy} aria-label="更多记忆操作"><MoreHorizontal />更多</Button></DropdownMenuTrigger><DropdownMenuContent align="end">{selected.status === 'active' && <><DropdownMenuItem disabled={busy || !selected.eligible} onSelect={() => openAction('reinforce', selected)}><Check className="size-3.5" />再次确认</DropdownMenuItem><DropdownMenuItem disabled={busy || !selected.eligible || (scope === 'work' && !['habit', 'project'].includes(selected.category))} onSelect={() => openAction('core', selected)}><ShieldCheck className="size-3.5" />{selected.layer === 4 ? '修改核心' : '设为核心'}</DropdownMenuItem>{scope !== 'shared' && <DropdownMenuItem disabled={busy || !selected.eligible || selected.layer === 4} onSelect={() => openAction('share', selected)}><Share2 className="size-3.5" />授权共享</DropdownMenuItem>}<DropdownMenuItem disabled={busy} onSelect={() => openAction('delete', selected)}><Trash2 className="size-3.5" />删除</DropdownMenuItem></>}{selected.status === 'deleted' && <DropdownMenuItem disabled={busy} onSelect={() => openAction('restore', selected)}><Undo2 className="size-3.5" />恢复记忆</DropdownMenuItem>}{!selected.redacted && <DropdownMenuItem className="text-destructive" disabled={busy} onSelect={() => openAction('forget', selected)}><AlertTriangle className="size-3.5" />彻底忘记…</DropdownMenuItem>}</DropdownMenuContent></DropdownMenu></div>{selected.layer === 4 && <p className="text-xs leading-5 text-muted-foreground">修改核心需重新确认；核心记忆不能直接共享。</p>}</div> : <div className="memory-detail-empty"><BookOpen/><h3>选择一条记忆</h3><p>在左侧查看记忆内容与来源。</p></div>}</div>
  return <section hidden={settingsOnly} className="space-y-4" aria-label="四层记忆管理">

    {externalPending && <div role="alert" className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-4 text-sm leading-6"><strong>外部更新，待重新比较</strong><p>记忆有更新，当前操作已暂停，输入和历史选择已保留。取消编辑或关闭历史后，在设置的「同步」中检查更新，再重新核对并确认。</p></div>}
    {connection === 'connected' && !externalPending && (!writesReady || baselinePending) && <p role="status" className="rounded-lg border border-amber-500/30 p-3 text-xs text-amber-800 dark:text-amber-300">正在校核记忆，保存暂不可用。若长时间未完成，可在设置的「同步」中检查更新。</p>}
    <fieldset className="min-w-0 space-y-4 border-0 p-0" aria-label={externalPending ? '旧快照操作已停用' : '当前记忆视图'}>
    {unavailable ? <Card className="p-8 text-center"><LockKeyhole className="mx-auto mb-3 size-6 text-muted-foreground"/><h2>请先选择一个工作会话</h2></Card> : view === 'list' ? <div className="memory-list-view">
      <div className="memory-list-toolbar"><label className="memory-list-search"><Search className="size-4"/><Input aria-label="筛选记忆列表" placeholder="搜索记忆" value={browseQuery} onChange={event => changeFilter(() => setBrowseQuery(event.target.value))}/></label></div>
      <div className="memory-list-subtoolbar"><span>{page ? `共 ${page.total} 条` : '数量未取得'}</span><div className="memory-list-actions"><Button size="sm" variant="outline" aria-pressed={pinnedOnly} onClick={() => changeFilter(() => setPinnedOnly(value => !value))}>{pinnedOnly ? '已钉选' : '只看钉选'}</Button><Button size="sm" disabled={externalPending || !writesReady || busy || connection !== 'connected'} onClick={() => openEditor()}><Plus className="size-4"/>记住一件事</Button><Button size="sm" variant="outline" disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - MEMORY_V2_PAGE_SIZE))}>上一页</Button><Button size="sm" variant="outline" disabled={loading || !page || offset + MEMORY_V2_PAGE_SIZE >= page.total} onClick={() => setOffset(offset + MEMORY_V2_PAGE_SIZE)}>下一页</Button></div></div>
      {error && <p role="alert" className="personal-error">{error}</p>}
      <div className="memory-list-content"><div className="memory-list-rows">{loading ? <p className="memory-list-message" role="status">正在读取记忆…</p> : page?.items.length ? page.items.map(item => <button key={item.id} type="button" className="memory-list-row" aria-current={selected?.id === item.id ? 'true' : undefined} onClick={() => void selectItem(item.id)}><span><strong>{item.key}</strong><small>{item.value}</small></span><span>L{item.layer} · {categoryNames[item.category] || item.category}{item.pinned ? ' · 已钉选' : ''}</span></button>) : <div className="memory-list-empty"><Search/><h3>没有找到记忆</h3><p>{error ? '读取失败，请在同步中检查连接。' : '调整检索词，或添加一条记忆。'}</p></div>}</div><div className="memory-list-detail">{detailContent}</div></div>
    </div> : <MemoryExplorer
       scope={scope} sessionId={scope==='work'?workSessionId:undefined} catalog={catalog} graph={graph} loading={graphLoading||loading} error={graphError||catalogError||error} onViewList={onViewList}
      selected={selected} detailOpen={starDetailOpen} disabled={externalPending||!writesReady} busy={busy} connected={connection==='connected'} directoryRequest={directoryRequest}
      query={browseQuery} onQuery={value=>changeFilter(()=>setBrowseQuery(value))} pinned={pinnedOnly} onPinned={value=>changeFilter(()=>setPinnedOnly(value))}
      layer={layer} onLayer={value=>changeFilter(()=>setLayer(value as MemoryV2Layer|undefined))} before={before} onBefore={value=>changeFilter(()=>setBefore(value))}
      records={page?.items||[]} total={page?.total||0} offset={offset} includeDeleted={includeDeleted} onIncludeDeleted={value=>changeFilter(()=>setIncludeDeleted(value))} onPage={setOffset} onSelect={id=>void selectItem(id).then(found=>{if(found)setStarDetailOpen(true)})} onCloseDetail={closeDetail}
      onAccess={()=>setAdvancedDetailOpen(true)} onNew={()=>openEditor()} onEdit={()=>selected&&(selected.layer===4?openAction('core',selected):openEditor(selected))} onDelete={()=>selected&&openAction('delete',selected)} onRestore={()=>selected&&openAction('restore',selected)} onAdvanced={()=>setAdvancedDetailOpen(true)}
      onPin={()=>selected&&togglePin(selected)} onPinRecord={togglePin}
      onConnect={()=>{setRelationOpen(true);setRelationTarget('');setRelationLabel('相关记忆');setRelationKind('similarity');setRelationAcknowledged(false);setError('');setConflict(undefined)}}
      curve={selected?<ActivationCurve item={selected}/>:null}/>
    }
    {notice&&<p role="status" className="memory-view-notice">{notice}</p>}
    </fieldset>
    <MemoryToolsDrawer open={advancedDetailOpen} onOpenChange={changeAdvancedDetail} title="记忆详情" className="memory-detail-drawer">
      {(externalPending || !writesReady) && <p role="status" className="memory-tool-notice">{externalPending ? '记忆有更新，关闭详情后可在设置中检查更新。' : '正在校核记忆…'}</p>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <fieldset disabled={externalPending || !writesReady} className="min-w-0 border-0 p-0">{detailContent}</fieldset>
    </MemoryToolsDrawer>
    {searchHost&&createPortal(<>{unavailable && <p role="status">请先选择一个工作会话。</p>}{connection !== 'connected' && <p role="status" className="memory-tool-notice">连接本地服务后检索记忆。</p>}{searchError && <p role="alert" className="text-sm text-destructive">{searchError}</p>}<fieldset disabled={connection !== 'connected'||unavailable} className="min-w-0 border-0 p-0"><Card className="memory-tools-content space-y-4 p-4"><div><h2 className="flex items-center gap-2 text-sm font-semibold"><Search className="size-4" />检索记忆</h2></div><form className="flex flex-wrap items-center gap-3" onSubmit={event => { event.preventDefault(); if (!searchLoading) void contextSearch() }}><Input className="min-w-44 flex-1" aria-label="记忆上下文关键词" maxLength={16000} value={query} onChange={event => updateSearch(event.target.value)} placeholder="搜索记忆…" /><label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={searchHistory} onChange={event => updateSearch(query, event.target.checked)} />包含历史版本</label><Button type="submit" size="sm" variant="outline" disabled={searchLoading}>{searchLoading ? '检索中…' : '搜索'}</Button></form>{search && <div className="space-y-3"><p className="text-xs text-muted-foreground">共 {search.total} 条匹配，显示前 {search.items.length} 条</p><div className="grid gap-2 md:grid-cols-2">{search.items.map(item => <button type="button" key={`${item.id}:${item.version_id}`} className="rounded-lg border border-border p-3 text-left text-sm hover:bg-accent/40" onClick={() => void locateSearchResult(item)}><span className="flex gap-2"><strong className="flex-1">{item.key}</strong><Badge variant={item.is_current ? 'outline' : 'warning'}>{item.is_current ? '当前' : '历史 · 非当前'}</Badge></span><span className="mt-1 block line-clamp-2 text-xs text-muted-foreground">{item.value}</span></button>)}</div><details className="rounded-lg border border-border p-3 text-xs"><summary className="cursor-pointer">项目记录 · {search.projects.length} 条</summary><ul className="mt-2 space-y-2">{search.projects.map(project => <li key={project.id}><button type="button" className="text-left text-primary underline-offset-2 hover:underline" onClick={() => { if(settingsOnly) navigate(`/memory?scope=${encodeURIComponent(project.scope)}&item=${encodeURIComponent(project.id)}`); else void selectItem(project.id).then(found => { if (found) { setAdvancedDetailOpen(true); onLocated?.() } else setSearchError('记忆读取失败，请重试。') }) }}>{project.key} · {project.value} · {project.scope} · r{project.revision}</button></li>)}</ul></details></div>}</Card></fieldset></>,searchHost)}

    <Dialog open={relationOpen} onOpenChange={value=>{if(!busy)setRelationOpen(value)}}><DialogContent data-memory-dialog><DialogHeader><DialogTitle>连接两条记忆</DialogTitle><DialogDescription>可连接不同层级的记忆。相似候选不会作为已确认事实。</DialogDescription></DialogHeader><label className="ui-form-field">连接到<Select aria-label="连接目标" value={relationTarget} onChange={event=>{setRelationTarget(event.target.value);setRelationAcknowledged(false)}}><option value="">选择一条记忆</option>{graph?.nodes.filter(item=>item.id!==selected?.id&&item.eligible&&item.status==='active').map(item=><option key={item.id} value={item.id}>L{item.layer} · {item.key}</option>)}</Select></label><label className="ui-form-field">说明<Input aria-label="连接说明" maxLength={160} value={relationLabel} onChange={event=>{setRelationLabel(event.target.value);setRelationAcknowledged(false)}}/></label><Select aria-label="连接类型" value={relationKind} onChange={event=>{setRelationKind(event.target.value as 'confirmed'|'similarity');setRelationAcknowledged(false)}}><option value="similarity">相似候选</option><option value="confirmed">已确认连接</option></Select><label className="flex gap-2 text-sm"><input type="checkbox" aria-label="确认记忆连接" disabled={externalPending} checked={relationAcknowledged} onChange={event=>setRelationAcknowledged(event.target.checked)}/>我已核对这两条记忆，确认添加连接。</label>{error&&<p role="alert" className="text-sm text-destructive">{error}</p>}{externalPending&&<p role="alert">记忆已更新，请关闭后检查更新。</p>}<DialogFooter><Button variant="outline" disabled={busy} onClick={()=>setRelationOpen(false)}>取消</Button><Button disabled={busy||externalPending||!writesReady||!relationTarget||!relationLabel.trim()||!relationAcknowledged||Boolean(conflict)} onClick={()=>void run(async()=>{const target=graph?.nodes.find(item=>item.id===relationTarget);if(!selected||!target)return;await memoryV2.relate(selected,target,relationLabel.trim(),relationKind==='confirmed');if(alive.current&&!syncGuard.current.externalPending){setSelected(await memoryV2.get(selected.id));setRelationOpen(false);refresh()}})}>添加连接</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={editOpen} onOpenChange={value => { if (!busy) { setEditOpen(value); if (!value) setConflict(undefined) } }}><DialogContent data-memory-dialog className="max-w-2xl"><DialogHeader><DialogTitle>{editing ? '改一下记忆' : '记住一件事'}</DialogTitle>{externalPending && <p role="alert" className="text-sm text-amber-700">外部更新：输入仍保留，旧保存已停用。请复制需要的内容，取消此操作后在页面点击「检查更新」。</p>}<DialogDescription>核心记忆需在保存后单独确认。</DialogDescription></DialogHeader><div className="space-y-3"><label className="block space-y-1 text-xs"><span>记忆标题</span><Input aria-label="记忆标题" maxLength={160} value={draft.key} onChange={event => setDraft(current => ({ ...current, key: event.target.value }))} /></label><label className="block space-y-1 text-xs"><span>记忆内容（1–16000 字）</span><Textarea aria-label="记忆内容" className="min-h-36" maxLength={16000} value={draft.value} onChange={event => setDraft(current => ({ ...current, value: event.target.value }))} /></label><p className="text-xs text-muted-foreground">本次记录范围：{memoryV2ScopeNames[scope]}。默认保存为一般记忆，核心与共享需另行确认。</p><details className="personal-disclosure"><summary>保留方式、类别与更多信息</summary><div className="grid gap-3 sm:grid-cols-2"><label className="space-y-1 text-xs"><span>记忆层级</span><Select className={`${selectClass} w-full`} value={draft.layer} onChange={event => setDraft(current => ({ ...current, layer: Number(event.target.value) as 1 | 2 | 3 }))}>{memoryV2Layers.filter(info => info.id !== 4).map(info => <option key={info.id} value={info.id}>{info.name}</option>)}</Select></label><label className="space-y-1 text-xs"><span>类别{scope === 'work' ? '（仅工作习惯 / 项目名册）' : ''}</span>{scope === 'work' ? <Select className={`${selectClass} w-full`} value={draft.category} onChange={event => setDraft(current => ({ ...current, category: event.target.value }))}><option value="habit">操作习惯 · habit</option><option value="project">项目名册 · project</option></Select> : <Input maxLength={80} value={draft.category} onChange={event => setDraft(current => ({ ...current, category: event.target.value }))} />}</label></div><div className="memory-editor-metadata"><label className="ui-form-field">日期<Input aria-label="记忆日期" type="date" value={draft.occurred_at.slice(0,10)} onChange={event=>setDraft(current=>({...current,occurred_at:event.target.value}))}/></label><label className="ui-form-field">标签<Input aria-label="记忆标签" value={draft.tags} onChange={event=>setDraft(current=>({...current,tags:event.target.value}))} placeholder="用逗号分隔，最多12个"/></label><label className="flex items-center gap-2 text-xs"><input aria-label="记录情绪坐标" type="checkbox" checked={draft.valence!==null&&draft.arousal!==null} onChange={event=>setDraft(current=>({...current,valence:event.target.checked?.5:null,arousal:event.target.checked?.5:null}))}/>记录情绪坐标</label>{draft.valence!==null&&draft.arousal!==null&&<div className="memory-editor-emotion"><label>感受 · {draft.valence.toFixed(2)}<input aria-label="情绪感受" type="range" min="0" max="1" step="0.01" value={draft.valence} onChange={event=>setDraft(current=>({...current,valence:Number(event.target.value)}))}/><small>低落 — 愉悦</small></label><label>强度 · {draft.arousal.toFixed(2)}<input aria-label="情绪强度" type="range" min="0" max="1" step="0.01" value={draft.arousal} onChange={event=>setDraft(current=>({...current,arousal:Number(event.target.value)}))}/><small>平静 — 强烈</small></label></div>}</div><details className="personal-disclosure"><summary>更多选项</summary><div className="space-y-3"><label className="block space-y-1 text-xs"><span>重要性 {Math.round(draft.importance * 100)}%（不等于激活度）</span><input className="w-full accent-primary" aria-label="重要性" type="range" min="0" max="1" step="0.01" value={draft.importance} onChange={event => setDraft(current => ({ ...current, importance: Number(event.target.value) }))} /></label><label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={draft.pinned} onChange={event => setDraft(current => ({ ...current, pinned: event.target.checked }))} />置顶（不改变层级）</label></div></details><p className="text-xs leading-5 text-muted-foreground">{memoryV2Layers.find(info => info.id === draft.layer)?.note}{draft.layer === 2 ? `；本次重要性对应 ${draft.importance >= .7 ? 180 : 30} 天半衰期。` : ''}</p></details></div>{conflict && <ConflictComparison original={editing} latest={conflict} onAccept={() => { if (syncGuard.current.externalPending) return; if (conflict.layer === 4 || conflict.status !== 'active') { setError('最新记录已是核心或非有效状态，请关闭编辑，使用对应的明确操作。'); return } setEditing(conflict); setConflict(undefined); setError('') }} />}{error && <p role="alert" className="text-sm text-destructive">{error}</p>}<DialogFooter><Button variant="outline" disabled={busy} onClick={() => setEditOpen(false)}>取消</Button><Button disabled={busy || externalPending || !writesReady || Boolean(conflict) || !draft.key.trim() || !draft.value.trim() || !draft.category.trim()} onClick={() => void run(save)}>保存记忆</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(action)} onOpenChange={value => { if (!value && !busy) { setAction(undefined); setConflict(undefined); setForgetPreview(undefined) } }}><DialogContent data-memory-dialog className="max-w-2xl"><DialogHeader><DialogTitle>{action ? actionTitles[action.kind] : '明确记忆操作'}</DialogTitle>{externalPending && <p role="alert" className="text-sm text-amber-700">外部更新：旧批准与忘记预览已撤销。输入仍保留，请取消此操作后检查更新并重新比较。</p>}<DialogDescription>{action?.item.key} · r{action?.item.revision}。{action && action.kind !== 'forget' && actionNotes[action.kind]}</DialogDescription></DialogHeader>{action?.kind === 'share' && <><label className="block space-y-2 text-sm"><span>共享投影标题（如冲突请选择不同标题）</span><Input maxLength={160} value={shareKey} onChange={event => { setShareKey(event.target.value); setAcknowledged(false) }} /></label><p className="text-sm leading-6">仅共享此条记忆，其他私密记忆仍保密。工作长期仅使用习惯和项目记录；内容变更或恢复后需重新授权。</p></>}{action?.kind === 'core' && <label className="block space-y-2 text-sm"><span>你明确确认的核心内容（可在此修改）</span><Textarea maxLength={16000} className="min-h-36" value={coreValue} onChange={event => { setCoreValue(event.target.value); setAcknowledged(false) }} /><span className="block text-xs leading-5 text-muted-foreground">确认后设为核心记忆，不会自动置顶或共享。</span></label>}{action?.kind === 'reinforce' && <p className="text-sm leading-6">再次确认会提高此条记忆的活跃度。浏览和搜索不会触发确认。</p>}{action?.kind === 'forget' && <div className="space-y-3"><p className="rounded-lg border border-destructive/20 bg-destructive/5 p-3 text-sm leading-6">彻底忘记不可恢复，原聊天和备份保留，不能抹除外部备份。请先核对影响范围。</p><Button variant="outline" disabled={busy || externalPending || !writesReady || Boolean(conflict)} onClick={() => void run(async () => { const preview = await memoryV2.forgetPreview(action.item.id); if (alive.current) { setForgetPreview(preview); setForgetPhrase(''); setAcknowledged(false) } })}>{forgetPreview ? '重新预览' : '预览影响范围'}</Button>{forgetPreview && <div className="space-y-3 rounded-lg border border-destructive/25 p-3"><dl className="grid grid-cols-2 gap-2 text-xs"><div><dt className="text-muted-foreground">影响记忆（含派生）</dt><dd>{forgetPreview.entity_ids.length} 条 · 派生 {forgetPreview.derived_count} 条</dd></div><div><dt className="text-muted-foreground">被阻断的聊天来源</dt><dd>{forgetPreview.source_message_ids.length} 条（原聊天保留）</dd></div></dl><details className="text-xs"><summary className="cursor-pointer">查看影响详情</summary><pre className="mt-2 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify({ entity_ids: forgetPreview.entity_ids, source_message_ids: forgetPreview.source_message_ids, scope: forgetPreview.scope, operation_id: forgetPreview.operation_id, token: forgetPreview.token }, null, 2)}</pre></details><p className="text-xs leading-5 text-destructive">{forgetPreview.warning}</p><label className="block space-y-1 text-xs"><span>输入“彻底忘记”进行强确认</span><Input value={forgetPhrase} onChange={event => setForgetPhrase(event.target.value)} autoComplete="off" /></label></div>}</div>}{conflict && <ConflictComparison original={action?.item} latest={conflict} onAccept={() => { if (syncGuard.current.externalPending) return; if (action) setAction({ ...action, item: conflict, operationId: memoryV2OperationId(action.kind) }); setConflict(undefined); setForgetPreview(undefined); setAcknowledged(false); setError('') }} />}{(!action || action.kind !== 'forget' || forgetPreview) && <label className="flex items-start gap-2 rounded-lg border border-border p-3 text-sm leading-6"><input type="checkbox" className="mt-1.5" checked={acknowledged} disabled={externalPending} onChange={event => setAcknowledged(!syncGuard.current.externalPending && event.target.checked)} />{action?.kind === 'forget' ? '我已核对影响范围，理解此操作不可恢复，原聊天和备份会保留。' : '我已了解，确认本次操作。'}</label>}{error && <p role="alert" className="text-sm text-destructive">{error}</p>}<DialogFooter><Button variant="outline" disabled={busy} onClick={() => setAction(undefined)}>取消</Button><Button disabled={busy || externalPending || !writesReady || Boolean(conflict) || !acknowledged || (action?.kind === 'forget' && (!forgetPreview || forgetPhrase !== '彻底忘记')) || (action?.kind === 'share' && !shareKey.trim()) || (action?.kind === 'core' && !coreValue.trim())} onClick={() => void run(performAction)}>{action?.kind === 'forget' ? '确认彻底忘记' : action ? actionTitles[action.kind] : '确认'}</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={versionsOpen} onOpenChange={value => { if (!busy) { setVersionsOpen(value); if (!value) { historyOwner.current++; setRestoreVersion(undefined); setHistorical(undefined) } } }}><DialogContent data-memory-dialog className="max-w-3xl"><DialogHeader><DialogTitle>来源与真实版本历史</DialogTitle>{externalPending && <p role="alert" className="text-sm text-amber-700">外部更新：以下历史为旧快照，来源可能失效，不能恢复或继续反查。请关闭历史及恢复对话后检查更新。</p>}<DialogDescription>当前修订 r{selected?.revision}。历史版本不代表当前事实；删除后保留版本，彻底忘记后内容不可恢复。读取不强化。</DialogDescription></DialogHeader>{versionError && <p role="alert" className="text-sm text-destructive">{versionError}</p>}<Pager offset={versionOffset} total={versions?.total} busy={versionLoading} onPage={setVersionOffset} /><p className="text-xs text-muted-foreground">后端共 {versions?.total ?? '—'} 个内容版本；强化 / 删除的修订号不等于新增内容版本。</p><div className="space-y-2">{versions?.items.map(item => <button type="button" key={item.version_id || item.revision} className="flex w-full items-center gap-3 rounded-lg border border-border p-3 text-left" disabled={busy} onClick={() => void run(async () => { const owner = ++historyOwner.current; const contentRevision = memoryV2ContentRevision(item); if (contentRevision === undefined) throw new Error('内容版本标识无效，不能使用实体修订号代替。'); const result = await memoryV2.get(item.id, contentRevision); if (alive.current && owner === historyOwner.current) setHistorical(result) })}><Badge variant={item.is_current ? 'success' : 'warning'}>{item.is_current ? '当前内容' : '历史 · 非当前'}</Badge><span className="flex-1 text-sm">{item.key || '内容已清除'} · {item.version_id || `r${item.revision}`}</span><span className="text-xs text-muted-foreground">L{item.layer}</span></button>)}</div>{historical && <article className="space-y-3 rounded-xl border border-border bg-muted/20 p-4"><Badge variant={historical.is_current ? 'success' : 'warning'}>{historical.is_current ? '当前内容版本' : '历史证据 · 不作为当前事实'}</Badge><h3 className="font-medium">{historical.key}</h3><p className="whitespace-pre-wrap break-words text-sm leading-6">{historical.redacted ? '已彻底忘记，不可恢复。' : historical.value}</p>{!externalPending && !historical.redacted && <SourceDetails item={historical} mode={scope === 'work' ? 'work' : 'companion'} workSessionId={workSessionId} />}<div className="space-y-2 border-t border-border pt-3"><Button type="button" size="sm" variant="outline" disabled={busy || Boolean(memoryV2RestoreVersionProblem(selected, historical))} onClick={openRestoreVersion}><Undo2 className="size-3.5" />以此历史内容创建新版本</Button>{memoryV2RestoreVersionProblem(selected, historical) && <p className="text-xs leading-5 text-muted-foreground">{memoryV2RestoreVersionProblem(selected, historical)}</p>}</div></article>}</DialogContent></Dialog>
    <Dialog open={Boolean(restoreVersion)} onOpenChange={value => { if (!value) cancelRestoreVersion() }}><DialogContent data-memory-dialog className="max-w-2xl"><DialogHeader><DialogTitle>以此历史内容创建新版本</DialogTitle>{externalPending && <p role="alert" className="text-sm text-amber-700">外部更新：历史选择保留，恢复批准已撤销。请取消并关闭历史后检查更新，不会自动采用新修订。</p>}<DialogDescription>这是新的内容编辑，不是回退实体修订或撤销普通删除。必须独立确认，失败或结果未知不会自动重试。</DialogDescription></DialogHeader>{restoreVersion && <div className="space-y-4 text-sm"><dl className="grid gap-2 text-xs"><div>当前记录：{restoreVersion.current.key} · r{restoreVersion.current.revision} · {restoreVersion.current.version_id}</div><div>目标历史内容：{restoreVersion.historical.version_id}（内容版本 {memoryV2ContentRevision(restoreVersion.historical)}）</div></dl><section className="space-y-2 rounded-lg border border-border p-3"><h3 className="font-medium">将恢复的历史字段</h3><pre className="max-h-52 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify({ key: restoreVersion.historical.key, value: restoreVersion.historical.value, category: restoreVersion.historical.category, tags: restoreVersion.historical.tags, valence: restoreVersion.historical.valence, arousal: restoreVersion.historical.arousal, occurred_at: restoreVersion.historical.occurred_at ?? null, source: restoreVersion.historical.source }, null, 2)}</pre></section><section className="rounded-lg bg-muted/30 p-3 text-xs leading-6"><h3 className="font-medium">将保留的当前字段</h3><p>层级 L{restoreVersion.current.layer}、重要性 {Math.round(restoreVersion.current.importance * 100)}%、置顶 {restoreVersion.current.pinned ? '是' : '否'}、真实强化 {restoreVersion.current.reinforcement_count} 次及其事件。</p><p>不恢复旧确认日期或核心确认；共享授权不会恢复，已有共享将撤销，需重新明确授权。原聊天与备份不删除。</p></section>{restoreVersionLatest && <div className="space-y-3 rounded-lg border border-amber-500/30 p-3"><p>版本冲突：原 r{restoreVersion.current.revision} → 最新 r{restoreVersionLatest.revision}。目标历史版本未更换。</p><pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify({ key: restoreVersionLatest.key, value: restoreVersionLatest.value, layer: restoreVersionLatest.layer, status: restoreVersionLatest.status, revision: restoreVersionLatest.revision, version_id: restoreVersionLatest.version_id }, null, 2)}</pre><Button size="sm" variant="outline" disabled={busy} onClick={() => { if (syncGuard.current.externalPending) return; setRestoreVersion(current => current ? { ...current, current: restoreVersionLatest, operationId: memoryV2OperationId('restore-version') } : current); setRestoreVersionLatest(undefined); setRestoreVersionAttempted(false); setRestoreVersionAcknowledged(false); setRestoreVersionError('') }}>我已比较，采用最新修订并重新确认</Button></div>}{(restoreVersionError || memoryV2RestoreVersionProblem(restoreVersion.current, restoreVersion.historical)) && <p role="alert" className="text-xs text-destructive">{restoreVersionError || memoryV2RestoreVersionProblem(restoreVersion.current, restoreVersion.historical)}</p>}<label className="flex items-start gap-2 text-xs leading-6"><input type="checkbox" checked={restoreVersionAcknowledged} disabled={busy || externalPending || !writesReady || restoreVersionAttempted || Boolean(restoreVersionLatest)} onChange={event => setRestoreVersionAcknowledged(!syncGuard.current.externalPending && event.target.checked)} />我已核对当前修订、目标历史内容及保留字段，确认以此内容创建新版本；共享须重新授权。</label></div>}<DialogFooter><Button variant="outline" disabled={busy} onClick={cancelRestoreVersion}>取消，不恢复</Button><Button disabled={busy || externalPending || !writesReady || !restoreVersionAcknowledged || restoreVersionAttempted || Boolean(restoreVersionLatest) || Boolean(memoryV2RestoreVersionProblem(restoreVersion?.current, restoreVersion?.historical))} onClick={() => void performRestoreVersion()}>确认创建新内容版本（一次）</Button></DialogFooter></DialogContent></Dialog>
  </section>
}

function newDraft(scope: MemoryV2Scope): MemoryDraft { return { key: '', value: '', category: scope === 'work' ? 'habit' : 'experience', layer: 2, importance: .5, pinned: false, occurred_at: '',tags:'',valence:null,arousal:null } }
function pretty(document: string): string { try { return JSON.stringify(JSON.parse(document), null, 2) } catch { return document } }
function Pager({ offset, total, busy, onPage }: { offset: number; total?: number; busy: boolean; onPage: (value: number) => void }) {
  return <nav className="flex items-center gap-2 text-xs" aria-label="后端分页"><Button type="button" size="sm" variant="outline" disabled={busy || offset === 0} onClick={() => onPage(Math.max(0, offset - MEMORY_V2_PAGE_SIZE))}>上一页</Button><span className="whitespace-nowrap">{Math.floor(offset / MEMORY_V2_PAGE_SIZE) + 1} / {total === undefined ? '—' : Math.max(1, Math.ceil(total / MEMORY_V2_PAGE_SIZE))}</span><Button type="button" size="sm" variant="outline" disabled={busy || total === undefined || offset + MEMORY_V2_PAGE_SIZE >= total} onClick={() => onPage(offset + MEMORY_V2_PAGE_SIZE)}>下一页</Button></nav>
}
function SourceDetails({ item, mode, workSessionId }: { item: MemoryV2Item; mode: 'companion' | 'work'; workSessionId?: number }) {
  const source = item.source
  return <details className="personal-disclosure text-xs"><summary className="cursor-pointer font-medium">来源{source?.known ? ' · 已验证' : ' · 未验证'}</summary><dl className="mt-3 grid gap-2 leading-5"><div><dt className="inline text-muted-foreground">来源人物：</dt><dd className="inline">{item.source_character_id ?? source?.character_id ?? '未记录（不推测）'}</dd></div><div><dt className="inline text-muted-foreground">来源会话：</dt><dd className="inline">{source?.session_id ?? '未记录'}</dd></div><div><dt className="inline text-muted-foreground">真实聊天 ID：</dt><dd className="inline break-all">{source?.message_ids?.join('、') || '未提供（不制造原文）'}</dd></div><div><dt className="inline text-muted-foreground">派生来源 ID：</dt><dd className="inline break-all">{source?.parent_ids?.join('、') || '无'}</dd></div></dl><pre className="mt-3 max-h-44 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-muted/40 p-3 text-[11px]">{JSON.stringify(source || { kind: 'unknown' }, null, 2)}</pre><MemorySourceMessages key={`${item.id}:${item.current_revision}:${item.version_id}:${mode}:${workSessionId ?? 'none'}`} item={item} mode={mode} sessionId={workSessionId} /></details>
}
function RelationDetails({ item, onLocate }: { item: MemoryV2Item; onLocate: (id: string) => void }) {
  if (!item.relations?.length) return null
  return <details className="rounded-lg border border-border p-3 text-xs"><summary className="cursor-pointer font-medium">关联记忆 · {item.relations.length} 条</summary><ul className="mt-3 space-y-2">{item.relations.map(edge => <li key={edge.id} className="flex flex-wrap items-center gap-2"><Badge variant={edge.kind === 'confirmed' && edge.asserted ? 'success' : 'warning'}>{edge.kind === 'confirmed' && edge.asserted ? '证据关系' : '候选相似 · 非事实'}</Badge><span>{edge.label}</span><button className="break-all text-primary hover:underline" type="button" onClick={() => onLocate(edge.from_id === item.id ? edge.to_id : edge.from_id)}>定位关联记忆</button></li>)}</ul></details>
}
function ActivationCurve({ item }: { item: MemoryV2Item }) {
  const curve = memoryV2Curve(item), points = memoryV2ReinforcementPoints(item)
  const start = curve[0]?.at, end = curve.at(-1)?.at
  const line = curve.map((point, index) => `${index / 60 * 320 + 30},${110 - point.activation * 84}`).join(' ')
  return <section className="rounded-xl border border-border bg-muted/15 p-3" aria-label="真实参数衰减曲线"><div className="flex flex-wrap items-center justify-between gap-2"><h4 className="text-xs font-medium">活跃度变化</h4><span className="text-[11px] text-muted-foreground">{item.half_life_days === null ? 'L3 / L4 不按时间衰减' : `半衰期 ${item.half_life_days} 天`}</span></div>{curve.length ? <svg className="mt-2 h-36 w-full text-primary" viewBox="0 0 380 140" role="img" aria-label={`从最近真实起点计算的理论曲线；后端当前真实活跃度 ${Math.round(item.activation * 100)}%，${item.reinforcement_count} 次真实强化。不是历史测量数据。`}><path d="M30 20V110H350" fill="none" stroke="currentColor" strokeOpacity=".2" /><text x="2" y="30" fontSize="9" fill="currentColor">100%</text><text x="10" y="112" fontSize="9" fill="currentColor">0%</text><polyline points={line} fill="none" stroke="currentColor" strokeWidth="2" /><text x="30" y="128" fontSize="9" fill="currentColor">真实最近起点</text><text x="275" y="128" fontSize="9" fill="currentColor">参数模型（非实测）</text>{points.filter(point => start !== undefined && end !== undefined && Date.parse(point.at) >= start && Date.parse(point.at) <= end).map(point => <circle key={point.id} cx={30 + (Date.parse(point.at) - start!) / Math.max(1, end! - start!) * 320} cy="26" r="4" fill="currentColor"><title>{point.action} · {date(point.at)}（真实强化事件）</title></circle>)}</svg> : <p className="py-3 text-xs text-muted-foreground">缺少真实起始时间，不绘制假曲线。</p>}<p className="text-[11px] leading-5 text-muted-foreground">起点：{date(item.reinforced_at || item.confirmed_at || item.created_at)}。曲线为推算值，非历史测量。活跃度下降不会删除记忆。</p><ul className="mt-2 space-y-1 text-[11px]">{points.map(point => <li key={point.id}>● {date(point.at)} · {point.action === 'user_confirm' ? '用户主动确认' : point.action === 'user_remention' ? '用户主动重提' : point.action}</li>)}</ul>{!item.reinforcement_events && item.reinforcement_count > 0 && <p className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">后端未提供完整事件序列，只标记真实最后强化时间，不按次数补造历史点。</p>}{item.reinforcement_count === 0 && <p className="mt-1 text-[11px] text-muted-foreground">暂无用户主动强化事件。</p>}</section>
}
function ConflictComparison({ original, latest, onAccept }: { original?: MemoryV2Item; latest: MemoryV2Item; onAccept: () => void }) {
  return <div className="space-y-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3"><p className="text-sm font-medium">版本冲突 · 原 r{original?.revision} → 最新 r{latest.revision}</p><p className="text-xs leading-5">你的编辑 / 确认输入仍保留；下面是重新 GET 的当前内容。不会自动覆盖、重试授权或沿用旧忘记 token。</p><pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify({ key: latest.key, value: latest.value, layer: latest.layer, status: latest.status, revision: latest.revision, source: latest.source }, null, 2)}</pre><Button type="button" variant="outline" size="sm" onClick={onAccept}>已比较，采用最新修订（保留输入，重新确认）</Button></div>
}
const actionTitles: Record<ItemAction, string> = { delete: '删除记忆', restore: '恢复记忆', share: '共享记忆', core: '确认核心记忆', reinforce: '再次确认记忆', forget: '彻底忘记' }
const actionNotes: Record<ItemAction, string> = { delete: '删除后可恢复，共享授权会撤销；原聊天和备份保留。', restore: '恢复后需重新授权共享；失效来源仍无法用于对话。', share: '授权后可跨模式使用；内容变更或恢复后需重新授权。', core: '核心记忆由你确认，置顶不改变层级。', reinforce: '确认后提高活跃度，浏览不会触发。', forget: '不可恢复，请先核对影响范围再确认。' }
export default MemoryV2Panel



