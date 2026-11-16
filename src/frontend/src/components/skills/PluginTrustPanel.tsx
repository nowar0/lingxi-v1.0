import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, RefreshCw, ShieldCheck } from 'lucide-react'
import { useApp } from '@/context/AppContext'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { createPluginTrustClient, trustOperationProblem } from '@/lib/pluginTrust'
import type { TrustKey, TrustListing, TrustPreview } from '@/lib/pluginTrust'

/** Explicit public trust roots. Package listing failures do not hide this recovery tab. */
export function PluginTrustPanel(){
 const {connection}=useApp();const connected=connection==='connected'
 const client=useMemo(()=>createPluginTrustClient(),[])
 const [listing,setListing]=useState<TrustListing>();const [busy,setBusy]=useState(false)
 const [error,setError]=useState('');const [notice,setNotice]=useState('');const [stale,setStale]=useState(true)
 const [input,setInput]=useState({key_id:'',public_key:'',display_name:''})
 const [preview,setPreview]=useState<TrustPreview>();const [ack,setAck]=useState(false)
 const [attempted,setAttempted]=useState(false);const [now,setNow]=useState(Date.now)
 const epoch=useRef(0),lock=useRef<number|null>(null),abort=useRef<AbortController|undefined>(undefined)
 const clearPreview=()=>{setPreview(undefined);setAck(false);setAttempted(false)}
 const refresh=useCallback(async()=>{
  abort.current?.abort();client.invalidate();const generation=++epoch.current
  const controller=new AbortController();abort.current=controller;lock.current=generation
  setBusy(true);setError('');setStale(true);setPreview(undefined);setAck(false);setAttempted(false)
  try{const value=await client.list(connection,controller.signal);if(generation===epoch.current&&!controller.signal.aborted&&value){setListing(value);setStale(false)}}
  catch(cause){if(generation===epoch.current&&!controller.signal.aborted)setError(cause instanceof Error?cause.message:'公钥读取失败')}
  finally{if(lock.current===generation){lock.current=null;setBusy(false)}}
 },[client,connection])
 useEffect(()=>{void refresh();return()=>{epoch.current++;lock.current=null;abort.current?.abort();client.invalidate()}},[refresh,client])
 useEffect(()=>{if(!preview)return;setNow(Date.now());const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer)},[preview])
 const run=async(task:(signal:AbortSignal,generation:number)=>Promise<void>)=>{
  if(!connected||lock.current!==null)return
  const generation=epoch.current;const controller=new AbortController();abort.current=controller;lock.current=generation
  setBusy(true);setError('');setNotice('')
  try{await task(controller.signal,generation)}catch(cause){if(generation===epoch.current&&!controller.signal.aborted){setError(cause instanceof Error?cause.message:'操作未完成，结果未知不会自动重试');setStale(true)}}
  finally{if(lock.current===generation){lock.current=null;setBusy(false)}}
 }
 const generate=(kind:'add'|'revoke'|'trust',key?:TrustKey)=>void run(async(signal,generation)=>{
  if(!listing||stale)throw Error('请先刷新当前可信公钥库')
  clearPreview()
  const value=kind==='add'?await client.add(listing,input,signal):kind==='revoke'?await client.revoke(listing,key!,signal):await client.retrust(listing,key!,signal)
  if(generation===epoch.current&&!signal.aborted){setPreview(value);setNow(Date.now())}
 })
 const commit=()=>void run(async(signal,generation)=>{
  if(!preview||!ack||attempted||stale)return
  const selected=preview;setAttempted(true);setPreview(undefined);setAck(false)
  await client.commit(selected,true,signal)
  if(generation!==epoch.current||signal.aborted)return
  setStale(true)
  const value=await client.list(connection,signal)
  if(generation===epoch.current&&!signal.aborted&&value){setListing(value);setStale(false);setInput({key_id:'',public_key:'',display_name:''});setNotice('公钥操作已明确提交并重新读取。没有自动启用或执行插件；旧审批需重新预览。')}
 })
 const problem=preview?trustOperationProblem(preview,now):''
 const disabled=!connected||busy||stale||!listing
 return <Card className="space-y-4 border border-border/60 p-4" aria-busy={busy}>
  <header className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="flex items-center gap-2 text-sm font-semibold"><ShieldCheck className="size-4"/>可信发布者公钥</h2><p className="mt-1 text-xs leading-6 text-muted-foreground">只管理32字节 Ed25519 公钥；不会生成密钥、接收私钥或自动信任发布者。</p></div><Button size="sm" variant="outline" disabled={!connected||busy} onClick={()=>void refresh()}><RefreshCw className="size-3.5"/>只读刷新</Button></header>
  <p className="flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-500/5 p-3 text-xs leading-6"><AlertTriangle className="mt-1 size-4 shrink-0"/>签名只验证来源与内容完整性，不证明代码安全，也不代替 OS 隔离或执行批准。未签名/未知签名不冒称可信；本地包仍默认停用并逐次授权。</p>
  <p className="text-xs leading-6 text-muted-foreground">公钥信任库不随用户备份自动迁移。跨电脑必须重新审阅并显式信任。撤销会使旧审批与会话失效，不保证瞬间终止已执行任务，也不撤销已经发生的外部副作用。</p>
  {!connected&&<p role="status" className="text-xs text-muted-foreground">{connection==='demo'?'演示模式：不读取或更改真实信任库。':'本地服务未连接，不查询或提交公钥。'}{listing&&'下方仅为上次结果。'}</p>}
  {busy&&<p role="status" className="text-xs text-muted-foreground">正在处理…没有自动批准或重试。</p>}
  {error&&<p role="alert" className="rounded-lg border border-destructive/30 p-3 text-xs leading-6 text-destructive">{error}。已有结果标为未核实；请先刷新，未知提交不要重复执行。</p>}
  {notice&&<p role="status" className="text-xs leading-6 text-emerald-700 dark:text-emerald-300">{notice}</p>}
  {listing&&<><div className="flex flex-wrap items-center gap-2 text-xs"><Badge variant={stale?'warning':'outline'}>{stale?'上次结果 · 当前未核实':'当前只读信任状态'}</Badge><span>revision {listing.revision}</span><span className="break-all font-mono">{listing.fingerprint}</span></div>
   {listing.requires_retrust&&<p className="rounded-lg border border-amber-500/30 p-3 text-xs leading-6">公钥库缺失，插件执行被阻止。仍可在此显式添加你已核对的公钥，不会因插件列表故障锁住此入口；不会自动恢复旧信任。</p>}
   {!listing.keys.length&&<p className="text-xs leading-6 text-muted-foreground">当前没有可信公钥。空库不表示自动信任任何来源。</p>}
   <div className="space-y-3">{listing.keys.map(key=><article key={key.key_id} className="space-y-2 rounded-lg border border-border/60 p-3 text-xs"><div className="flex flex-wrap items-center gap-2"><strong>{key.display_name}</strong><code>{key.key_id}</code><Badge variant={key.status==='trusted'?'outline':'warning'}>{key.status==='trusted'?'已显式信任':'已撤销'}</Badge></div><p className="break-all font-mono">公钥：{key.public_key}</p><p className="break-all font-mono">SHA-256：{key.fingerprint}</p><Button size="sm" variant="outline" disabled={disabled} onClick={()=>generate(key.status==='trusted'?'revoke':'trust',key)}>{key.status==='trusted'?'预览撤销信任':'预览重新信任原公钥'}</Button></article>)}</div>
  </>}
  <section className="space-y-3 border-t border-border pt-4"><h3 className="text-xs font-medium">显式添加发布者公钥</h3><div className="grid gap-3 md:grid-cols-2"><label className="space-y-1 text-xs"><span>发布者 key_id</span><Input aria-label="发布者key_id" value={input.key_id} maxLength={100} autoComplete="off" disabled={disabled} onChange={event=>{clearPreview();setInput({...input,key_id:event.target.value})}}/></label><label className="space-y-1 text-xs"><span>单行显示名称</span><Input aria-label="发布者显示名称" value={input.display_name} maxLength={200} autoComplete="off" disabled={disabled} onChange={event=>{clearPreview();setInput({...input,display_name:event.target.value})}}/></label><label className="space-y-1 text-xs md:col-span-2"><span>Ed25519 公钥（32字节，44位规范base64；不是私钥）</span><Input aria-label="Ed25519公钥base64" value={input.public_key} maxLength={44} autoComplete="off" spellCheck={false} disabled={disabled} onChange={event=>{clearPreview();setInput({...input,public_key:event.target.value})}}/></label></div><p className="text-xs text-muted-foreground">同一公钥不能用多个 key_id 登记。已撤销的公钥必须通过原记录单独重新信任，不能换别名绕过撤销。</p><Button size="sm" variant="outline" disabled={disabled} onClick={()=>generate('add')}>生成添加公钥预览（不提交）</Button></section>
  <Dialog open={Boolean(preview)} onOpenChange={open=>{if(!open&&!busy)clearPreview()}}><DialogContent><DialogHeader><DialogTitle>独立确认本次公钥信任操作</DialogTitle><DialogDescription>预览不是批准插件执行。核对动作、发布者、公钥、指纹、目标状态和修订后，只提交这一次。</DialogDescription></DialogHeader>{preview&&<div className="space-y-3 text-xs leading-6"><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-secondary/40 p-3">{JSON.stringify(preview.snapshot,null,2)}</pre><p>当前读取修订：{listing?.revision} · 预览修订：{preview.snapshot.expected_revision}</p>{problem&&<p role="alert" className="text-destructive">{problem}</p>}<label className="flex items-start gap-2"><input type="checkbox" checked={ack} disabled={busy||Boolean(problem)} onChange={event=>setAck(event.target.checked)}/><span>我已通过可信渠道核对上述公钥指纹，理解信任不等于代码安全；只批准此快照，不启用或执行插件。</span></label></div>}<DialogFooter><Button variant="outline" disabled={busy} onClick={clearPreview}>取消，不提交</Button><Button disabled={busy||!preview||!ack||attempted||Boolean(problem)||disabled} onClick={commit}>批准本次公钥快照（一次性）</Button></DialogFooter></DialogContent></Dialog>
 </Card>
}
