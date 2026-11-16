import { useWorkRailLayout } from './useWorkRailLayout'
import { requestWorkMenu } from './WorkConversationMenu'
import { WorkspaceFolderPicker } from './WorkspaceFolderPicker'
import { useEffect, useState } from 'react'
import { Archive, ChevronDown, ChevronRight, Folder, FolderPlus, Laptop, MoreHorizontal, Pencil, Pin, Search, SquarePen, X } from 'lucide-react'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
import { api, json } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Tooltip } from '@/components/ui/tooltip'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import type { Session } from '@/types'
import './work-session-rail.css'

type Project = { id: string; name: string; path: string; pinned: boolean; revision: number }
type WorkSession = Session & { project_id?: string; project_name: string; project_path: string; rail_revision: number }
type Rail = { next_offset?:number|null;total?:number;projects: Project[]; sessions: WorkSession[] }
function ids(key: string): string[] {
  try { const items: unknown = JSON.parse(localStorage.getItem(key) || '[]'); return Array.isArray(items) ? items.map(String) : [] } catch { return [] }
}
function elapsed(at?: string) {
  if (!at || !Number.isFinite(new Date(at).getTime())) return '未记录时间'
  const n = Math.max(0, Math.floor((Date.now() - new Date(at).getTime()) / 60000))
  return n < 1 ? '刚刚' : n < 60 ? `${n} 分钟前` : n < 1440 ? `${Math.floor(n / 60)} 小时前` : n < 10080 ? `${Math.floor(n / 1440)} 天前` : `${Math.floor(n / 10080)} 周前`
}

export function WorkSessionRail({ visible, onClose, embedded = false, onOpenChange }: { visible: boolean; onClose: () => void; embedded?: boolean; onOpenChange?:(open:boolean)=>void }) {
  const { workSessions, activeWorkSessionId, setActiveWorkSessionId, createSession, renameSession, archiveSession, refreshWorkSessions, connection } = useApp()
  const [data, setData] = useState<Rail>({ projects: [], sessions: [] })
  const [pageSize,setPageSize]=useState(100)
  const [error, setError] = useState(''), [loaded, setLoaded] = useState(false), [revision, setRevision] = useState(0)
  const [searching, setSearching] = useState(false), [query, setQuery] = useState('')
  const [renaming, setRenaming] = useState<WorkSession>(), [name, setName] = useState('')
  const [editing, setEditing] = useState<Project>(), [adding, setAdding] = useState(false), [path, setPath] = useState('')
  const [moving, setMoving] = useState<WorkSession>(), [moveTo, setMoveTo] = useState('')
  const [busy, setBusy] = useState(false), [picking, setPicking] = useState(false), [menu, setMenu] = useState('')
  const [collapsed, setCollapsed] = useState(() => ids('lingxi-work-projects-collapsed'))
  const [legacyPins, setLegacyPins] = useState(() => ids('lingxi-pinned-work'))
  const {rootRef,present,resizing,width,maxWidth,handleProps}=useWorkRailLayout(visible,value=>onOpenChange?onOpenChange(value):!value&&onClose())
  const ready = connection === 'connected'
  useEffect(() => {
    if (!ready || !visible) return
    const c = new AbortController(); let reading = false
    const load = async () => {
      if (reading||document.hidden) return
      reading = true
      try { const read=(offset:number)=>api<Rail>(`/v6/work-rail?offset=${offset}&limit=100&q=${encodeURIComponent(query)}`, { signal: AbortSignal.any([c.signal,AbortSignal.timeout(15000)]) });const value=await read(0);while(value.next_offset!=null&&value.next_offset<pageSize&&!c.signal.aborted){const page=await read(value.next_offset);value.sessions.push(...page.sessions.filter(item=>!value.sessions.some(v=>v.id===item.id)));value.next_offset=page.next_offset} if (!c.signal.aborted) { setData(value); setError(''); setLoaded(true); if (workSessions.some(item => value.sessions.some(next => next.id === item.id && next.title !== item.title))) void refreshWorkSessions().catch(() => {}) } }
      catch (e) { if (!c.signal.aborted) setError((e as Error).message) }
      finally { reading = false }
    }
    void load(); const timer = window.setInterval(() => void load(), 15000)
    const changed=()=>void load();window.addEventListener('focus',changed);document.addEventListener('visibilitychange',changed);return () => { c.abort(); window.clearInterval(timer);window.removeEventListener('focus',changed);document.removeEventListener('visibilitychange',changed) }
  }, [ready, visible, revision, workSessions, activeWorkSessionId, refreshWorkSessions,query,pageSize])
  useEffect(()=>{const changed=()=>setRevision(v=>v+1);window.addEventListener('lingxi-work-rail-refresh',changed);return()=>window.removeEventListener('lingxi-work-rail-refresh',changed)},[])
  const more=async()=>{setPageSize(value=>value+100)}
  const run = async (action: () => Promise<void>) => {
    if (busy || !ready) return
    setBusy(true)
    try { await action() } catch (e) { toast.error('操作未完成', { description: (e as Error).message }) }
    finally { setBusy(false); setRevision(v => v + 1) }
  }
  const select = (id: number) => { const item=data.sessions.find(v=>v.id===id); if(item?.unread) void api(`/v6/work-sessions/${id}/read-state`,json('PUT',{unread:false,expected_revision:item.rail_revision})).then(()=>setRevision(v=>v+1)).catch(()=>{}); setActiveWorkSessionId(id); if (window.innerWidth < 1200) onClose() }
  const create = (owner?: Project) => run(async () => {
    if(!owner && activeWorkSessionId){const selected=await api<{path:string}>(`/v6/work-settings/${activeWorkSessionId}`);owner=data.projects.find(v=>v.path===selected.path)}
    if (owner) {
      const item = await api<Session>(`/v6/work-projects/${owner.id}/sessions`, json('POST', { creation_id: crypto.randomUUID() }))
      await refreshWorkSessions(); select(item.id)
    } else { await createSession('work'); if (window.innerWidth < 1200) onClose() }
  })
  const isPinned = (item: WorkSession) => item.pinned || (item.rail_revision === 0 && legacyPins.includes(String(item.id)))
  const pin = (item: WorkSession) => run(async () => {
    await api(`/v6/work-sessions/${item.id}/rail`, json('PUT', { pinned: !isPinned(item), expected_revision: item.rail_revision }))
    const next = legacyPins.filter(id => id !== String(item.id)); setLegacyPins(next)
    try { localStorage.setItem('lingxi-pinned-work', JSON.stringify(next)) } catch { /* Server pin is already saved. */ }
  })
  const changeProject = (owner: Project, patch: { pinned?: boolean; removed?: boolean; name?: string }) => api(`/v6/work-projects/${owner.id}`, json('PUT', { name: owner.name, pinned: owner.pinned, expected_revision: owner.revision, ...patch }))
  const collapse = (id: string) => {
    const next = collapsed.includes(id) ? collapsed.filter(v => v !== id) : [...collapsed, id]; setCollapsed(next)
    try { localStorage.setItem('lingxi-work-projects-collapsed', JSON.stringify(next)) } catch { toast.error('折叠状态未能保存') }
  }
  const items = data.sessions.filter(item => `${item.title} ${item.project_name} ${item.project_path}`.toLowerCase().includes(query.trim().toLowerCase()))
  const rows = (list: WorkSession[]) => list.map(item => <div key={item.id} className={cn('workspace-session', item.id === activeWorkSessionId && 'is-active')} onContextMenu={event => { event.preventDefault(); requestWorkMenu(item.id,event.currentTarget,{x:event.clientX,y:event.clientY}) }}>
    <Tooltip side="right" content={<div className="work-session-preview"><div><strong>{item.title}</strong><Laptop aria-label="本地会话" /><time dateTime={item.updated_at} title={item.updated_at}>{elapsed(item.updated_at)}</time></div><p title={item.project_path || undefined}><Folder />{item.project_name || '未分配工作区'}</p></div>}>
      <button className="workspace-session-select" aria-current={item.id === activeWorkSessionId ? 'page' : undefined} onClick={() => select(item.id)}><span className="workspace-session-name"><span>{item.title}</span></span>{Boolean(item.unread) && <i className="work-session-unread" aria-label={`${item.unread} 条未读`} />}</button>
    </Tooltip>
    <div className="work-session-actions">
      <button className={cn('work-rail-icon', isPinned(item) && 'is-pinned')} aria-label={`${isPinned(item) ? '取消置顶' : '置顶'}：${item.title}`} aria-pressed={!!isPinned(item)} disabled={!ready || busy} onClick={() => void pin(item)}><Pin /></button>
      <button className="work-rail-icon" aria-label={`归档：${item.title}`} disabled={!ready || busy} onClick={() => void run(() => archiveSession(item))}><Archive /></button>
      <button className="work-rail-icon" aria-label={`会话操作：${item.title}`} onClick={event=>requestWorkMenu(item.id,event.currentTarget)}><MoreHorizontal/></button>
    </div>
  </div>)
  const pinned = items.filter(isPinned), recent = items.filter(item => !isPinned(item) && !item.project_id)
  return <>
    {visible && !embedded && <button className="workspace-rail-scrim" aria-label="收起工作会话列表" onClick={onClose} />}
    <aside ref={rootRef} className={cn('workspace-rail work-session-rail',visible&&'is-visible',present&&'is-present',resizing&&'is-resizing',embedded&&'is-embedded')} aria-label="工作会话列表" aria-hidden={!visible} inert={!visible}><div className="work-rail-content">
      <div className="workspace-rail-heading"><h2>工作会话</h2><Tooltip content="搜索工作会话"><button className="work-rail-icon work-rail-search" aria-label="搜索工作会话" aria-expanded={searching} onClick={() => { setSearching(v => !v); setQuery('') }}><Search /></button></Tooltip><button className="work-rail-icon workspace-rail-close" aria-label="收起会话列表" onClick={onClose}><X /></button></div>
      {searching && <label className="workspace-session-search"><Search /><input autoFocus aria-label="搜索会话或工作区" placeholder="搜索会话或工作区" value={query} onChange={event => {setQuery(event.target.value);setData({projects:[],sessions:[]});setPageSize(100)}} onKeyDown={event => { if (event.key === 'Escape') { setSearching(false); setQuery('') } }} /><button aria-label="关闭搜索" onClick={() => { setSearching(false); setQuery('') }}><X /></button></label>}
      <button className="workspace-new-session" disabled={!ready || busy} onClick={() => void create()}><SquarePen />新聊天</button>
      <div className="workspace-session-scroll">
        {!ready && <p className="work-rail-notice">连接本地服务后管理工作区。</p>}
        {error && <p className="work-rail-notice" role="alert">读取失败：{error}<button onClick={() => setRevision(v => v + 1)}>重试</button></p>}
        {ready && !loaded && !error && <p className="work-rail-notice" role="status">正在读取会话…</p>}
        {!!pinned.length && <section><h3>置顶</h3>{rows(pinned)}</section>}
        {!!recent.length && <section><h3>最近会话</h3>{rows(recent)}</section>}
        {data.next_offset!=null&&<Button variant="ghost" size="sm" disabled={busy} onClick={()=>void run(more)}>加载更多会话</Button>}<section className="work-rail-projects"><div className="work-rail-section-heading"><h3>工作区</h3><Tooltip content="添加工作区"><button className="work-rail-icon" aria-label="添加工作区" disabled={!ready || busy} onClick={() => { setPath(''); setAdding(true) }}><FolderPlus /></button></Tooltip></div>
          {data.projects.filter(owner => !query.trim() || owner.name.toLowerCase().includes(query.toLowerCase()) || items.some(item => item.project_id === owner.id)).map(owner => <div key={owner.id} className="work-rail-project">
            <div className="work-project-heading" onContextMenu={event => { event.preventDefault(); setMenu('p' + owner.id) }}><button className="work-project-toggle" aria-label={`${collapsed.includes(owner.id) ? '展开' : '收起'}工作区：${owner.name}`} aria-expanded={!collapsed.includes(owner.id)} onClick={() => collapse(owner.id)}>{collapsed.includes(owner.id) ? <ChevronRight /> : <ChevronDown />}<Folder /><span>{owner.name}</span>{owner.pinned && <Pin className="work-project-pin" />}</button><div className="work-project-actions">
              <DropdownMenu open={menu === 'p' + owner.id} onOpenChange={open => setMenu(open ? 'p' + owner.id : '')}><DropdownMenuTrigger asChild><button className="work-rail-icon" aria-label={`工作区操作：${owner.name}`}><MoreHorizontal /></button></DropdownMenuTrigger><DropdownMenuContent side="right" align="start">
                <DropdownMenuItem disabled={!ready || busy} onSelect={() => void run(async () => { await changeProject(owner, { pinned: !owner.pinned }) })}><Pin />{owner.pinned ? '取消置顶' : '置顶工作区'}</DropdownMenuItem>
                <DropdownMenuItem disabled={!ready || busy} onSelect={() => { setEditing(owner); setName(owner.name) }}><Pencil />编辑名称</DropdownMenuItem>
                <DropdownMenuItem disabled={!ready || busy} onSelect={() => void create(owner)}><SquarePen />新聊天</DropdownMenuItem>
                <DropdownMenuSeparator /><DropdownMenuItem disabled={!ready || busy} onSelect={() => void run(async () => { await changeProject(owner, { removed: true }) })}><X />移除工作区</DropdownMenuItem>
              </DropdownMenuContent></DropdownMenu>
              <button className="work-rail-icon" aria-label={`在 ${owner.name} 中新聊天`} disabled={!ready || busy} onClick={() => void create(owner)}><SquarePen /></button>
            </div></div>
            {(!collapsed.includes(owner.id) || !!query) && <div className="work-project-sessions">{rows(items.filter(item => item.project_id === owner.id && !isPinned(item)))}{!items.some(item => item.project_id === owner.id) && <button className="work-project-empty" disabled={!ready || busy} onClick={() => void create(owner)}>开始新聊天</button>}</div>}
          </div>)}
          {!data.projects.length && loaded && !query && <button className="work-project-empty" disabled={!ready || busy} onClick={() => { setPath(''); setAdding(true) }}>添加文件夹，按工作区整理聊天</button>}
        </section>
        {!!query && !items.length && <p className="work-rail-notice">没有匹配的会话，试试其他关键词。</p>}
      </div>
    </div>{!embedded&&<div role="separator" aria-label="调整工作会话侧栏宽度，向左拖动可收起" aria-orientation="vertical" aria-valuemin={0} aria-valuemax={maxWidth} aria-valuenow={visible?width:0} tabIndex={visible?0:-1} className="work-rail-resize" {...handleProps}/>}
    </aside>
    <Dialog open={adding} onOpenChange={open => { if (!busy && !picking) setAdding(open) }}><DialogContent><DialogHeader><DialogTitle>添加工作区</DialogTitle></DialogHeader><form onSubmit={event => { event.preventDefault(); void run(async () => { await api('/v6/work-projects', json('POST', { path })); setAdding(false) }) }}><label className="work-rail-field">文件夹路径<Input autoFocus aria-label="工作区文件夹路径" placeholder="选择文件夹或填写完整路径" value={path} onChange={event => setPath(event.target.value)} /></label><DialogFooter><Button type="button" variant="outline" disabled={busy} onClick={() => setPicking(true)}><Folder />选择文件夹</Button><Button type="submit" disabled={busy || !path.trim()}>添加</Button></DialogFooter></form></DialogContent></Dialog>
    <WorkspaceFolderPicker open={picking} initialPath={path} onOpenChange={setPicking} onSelect={setPath} />
    <Dialog open={!!renaming || !!editing} onOpenChange={open => { if (!open && !busy) { setRenaming(undefined); setEditing(undefined) } }}><DialogContent><DialogHeader><DialogTitle>{editing ? '编辑工作区名称' : '重命名工作会话'}</DialogTitle></DialogHeader><form onSubmit={event => { event.preventDefault(); void run(async () => { if (editing) await changeProject(editing, { name: name.trim() }); else if (renaming) await renameSession(renaming, name.trim()); setRenaming(undefined); setEditing(undefined) }) }}><Input autoFocus aria-label={editing ? '工作区名称' : '工作会话名称'} maxLength={80} value={name} onChange={event => setName(event.target.value)} /><DialogFooter><Button type="submit" disabled={busy || !name.trim()}>保存名称</Button></DialogFooter></form></DialogContent></Dialog>
    <Dialog open={!!moving} onOpenChange={open => { if (!open && !busy) setMoving(undefined) }}><DialogContent><DialogHeader><DialogTitle>移至工作区</DialogTitle></DialogHeader><form onSubmit={event => { event.preventDefault(); void run(async () => { if (!moving) return; const checks: Promise<boolean>[] = []; window.dispatchEvent(new CustomEvent('lingxi-work-project-request', { detail: { sessionId: moving.id, checks } })); if ((await Promise.all(checks)).some(ok => !ok)) return; const current = await api<{ revision: number }>(`/v6/work-settings/${moving.id}`); await api(`/v6/work-projects/${moveTo}/sessions/${moving.id}`, json('PUT', { expected_revision: current.revision })); setMoving(undefined); window.dispatchEvent(new CustomEvent('lingxi-work-project-changed', { detail: moving.id })) }) }}><label className="work-rail-field">工作区<select aria-label="目标工作区" value={moveTo} onChange={event => setMoveTo(event.target.value)}>{data.projects.map(owner => <option key={owner.id} value={owner.id}>{owner.name}</option>)}</select></label><DialogFooter><Button type="submit" disabled={busy || !moveTo}>移动</Button></DialogFooter></form></DialogContent></Dialog>
  </>
}
