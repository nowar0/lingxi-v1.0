import {confirmDiscard,useUnsavedChanges} from './UnsavedChanges'
import { Select } from '@/components/ui/select'
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import * as PresetPopover from '@radix-ui/react-dialog'
import { useNavigate } from 'react-router-dom'
import { Bot, Check, ChevronDown, FileText, Plus, Settings2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, json } from '@/api/client'
import { useApp } from '@/context/AppContext'
import { useMessageTokens } from '@/context/SurfaceContext'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input, Textarea } from '@/components/ui/input'
import type { WorkPreset, WorkBinding } from '@/lib/workComposerSettings'
import './work-presets.css'

type Preset = WorkPreset & { temperature?: number | null }
type Catalog = { items: Preset[]; models: Record<string,string>; tools: string[];skills:Array<{id:string;name:string}>;parameters?:Record<string,{levels:string[];temperature:boolean}> }
const templates = [
  {id:'standard',name:'标准模式',brief:'围绕目标、事实与证据推进工作。按需读取资料和使用工具，修改后检查结果，明确尚未完成的事项。',note:'按任务需要使用文件、检索和工具。'},
  {id:'analysis',name:'分析模式',brief:'优先分析已有资料，给出证据、风险与建议。不主动修改文件或运行命令，先讨论并确认方案。',note:'适合代码阅读与资料分析，使用读取类工作工具。'},
  {id:'terminal',name:'终端模式',brief:'通过终端完成工作。每条命令遵守应用的预览与逐项确认，根据实际执行结果继续，不绕过审批。',note:'仅使用工作命令工具，执行仍需确认。'},
  {id:'dialogue',name:'讨论模式',brief:'通过对话梳理工作目标、约束与方案，不主动调用工作工具，不声称已执行操作。',note:'不分配工作工具，专注讨论、解释与计划。'},
]
type Template = typeof templates[number]
const make = (t:Template,tools:string[]):Preset => ({id:'',revision:0,name:t.name,brief:t.brief,model:'',effort:'auto',skill_ids:[],tools:t.id==='standard'?tools:t.id==='terminal'?tools.filter(n=>n==='workspace_command'):t.id==='analysis'?tools.filter(n=>['workspace_read','workspace_list','workspace_search','recall_history','read_document','search_project','load_skill'].includes(n)):[]})
const body = (p:Preset) => ({name:p.name,brief:p.brief,model:p.model,effort:p.effort,skill_ids:p.skill_ids,tools:p.tools,temperature:p.temperature??null,expected_revision:p.revision})
function useCatalog(connected:boolean){
  const [data,setData]=useState<Catalog>(),[error,setError]=useState(''),[version,setVersion]=useState(0)
  useEffect(()=>{const c=new AbortController();if(connected)void api<Catalog>('/v7/presets',{signal:c.signal}).then(d=>{if(!c.signal.aborted){setData(d);setError('')}}).catch(e=>{if(!c.signal.aborted)setError(e.message)});return()=>c.abort()},[connected,version])
  return {data,error,setError,refresh:()=>setVersion(n=>n+1)}
}
export function WorkPresetPicker({sessionId,disabled,onSelected,compact=false}:{sessionId?:number;disabled:boolean;onSelected:(binding?:WorkBinding)=>void;compact?:boolean}){
  const navigate=useNavigate(),{connection}=useApp(),{data,error,setError,refresh}=useCatalog(connection==='connected')
  const [binding,setBinding]=useState<WorkBinding>(),[busy,setBusy]=useState(false),[retry,setRetry]=useState(0)
  const scope=useRef(sessionId);scope.current=sessionId;const alive=useRef(true),lock=useRef(false)
  useEffect(()=>{alive.current=true;return()=>{alive.current=false}},[])
  useEffect(()=>{const c=new AbortController();setBinding(undefined);if(sessionId)void api<WorkBinding>(`/v7/sessions/${sessionId}/preset`,{signal:c.signal}).then(b=>{if(!c.signal.aborted)setBinding(b)}).catch(e=>{if(!c.signal.aborted)setError(e.message)});else setBinding({revision:0,preset:null});return()=>c.abort()},[sessionId,retry])
  const choose=async(p?:Preset,t?:Template)=>{
    if(disabled||lock.current||!data||!binding)return
    lock.current=true;setBusy(true);setError('');const captured=sessionId
    try{if(!sessionId)throw new Error('请先新建工作会话，再选择预设');const sid=sessionId;if(scope.current!==captured)return
      const built=t?make(t,data.tools):undefined
      const existing=built?data.items.find(value=>JSON.stringify({...body(value),expected_revision:0})===JSON.stringify(body(built))):undefined
      const selected=built?existing||await api<Preset>('/v7/presets',json('POST',body(built))):p
      if(!alive.current||captured&&scope.current!==captured)return
      const current=binding
      const saved=await api<WorkBinding>(`/v7/sessions/${sid}/preset`,json('PUT',{preset_id:selected?.id||'',expected_revision:current.revision}))
      window.dispatchEvent(new CustomEvent('lingxi-options-changed',{detail:sid}));toast.success('已切换预设',{position:'top-center'});if(alive.current)onSelected(saved)
    }catch(e){if(alive.current&&scope.current===captured)setError((e as Error).message)}finally{lock.current=false;if(alive.current)setBusy(false)}
  }
  const custom=data?.items.filter(p=>!compact||!templates.some(t=>p.name===t.name&&p.brief===t.brief)&&(!p.name.startsWith('工作区输入栏 · 会话 #')||p.id===binding?.preset?.id))||[]
  return <div className={`work-preset-picker${compact?' is-compact':''}`}>{!compact&&<p>切换当前工作会话的提示词与工具范围。</p>}{error&&<p role="alert">{error}<button onClick={()=>{refresh();setRetry(n=>n+1)}}>重新读取</button></p>}{!data?<p>正在读取预设…</p>:<>
    <button className="work-preset-choice" disabled={disabled||busy||!binding} aria-pressed={!binding?.preset} onClick={()=>void choose()}><span><strong>{compact?'默认模式':'沿用工作模块'}</strong><small>沿用工作模块设置</small></span>{!binding?.preset&&<Check/>}</button>
    {!compact&&<h3>内置模板</h3>}<div className="work-preset-choice-grid">{templates.map(t=>{const selected=binding?.preset?.name===t.name&&binding.preset.brief===t.brief;return <button className="work-preset-choice" key={t.id} disabled={disabled||busy||!binding} aria-pressed={selected} onClick={()=>void choose(undefined,t)}><span><strong>{t.name}</strong><small>{t.note}</small></span>{selected&&<Check/>}</button>})}</div>
    {!!custom.length&&<h3>已保存预设</h3>}{custom.map(p=><button key={p.id} className="work-preset-choice" disabled={disabled||busy||!binding} aria-pressed={binding?.preset?.id===p.id} onClick={()=>void choose(p)}><span><strong>{p.name.startsWith('工作区输入栏 · 会话 #')?'自定义模式':p.name}</strong><small>{p.brief||'未填写提示词'}</small></span>{binding?.preset?.id===p.id&&<Check/>}</button>)}{!compact&&!data.items.length&&<p>尚无自定义预设。</p>}
    <button className="work-preset-settings-link" onClick={()=>navigate('/settings?tab=work')}><Settings2/>{compact?'管理预设':'管理预设与提示词'}</button>{!compact&&<p>只作用于本会话。单独设置的模型优先；工具审批保持有效。</p>}
  </>}</div>
}
export function WorkPresetControl({sessionId,disabled}:{sessionId?:number;disabled:boolean}){
  const [open,setOpen]=useState(false),[binding,setBinding]=useState<WorkBinding>(),[version,setVersion]=useState(0),[position,setPosition]=useState<CSSProperties>({})
  const trigger=useRef<HTMLButtonElement>(null),tokens=useMessageTokens()
  useEffect(()=>{setOpen(false);setBinding(undefined)},[sessionId])
  useEffect(()=>{if(!sessionId||disabled)return;const c=new AbortController();void api<WorkBinding>(`/v7/sessions/${sessionId}/preset`,{signal:c.signal}).then(v=>{if(!c.signal.aborted)setBinding(v)}).catch(()=>{});return()=>c.abort()},[sessionId,disabled,version])
  useEffect(()=>{const changed=(event:Event)=>{const sid=(event as CustomEvent<number>).detail;if(!sid||sid===sessionId)setVersion(v=>v+1)};window.addEventListener('lingxi-options-changed',changed);return()=>window.removeEventListener('lingxi-options-changed',changed)},[sessionId])
  useLayoutEffect(()=>{if(!open)return;const place=()=>{const b=trigger.current?.getBoundingClientRect();if(!b)return;const width=Math.min(360,innerWidth-24),above=b.top-20,below=innerHeight-b.bottom-20,up=above>=Math.min(300,below);setPosition({width,left:Math.max(12,Math.min(innerWidth-width-12,b.left)),maxHeight:Math.max(120,Math.min(420,up?above:below)),...(up?{bottom:innerHeight-b.top+8}:{top:b.bottom+8})})};place();window.addEventListener('resize',place);return()=>window.removeEventListener('resize',place)},[open])
  const name=binding?.preset?.name||'默认模式',label=name.startsWith('工作区输入栏 · 会话 #')?'自定义模式':name
  return <PresetPopover.Root open={open} onOpenChange={setOpen} modal={false}><PresetPopover.Trigger asChild><button ref={trigger} type="button" className="work-agent-preset-trigger" aria-label={`Agent 预设：${label}`} disabled={disabled||!sessionId}><Bot/><span>{label}</span><ChevronDown/></button></PresetPopover.Trigger><PresetPopover.Portal><PresetPopover.Content className="work-agent-preset-menu" style={{...tokens,...position}} data-ui-surface="message" aria-describedby={undefined}><PresetPopover.Title className="sr-only">当前对话的 Agent 预设</PresetPopover.Title><WorkPresetPicker sessionId={sessionId} disabled={disabled} compact onSelected={next=>{if(next)setBinding(next);setOpen(false)}}/></PresetPopover.Content></PresetPopover.Portal></PresetPopover.Root>
}
export function WorkPresetSettings(){
  const {connection}=useApp(),connected=connection==='connected',{data,error,setError,refresh}=useCatalog(connected)
  const [draft,setDraft]=useState<Preset>(),[saving,setSaving]=useState(false),[removing,setRemoving]=useState<Preset>();const [initial,setInitial]=useState('');useEffect(()=>{if(draft&&!initial)setInitial(JSON.stringify(draft));if(!draft)setInitial('')},[draft,initial]);useUnsavedChanges(!!draft&&!!initial&&JSON.stringify(draft)!==initial);const lock=useRef(false),alive=useRef(true)
  useEffect(()=>{alive.current=true;return()=>{alive.current=false}},[])
  const save=async()=>{if(!draft||lock.current||!connected)return;lock.current=true;setSaving(true);setError('');try{await api(draft.id?`/v7/presets/${draft.id}`:'/v7/presets',json(draft.id?'PUT':'POST',body(draft)));if(alive.current){setDraft(undefined);refresh();toast.success('预设提示词已保存')}}catch(e){if(alive.current)setError((e as Error).message)}finally{lock.current=false;if(alive.current)setSaving(false)}}
  return <section className="work-preset-settings"><header><div><h2>工作模式 · Agent 预设</h2><p>管理提示词与工具范围，在工作输入框的预设气泡中切换。</p></div><Button variant="outline" disabled={!connected||!data} onClick={()=>setDraft({...make(templates[0],data?.tools||[]),name:'新预设'})}><Plus/>新建预设</Button></header>
    {error&&<p role="alert">{error}<Button size="xs" variant="ghost" onClick={refresh}>重新读取</Button></p>}
    {!connected?<p>连接本地服务后可编辑预设。</p>:!data?<p>正在读取预设…</p>:<><h3>内置模板</h3><div className="work-preset-cards">{templates.map(t=><article key={t.id}><header><h4>{t.name}</h4><span>模板</span></header><p>{t.note}</p><footer><button onClick={()=>setDraft(make(t,data.tools))}><FileText/>查看与编辑提示词</button></footer></article>)}</div><h3>自定义</h3><div className="work-preset-cards">{data.items.map(p=><article key={p.id}><header><h4>{p.name}</h4><span>已保存</span></header><p>{p.brief||'未填写提示词'}</p><footer><button onClick={()=>setDraft({...p})}><FileText/>编辑预设与提示词</button><button onClick={()=>setRemoving(p)}>删除…</button></footer></article>)}</div>{!data.items.length&&<p>使用模板创建，或新建自己的工作模式。</p>}<p>已绑定会话保留原快照；修改后需在预设气泡中重新选择才应用。</p></>}
    <Dialog open={!!removing} onOpenChange={v=>{if(!v&&!saving)setRemoving(undefined)}}><DialogContent><DialogHeader><DialogTitle>删除预设“{removing?.name}”？</DialogTitle><DialogDescription>已绑定会话保留原快照，不影响原有聊天。</DialogDescription></DialogHeader>{error&&<p role="alert">{error}</p>}<DialogFooter><Button variant="outline" onClick={()=>setRemoving(undefined)}>取消</Button><Button disabled={saving} onClick={()=>{if(!removing)return;setSaving(true);void api(`/v6/manage/presets/${removing.id}/delete`,json('POST',{expected_revision:removing.revision,confirmed:true})).then(()=>{setRemoving(undefined);refresh();toast.success('预设已删除')}).catch(e=>setError(e.message)).finally(()=>setSaving(false))}}>删除预设</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={!!draft} onOpenChange={open=>{if(!open&&!saving)void confirmDiscard().then(ok=>{if(ok)setDraft(undefined)})}}><DialogContent className="work-preset-editor"><DialogHeader><DialogTitle>{draft?.id?'编辑工作预设':'新建工作预设'}</DialogTitle><DialogDescription>提示词定义工作方式，工具权限和逐项审批仍由应用执行。</DialogDescription></DialogHeader>{draft&&<form onSubmit={e=>{e.preventDefault();void save()}}><label>预设名称<Input aria-label="预设名称" required maxLength={80} value={draft.name} disabled={saving} onChange={e=>setDraft({...draft,name:e.target.value})}/></label><label>工作提示词<Textarea aria-label="工作提示词" rows={8} maxLength={4000} value={draft.brief} disabled={saving} onChange={e=>setDraft({...draft,brief:e.target.value})}/></label><label>模型<Select aria-label="预设模型" value={draft.model} disabled={saving} onChange={e=>setDraft({...draft,model:e.target.value,effort:'auto',temperature:null})}><option value="">沿用工作模块</option>{draft.model&&!data?.models[draft.model]&&<option>{draft.model}</option>}{Object.entries(data?.models||{}).map(([key,value])=><option key={key} value={key}>{key} · {value}</option>)}</Select></label><section className="work-preset-editor-section"><h3>模型与技能</h3><label>思考强度<Select aria-label="预设思考强度" value={draft.effort} onChange={e=>setDraft({...draft,effort:e.target.value as Preset['effort']})}>{(data?.parameters?.[draft.model||'work']?.levels||['auto']).filter((v,i,all)=>all.indexOf(v)===i).map(v=><option key={v} value={v}>{v==='auto'?'沿用默认':v}</option>)}</Select></label><label>温度<Input aria-label="预设温度" disabled={data?.parameters?.[draft.model||'work']?.temperature===false&&draft.effort!=='none'} type="number" min={0} max={2} step={.1} placeholder="沿用默认" value={draft.temperature??''} onChange={e=>setDraft({...draft,temperature:e.target.value?Number(e.target.value):null})}/><Button type="button" size="xs" variant="ghost" onClick={()=>setDraft({...draft,temperature:null})}>沿用模型默认温度</Button></label><div className="work-preset-tool-list">{data?.skills.map(skill=><label key={skill.id}><input type="checkbox" checked={draft.skill_ids.includes(skill.id)} onChange={e=>setDraft({...draft,skill_ids:e.target.checked?[...draft.skill_ids,skill.id]:draft.skill_ids.filter(v=>v!==skill.id)})}/>{skill.name}</label>)}</div></section><section className="work-preset-editor-section"><h3>工具范围 · {draft.tools.length} 项</h3><div className="work-preset-tool-list">{data?.tools.map(tool=><label key={tool}><input type="checkbox" checked={draft.tools.includes(tool)} disabled={saving} onChange={e=>setDraft({...draft,tools:e.target.checked?[...draft.tools,tool]:draft.tools.filter(t=>t!==tool)})}/>{tool}</label>)}</div></section>{error&&<p role="alert">{error}</p>}<DialogFooter><Button type="button" variant="outline" disabled={saving} onClick={()=>void confirmDiscard().then(ok=>{if(ok)setDraft(undefined)})}>取消</Button><Button type="submit" disabled={saving||!connected||!draft.name.trim()}>{saving?'保存中…':'保存预设'}</Button></DialogFooter></form>}</DialogContent></Dialog>
  </section>
}
