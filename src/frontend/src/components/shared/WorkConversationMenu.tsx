import { WorkScheduleDialog } from './WorkScheduleDialog'
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Archive, Check, ChevronRight, Copy, Download, Eye, FileText, Folder, GitBranch, Globe, ListChecks, Minimize2, Send, Settings2, ShieldCheck, Split, Target, Terminal, MessageSquarePlus, Pencil, Pin, Plus, SquareArrowOutUpRight, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, apiBlob, json } from '@/api/client'
import { useApp } from '@/context/AppContext'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuSub, DropdownMenuSubContent, DropdownMenuSubTrigger, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { Session } from '@/types'
import './work-conversation-menu.css'

type Item = Session & { rail_revision: number; project_id?: string }
type Project = { id: string; name: string }
type Target = { id: number; x: number; y: number; owner: HTMLElement; toolbar?: boolean; sideId?:number }
export function requestWorkMenu(id: number, owner: HTMLElement, position?: {x:number;y:number}, toolbar=false,sideId?:number) {
  const r=owner.getBoundingClientRect()
  window.dispatchEvent(new CustomEvent('lingxi-work-session-menu',{detail:{id,owner,x:position?.x??r.right,y:position?.y??r.bottom,toolbar,sideId}}))
}
export function WorkConversationMenu() {
  const { connection, activeWorkSessionId, setActiveWorkSessionId, renameSession, archiveSession, refreshWorkSessions, createSession }=useApp()
  const [target,setTarget]=useState<Target>(),[open,setOpen]=useState(false),[item,setItem]=useState<Item>(),[projects,setProjects]=useState<Project[]>([])
  const [planning,setPlanning]=useState(false)
  const [error,setError]=useState(''),[busy,setBusy]=useState(false),[rename,setRename]=useState(false),[name,setName]=useState(''),[deleting,setDeleting]=useState(false)
  const operation=useRef<{kind:string;id:number;request:string;version?:number}|undefined>(undefined)
  useEffect(()=>{const show=(event:Event)=>{const v=(event as CustomEvent<Target>).detail;setTarget(v);setItem(undefined);setError('');setOpen(true)};window.addEventListener('lingxi-work-session-menu',show);return()=>window.removeEventListener('lingxi-work-session-menu',show)},[])
  useEffect(()=>{if(!target||!open)return;const c=new AbortController();void Promise.all([api<{sessions:Item[];projects:Project[]}>('/v6/work-rail',{signal:c.signal}),api<Session>(`/v2/sessions/${target.id}`,{signal:c.signal})]).then(([rail,s])=>{if(!c.signal.aborted){setItem(rail.sessions.find(v=>v.id===s.id)||{...s,rail_revision:0});setProjects(rail.projects)}}).catch(e=>{if(!c.signal.aborted)setError((e as Error).message)});return()=>c.abort()},[target,open])
  const refresh=async()=>{await refreshWorkSessions();window.dispatchEvent(new Event('lingxi-work-rail-refresh'))}
  const run=async(action:()=>Promise<void>)=>{if(busy)return;setBusy(true);try{await action();await refresh()}catch(e){toast.error('会话操作未完成',{description:(e as Error).message})}finally{setBusy(false)}}
  const pin=()=>void run(async()=>{if(item)await api(`/v6/work-sessions/${item.id}/rail`,json('PUT',{pinned:!item.pinned,expected_revision:item.rail_revision}))})
  const mark=()=>void run(async()=>{if(item)await api(`/v6/work-sessions/${item.id}/read-state`,json('PUT',{unread:!item.unread,expected_revision:item.rail_revision}))})
  const cloneOperation=async(kind:'side'|'fork',source=item)=>{
    if(!source)return
    if(operation.current?.id!==source.id||operation.current.kind!==kind)operation.current={id:source.id,kind,request:crypto.randomUUID().replaceAll('-','')}
    const view=operation.current.version!==undefined?{session_version:operation.current.version}:await api<{session_version:number}>(`/v3/sessions/${source.id}/view?mode=work`);operation.current.version=view.session_version
    const result=await api<{session:Session}>(kind==='side'?'/v9/workspace/side-chats':`/v6/work-sessions/${source.id}/fork`,json('POST',kind==='side'?{parent_session_id:source.id,request_id:operation.current.request,expected_version:view.session_version,inherit_record:true}:{request_id:operation.current.request,expected_version:view.session_version})).catch(error=>{if(error.status===409)operation.current=undefined;throw error})
    operation.current=undefined;await refresh()
    if(kind==='side')window.dispatchEvent(new CustomEvent('lingxi-work-open-side',{detail:result.session.id}));else setActiveWorkSessionId(result.session.id)
  }
  const clone=(kind:'side'|'fork',source=item)=>void run(()=>cloneOperation(kind,source))
  const move=(id:string)=>void run(async()=>{if(!item)return;const checks:Promise<boolean>[]=[];window.dispatchEvent(new CustomEvent('lingxi-work-project-request',{detail:{sessionId:item.id,checks}}));if((await Promise.all(checks)).some(v=>!v))return;const value=await api<{revision:number}>(`/v6/work-settings/${item.id}`);await api(`/v6/work-projects/${id}/sessions/${item.id}`,json('PUT',{expected_revision:value.revision}));window.dispatchEvent(new CustomEvent('lingxi-work-project-changed',{detail:item.id}))})
  const copy=(kind:'title'|'link'|'all')=>void run(async()=>{if(!item)return;const text=kind==='title'?item.title:kind==='link'?`${location.origin}${location.pathname}#/work?session=${item.id}`:await (await apiBlob(`/v6/sessions/${item.id}/export`)).text();await navigator.clipboard.writeText(text);toast.success('已复制')})
  const composer=(action:string)=>{if(target)window.dispatchEvent(new CustomEvent('lingxi-work-composer-action',{detail:{sessionId:target.sideId||target.id,action}}))}
  const tool=(kind:string)=>window.dispatchEvent(new CustomEvent('lingxi-work-tool',{detail:kind}))
  useEffect(()=>{
    const keys=(event:KeyboardEvent)=>{
      if(!activeWorkSessionId||connection!=='connected'||event.defaultPrevented||event.isComposing||document.querySelector('[role=dialog][data-state=open]'))return
      if(event.target instanceof HTMLElement&&event.target.closest('input,textarea,[contenteditable=true]'))return
      const key=event.key.toLowerCase(),alt=event.ctrlKey&&event.altKey&&!event.shiftKey,shift=event.ctrlKey&&event.shiftKey&&!event.altKey
      if(!(alt&&['r','p','s'].includes(key)||shift&&['u','a'].includes(key)))return
      event.preventDefault()
      void run(async()=>{
        const rail=await api<{sessions:Item[]}>('/v6/work-rail'),value=rail.sessions.find(v=>v.id===activeWorkSessionId)
        if(!value)return
        if(alt&&key==='r'){setItem(value);setName(value.title);setRename(true)}
        else if(alt&&key==='s'){await cloneOperation('side',value)}
        else if(alt&&key==='p')await api(`/v6/work-sessions/${value.id}/rail`,json('PUT',{pinned:!value.pinned,expected_revision:value.rail_revision}))
        else if(shift&&key==='u')await api(`/v6/work-sessions/${value.id}/read-state`,json('PUT',{unread:!value.unread,expected_revision:value.rail_revision}))
        else if(shift&&key==='a')await archiveSession(value)
      })
    }
    window.addEventListener('keydown',keys);return()=>window.removeEventListener('keydown',keys)
  },[activeWorkSessionId,connection,busy])
  const disabled=!item||busy||connection!=='connected'
  return <>{target&&createPortal(<DropdownMenu open={open} modal={false} onOpenChange={setOpen}><DropdownMenuTrigger asChild><button aria-hidden tabIndex={-1} className="work-menu-anchor" style={{left:target.x,top:target.y}} /></DropdownMenuTrigger><DropdownMenuContent className="work-conversation-menu" aria-label="工作会话操作" align={target.toolbar?'end':'start'} sideOffset={3} collisionPadding={8} onCloseAutoFocus={event=>event.preventDefault()} onContextMenu={event=>event.preventDefault()}>
    {error&&<p role="alert">{error}</p>}
    {target.toolbar&&<>
    <DropdownMenuItem disabled={disabled} onSelect={()=>{setName(item!.title);setRename(true)}}><Pencil/><span>重命名</span><kbd>Ctrl+Alt+R</kbd></DropdownMenuItem>
    <DropdownMenuItem disabled={disabled} onSelect={pin}><Pin/><span>{item?.pinned?'取消置顶':'置顶'}</span><kbd>Ctrl+Alt+P</kbd></DropdownMenuItem>
    <DropdownMenuSeparator/>
    <DropdownMenuItem disabled={disabled} onSelect={()=>clone('side')}><MessageSquarePlus/><span>新建侧边聊天</span><kbd>Ctrl+Alt+S</kbd></DropdownMenuItem>
    <DropdownMenuSub><DropdownMenuSubTrigger disabled={disabled}><Split/><span>分叉</span><ChevronRight/></DropdownMenuSubTrigger><DropdownMenuSubContent><DropdownMenuItem onSelect={()=>clone('fork')}><Split/>分叉为新对话</DropdownMenuItem><DropdownMenuItem onSelect={()=>clone('side')}><MessageSquarePlus/>分叉为侧边聊天</DropdownMenuItem></DropdownMenuSubContent></DropdownMenuSub>
    <DropdownMenuItem disabled={disabled} onSelect={()=>setPlanning(true)}><Plus/><span>添加计划任务…</span></DropdownMenuItem>
    {target.sideId&&<DropdownMenuSub><DropdownMenuSubTrigger><MessageSquarePlus/><span>侧聊操作</span><ChevronRight/></DropdownMenuSubTrigger><DropdownMenuSubContent>{[['history','侧聊历史'],['save','保存为普通会话'],['record','编辑侧聊记录'],['return','预览并带回主对话']].map(([action,label])=><DropdownMenuItem key={action} onSelect={()=>window.dispatchEvent(new CustomEvent('lingxi-side-action',{detail:{sessionId:target.sideId,action}}))}>{label}</DropdownMenuItem>)}</DropdownMenuSubContent></DropdownMenuSub>}
    <DropdownMenuSeparator/>
    <DropdownMenuSub><DropdownMenuSubTrigger disabled={disabled}><Settings2/><span>会话工具</span><ChevronRight/></DropdownMenuSubTrigger><DropdownMenuSubContent className="work-session-tools-submenu">
      {([['goal','会话目标',Target],['plan','计划模式',ListChecks],['compact','上下文压缩',Minimize2],['permission','执行权限',ShieldCheck],['model','模型与推理强度',Settings2],['settings','会话设置',Settings2],['context','工作记录',FileText],['docs','项目资料',Folder],['search','网页搜索',Globe],['handoff','转工作',ListChecks],['feedback','反馈',Send],['export','下载日志',Download]] as const).map(([action,label,Icon])=><DropdownMenuItem key={action} onSelect={()=>composer(action)}><Icon/>{label}</DropdownMenuItem>)}
    </DropdownMenuSubContent></DropdownMenuSub>
    <DropdownMenuSub><DropdownMenuSubTrigger disabled={disabled}><Folder/><span>工具</span><ChevronRight/></DropdownMenuSubTrigger><DropdownMenuSubContent>
      <DropdownMenuItem onSelect={()=>tool('files')}><Folder/>项目文件</DropdownMenuItem><DropdownMenuItem onSelect={()=>tool('terminal')}><Terminal/>终端</DropdownMenuItem><DropdownMenuItem onSelect={()=>tool('browser')}><Globe/>浏览器</DropdownMenuItem>
      <DropdownMenuSub><DropdownMenuSubTrigger><GitBranch/><span>Git</span><ChevronRight/></DropdownMenuSubTrigger><DropdownMenuSubContent><DropdownMenuItem onSelect={()=>tool('git')}>分支与工作树</DropdownMenuItem><DropdownMenuItem onSelect={()=>tool('changes')}>更改与提交</DropdownMenuItem></DropdownMenuSubContent></DropdownMenuSub>
      <DropdownMenuSub><DropdownMenuSubTrigger><Folder/><span>工作区</span><ChevronRight/></DropdownMenuSubTrigger><DropdownMenuSubContent>{projects.map(p=><DropdownMenuItem key={p.id} onSelect={()=>move(p.id)}><Folder/>{p.name}{p.id===item?.project_id&&<Check className="ml-auto"/>}</DropdownMenuItem>)}<DropdownMenuItem onSelect={()=>tool('edit-workspace')}>选择工作目录</DropdownMenuItem></DropdownMenuSubContent></DropdownMenuSub>
      <DropdownMenuSub><DropdownMenuSubTrigger><Settings2/><span>更多工具</span><ChevronRight/></DropdownMenuSubTrigger><DropdownMenuSubContent className="work-session-tools-submenu">{[['start','工具首页'],['context','上下文'],['tasks','任务'],['activity','文件活动'],['artifacts','产物与附件'],['interactive-terminal','交互终端'],['workflow','工具流程'],['office','Office 与 OCR'],['ssh','SSH / SFTP'],['review','预算与验收'],['refresh-files','刷新文件目录']].map(([id,label])=><DropdownMenuItem key={id} onSelect={()=>tool(id)}>{label}</DropdownMenuItem>)}</DropdownMenuSubContent></DropdownMenuSub>
    </DropdownMenuSubContent></DropdownMenuSub>
    <DropdownMenuSub><DropdownMenuSubTrigger disabled={disabled}><Copy/><span>复制</span><ChevronRight/></DropdownMenuSubTrigger><DropdownMenuSubContent><DropdownMenuItem onSelect={()=>copy('all')}>复制对话</DropdownMenuItem><DropdownMenuItem onSelect={()=>copy('title')}>复制名称</DropdownMenuItem><DropdownMenuItem onSelect={()=>copy('link')}>复制会话链接</DropdownMenuItem></DropdownMenuSubContent></DropdownMenuSub>
    <DropdownMenuItem disabled={disabled} onSelect={()=>{if(item){const child=window.open(`${location.origin}${location.pathname}#/work?session=${item.id}`,'_blank');if(!child)toast.error('新窗口未能打开，请检查窗口权限')}}}><SquareArrowOutUpRight/><span>在新窗口中打开</span></DropdownMenuItem>
    <DropdownMenuSeparator/><DropdownMenuItem disabled={disabled} onSelect={()=>void run(async()=>{if(item)await archiveSession(item)})}><Archive/><span>归档</span><kbd>Ctrl+Shift+A</kbd></DropdownMenuItem>
    </>}
    {!target.toolbar&&<>
    <DropdownMenuItem disabled={disabled} onSelect={()=>{setName(item!.title);setRename(true)}}><Pencil/><span>重命名</span><kbd>Ctrl+Alt+R</kbd></DropdownMenuItem>
    <DropdownMenuItem disabled={disabled} onSelect={pin}><Pin/><span>{item?.pinned?'取消置顶':'置顶'}</span><kbd>Ctrl+Alt+P</kbd></DropdownMenuItem>
    {!target.toolbar&&<DropdownMenuItem disabled={disabled} onSelect={mark}><Eye/><span>{item?.unread?'标记为已读':'标记为未读'}</span><kbd>Ctrl+Shift+U</kbd></DropdownMenuItem>}
    <DropdownMenuSub><DropdownMenuSubTrigger disabled={disabled}><Folder/><span>工作区</span><ChevronRight/></DropdownMenuSubTrigger><DropdownMenuSubContent>{projects.map(p=><DropdownMenuItem key={p.id} onSelect={()=>move(p.id)}><Folder/>{p.name}{p.id===item?.project_id&&<Check className="ml-auto"/>}</DropdownMenuItem>)}{!projects.length&&<DropdownMenuItem disabled>先添加工作区</DropdownMenuItem>}</DropdownMenuSubContent></DropdownMenuSub>
    <DropdownMenuSeparator/>
    <DropdownMenuSub><DropdownMenuSubTrigger disabled={disabled}><Split/><span>分叉</span><ChevronRight/></DropdownMenuSubTrigger><DropdownMenuSubContent><DropdownMenuItem onSelect={()=>clone('fork')}><Split/>分叉为新对话</DropdownMenuItem><DropdownMenuItem onSelect={()=>clone('side')}><MessageSquarePlus/>分叉为侧边聊天</DropdownMenuItem></DropdownMenuSubContent></DropdownMenuSub>
    <DropdownMenuSeparator/>
    <DropdownMenuSub><DropdownMenuSubTrigger disabled={disabled}><Copy/><span>复制</span><ChevronRight/></DropdownMenuSubTrigger><DropdownMenuSubContent><DropdownMenuItem onSelect={()=>copy('all')}>复制对话</DropdownMenuItem><DropdownMenuItem onSelect={()=>copy('title')}>复制名称</DropdownMenuItem><DropdownMenuItem onSelect={()=>copy('link')}>复制会话链接</DropdownMenuItem></DropdownMenuSubContent></DropdownMenuSub>
    <DropdownMenuSeparator/><DropdownMenuItem disabled={disabled} onSelect={()=>{if(item){const child=window.open(`${location.origin}${location.pathname}#/work?session=${item.id}`,'_blank');if(!child)toast.error('新窗口未能打开，请检查窗口权限')}}}><SquareArrowOutUpRight/><span>在新窗口中打开</span></DropdownMenuItem>
    <DropdownMenuSeparator/><DropdownMenuItem disabled={disabled} onSelect={()=>void run(async()=>{if(item)await archiveSession(item)})}><Archive/><span>归档</span><kbd>Ctrl+Shift+A</kbd></DropdownMenuItem>
    {!target.toolbar&&<DropdownMenuItem disabled={disabled} onSelect={()=>setDeleting(true)}><Trash2/><span>永久删除</span></DropdownMenuItem>}
    </>}
  </DropdownMenuContent></DropdownMenu>,document.body)}
  <WorkScheduleDialog sessionId={item?.id} open={planning} onClose={()=>setPlanning(false)}/>
  <Dialog open={rename} onOpenChange={v=>{if(!busy)setRename(v)}}><DialogContent><DialogHeader><DialogTitle>重命名工作会话</DialogTitle></DialogHeader><form onSubmit={event=>{event.preventDefault();void run(async()=>{if(item)await renameSession(item,name.trim());setRename(false)})}}><Input autoFocus aria-label="工作会话名称" value={name} maxLength={80} onChange={e=>setName(e.target.value)}/><DialogFooter><Button type="submit" disabled={busy||!name.trim()}>保存名称</Button></DialogFooter></form></DialogContent></Dialog>
  <Dialog open={deleting} onOpenChange={v=>{if(!busy)setDeleting(v)}}><DialogContent><DialogHeader><DialogTitle>永久删除“{item?.title}”？</DialogTitle><DialogDescription>将删除此会话及其记录，无法撤销。关联的项目文件不会删除。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" disabled={busy} onClick={()=>setDeleting(false)}>取消</Button><Button disabled={busy} onClick={()=>void run(async()=>{if(!item)return;await api(`/v2/sessions/${item.id}`,json('DELETE'));setDeleting(false);if(activeWorkSessionId===item.id){const rail=await api<{sessions:Session[]}>('/v6/work-rail');if(rail.sessions[0])setActiveWorkSessionId(rail.sessions[0].id);else await createSession('work')}})}>永久删除</Button></DialogFooter></DialogContent></Dialog>
  </>
}
