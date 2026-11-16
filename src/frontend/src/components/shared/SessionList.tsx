import {useNavigate} from 'react-router-dom'
import { useState } from 'react'
import { Archive, MoreHorizontal, Pencil, Pin, Plus } from 'lucide-react'
import { toast } from 'sonner'
import type { Mode, Session } from '@/types'
import { cn } from '@/lib/utils'
import { formatDate } from '@/lib/format'
import { Button } from '@/components/ui/button'
import { SearchInput } from './SearchInput'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { useApp } from '@/context/AppContext'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'

export function SessionList({ mode, sessions, activeId, onSelect, onCreate, title }: { mode: Mode; sessions: Session[]; activeId?: number; onSelect: (id: number) => void; onCreate: () => void | Promise<number>; title: string }) {
  const [query, setQuery] = useState('')
  const { sessionHasMore,loadMoreSessions,renameSession, archiveSession, connection } = useApp()
  const [renaming, setRenaming] = useState<Session>()
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [pinned, setPinned] = useState<number[]>(() => {
    try { return JSON.parse(localStorage.getItem(`lingxi-pinned-${mode}`) || '[]') } catch { return [] }
  })
  const pin = (session: Session) => {
    const next = pinned.includes(session.id) ? pinned.filter((id) => id !== session.id) : [...pinned, session.id]
    setPinned(next)
    try { localStorage.setItem(`lingxi-pinned-${mode}`, JSON.stringify(next)) } catch { toast.error('置顶偏好未能保存') }
  }
  const rename = async () => {
    if (!renaming || busy || !name.trim()) return
    setBusy(true)
    try { await renameSession(renaming, name.trim()); setRenaming(undefined) }
    catch (error) { toast.error('重命名失败', { description: (error as Error).message }) }
    finally { setBusy(false) }
  }
  const filtered = sessions.map((session) => ({ ...session, pinned: pinned.includes(session.id) })).filter((session) => session.title.toLowerCase().includes(query.toLowerCase())).sort((a, b) => Number(b.pinned) - Number(a.pinned))
  return <section className="session-rail">
    <div className="flex h-14 items-center justify-between px-4"><div><h2 className="text-sm font-semibold">{title}</h2><p className="mt-0.5 text-[10px] text-muted-foreground">{sessions.length} 个会话</p></div><Button variant="ghost" size="icon" className="size-8" aria-label="新建会话" disabled={connection !== 'connected'} onClick={() => { try { Promise.resolve(onCreate()).catch((error) => toast.error('创建会话失败', { description: (error as Error).message })) } catch (error) { toast.error((error as Error).message) } }}><Plus /></Button></div>
    <div className="px-3 pb-3"><SearchInput placeholder="搜索会话" value={query} onChange={(event) => setQuery(event.target.value)} className="[&_input]:h-8 [&_input]:bg-secondary/50 [&_input]:text-xs" /></div>
    <div className="flex-1 space-y-1 overflow-y-auto px-2 pb-3">{filtered.map((session) => <div key={session.id} className={cn('group relative flex cursor-pointer items-center rounded-[10px] border border-transparent px-2.5 py-2.5 transition-colors hover:bg-accent/65', activeId === session.id && 'border-primary/10 bg-primary/[0.085]')} onClick={() => onSelect(session.id)}>
      <div className="min-w-0 flex-1"><div className="flex items-center gap-1.5">{session.pinned && <Pin className="size-3 shrink-0 fill-primary/20 text-primary" />}<span className={cn('truncate text-xs', activeId === session.id && 'font-medium text-primary')}>{session.title}</span>{Boolean(session.unread) && <span className="grid size-4 shrink-0 place-items-center rounded-full bg-primary text-[9px] text-white">{session.unread}</span>}</div><div className="mt-1 text-[10px] text-muted-foreground/75">{formatDate(session.updated_at)} · {mode === 'work' ? '独立工作记录' : '同一陪伴记忆'}</div></div>
      <DropdownMenu><DropdownMenuTrigger asChild><Button variant="ghost" size="icon" className="size-6 opacity-0 group-hover:opacity-100" onClick={(event) => event.stopPropagation()}><MoreHorizontal className="size-3.5" /></Button></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem disabled={connection !== 'connected'} onSelect={() => { setRenaming(session); setName(session.title) }}><Pencil />重命名</DropdownMenuItem><DropdownMenuItem onSelect={() => pin(session)}><Pin />{session.pinned ? '取消置顶' : '置顶'}</DropdownMenuItem><DropdownMenuItem disabled={connection !== 'connected'} onSelect={() => void archiveSession(session).catch((error) => toast.error('归档失败', { description: (error as Error).message }))}><Archive />归档</DropdownMenuItem></DropdownMenuContent></DropdownMenu>
    </div>)}{sessionHasMore[mode]&&<Button variant="ghost" size="sm" onClick={()=>void loadMoreSessions(mode).catch(error=>toast.error('读取失败',{description:error.message}))}>加载更多会话</Button>}</div>
    <Dialog open={Boolean(renaming)} onOpenChange={(open) => { if (!open && !busy) setRenaming(undefined) }}><DialogContent><DialogHeader><DialogTitle>重命名会话</DialogTitle></DialogHeader><Input aria-label="会话名称" maxLength={80} value={name} onChange={(event) => setName(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void rename() }} /><DialogFooter><Button disabled={busy || !name.trim()} onClick={() => void rename()}>保存名称</Button></DialogFooter></DialogContent></Dialog>
  </section>
}
