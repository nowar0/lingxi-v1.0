import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { createProgramManagerClient } from '@/lib/programManager'
import type { ManagerMetadata, ManagerPlan, ManagerRecovery, ManagerResult, ManagerDiscovery, ManagerDiscoveryEntry } from '@/lib/programManager'

type Client = ReturnType<typeof createProgramManagerClient>
/** Independent view: the external manager entry can supply its own authenticated client. */
export function ProgramManagerView({ connected, client }: { connected:boolean; client:Client }) {
  const [metadata,setMetadata]=useState<ManagerMetadata>()
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('')
  const [source,setSource]=useState(''),[target,setTarget]=useState(''),[sidecar,setSidecar]=useState(''),[manifestSha,setManifestSha]=useState('')
  const [preview,setPreview]=useState<ManagerPlan>(),[recovery,setRecovery]=useState<ManagerRecovery>()
  const [last,setLast]=useState<Pick<ManagerPlan,'plan_id'|'kind'>>(),[result,setResult]=useState<ManagerResult>()
  const [discovery,setDiscovery]=useState<ManagerDiscovery>(),[adoption,setAdoption]=useState<ManagerDiscoveryEntry>()
  const [ack,setAck]=useState(false),[attempted,setAttempted]=useState(false),[now,setNow]=useState(Date.now())
  const generation=useRef(0),lock=useRef(false),closing=useRef(false),abort=useRef<AbortController|null>(null)
  useEffect(()=>{
    const owner=++generation.current;lock.current=false;closing.current=false;client.invalidate();setMetadata(undefined);setPreview(undefined);setRecovery(undefined);setDiscovery(undefined);setAdoption(undefined);setLast(undefined);setResult(undefined);setAck(false);setAttempted(false);setError('');setNotice('');setBusy(false)
    const controller=new AbortController();abort.current=controller
    if(connected){setBusy(true);void client.metadata(controller.signal).then(value=>{if(owner===generation.current&&!controller.signal.aborted)setMetadata(value)}).catch(cause=>{if(owner===generation.current&&!controller.signal.aborted)setError(String(cause instanceof Error?cause.message:cause))}).finally(()=>{if(owner===generation.current)setBusy(false)})}
    return()=>{generation.current++;controller.abort();abort.current?.abort();client.invalidate()}
  },[connected,client])
  useEffect(()=>{if(!preview&&!recovery&&!adoption)return;const timer=window.setInterval(()=>setNow(Date.now()),1000);return()=>window.clearInterval(timer)},[preview,recovery,adoption])
  const run=async(action:(signal:AbortSignal,publish:(update:()=>void)=>void)=>Promise<void>)=>{
    if(lock.current||busy||!connected||!metadata)return
    lock.current=true;setBusy(true);setError('');const owner=generation.current;const controller=new AbortController();abort.current=controller
    const publish=(update:()=>void)=>{if(owner===generation.current&&!controller.signal.aborted)update()}
    try{await action(controller.signal,publish)}catch(cause){publish(()=>setError(cause instanceof Error?cause.message:'操作结果未确认；不会自动重试'))}
    finally{if(owner===generation.current){lock.current=false;setBusy(false)}}
  }
  const showPlan=(value:ManagerPlan)=>{setPreview(value);setRecovery(undefined);setAck(false);setAttempted(false);setResult(undefined);setNotice('只生成预览，尚未修改程序；请核对全部路径、文件和哈希。')}
  const install=()=>void run(async(signal,publish)=>{const value=await client.previewInstall({source,target,...(sidecar?{archive_record:sidecar}:{})},signal);publish(()=>showPlan(value))})
  const uninstall=()=>void run(async(signal,publish)=>{const value=await client.previewUninstall(target,manifestSha,signal);publish(()=>showPlan(value))})
  const approve=()=>void run(async(signal,publish)=>{
    if(!ack||attempted||!preview||client.approvalProblem(preview))return
    const plan=preview;setAttempted(true);setAck(false);setPreview(undefined);setLast(plan)
    const output=plan.kind==='uninstall'?await client.commitUninstall(plan,true,signal):await client.commit(plan,true,signal)
    publish(()=>{setResult(output);setNotice('本次准确计划已提交。不会自动启动程序、停止进程或切换用户数据。')})
  })
  const recover=()=>void run(async(signal,publish)=>{
    if(!ack||attempted||!recovery||client.approvalProblem(recovery))return
    const approval=recovery;setAttempted(true);setAck(false);setRecovery(undefined)
    const output=await client.recover(approval,true,signal)
    publish(()=>{setResult(output);setNotice('恢复动作已提交，请核对真实事务状态；不是数据回退。')})
  })
  const findTransactions=()=>void run(async(signal,publish)=>{const value=await client.transactions(signal);publish(()=>{setDiscovery(value);setNotice('只读查找完成；未导入、恢复或执行事务，未知目录保持原样。')})})
  const adoptTransaction=()=>void run(async(signal,publish)=>{
    if(!ack||attempted||!adoption||client.discoveryApprovalProblem(adoption))return
    const entry=adoption;setAttempted(true);setAck(false);setAdoption(undefined)
    const handle=await client.adopt(entry,true,signal)
    publish(()=>{setLast(handle);setResult(undefined);setDiscovery(undefined);setNotice('已导入已尝试事务的查询句柄；这不是安装批准，也没有执行恢复。请独立核对状态或预览恢复。')})
  })
  // Escape, X, outside dismissal and the cancel button share one revocation path.
  // A recovery approval has no server cancel route: only discard its local capability.
  const closePreview=()=>{
    if(busy||lock.current||closing.current)return
    const plan=preview,approval=recovery,found=adoption,owner=generation.current
    if(!plan&&!approval&&!found)return
    closing.current=true;setPreview(undefined);setRecovery(undefined);setAdoption(undefined);setAck(false)
    if(found){closing.current=false;setNotice('已关闭事务导入确认；未导入或恢复，发现批准按期限失效。');return}
    if(approval){try{client.discardRecovery(approval);setNotice('已关闭恢复预览；未执行恢复，服务端批准按期限失效。')}catch(cause){setError(cause instanceof Error?cause.message:'恢复预览已关闭')}finally{closing.current=false}return}
    if(plan)void run(async(signal,publish)=>{try{await client.cancel(plan,signal);publish(()=>setNotice('预览已取消；未删除程序、数据或事务日志。'))}finally{if(owner===generation.current)closing.current=false}})
  }
  const ready=connected&&Boolean(metadata)&&!busy
  const pending=preview??recovery??adoption
  const problem=pending?(now>=pending.client_expires_at?'本地确认期限已过，请重新预览':adoption?client.discoveryApprovalProblem(adoption):client.approvalProblem(pending as ManagerPlan|ManagerRecovery)):''
  const approvalDetails=(adoption??recovery?.snapshot??preview?.plan) as Record<string,unknown>|undefined
  const approvalTarget=adoption?.target||(typeof approvalDetails?.target==='string'?approvalDetails.target:target)
  const approvalAction=adoption?'导入事务记录':recovery?'恢复中断的程序变更':preview?.kind==='uninstall'?'卸载跟踪的程序文件':preview?.kind==='rollback'?'回滚程序文件':'安装或升级程序'
  return <Card className="settings-section"><div className="settings-section-heading"><div><h2>便携程序管理</h2><p>程序安装、回滚与卸载；用户数据独立保留，根目录必须由宿主明确授权</p></div></div><div className="space-y-4 p-5 text-xs leading-6">
    {!connected&&<p role="status">离线或演示模式：未查询程序根目录，不生成或提交安装计划。</p>}
    {error&&<p role="alert" className="whitespace-pre-wrap rounded-lg border border-destructive/20 bg-destructive/5 p-3 text-destructive">{error}</p>}
    {!metadata&&connected&&!busy&&<p>宿主未配置安装管理范围，未对任意目录授权。请使用外置便携管理器，普通设置页不会自行放宽目录。</p>}
    {metadata&&<div className="rounded-lg border border-border p-3"><p>宿主批准根：<code className="break-all">{metadata.approved_root}</code></p><p>事务目录：<code className="break-all">{metadata.state_dir}</code></p><p>保留数据：<code className="break-all">{metadata.data_dir}</code></p><p>{metadata.notice}</p><p>{metadata.owned_journal_discovery===true?'可手动只读查找宿主事务目录中的已有记录；只有后端验证的已尝试事务可明确导入查询句柄，再独立预览恢复。未知、旧格式或无法验证记录仍需固定离线 CLI 核对。':'管理器未声明已有事务发现能力；重启后的旧事务需固定离线 CLI 核对。'}此界面不接受任意日志路径。</p></div>}
    <p>预览不是批准。安装与回滚保留一代程序文件；卸载不可撤销，仅移除清单跟踪文件，未知文件和用户数据保留。程序占用时请正常退出，不自动结束进程。</p>
    <div className="grid gap-3 md:grid-cols-2"><label>来源目录或 ZIP（宿主本机路径）<Input aria-label="程序管理来源路径" value={source} maxLength={4000} disabled={!ready} onChange={event=>{setSource(event.target.value);setPreview(undefined);setAck(false)}}/></label><label>目标程序目录（不含管理器/数据）<Input aria-label="程序管理目标路径" value={target} maxLength={4000} disabled={!ready} onChange={event=>{setTarget(event.target.value);setPreview(undefined);setAck(false)}}/></label><label>ZIP 外置完整性记录路径（可选）<Input aria-label="程序管理外置记录路径" value={sidecar} maxLength={4000} disabled={!ready} onChange={event=>{setSidecar(event.target.value);setPreview(undefined);setAck(false)}}/></label><label>卸载所依据的可信 manifest SHA256<Input aria-label="程序卸载清单SHA256" value={manifestSha} maxLength={64} disabled={!ready} onChange={event=>{setManifestSha(event.target.value);setPreview(undefined);setAck(false)}}/></label></div>
    <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={!ready||!source||!target} onClick={install}>预览安装／升级（不执行）</Button><Button size="sm" variant="outline" disabled={!ready||!target||!/^[0-9a-f]{64}$/.test(manifestSha)} onClick={uninstall}>预览卸载程序（保留数据）</Button></div>
    <Button size="sm" variant="outline" disabled={!ready||metadata?.owned_journal_discovery!==true} onClick={findTransactions}>只读查找已有事务</Button>
    {discovery&&<section className="space-y-2 rounded-lg border border-border p-3"><p>只读扫描完整：{discovery.entries.length} 条记录；未知条目数 {discovery.unknown_entries}。只发现宿主事务目录内的一级事务记录；同时有界检查日志与目录容量，不修改或删除未知文件，也不自动导入。</p><div className="max-h-80 overflow-auto"><table className="w-full text-left"><thead><tr><th>事务 / 动作</th><th>状态 / 目标</th><th>验证 / 原因</th><th>明确操作</th></tr></thead><tbody>{discovery.entries.map((entry,index)=><tr key={entry.discovery_id??entry.known_plan_id??`unknown-${index}`} className="border-t border-border align-top"><td className="break-all p-2">{entry.transaction_id??'未知事务'}<br/>{entry.kind??'未知动作'}</td><td className="break-all p-2">{entry.phase??'未知状态'}<br/>{entry.target??'目标未知'}</td><td className="break-all p-2">{entry.verified?'后端已验证':'未验证'} · {entry.adoptable?'可独立确认导入':'不可新导入'}<br/>{entry.reason??'无已报告阻断'}</td><td className="p-2"><Button size="xs" variant="outline" disabled={!ready||!entry.verified||!entry.adoptable||!entry.discovery_id} onClick={()=>{setAdoption(entry);setPreview(undefined);setRecovery(undefined);setAck(false);setAttempted(false)}}>预览导入记录（不恢复）</Button></td></tr>)}</tbody></table></div></section>}
    {last&&<div className="space-y-2 rounded-lg border border-border p-3"><p className="break-all">最近已尝试计划：{last.plan_id} · {last.kind}</p><div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={!ready} onClick={()=>void run(async(signal,publish)=>{const output=await client.status(last.plan_id,signal);publish(()=>setResult(output))})}>核对状态</Button><Button size="sm" variant="outline" disabled={!ready||last.kind!=='install'} onClick={()=>void run(async(signal,publish)=>{const value=await client.previewRollback(last.plan_id,signal);publish(()=>showPlan(value))})}>独立预览程序回滚</Button><Button size="sm" variant="outline" disabled={!ready} onClick={()=>void run(async(signal,publish)=>{const value=await client.previewRecovery(last.plan_id,signal);publish(()=>{setRecovery(value);setPreview(undefined);setAck(false);setAttempted(false)})})}>独立预览中断恢复</Button><Button size="sm" variant="outline" disabled={!ready} onClick={()=>void run(async(signal,publish)=>{await client.retire(last.plan_id,signal);publish(()=>{setLast(undefined);setNotice('已释放经核对完成的事务句柄；持久日志保留。此编号之后改用固定离线CLI核对，不再从本页回滚。')})})}>释放已完成事务句柄（保留日志）</Button></div></div>}
    {notice&&<p role="status">{notice}</p>}{result&&<details><summary>完整事务结果（未自动启动或切换数据）</summary><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded bg-secondary/40 p-3">{JSON.stringify(result,null,2)}</pre></details>}
    <Dialog open={Boolean(pending)} onOpenChange={open=>{if(!open)closePreview()}}><DialogContent className="max-w-3xl"><DialogHeader><DialogTitle>{adoption?'确认导入已有事务查询记录（不执行恢复）':recovery?'确认准确现场恢复计划':preview?.kind==='uninstall'?'确认卸载跟踪程序文件':'确认准确程序变更计划'}</DialogTitle><DialogDescription>确认后只执行本次预览的操作。程序占用或结果未知时不会自动重试。</DialogDescription></DialogHeader><section className="space-y-1 rounded-xl border border-border p-3 text-xs leading-6"><p><strong>操作：</strong>{approvalAction}</p><p className="break-all"><strong>目标：</strong>{approvalTarget||'未提供'}</p>{preview?.kind==='install'&&<p className="break-all"><strong>来源：</strong>{source||'未提供'}</p>}{adoption&&<p><strong>事务：</strong>{adoption.transaction_id||'未提供'} · {adoption.phase||'状态未知'}</p>}<p>用户数据目录保留；不会自动启动程序或切换数据。</p></section><details className="personal-disclosure"><summary>查看完整计划、文件与哈希</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-secondary/40 p-3 text-[11px]">{JSON.stringify(approvalDetails,null,2)}</pre></details>{problem&&<p role="alert" className="text-destructive">{problem}</p>}<label className="flex items-start gap-2 text-xs"><input type="checkbox" checked={ack} disabled={busy||Boolean(problem)} onChange={event=>setAck(event.target.checked)}/>{adoption?'我已核对事务编号、动作、目标、状态、指纹和后端验证结果；仅导入已尝试记录的查询句柄，不恢复、不重新批准安装。':'我已核对完整路径、版本、每个文件与哈希、保留数据边界；仅批准这一次计划。'}</label><DialogFooter><Button variant="outline" disabled={busy} onClick={closePreview}>取消本次操作</Button><Button disabled={!ready||!ack||attempted||Boolean(problem)} onClick={adoption?adoptTransaction:recovery?recover:approve}>{adoption?'明确导入记录（一次，不恢复）':'明确批准本次计划（一次）'}</Button></DialogFooter></DialogContent></Dialog>
  </div></Card>
}

