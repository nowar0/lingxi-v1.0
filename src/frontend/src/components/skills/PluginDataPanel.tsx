import { useEffect, useMemo, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { createPluginDataClient } from '@/lib/pluginData'
import { PluginDataRecovery } from './PluginDataRecovery'
import { PluginStagePanel } from './PluginStagePanel'
import type { InventoryPreview, PluginDataClient, PluginDataStatus } from '@/lib/pluginData'

type Props={pluginId:string;revision:number;installed:boolean;disabled?:boolean;client?:PluginDataClient;onChanged?:()=>void}
/** Host metadata only. No stage, schema, file path or migration result is supplied by UI. */
export function PluginDataPanel({pluginId,revision,installed,disabled=false,client:suppliedClient,onChanged}:Props){
 const client=useMemo(()=>suppliedClient??createPluginDataClient(),[suppliedClient])
 const [status,setStatus]=useState<PluginDataStatus>(),[preview,setPreview]=useState<InventoryPreview>()
 const [busy,setBusy]=useState(false),[ack,setAck]=useState(false),[attempted,setAttempted]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[tick,setTick]=useState(0)
 const generation=useRef(0),lock=useRef(false),abort=useRef<AbortController|null>(null),closing=useRef(false)
 useEffect(()=>{generation.current++;client.invalidate();abort.current?.abort();lock.current=false;closing.current=false;setStatus(undefined);setPreview(undefined);setAck(false);setAttempted(false);setBusy(false);setError('');setNotice('');return()=>{generation.current++;abort.current?.abort();client.invalidate()}},[pluginId,revision,installed,disabled,client])
 useEffect(()=>{if(!preview)return;const timer=window.setInterval(()=>setTick(value=>value+1),1000);return()=>window.clearInterval(timer)},[preview])
 const run=async(action:(signal:AbortSignal,publish:(change:()=>void)=>void)=>Promise<void>)=>{
  if(lock.current||busy||disabled||!installed)return
  lock.current=true;setBusy(true);setError('');const owner=generation.current,controller=new AbortController();abort.current=controller
  const publish=(change:()=>void)=>{if(owner===generation.current&&!controller.signal.aborted)change()}
  try{await action(controller.signal,publish)}catch(cause){publish(()=>setError(cause instanceof Error?cause.message:'数据操作未确认；保留现场，不自动重试'))}finally{if(owner===generation.current){lock.current=false;setBusy(false)}}
 }
 const load=()=>void run(async(signal,publish)=>{const value=await client.status(pluginId,signal);publish(()=>{setStatus(value);setNotice('仅读取宿主数据元信息；没有登记代际、推断旧schema或迁移。')})})
 const inventoryPreview=()=>void run(async(signal,publish)=>{const value=await client.inventoryPreview(pluginId,revision,signal);publish(()=>{setPreview(value);setAck(false);setAttempted(false);setNotice('仅观察实际文件清单，不修改文件、schema或启用状态。请另行确认是否更新库存元数据。')})})
 const confirm=()=>void run(async(signal,publish)=>{
  if(!preview||!ack||attempted||client.approvalProblem(preview))return
  const pending=preview;setAttempted(true);setAck(false);setPreview(undefined)
  const result=await client.inventoryCommit(pending,true,signal)
  publish(()=>{setNotice(`已重核库存元数据，SHA256 ${result.inventory_sha256}。没有修改文件、推断schema或启用插件。`);setStatus(undefined);onChanged?.()})
 })
 const close=()=>{
  if(lock.current||busy||closing.current)return
  const pending=preview;if(!pending)return
  closing.current=true;setPreview(undefined);setAck(false);setAttempted(true)
  void run(async(signal,publish)=>{await client.cancel(pending,signal);publish(()=>setNotice('本次库存预览已撤销，没有删除文件、迁移或启用插件。'))}).finally(()=>{closing.current=false})
 }
 void tick
 const ready=installed&&!disabled&&!busy
 const current=status?.revision===revision
 const problem=preview?client.approvalProblem(preview):''
 if(!installed)return null
 return <section aria-label={`插件数据与代际：${pluginId}`} className="space-y-3 rounded-xl border border-border/60 bg-background/30 p-3 text-xs leading-6">
  <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="font-semibold">插件数据与代际 · 独立于代码版本</h4><Button size="xs" variant="outline" disabled={!ready} onClick={load}>只读刷新数据状态</Button></div>
  <p>状态读取不自动登记旧数据；schema 未知不当作0。代码回滚不等于数据回滚，旧批准不能恢复。</p>
  {disabled&&<p role="status">宿主离线或当前不可操作：暂停读取与批准，不使用演示代际。</p>}
  {!status&&!busy&&!disabled&&<p>尚未读取宿主数据元信息；点击只读刷新，不会自动扫描每个插件。</p>}
  {error&&<p role="alert" className="whitespace-pre-wrap break-all text-destructive">{error}</p>}
  {status&&<div className="space-y-2 rounded-lg border border-border/60 p-3"><dl className="grid gap-2 sm:grid-cols-2"><div><dt className="text-muted-foreground">宿主修订 / 当前包列表修订</dt><dd>{status.revision} / {revision}</dd></div><div><dt className="text-muted-foreground">数据 schema</dt><dd>{status.data_schema===null?'未知（NULL，不推断为0）':status.data_schema}</dd></div><div className="sm:col-span-2"><dt className="text-muted-foreground">活动数据代际</dt><dd className="break-all font-mono">{status.active_generation||'未登记；不会自动生成代际'}</dd></div><div><dt className="text-muted-foreground">数据写入 / 外部作用 epoch</dt><dd>{status.data_write_epoch} / {status.data_effect_epoch}</dd></div><div><dt className="text-muted-foreground">库存指纹状态</dt><dd>{status.data_inventory_dirty===1?'脏／待准确重核（不等于文件可删除）':'宿主库存元数据无脏标记（不等于执行已获准）'}</dd></div><div><dt className="text-muted-foreground">迁移事务</dt><dd className="break-all">{status.migration_phase??'无活动迁移标记'}{status.migration_id&&<code className="block">{status.migration_id}</code>}</dd></div><div><dt className="text-muted-foreground">运行资源</dt><dd>{status.loaded?'仍有运行／清理资源，不能当作空闲':'宿主未报告已加载资源'}</dd></div></dl><p>{status.notice}</p>{!current&&<p role="alert" className="text-amber-700">数据修订与插件列表不同，请先刷新插件列表；旧修订不用于批准。</p>}
   <details><summary>当前来源与配置／状态哈希（只读元数据）</summary><pre className="max-h-56 overflow-auto whitespace-pre-wrap break-all text-[11px]">{JSON.stringify({plugin_id:status.plugin_id,digest:status.digest,config_sha:status.config_sha,state_sha:status.state_sha,generation_registered:status.generation_registered,schema_known:status.schema_known},null,2)}</pre></details>
   <details><summary>上一代只读摘要（不执行回退）</summary>{status.previous?<pre className="max-h-56 overflow-auto whitespace-pre-wrap break-all text-[11px]">{JSON.stringify(status.previous,null,2)}</pre>:<p>宿主没有提供上一代记录；不从上一版代码推导数据快照。</p>}</details>
   <p>联合回滚与previous清理仍使用下面的独立预览和一次性确认。迁移A的实际原生能力及来源门禁由独立入口逐次核验，没有自动注册、迁移、回滚或启用。</p>
    <PluginDataRecovery pluginId={pluginId} revision={revision} disabled={disabled||busy||Boolean(preview)||!current} status={status} client={client} onChanged={()=>{setStatus(undefined);onChanged?.()}}/>
  </div>}
  <div className="flex flex-wrap gap-2"><Button size="xs" variant="outline" disabled={!ready||!current||!status?.generation_registered||status.loaded||Boolean(status.migration_id)} onClick={inventoryPreview}>预览实际库存重核（不修改文件）</Button></div>
  
  <PluginStagePanel pluginId={pluginId} revision={revision} disabled={disabled||busy||Boolean(preview)} status={status} onChanged={()=>{setStatus(undefined);onChanged?.()}}/>
  {notice&&<p role="status" className="break-all">{notice}</p>}
  <Dialog open={Boolean(preview)} onOpenChange={open=>{if(!open)close()}}><DialogContent className="max-w-3xl"><DialogHeader><DialogTitle>确认准确库存元数据重核</DialogTitle><DialogDescription>只更新已登记活动代际的库存指纹；不写原件、不推断schema、不运行迁移或启用。下面是完整授权快照，不截断文件量和哈希。</DialogDescription></DialogHeader><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-secondary/40 p-3 text-[11px]">{JSON.stringify(preview?.snapshot,null,2)}</pre>{problem&&<p role="alert" className="text-destructive">{problem}</p>}<label className="flex items-start gap-2 text-xs"><input type="checkbox" checked={ack} disabled={busy||Boolean(problem)} onChange={event=>setAck(event.target.checked)}/>我已核对插件、修订、活动代际、写入epoch、实际文件数量与哈希，只批准这一次库存元数据更新，不批准迁移、回退或启用。</label><DialogFooter><Button variant="outline" disabled={busy} onClick={close}>取消本次重核</Button><Button disabled={!ready||!ack||attempted||Boolean(problem)} onClick={confirm}>明确重核库存（一次）</Button></DialogFooter></DialogContent></Dialog>
 </section>
}
