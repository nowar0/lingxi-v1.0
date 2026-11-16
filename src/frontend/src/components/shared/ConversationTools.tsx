import { Select } from '@/components/ui/select'
import { useConversationToolPanel } from '@/context/ConversationToolContext'
import { useCallback, useEffect, useState } from 'react'
import { Download, ListChecks, Settings2, Terminal, X } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api, apiBlob, json } from '@/api/client'
import { evolution, type OptionsView, type Proposal } from '@/lib/evolution'
import { downloadBlob } from '@/lib/utils'
import { useApp } from '@/context/AppContext'
import type { Mode } from '@/types'
import { Button } from '@/components/ui/button'
import { Input, Textarea } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { ProjectDocsPanel, RemindersPanel } from './EvolutionPanels'
import { AutoRead } from './AutoRead'
import { SearchPanel } from './ResearchWorkspacePanel'

const sources:Record<string,string>={session:'本会话',preset:'工作预设',module:'模块默认','preset/module':'预设 / 模块默认'}
const states:Record<string,string>={proposed:'建议交给工作',planning:'正在拆分',created:'已创建 · 等待启动',failed:'拆分未完成',needs_workspace:'需要工作目录'}

export function ConversationTools({mode,sessionId}:{mode:Mode;sessionId?:number}){
 const {connection,messages,workMessages,setDraft,refreshWorkSessions,refreshTasks,setActiveWorkSessionId}=useApp()
 const navigate=useNavigate();const ready=connection==='connected'&&!!sessionId
 const panel=useConversationToolPanel();const [localTab,setLocalTab]=useState('');const tab=panel?.tab??localTab,setTab=panel?.setTab??setLocalTab
 const [view,setView]=useState<OptionsView>(),[choices,setChoices]=useState<{models:string[];groups:string[]}>({models:[],groups:[]})
 const [items,setItems]=useState<Proposal[]>([]),[goal,setGoal]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[directory,setDirectory]=useState('')
 const sid=sessionId||0
 const refresh=useCallback(async()=>{if(ready)setItems((await evolution.proposals(sid)).items)},[sid,ready])
 useEffect(()=>{setTab('');setView(undefined);setItems([]);setError('');let live=true;const read=()=>void evolution.proposals(sid).then(v=>{if(live)setItems(v.items)}).catch(()=>{});if(ready){read();const timer=window.setInterval(read,3000);return()=>{live=false;window.clearInterval(timer)}}},[sid,ready])
 useEffect(()=>{if(!ready||tab!=='settings')return;let live=true;void Promise.all([evolution.options(sid),api<{models:string[];groups:string[]}>(`/v6/sessions/${sid}/options/catalog`)]).then(([v,c])=>{if(live){setView(v);setChoices(c)}}).catch(e=>setError((e as Error).message));return()=>{live=false}},[sid,ready,tab])
 const run=async(operation:()=>Promise<void>)=>{if(busy)return;setBusy(true);setError('');try{await operation()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
 const active=items.find(item=>['proposed','planning','failed','needs_workspace'].includes(item.status))
 const latest=(mode==='companion'?messages:workMessages).filter(m=>m.role==='user').at(-1)?.content||''
 useEffect(()=>{if(tab==='handoff')setGoal(latest.slice(0,12000))},[tab,latest])
 const propose=()=>{setGoal(latest.slice(0,12000));setTab('handoff')}
 const decide=(item:Proposal,accept:boolean)=>void run(async()=>{
   if(item.status==='needs_workspace'&&accept){
    if(!directory.trim())throw new Error('请填写存在的工作目录，或先进入工作会话绑定目录')
    const workspaces=await api<{items:Array<{id:string;session_id:number;revision:number}>}>('/v6/workspaces')
    const prior=workspaces.items.find(value=>value.session_id===item.work_session_id)
    await api(`/v6/workspaces/${prior?.id||'handoff_'+item.work_session_id}`,json('PUT',{session_id:item.work_session_id,path:directory.trim(),expected_revision:prior?.revision||0}))
   }
   const changed=await evolution.decide(sid,item,accept);await refresh();await refreshWorkSessions();await refreshTasks()
   if(changed.job_id){toast.success('工作任务已创建，尚未启动');setTab('')}
 })
 const change=(patch:Partial<OptionsView['settings']>)=>setView(old=>old?{...old,settings:{...old.settings,...patch}}:old)
 return <div className="evo-conversation-tools"><AutoRead mode={mode}/>
  {!panel&&<nav className="evo-toolbar" aria-label="会话工具"><Button variant="ghost" size="xs" disabled={!ready} onClick={()=>setTab(tab==='settings'?'':'settings')}><Settings2/>会话设置</Button><Button variant="ghost" size="xs" disabled={!ready} onClick={propose}><ListChecks/>转工作</Button><Button variant="ghost" size="xs" disabled={!ready} onClick={()=>setTab(tab==='commands'?'':'commands')}><Terminal/>指令</Button><Button variant="ghost" size="xs" disabled={!ready} onClick={()=>setTab(tab==='search'?'':'search')}>网页搜索</Button>{mode==='work'?<Button variant="ghost" size="xs" disabled={!ready} onClick={()=>setTab(tab==='docs'?'':'docs')}>项目资料</Button>:<Button variant="ghost" size="xs" disabled={!ready} onClick={()=>setTab(tab==='reminders'?'':'reminders')}>定时消息</Button>}<Button variant="ghost" size="xs" aria-label="导出当前会话为 Markdown" disabled={!ready||busy} onClick={()=>void run(async()=>downloadBlob(await apiBlob(`/v6/sessions/${sid}/export`),`零汐-会话-${sid}.md`))}><Download/></Button>{tab&&<Button variant="ghost" size="xs" aria-label="收起会话工具" onClick={()=>setTab('')}><X/></Button>}</nav>}
  {panel&&tab&&<div className="conversation-panel-heading"><span>会话工具</span><Button variant="ghost" size="xs" aria-label="收起会话工具" onClick={()=>setTab('')}><X/></Button></div>}
  {error&&<p className="evo-error" role="alert">{error}<Button size="xs" variant="ghost" onClick={()=>void run(async()=>{if(tab==='settings'){const [v,c]=await Promise.all([evolution.options(sid),api<{models:string[];groups:string[]}>(`/v6/sessions/${sid}/options/catalog`)]);setView(v);setChoices(c)}else await refresh()})}>重新读取</Button></p>}
  {tab==='commands'&&<div className="evo-inline-panel"><h3>聊天指令</h3><p>指令由本地程序处理；转工作仍需确认，不会自动启动。</p><div className="evo-command-list">{[['/work ','转工作'],['/skill ','选择技能'],['/model ','选择模型'],['/reset','新会话'],['/stop','停止生成'],['/help','查看帮助']].map(([text,label])=><Button key={text} size="sm" variant="outline" onClick={()=>{setDraft(mode,text);setTab('')}}>{text} <span>{label}</span></Button>)}</div></div>}
  {tab==='handoff'&&<form className="evo-inline-panel evo-form" onSubmit={e=>{e.preventDefault();void run(async()=>{await evolution.suggest(sid,goal);await refresh();setTab('')})}}><h3>把目标交给工作模块</h3><label>工作目标<Textarea required maxLength={12000} value={goal} onChange={e=>setGoal(e.target.value)}/></label><p>先建立建议卡片；确认拆分会调用模型 1 次，创建后等待你启动。</p><Button size="sm" disabled={busy||!goal.trim()}>建立工作建议</Button></form>}
  {tab==='settings'&&<div className="evo-inline-panel evo-form"><h3>本会话的配置</h3>{!view?<p>正在读取配置…</p>:<><label>文字模型 <small>当前来源：{sources[view.sources.model]||view.sources.model}</small><Select value={view.settings.model} onChange={e=>change({model:e.target.value,effort:'auto'})}><option value="">沿用预设 / 模块默认（{view.effective.model}）</option>{choices.models.map(id=><option key={id}>{id}</option>)}</Select><small>当前生效模型：{view.effective.resolved_model||view.effective.model}。更换模型后先保存，以刷新可用参数。</small></label><div className="evo-form-columns"><label>思考强度 <small>{sources[view.sources.effort]}</small><Select value={view.settings.effort} onChange={e=>change({effort:e.target.value})}><option value="auto">自动 / 沿用默认</option>{view.parameter_support.effort_levels.filter(v=>v!=='auto').map(v=><option key={v}>{v}</option>)}</Select></label><label>温度 <small>{sources[view.sources.temperature]}</small><Input type="number" min={0} max={2} step={.1} placeholder="沿用模块默认" disabled={!view.parameter_support.temperature} value={view.settings.temperature??''} onChange={e=>change({temperature:e.target.value===''?null:Number(e.target.value)})}/>{!view.parameter_support.temperature&&<small>当前模型不支持温度覆盖</small>}</label></div><fieldset><legend>工具组</legend><label className="evo-check"><input type="checkbox" checked={view.settings.tool_groups===null} onChange={e=>change({tool_groups:e.target.checked?null:[]})}/>沿用默认工具权限</label>{view.settings.tool_groups!==null&&<div className="evo-check-grid">{choices.groups.map(group=><label key={group}><input type="checkbox" checked={view.settings.tool_groups!.includes(group)} onChange={e=>change({tool_groups:e.target.checked?[...view.settings.tool_groups!,group]:view.settings.tool_groups!.filter(v=>v!==group)})}/>{group}</label>)}</div>}<small>选中工具组仍受模式、角色和逐项审批限制。</small></fieldset><label>朗读引擎<Select value={view.settings.voice_engine} onChange={e=>change({voice_engine:e.target.value})}><option value="">沿用语音模块</option><option value="cosyvoice2">CosyVoice2（默认推荐）</option><option value="indextts2.5">IndexTTS 2.5（建议独立显卡）</option><option value="gpt-sovits-v2">GPT-SoVITS（已有音色）</option></Select></label><label className="evo-toggle">自动朗读回复<Switch checked={view.settings.auto_read} onCheckedChange={value=>change({auto_read:value})}/></label><Button size="sm" disabled={busy} onClick={()=>void run(async()=>{setView(await evolution.saveOptions(sid,view.settings));window.dispatchEvent(new CustomEvent('lingxi-options-changed',{detail:sid}));toast.success('会话配置已保存')})}>保存会话配置</Button></>}</div>}
  {tab==='search'&&<SearchPanel sessionId={sid}/>} {tab==='docs'&&<ProjectDocsPanel sessionId={sid}/>} {tab==='reminders'&&<RemindersPanel sessionId={sid}/>}
  {active&&<section className="evo-proposal" aria-label="工作建议"><div><h3>{states[active.status]}</h3><strong>{active.goal}</strong><p>{active.why}</p><small>{active.cost_hint}</small>{active.error&&<p className="evo-error">{active.error}</p>}</div>{active.status==='needs_workspace'&&<label>工作目录<Input placeholder="存在的绝对目录" value={directory} onChange={e=>setDirectory(e.target.value)}/></label>}<div className="evo-actions"><Button variant="ghost" size="xs" disabled={busy||active.status==='planning'} onClick={()=>decide(active,false)}>暂不需要</Button><Button size="xs" disabled={busy||active.status==='planning'} onClick={()=>decide(active,true)}>{busy?'处理中…':active.status==='needs_workspace'?'绑定目录并继续（不再拆分）':active.status==='failed'?'确认重新拆分（1次调用）':'确认拆分并创建（1次调用）'}</Button></div></section>}
  {!active&&items[0]?.job_id&&<div className="evo-handoff-link"><span>工作任务已创建，需在任务页启动</span><Button size="xs" variant="ghost" onClick={()=>{if(items[0].work_session_id)setActiveWorkSessionId(items[0].work_session_id);navigate('/tasks?job='+items[0].job_id)}}>查看任务</Button></div>}
 </div>
}
