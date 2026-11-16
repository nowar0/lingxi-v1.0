import {useNavigate} from 'react-router-dom'
import {Dialog,DialogContent,DialogHeader,DialogTitle,DialogDescription,DialogFooter} from '@/components/ui/dialog'
import {api,json} from '@/api/client'
import { useRef, useState } from 'react'
import * as Menu from '@radix-ui/react-dropdown-menu'
import { Archive, Check, MessageCircle, Pencil, Pin, Plus } from 'lucide-react'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
import type { Session } from '@/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import './conversation-menu.css'

export function ConversationMenu() {
  const { sessionHasMore,loadMoreSessions,sessions, activeSessionId, setActiveSessionId, createSession, renameSession, archiveSession, connection,refreshSessions } = useApp()
  const navigate=useNavigate()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<Session>(),[deleting,setDeleting]=useState<Session>()
  const [name, setName] = useState('')
  const nameInput = useRef<HTMLInputElement>(null)
  const [pinned, setPinned] = useState<number[]>(() => {
    try { const value = JSON.parse(localStorage.getItem('lingxi-pinned-companion') || '[]'); return Array.isArray(value) ? value.filter((id) => typeof id === 'number') : [] } catch { return [] }
  })
  const current = sessions.find((session) => session.id === activeSessionId)
  const ordered = [...sessions].sort((a, b) => Number(pinned.includes(b.id)) - Number(pinned.includes(a.id)))
  const connected = connection === 'connected'
  const perform = async (task: () => Promise<unknown>) => {
    if (busy) return
    setBusy(true)
    try { await task() } catch (error) { toast.error('对话操作未完成', { description: (error as Error).message }) }
    finally { setBusy(false) }
  }
  const togglePin = () => {
    if (!current) return
    const next = pinned.includes(current.id) ? pinned.filter((id) => id !== current.id) : [...pinned, current.id]
    setPinned(next)
    try { localStorage.setItem('lingxi-pinned-companion', JSON.stringify(next)) } catch { toast.error('置顶偏好未能保存') }
  }
  const saveName = async () => {
    if (!editing || !name.trim()) return
    await perform(async () => { await renameSession(editing, name.trim()); setEditing(undefined); setOpen(false) })
  }
  return <><DropdownMenu modal={false} open={open} onOpenChange={(value) => { setOpen(value); if (!value) setEditing(undefined) }}>
    <DropdownMenuTrigger asChild><Button variant="ghost" size="icon" className="size-8 shrink-0" aria-label="切换对话" title="切换对话" disabled={busy}><MessageCircle /></Button></DropdownMenuTrigger>
    <DropdownMenuContent align="start" side="bottom" sideOffset={8} collisionPadding={12} className="companion-conversation-menu" aria-label="切换对话菜单">
      <DropdownMenuItem disabled={!connected || busy} onSelect={() => void perform(() => createSession('companion'))}><Plus />新建对话</DropdownMenuItem>
      <DropdownMenuItem onSelect={()=>navigate('/settings?tab=sessions')}>管理会话与归档</DropdownMenuItem><DropdownMenuSeparator />
      <div className="companion-conversation-options"><Menu.RadioGroup value={String(activeSessionId || '')} onValueChange={(id) => setActiveSessionId(Number(id))} aria-label="对话列表">{ordered.map((session) => <Menu.RadioItem key={session.id} value={String(session.id)} disabled={busy} className="companion-conversation-option"><span className="companion-conversation-check"><Menu.ItemIndicator><Check /></Menu.ItemIndicator></span><span className="companion-conversation-label" title={session.title}>{session.title}</span>{pinned.includes(session.id) && <Pin className="companion-conversation-pin" aria-label="已置顶" />}</Menu.RadioItem>)}</Menu.RadioGroup>{sessionHasMore.companion&&<DropdownMenuItem onSelect={event=>{event.preventDefault();void perform(()=>loadMoreSessions('companion'))}}>加载更多会话</DropdownMenuItem>}{!ordered.length && <p className="companion-conversation-empty">还没有对话</p>}</div>
      {current && <><DropdownMenuSeparator />{editing ? <form className="companion-conversation-rename" onSubmit={(event) => { event.preventDefault(); void saveName() }} onKeyDown={(event) => { event.stopPropagation(); if (event.key === 'Escape') { event.preventDefault(); setEditing(undefined) } }}><label htmlFor="conversation-menu-name">对话名称</label><Input id="conversation-menu-name" ref={nameInput} maxLength={80} value={name} disabled={busy} onChange={(event) => setName(event.target.value)} /><div><Button type="button" variant="ghost" size="xs" disabled={busy} onClick={() => setEditing(undefined)}>取消</Button><Button size="xs" disabled={busy || !name.trim()}>{busy ? '保存中…' : '保存名称'}</Button></div></form> : <>
        <DropdownMenuItem disabled={!connected || busy} onSelect={(event) => { event.preventDefault(); setEditing(current); setName(current.title); requestAnimationFrame(() => { nameInput.current?.focus(); nameInput.current?.select() }) }}><Pencil />重命名当前对话</DropdownMenuItem>
        <DropdownMenuItem onSelect={togglePin}><Pin />{pinned.includes(current.id) ? '取消置顶当前对话' : '置顶当前对话'}</DropdownMenuItem>
        <DropdownMenuItem disabled={!connected || busy} onSelect={() => void perform(() => archiveSession(current))}><Archive />归档当前对话</DropdownMenuItem><DropdownMenuItem disabled={!connected||busy} onSelect={()=>setDeleting(current)}>永久删除当前对话…</DropdownMenuItem>
      </>}</>}
    </DropdownMenuContent>
  </DropdownMenu><Dialog open={!!deleting} onOpenChange={v=>{if(!v&&!busy)setDeleting(undefined)}}><DialogContent><DialogHeader><DialogTitle>永久删除“{deleting?.title}”？</DialogTitle><DialogDescription>不能撤销。若仍关联任务、附件或记忆来源，会保留并提示处理关联资料。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" disabled={busy} onClick={()=>setDeleting(undefined)}>取消</Button><Button disabled={busy} onClick={()=>void perform(async()=>{await api(`/v6/manage/sessions/${deleting?.id}`,json('POST',{action:'delete',confirmed:true}));setDeleting(undefined);await refreshSessions()})}>永久删除</Button></DialogFooter></DialogContent></Dialog></>
}
