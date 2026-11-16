import { useEffect, useMemo, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { createPluginStageClient, nativeStageSelector, parseStageSource } from '@/lib/pluginStage'
import type { ActivationResult, CandidateSummary, CompatibilityResult, LegacyResult, PluginStageClient, StageApproval, StageDiagnostic, StageMetadata, StageSelector, StageSource, StageSourceKind } from '@/lib/pluginStage'
import type { PluginDataStatus } from '@/lib/pluginData'

type Props={pluginId:string;revision:number;disabled?:boolean;status?:PluginDataStatus;client?:PluginStageClient;selector?:StageSelector;onChanged?:()=>void}
const titles={legacy_registration:'独立确认旧代登记（schema保持NULL）',compatibility_audit:'独立确认未知schema真实证据审核',migration_A:'确认A：仅运行一次严格原生迁移',activation:'第二次批准B：原子发布代码与数据元组'}
const acknowledgements={
 legacy_registration:'我已核对旧代码、实际完整库存、修订与epoch，只登记当前旧代元数据，保持schema为NULL；不改文件、不推断格式、不迁移或启用。',
 compatibility_audit:'我作为所填审核人已阅读真实证据的全部字节／文本及哈希，并核对完整旧元组、目标代码、迁移声明与明确resolved_schema；本次只生成宿主审核能力，不登记schema、不运行A或批准B。',
 migration_A:'我已核对完整旧元组、候选代码清单、迁移hook、真实证据审核、原生策略与全部限额；只批准A运行这一次，不允许普通进程降级、重试或发布活动元组。',
 activation:'这是独立于A的第二次批准。我已核对宿主持有的A结果、代码／数据／schema／配置／状态哈希、完整文件清单及previous保留语义；只发布本次B，保持插件停用，不批准再次运行hook或启用。',
}
const json=(value:unknown)=>JSON.stringify(value,null,2)

/** Product entry: native source-ID -> real evidence review -> A -> independent B. */
export function PluginStagePanel({pluginId,revision,disabled=false,status,client:suppliedClient,selector:suppliedSelector,onChanged}:Props){
 const client=useMemo(()=>suppliedClient??createPluginStageClient(),[suppliedClient])
 const selector=suppliedSelector??nativeStageSelector()
 const [source,setSource]=useState<StageSource>(),[evidence,setEvidence]=useState<StageSource>()
 const [resolvedSchema,setResolvedSchema]=useState(''),[reviewer,setReviewer]=useState('')
 const [compatibility,setCompatibility]=useState<CompatibilityResult>(),[preview,setPreview]=useState<StageApproval>(),[diagnostic,setDiagnostic]=useState<StageDiagnostic>()
 const [stage,setStage]=useState<StageMetadata>(),[candidates,setCandidates]=useState<CandidateSummary[]>([]),[runningCandidate,setRunningCandidate]=useState<string>()
 const [result,setResult]=useState<LegacyResult|ActivationResult>(),[busy,setBusy]=useState(false),[ack,setAck]=useState(false),[attempted,setAttempted]=useState(false),[cancelBusy,setCancelBusy]=useState(false)
 const [error,setError]=useState(''),[notice,setNotice]=useState(''),[tick,setTick]=useState(0),[offline,setOffline]=useState(false)
 const generation=useRef(0),lock=useRef(false),abort=useRef<AbortController|null>(null),closing=useRef(false),cancelLock=useRef(false)
 const invalidate=()=>{generation.current++;client.invalidate();abort.current?.abort();lock.current=false;closing.current=false;setBusy(false);setPreview(undefined);setAck(false);setAttempted(false);setCompatibility(undefined);setDiagnostic(undefined);setStage(undefined)}
 useEffect(()=>{
  invalidate();setSource(undefined);setEvidence(undefined);setResolvedSchema('');setReviewer('');setCandidates([]);setRunningCandidate(undefined);setResult(undefined);setError('');setNotice('')
  return()=>{generation.current++;abort.current?.abort();client.invalidate()}
 // Lifecycle/revision changes revoke local approvals; do not perform any request.
 // eslint-disable-next-line react-hooks/exhaustive-deps
 },[pluginId,revision,disabled,client])
 useEffect(()=>{const disconnected=()=>{setOffline(true);invalidate();setNotice('连接已断开：旧批准全部失效。原件与阶段数据保留；恢复连接后只手动读取候选，不自动重试。')};const connected=()=>setOffline(false);window.addEventListener('offline',disconnected);window.addEventListener('online',connected);return()=>{window.removeEventListener('offline',disconnected);window.removeEventListener('online',connected)}
 // eslint-disable-next-line react-hooks/exhaustive-deps
 },[client])
 useEffect(()=>{if(!preview&&!compatibility)return;const timer=window.setInterval(()=>setTick(v=>v+1),1000);return()=>window.clearInterval(timer)},[preview,compatibility])
 const ready=!disabled&&!offline&&!busy&&!preview
 const current=status?.revision===revision
 const idle=current&&status&&!status.enabled&&!status.loaded&&!status.migration_id
 const run=async(action:(signal:AbortSignal,publish:(change:()=>void)=>void)=>Promise<void>)=>{
  if(lock.current||busy||disabled||offline)return
  lock.current=true;setBusy(true);setError('');const owner=generation.current,controller=new AbortController();abort.current=controller
  const publish=(change:()=>void)=>{if(owner===generation.current&&!controller.signal.aborted)change()}
  try{await action(controller.signal,publish)}catch(cause){publish(()=>{setError(cause instanceof Error?cause.message:'结果未确认；保留现场，不自动重试');setPreview(undefined);setAck(false);setAttempted(true);setCompatibility(undefined)})}finally{if(owner===generation.current){lock.current=false;setBusy(false)}}
 }
 const choose=(kind:StageSourceKind)=>void run(async(signal,publish)=>{
  if(!selector)throw new Error('宿主尚未提供原生来源选择器；HTTP不接受任意文件路径。请由桌面主进程接入受保护选择器。')
  const selected=await selector({plugin_id:pluginId,kind});if(selected===null)return
  const parsed=parseStageSource(selected,kind);await client.source(parsed,signal)
  publish(()=>{client.invalidate();setPreview(undefined);setAck(false);setAttempted(false);setCompatibility(undefined);setDiagnostic(undefined);setStage(undefined);setResult(undefined);setRunningCandidate(undefined);if(kind==='candidate')setSource(parsed);else setEvidence(parsed);setNotice('已读取原生选择器登记的来源ID与真实文件指纹；尚未批准审核、迁移或发布。来源变化使旧批准失效。')})
 })
 const changeReview=(field:'schema'|'reviewer',value:string)=>{
  if(busy||preview)return
  invalidate();if(field==='schema')setResolvedSchema(value);else setReviewer(value)
  setNotice('审核输入已变化，旧批准和兼容性能力已失效；需要重新预览并独立审核。')
 }
 const showPreview=(value:StageApproval,publish:(change:()=>void)=>void)=>publish(()=>{setPreview(value);setAck(false);setAttempted(false);setNotice('这是只读完整预览，尚未执行。关闭或取消不删除任何原件／阶段数据。')})
 const legacyPreview=()=>void run(async(signal,publish)=>showPreview(await client.legacyPreview(pluginId,revision,signal),publish))
 const auditPreview=()=>void run(async(signal,publish)=>{
  if(!source||!evidence||!/^(0|[1-9][0-9]*)$/.test(resolvedSchema))throw new Error('必须选择真实候选与审核证据，并明确填写旧schema；空值不能当作0。')
  showPreview(await client.auditPreview(pluginId,revision,source,evidence,Number(resolvedSchema),reviewer,signal),publish)
 })
 const stagePreview=()=>void run(async(signal,publish)=>{
  if(!source)return
  const value=await client.preview(pluginId,revision,source,compatibility?.compatibility_id,signal)
  if('blocked' in value)publish(()=>{setDiagnostic(value);setStage(undefined);setNotice('宿主当前门禁未满足；以下是真实阻止原因与完整快照，没有A运行批准。')})
  else{showPreview(value,publish);publish(()=>setDiagnostic(undefined))}
 })
 const activationPreview=()=>void run(async(signal,publish)=>{if(stage)showPreview(await client.activationPreview(pluginId,revision,stage,signal),publish)})
 const confirm=()=>void run(async(signal,publish)=>{
  if(!preview||!ack||attempted||client.approvalProblem(preview))return
  const pending=preview;setPreview(undefined);setAck(false);setAttempted(true)
  if(pending.kind==='migration_A'){const snap=pending.snapshot.stage;if(snap&&typeof snap==='object'&&'candidate_id' in snap)setRunningCandidate(String(snap.candidate_id));setNotice('A的一次性批准已提交；正在等待严格原生执行及清理确认。断线／超时不重试，请手动读取宿主候选。')}
  const value=await client.confirm(pending,true,signal)
  publish(()=>{
   if(pending.kind==='migration_A'){const metadata=value as StageMetadata;setStage(metadata);setRunningCandidate(undefined);setNotice('A已验证，结果留在宿主。活动代码／数据元组没有发布，插件仍停用；必须另行预览B并第二次明确批准。')}
   else if(pending.kind==='compatibility_audit'){setCompatibility(value as CompatibilityResult);setNotice('宿主已按真实证据、旧元组与目标代码生成审核能力；schema仍为NULL。现在可重新预览A，不能把审核批准当作A或B批准。')}
   else{setResult(value as LegacyResult|ActivationResult);setCompatibility(undefined);setDiagnostic(undefined);setStage(undefined);setNotice(pending.kind==='legacy_registration'?'已独立登记旧代，schema保持NULL；没有改文件或运行迁移。请手动刷新顶部数据状态。':'B已发布完整元组并保留previous，插件保持停用。请手动刷新插件列表与数据状态；没有自动启用。');onChanged?.()}
  })
 })
 const close=()=>{
  if(lock.current||busy||closing.current)return
  const pending=preview;if(!pending)return
  closing.current=true;setPreview(undefined);setAck(false);setAttempted(true)
  void run(async(signal,publish)=>{await client.cancel(pending,signal);publish(()=>setNotice('本次批准已撤销；没有删除候选、阶段数据或原件，没有运行迁移或发布B。'))}).finally(()=>{closing.current=false})
 }
 const refreshCandidates=()=>void run(async(signal,publish)=>{const values=await client.candidates(pluginId,signal);publish(()=>{setCandidates(values);setNotice('仅读取当前UI身份在本宿主生命周期内持有的候选；没有自动接管旧日志、重跑hook或删除阶段数据。')})})
 const inspectCandidate=(candidateId:string)=>void run(async(signal,publish)=>{const value=await client.prepared(pluginId,revision,candidateId,signal);publish(()=>{setStage(value);setRunningCandidate(undefined);setNotice('已重新验证宿主持有的A结果与真实代码／数据清单；仍需独立B预览及第二次批准。')})})
 const cancelExecution=async(candidateId:string)=>{
  if(cancelLock.current||disabled||offline)return
  cancelLock.current=true;setCancelBusy(true);const owner=generation.current
  try{await client.cancelExecution(pluginId,candidateId);if(owner===generation.current)setNotice('已请求取消这一个A调用；等待宿主清理核对。未删除原件或阶段数据，也不会自动重跑。请手动刷新候选。')}
  catch(cause){if(owner===generation.current)setError(cause instanceof Error?cause.message:'取消结果未知；不删除现场或重试')}
  finally{cancelLock.current=false;setCancelBusy(false)}
 }
 void tick
 const problem=preview?client.approvalProblem(preview):''
 const schemaNumber=/^(0|[1-9][0-9]*)$/.test(resolvedSchema)?Number(resolvedSchema):NaN
 return <section aria-label={`迁移A与B完整入口：${pluginId}`} className="space-y-3 rounded-lg border border-border/60 p-3">
  <div className="flex flex-wrap items-center justify-between gap-2"><h5 className="font-semibold">旧代登记 → 证据审核 → 迁移A → 第二次批准B</h5><Button size="xs" variant="outline" disabled={!ready} onClick={refreshCandidates}>手动刷新宿主候选</Button></div>
  <p>所有操作均独立预览、一次性批准；A只生成新候选，B才发布完整活动元组，最后仍停用。未知schema不猜0，不上传 StageResult，不使用普通进程降级。</p>
  {(disabled||offline)&&<p role="status" className="text-amber-700">宿主离线、上层操作中或当前不可操作：不接受旧批准。恢复后手动读取，不自动恢复执行。</p>}
  {!current&&<p>请先手动刷新顶部数据状态；只有与插件列表相同的修订可进入新批准。</p>}
  {current&&!status?.generation_registered&&<div className="space-y-2 rounded border border-border/60 p-2"><p>当前旧数据尚未登记。独立登记只绑定实际库存、代码及配置／状态哈希；保持schema为NULL，不改原件。</p><Button size="xs" variant="outline" disabled={!ready||!idle} onClick={legacyPreview}>预览旧代登记（schema保持NULL）</Button></div>}
  <div className="space-y-2 rounded border border-border/60 p-2"><h6 className="font-medium">1 · 宿主原生来源选择</h6><p>HTTP只接受受当前UI身份保护的source-id。文件路径不能在浏览器输入或上传；主进程选择后登记真实文件身份、SHA256和完整包清单。</p>
   {!selector&&<p role="status">当前宿主没有暴露原生选择器；其他只读／恢复入口可用，来源选择需桌面主进程接入，不能降级成浏览器路径框。</p>}
   <div className="flex flex-wrap gap-2"><Button size="xs" variant="outline" disabled={!ready||!selector} onClick={()=>choose('candidate')}>原生选择候选包／目录</Button><Button size="xs" variant="outline" disabled={!ready||!selector} onClick={()=>choose('compatibility_evidence')}>原生选择真实审核证据</Button></div>
   {source&&<dl><dt>候选来源</dt><dd className="break-all">{source.label}<code className="block">source-id {source.source_id}</code><code className="block">真实观察指纹 {source.fingerprint}</code></dd></dl>}
   {evidence&&<dl><dt>审核证据来源</dt><dd className="break-all">{evidence.label}<code className="block">source-id {evidence.source_id}</code><code className="block">真实观察指纹 {evidence.fingerprint}</code></dd></dl>}
  </div>
  {current&&status?.generation_registered&&status.data_schema===null&&<div className="space-y-2 rounded border border-amber-500/25 p-2"><h6 className="font-medium">2 · 未知schema的独立人工审核</h6><p>必须阅读真实证据字节／文本，核对完整旧元组、配置／状态哈希与目标迁移声明；显式确认后，宿主才签发owner能力。这里不持久写入schema，也不推断为0。</p><div className="grid gap-2 sm:grid-cols-2"><label>明确审核得到的旧schema<input aria-label="明确审核得到的旧schema" type="text" inputMode="numeric" value={resolvedSchema} disabled={!ready} onChange={event=>changeReview('schema',event.target.value)} placeholder="未知；请明确审核，不能留空当0" className="block w-full rounded border border-border bg-background px-2 py-1"/></label><label>实际审核人<input aria-label="实际审核人" type="text" value={reviewer} maxLength={200} disabled={!ready} onChange={event=>changeReview('reviewer',event.target.value)} placeholder="填写实际审核人" className="block w-full rounded border border-border bg-background px-2 py-1"/></label></div><Button size="xs" variant="outline" disabled={!ready||!idle||!source||!evidence||!Number.isSafeInteger(schemaNumber)||schemaNumber<0||schemaNumber>2147483647||!reviewer.trim()} onClick={auditPreview}>预览真实证据及审核绑定</Button>{compatibility&&<details open><summary>宿主审核能力已持有（不是A/B批准）</summary><pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all text-[11px]">{json(compatibility)}</pre></details>}</div>}
  <div className="space-y-2 rounded border border-border/60 p-2"><h6 className="font-medium">3 · A完整严格原生预览</h6><p>完整显示旧元组、目标代码与迁移hook清单、所有数据限额、AppContainer／Job／通道策略。只有宿主实际可用时才提供一次性A批准；取消不删阶段数据。</p><Button size="xs" variant="outline" disabled={!ready||!idle||!status?.generation_registered||status?.data_inventory_dirty!==0||!source} onClick={stagePreview}>预览A完整策略（不执行）</Button>
   {diagnostic&&<div role="status" className="space-y-2"><p className="text-amber-700">本次真实门禁：{diagnostic.reasons.join('、')}。没有A token，不能运行。</p><details><summary>完整门禁与来源快照</summary><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all text-[11px]">{json(diagnostic.snapshot)}</pre></details></div>}
   {runningCandidate&&<div role="status"><p className="break-all">本次A候选ID：<code>{runningCandidate}</code>。确认请求若超时仍不得重发；用手动候选刷新核对。</p><Button size="xs" variant="outline" disabled={cancelBusy||disabled||offline} onClick={()=>void cancelExecution(runningCandidate)}>仅请求取消本次A（不删除）</Button></div>}
  </div>
  {candidates.length>0&&<div className="space-y-2 rounded border border-border/60 p-2"><h6 className="font-medium">本UI身份的宿主候选（只读）</h6>{candidates.map(candidate=><div key={candidate.candidate_id} className="space-y-1 border-b border-border/40 pb-2"><p className="break-all"><code>{candidate.candidate_id}</code> · {candidate.phase} · 修订 {candidate.expected_revision}</p>{candidate.phase==='validated'?<Button size="xs" variant="outline" disabled={!ready||candidate.expected_revision!==revision} onClick={()=>inspectCandidate(candidate.candidate_id)}>重新核对宿主持有结果</Button>:candidate.phase==='running'?<Button size="xs" variant="outline" disabled={cancelBusy||disabled||offline} onClick={()=>void cancelExecution(candidate.candidate_id)}>请求取消此调用（不删除）</Button>:candidate.phase==='completed_disabled'?<p>B已由宿主真实发布回执确认，保持停用；不重复发布或运行。</p>:<p>结果／清理需宿主人工复核；不采用旧回执、不自动重跑或删除。</p>}</div>)}</div>}
  <div className="space-y-2 rounded border border-border/60 p-2"><h6 className="font-medium">4 · 独立B发布批准</h6>{stage?<><p>宿主已验证A并保留真实结果；这里只展示元数据，没有将 StageResult／数据字节复制到浏览器。B使用候选ID向现有dataAPI取宿主持有结果。</p><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-[11px]">{json(stage)}</pre><Button size="xs" variant="outline" disabled={!ready||!idle||stage.expected_revision!==revision} onClick={activationPreview}>独立预览B发布（第二次批准）</Button></>:<p>尚未手动取得本宿主持有的已验证A候选。A成功或手动核对候选后，才能另行预览B；不会自动发布或启用。</p>}</div>
  {result&&<details open><summary>宿主操作结果（保持停用）</summary><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-[11px]">{json(result)}</pre></details>}
  {error&&<p role="alert" className="whitespace-pre-wrap break-all text-destructive">{error}</p>}
  {notice&&<p role="status" className="break-all">{notice}</p>}
  <Dialog open={Boolean(preview)} onOpenChange={open=>{if(!open)close()}}><DialogContent className="max-w-4xl"><DialogHeader><DialogTitle>{preview?titles[preview.kind]:'独立批准'}</DialogTitle><DialogDescription>下面为本次完整授权快照，滚动查看不等于截断。与来源、修订、UI身份及宿主生命周期绑定；一次尝试即消费。取消保留原件与阶段现场，不自动重试。</DialogDescription></DialogHeader><pre className="max-h-[55vh] overflow-auto whitespace-pre-wrap break-all rounded-lg bg-secondary/40 p-3 text-[11px]">{json(preview?.snapshot)}</pre>{problem&&<p role="alert" className="text-destructive">{problem}</p>}<label className="flex items-start gap-2 text-xs"><input type="checkbox" checked={ack} disabled={busy||Boolean(problem)} onChange={event=>setAck(event.target.checked)}/>{preview&&acknowledgements[preview.kind]}</label><DialogFooter><Button variant="outline" disabled={busy} onClick={close}>取消本次批准（不删除）</Button><Button disabled={disabled||offline||busy||!ack||attempted||Boolean(problem)} onClick={confirm}>{preview?.kind==='migration_A'?'明确运行A一次':preview?.kind==='activation'?'第二次明确批准B发布':preview?.kind==='compatibility_audit'?'明确确认真实证据审核':'明确登记旧代一次'}</Button></DialogFooter></DialogContent></Dialog>
 </section>
}
