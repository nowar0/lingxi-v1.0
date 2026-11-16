import { useEffect,useRef,useState } from 'react'
import { AlertCircle,Check,ChevronDown,Shield,ShieldCheck,ShieldAlert } from 'lucide-react'
import { api,json } from '@/api/client'
import { useApp } from '@/context/AppContext'
import type { Mode } from '@/types'
import { Button } from '@/components/ui/button'
import { Dialog,DialogContent,DialogDescription,DialogFooter,DialogHeader,DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu,DropdownMenuContent,DropdownMenuItem,DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import './permission-picker.css'
type Permission='ask'|'readonly'|'workspace'|'full'
type Controls={permission:Permission;revision:number;[key:string]:unknown}
const labels:Record<Permission,string>={ask:'逐次审批',readonly:'仅可查看',workspace:'工作区内修改',full:'完全权限'}
export function PermissionPicker({mode='work',inline=false,compact=false}:{mode?:Mode;inline?:boolean;compact?:boolean}){
 const app=useApp(),sid=mode==='work'?app.activeWorkSessionId:app.activeSessionId,connected=app.connection==='connected'
 const [controls,setControls]=useState<Controls>(),[error,setError]=useState(''),[open,setOpen]=useState(false),[confirm,setConfirm]=useState(false),[accepted,setAccepted]=useState(false),[busy,setBusy]=useState(false)
 const scope=useRef(0)
 useEffect(()=>{scope.current++;setControls(undefined);setConfirm(false);setAccepted(false);setBusy(false);setOpen(false);return()=>{scope.current++}},[sid,connected])
 useEffect(()=>{if(!connected||!sid)return;const c=new AbortController();const read=()=>void api<Controls>(`/v6/sessions/${sid}/composer`,{signal:c.signal}).then(value=>{if(!c.signal.aborted){setControls(value);setError('')}}).catch(e=>{if(!c.signal.aborted)setError((e as Error).message)});read();window.addEventListener('lingxi-options-changed',read);return()=>{c.abort();window.removeEventListener('lingxi-options-changed',read)}},[sid,connected,open])
 const locked=busy||!connected||!sid||!controls||app.generating[mode]||app.sending[mode]
 const save=async(permission:Permission,ack=false)=>{if(locked||!sid||!controls)return;const current=scope.current;setBusy(true);setError('');try{const {revision,...value}=controls;const saved=await api<Controls>(`/v6/sessions/${sid}/composer`,json('PUT',{...value,permission,full_access_confirmed:ack,expected_revision:revision}));if(current!==scope.current)return;setControls(saved);setConfirm(false);setOpen(false);window.dispatchEvent(new Event('lingxi-options-changed'))}catch(e){if(current===scope.current)setError((e as Error).message)}finally{if(current===scope.current)setBusy(false)}}
 const options=mode==='work'?(['readonly','workspace','full'] as const):(['readonly','ask'] as const)
 const list=options.map(value=>{const Icon=value==='readonly'?ShieldCheck:value==='full'?ShieldAlert:Shield;return <DropdownMenuItem key={value} disabled={locked} onSelect={event=>{if(value==='full'&&controls?.permission!=='full'){setAccepted(false);setConfirm(true);setOpen(false)}else void save(value)}}><Icon/><span>{labels[value]}</span>{controls?.permission===value&&<Check className="ml-auto"/>}</DropdownMenuItem>})
 return <><DropdownMenu open={open} onOpenChange={setOpen}><DropdownMenuTrigger asChild><button type="button" className={inline?'permission-inline-trigger':`work-composer-permission${compact?' is-compact':''}`} aria-label="选择执行权限" title={labels[controls?.permission||'workspace']} disabled={!connected||!sid}><ShieldCheck/><span>{labels[controls?.permission||'workspace']}</span><ChevronDown/></button></DropdownMenuTrigger><DropdownMenuContent className="permission-options" align="start" side="top">{list}<p>{controls?.permission==='readonly'?'仅查看，不修改文件。':controls?.permission==='full'?'已启用完全权限。':'可修改工作区文件。'}</p>{error&&<p role="alert">{error}</p>}</DropdownMenuContent></DropdownMenu>
 <Dialog open={confirm} onOpenChange={value=>{if(!busy){setConfirm(value);if(!value)setAccepted(false)}}}><DialogContent className="full-permission-dialog"><DialogHeader><DialogTitle>确认启用完全权限？</DialogTitle><DialogDescription>仅在你信任当前任务时使用。</DialogDescription></DialogHeader><div className="full-permission-warning"><AlertCircle/><p>启用完全权限后，智能体将减少确认步骤，并且可以直接执行更多操作，包括文件修改、外部命令及其他已登记的写入工具。外部命令可能影响工作区以外的文件。</p></div><label className="full-permission-accept"><input type="checkbox" checked={accepted} onChange={e=>setAccepted(e.target.checked)} disabled={busy}/>我已了解风险，并愿意继续</label>{error&&<p role="alert">{error}</p>}<DialogFooter><Button variant="outline" disabled={busy} onClick={()=>{setConfirm(false);setAccepted(false)}}>取消</Button><Button disabled={!accepted||locked} onClick={()=>void save('full',true)}>{busy?'正在启用…':'启用完全权限'}</Button></DialogFooter></DialogContent></Dialog></>
}
