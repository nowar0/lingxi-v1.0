import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, LayoutPanelTop, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input, Textarea } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { createPluginPackagesClient, pluginErrorMessage, PLUGIN_PYTHON_RISK } from '@/lib/pluginPackages'
import { createPackageUiClient, packageUiApprovalProblem } from '@/lib/packageUi'
import type { PackageUiAction, PackageUiSession, PackageUiValue } from '@/lib/packageUi'
import type { PluginPreview } from '@/lib/pluginPackages'

export type PackageUiPanelProps = { pluginId:string; revision:number; digest:string; installed:boolean; enabled:boolean; configOnly?:boolean; title?:string; description?:string; disabled?:boolean; apiBase?:string; onChanged?:()=>void }
type Pending = {preview:PluginPreview;action:PackageUiAction;session:PackageUiSession}

/** Render only the host's bounded field vocabulary. No plugin HTML/JS is loaded. */
export function PackageUiPanel({pluginId,revision,digest,installed,enabled,configOnly=false,title,description,disabled=false,apiBase='/v1/plugin-packages',onChanged}:PackageUiPanelProps) {
  const client=useMemo(()=>createPackageUiClient({apiBase}),[apiBase])
  const packages=useMemo(()=>createPluginPackagesClient({apiBase}),[apiBase])
  const [session,setSession]=useState<PackageUiSession>()
  const [values,setValues]=useState<Record<string,PackageUiValue>>({})
  const [busy,setBusy]=useState(false)
  const [error,setError]=useState('')
  const [message,setMessage]=useState('')
  const [pending,setPending]=useState<Pending>()
  const [ack,setAck]=useState(false)
  const [attempted,setAttempted]=useState(false)
  const [result,setResult]=useState<unknown>()
  const [now,setNow]=useState(()=>Date.now()/1000)
  const epoch=useRef(0);const lock=useRef<number|null>(null);const alive=useRef(true)
  const available=installed&&(enabled||configOnly)&&!disabled
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;epoch.current++;client.invalidate()}},[client])
  useEffect(()=>{epoch.current++;lock.current=null;client.invalidate();setSession(undefined);setValues({});setPending(undefined);setAck(false);setAttempted(false);setResult(undefined);setError('');setMessage('');setBusy(false)},[client,pluginId,revision,digest,installed,enabled,disabled,configOnly])
  useEffect(()=>{if(!session)return;const timer=window.setInterval(()=>setNow(Date.now()/1000),1000);return()=>window.clearInterval(timer)},[session])
  useEffect(()=>{if(session&&session.expires_at<=now){client.invalidate();setSession(undefined);setPending(undefined);setValues({});setMessage('界面会话已过期，请显式重新打开；没有自动重试。')}},[client,now,session])
  const run=async(action:(generation:number)=>Promise<void>)=>{
    if(lock.current!==null||!available)return
    const generation=epoch.current;lock.current=generation;setBusy(true);setError('')
    try{await action(generation)}catch(cause){if(alive.current&&generation===epoch.current){client.invalidate();setSession(undefined);setPending(undefined);setValues({});setError(pluginErrorMessage(cause,'插件声明式界面操作未完成，结果未知时不会重试'))}}
    finally{if(lock.current===generation){lock.current=null;if(alive.current&&generation===epoch.current)setBusy(false)}}
  }
  const current=(generation:number)=>alive.current&&generation===epoch.current
  const open=()=>void run(async generation=>{
    setPending(undefined);setAck(false);setAttempted(false);setResult(undefined);setMessage('')
    const opened=await client.open({id:pluginId,revision,digest})
    const loaded=await client.readValues()
    if(current(generation)){setSession(opened);setValues(loaded)}
  })
  const preview=(action:PackageUiAction)=>void run(async generation=>{
    setPending(undefined);setAck(false);setAttempted(false);setResult(undefined);setMessage('')
    const value=await client.previewAction(action.id,values)
    if(current(generation))setPending(value)
  })
  const approve=()=>void run(async generation=>{
    if(!pending||!ack||attempted)return
    const value=pending
    const problem=packageUiApprovalProblem(value.preview,value.session,value.action)
    if(problem)throw new Error(problem)
    // UI state and the shared client both consume before sending. Failures and
    // lost results must never resend this token or keep a reusable RPC session.
    setAttempted(true);setPending(undefined);client.invalidate();setSession(undefined);setValues({})
    const output=value.action.kind==='tool'?await packages.execute(value.preview):await packages.commit(value.preview)
    if(current(generation)){setResult(output);setMessage('本次独立批准已提交；会话已关闭。保存会改变修订，需重新打开界面，不自动重试。');onChanged?.()}
  })
  const problem=pending?packageUiApprovalProblem(pending.preview,pending.session,pending.action):''
  return <section className="space-y-3 rounded-xl border border-border/60 bg-card/40 p-3 text-xs">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h4 className="flex items-center gap-2 font-medium"><LayoutPanelTop className="size-4"/>{title||'声明式插件界面'}</h4>{description&&<p className="mt-1 max-w-2xl whitespace-pre-wrap leading-5 text-muted-foreground">{description}</p>}</div><Badge variant="outline">宿主字段 · 无插件脚本</Badge></div>
    <p className="leading-6 text-muted-foreground">只渲染已校验的文字、数值、布尔和选项。界面 RPC 仅可读取声明值或生成预览；不能执行插件 HTML / JavaScript、直接读取文件 / Key 或连接网络。工具执行仍有本机代码风险。</p>
    {!available&&<p className="text-amber-700 dark:text-amber-300">所属插件未安装、未启用或宿主不可用；没有打开会话。</p>}
    <div className="flex flex-wrap items-center gap-2"><Button size="sm" variant="outline" disabled={!available||busy} onClick={open}><RefreshCw className={busy?'animate-spin':''}/>{session?'关闭旧会话并重新打开':'打开声明式界面'}</Button>{session&&<span className="text-muted-foreground">修订 {session.binding.revision} · 有效至 {new Date(session.expires_at*1000).toLocaleTimeString('zh-CN')}</span>}</div>
    {error&&<p role="alert" className="whitespace-pre-wrap leading-6 text-destructive">{error}</p>}
    {message&&<p role="status" className="leading-6 text-muted-foreground">{message}</p>}
    {session&&<div className="space-y-3 rounded-lg border border-border/50 p-3"><div><h5 className="font-medium">{session.schema.title}</h5>{session.schema.description&&<p className="mt-1 whitespace-pre-wrap text-muted-foreground">{session.schema.description}</p>}</div><div className="grid gap-3 md:grid-cols-2">{session.schema.fields.map(field=><label key={field.id} className={`block space-y-1.5 ${field.type==='textarea'||field.type==='readonly'?'md:col-span-2':''}`}><span>{field.label}{field.required&&field.type!=='readonly'?' *':''}</span>{field.type==='readonly'?<output className="block max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-secondary/40 p-2 text-muted-foreground">{String(values[field.id]??'')}</output>:field.type==='boolean'?<span className="flex items-center gap-2"><input type="checkbox" aria-label={field.label} checked={values[field.id]===true} disabled={busy} onChange={event=>{setPending(undefined);setValues(current=>({...current,[field.id]:event.target.checked}))}}/>启用该选项</span>:field.type==='select'?<select className="h-9 w-full rounded-lg border border-border bg-input px-2 text-sm" aria-label={field.label} disabled={busy} value={String(values[field.id]??'')} onChange={event=>{setPending(undefined);setValues(current=>({...current,[field.id]:event.target.value===''?null:event.target.value}))}}>{(!field.required||values[field.id]===null)&&<option value="">未选择（空值）</option>}{field.options?.map(option=><option key={option} value={option}>{option}</option>)}</select>:field.type==='textarea'?<Textarea aria-label={field.label} value={String(values[field.id]??'')} maxLength={field.max_length??4000} disabled={busy} autoComplete="off" onChange={event=>{setPending(undefined);setValues(current=>({...current,[field.id]:event.target.value}))}}/>:<Input aria-label={field.label} type={field.type==='number'?'number':'text'} value={values[field.id]===null?'':String(values[field.id]??'')} min={field.min} max={field.max} maxLength={field.max_length??4000} disabled={busy} autoComplete="off" onChange={event=>{const value=field.type==='number'?(event.target.value===''?null:Number(event.target.value)):event.target.value;setPending(undefined);setValues(current=>({...current,[field.id]:value}))}}/>}{field.source&&<small className="block text-[10px] text-muted-foreground">声明来源：{field.source}（不是任意路径）</small>}</label>)}</div><div className="flex flex-wrap gap-2 border-t border-border/50 pt-3">{session.schema.actions.map(action=><Button key={action.id} size="sm" variant="outline" disabled={busy||!available||session.expires_at<=now} onClick={()=>preview(action)}>{action.label} · 预览</Button>)}</div></div>}
    {result!==undefined&&<details className="rounded-lg border border-border/50 p-2"><summary className="cursor-pointer">本次宿主结果（未自动发送给模型）</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(result,null,2)}</pre></details>}
    <Dialog open={Boolean(pending)} onOpenChange={value=>{if(!value&&!busy){setPending(undefined);setAck(false)}}}><DialogContent className="max-h-[85vh] max-w-2xl overflow-auto"><DialogHeader><DialogTitle>独立确认本次声明式界面动作</DialogTitle><DialogDescription>会话 RPC 只生成预览；以下完整宿主快照才是本次确认内容。失败或结果未知不会自动重试。</DialogDescription></DialogHeader>{pending&&<div className="space-y-3 text-xs leading-6"><p className="flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-500/5 p-3 text-amber-800 dark:text-amber-300"><AlertTriangle className="mt-1 size-4 shrink-0"/>{PLUGIN_PYTHON_RISK}</p><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-secondary/40 p-3">{JSON.stringify(pending.preview.snapshot,null,2)}</pre>{problem&&<p role="alert" className="text-destructive">{problem}</p>}<label className="flex items-start gap-2"><input type="checkbox" checked={ack} disabled={busy} onChange={event=>setAck(event.target.checked)}/><span>我已核对完整参数、权限和具体目标，只批准这一次操作。</span></label></div>}<DialogFooter><Button variant="outline" disabled={busy} onClick={()=>{setPending(undefined);setAck(false)}}>不提交</Button><Button disabled={busy||!pending||!ack||attempted||Boolean(problem)||!available} onClick={approve}>批准本次快照（一次性）</Button></DialogFooter></DialogContent></Dialog>
  </section>
}
