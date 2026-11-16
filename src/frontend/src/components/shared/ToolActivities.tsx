import { useEffect, useState } from 'react'
import { Check, ChevronDown, ChevronUp, Clock3, LoaderCircle, ShieldCheck, X } from 'lucide-react'
import { toast } from 'sonner'
import type { Mode, ToolActivity } from '@/types'
import { useApp } from '@/context/AppContext'
import { Button } from '@/components/ui/button'
import { PackageCallRequest } from '@/components/skills/PackageCallRequest'
import { QuestionHistory } from './ConversationQuestionCard'

const names: Record<string,string> = { workspace_read:'读取文件', workspace_list:'浏览目录', workspace_write:'写入文件', workspace_edit:'编辑片段', workspace_command:'执行命令', propose_memory:'记忆候选', search_memory:'检索记忆', remember_fact:'写入记忆', dedicated_browser:'浏览器操作', read_package_tools:'读取插件包工具', request_package_call:'请求插件包调用' }
const labels: Record<string,string> = { running:'执行中', waiting_approval:'等待确认', waiting:'等待确认', approved:'已批准，等待执行结果', complete:'已完成', executed:'已完成', denied:'已拒绝', expired:'已过期', cancelled:'已取消', failed:'未完成', unknown:'结果未知', invalid:'已失效' }

export function ToolActivities({ mode, batchId }: { mode: Mode; batchId?: string }) {
  const { conversationBatches } = useApp()
  const details=conversationBatches[mode].find(batch=>batch.id===batchId)?.details
  const tools=(details?.tools || []).filter(tool=>tool.tool!=='request_user_input')
  const questions=details?.questions || []
  if (!tools.length && !questions.some(q=>q.status!=='waiting')) return null
  return <div className="message-tool-activities" aria-label="本轮工具活动">{tools.map((tool,index)=><ToolActivityCard key={tool.activity_id || `${tool.tool}:${index}`} activity={tool} mode={mode}/>)}<QuestionHistory questions={questions}/></div>
}

function ToolActivityCard({ activity, mode }: { activity: ToolActivity; mode: Mode }) {
  const { decideToolApproval, connection } = useApp()
  const [open,setOpen]=useState(activity.status==='waiting_approval')
  const [busy,setBusy]=useState(false)
  const [now,setNow]=useState(()=>Date.now()/1000)
  const approval=activity.approval
  const waiting=Boolean(approval?.valid && approval.status==='waiting')
  useEffect(()=>{if(!waiting)return;setOpen(true);const timer=window.setInterval(()=>setNow(Date.now()/1000),1000);return()=>window.clearInterval(timer)},[waiting])
  const remaining=approval?Math.max(0,Math.floor(approval.expires_at-now)):0
  const status=approval?.status && ['waiting','approved','denied','expired','cancelled','invalid'].includes(approval.status)?approval.status:activity.status
  const decide=async(allowed:boolean)=>{
    if(!approval||busy||!remaining)return
    setBusy(true)
    try{await decideToolApproval(mode,approval,allowed)}catch(error){toast.error('本次确认未生效',{description:(error as Error).message})}finally{setBusy(false)}
  }
  let command:unknown
  if(activity.tool==='workspace_command')try{command=JSON.parse(String(activity.args.argv_json))}catch{/* Show the exact raw arguments below. */}
  return <section className="message-tool-card"><button className="message-tool-heading" aria-expanded={open} onClick={()=>setOpen(value=>!value)}><span>{waiting?<ShieldCheck/>:status==='running'?<LoaderCircle className="animate-spin"/>:status==='complete'||status==='executed'?<Check/>:<Clock3/>}</span><strong>{names[activity.tool]||activity.tool}</strong><small>{labels[status]||status}</small>{open?<ChevronUp/>:<ChevronDown/>}</button>{open&&<div className="message-tool-details">{(approval?.scope_note||activity.scope_note)&&<p>{approval?.scope_note||activity.scope_note}</p>}{activity.args.path!==undefined&&<p>目标：{String(activity.args.path)}</p>}{command!==undefined&&<><h4>程序与参数（逐项）</h4><pre>{JSON.stringify(command,null,2)}</pre></>}{approval?.snapshot.domain&&<><h4>批准前的任务、模型与目录快照</h4><pre>{JSON.stringify(approval.snapshot.domain,null,2)}</pre></>}<h4>本次参数</h4><pre>{JSON.stringify(activity.args,null,2)}</pre>{activity.result!==undefined&&<><h4>结果</h4><pre>{activity.result}</pre>{activity.tool==='request_package_call'&&<PackageCallRequest result={activity.result} disabled={connection!=='connected'}/>}</>}{activity.duration_ms!==undefined&&activity.duration_ms!==null&&<p>执行耗时 {activity.duration_ms} ms</p>}{approval&&<p className="message-tool-request-id">确认编号 {approval.id} · 本轮 {approval.snapshot.batch_id}</p>}{waiting&&<div className="message-tool-approval"><p><strong>{names[activity.tool]||activity.tool}</strong>{activity.args.path!==undefined?` · ${String(activity.args.path)}`:''}</p>{approval?.scope_note&&<p>{approval.scope_note}</p>}<p>{remaining?`本次确认还剩 ${remaining} 秒；只对当前参数生效。`:'本次确认已过期，请核对当前对话。'}</p><div><Button size="xs" variant="outline" disabled={busy||!remaining||connection!=='connected'} onClick={()=>void decide(false)}><X/>拒绝本次</Button><Button size="xs" disabled={busy||!remaining||connection!=='connected'} onClick={()=>void decide(true)}>{busy?<LoaderCircle className="animate-spin"/>:<Check/>}批准本次</Button></div></div>}</div>}</section>
}
