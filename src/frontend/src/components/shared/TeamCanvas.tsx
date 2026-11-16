import { Select } from '@/components/ui/select'
import { TaskFlowCanvas } from './TaskFlowCanvas'
import { MemberUsage } from './MemberUsage'
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, apiBlob, json } from '@/api/client'
import type { TaskItem, TaskNode } from '@/types'
import { Button } from '@/components/ui/button'
import { downloadBlob } from '@/lib/utils'
import { Minus, Plus, Maximize2, Move, Save, Download, Copy, X, ArrowRight } from 'lucide-react'
import { toast } from 'sonner'
import './TeamCanvas.css'

type Definition = NonNullable<TaskNode['document']> & { id: string; title: string; task: string; depends_on: string[]; x: number; y: number; role: string; model: string; skill_id: string; backend: string; tools?: string[] | null }
type History = { seq: number; event: string; detail: string; at: string; node_id?: string | null }
type Options = { models: string[]; skills: { id: string; name: string; available: boolean }[]; tools: string[] }
type Point = {x:number;y:number}
type Draft = { nodes: Definition[]; revision: number; supervisor?:Point; entries?:string[] }
const initialSupervisor=(nodes:Definition[]):Point=>({x:nodes.length?(Math.min(...nodes.map(n=>n.x))+Math.max(...nodes.map(n=>n.x+232)))/2-92:40,y:nodes.length?Math.min(...nodes.map(n=>n.y))-122:28})
function freePosition(position:Point,nodes:Definition[],supervisor:Point):Point{
  let {x,y}=position
  const occupied=[...nodes.map(n=>({...n,width:232,height:134})),{...supervisor,width:184,height:76}]
  for(let step=0;step<50&&occupied.some(n=>x<n.x+n.width+20&&x+252>n.x&&y<n.y+n.height+20&&y+154>n.y);step++){if(y+170<=9700)y+=170;else{x=Math.min(9700,x+270);y=0}}
  return {x,y}
}
const complete = (status?: string) => status === 'complete' || status === 'completed'
const labels: Record<string, string> = { pending: '待执行', running: '执行中', complete: '已完成', completed: '已完成', failed: '失败', interrupted: '中断', needs_configuration: '待配置', paused: '已暂停', pausing: '正在暂停', review: '待验收', blocked: '需处理', cancelled: '已取消' }
const eventLabels: Record<string, string> = { member_input: '任务输入', member_output: '已保存成果', member_message: '成员消息', followup_user: '你的追问', followup_assistant: '成员回复', tool_started: '工具调用', tool_result: '工具返回', tool_rejected: '工具未执行', tool_reused: '复用已完成操作', tool_interrupted: '操作结果待核对', subteam_message: '子团队模型消息', subteam_event: '子团队事件', coordination_input: '调度输入', coordination_message: '调度消息', team_event: '团队事件', team_started: '主团队启动', team_failed: '主团队错误', team_incomplete: '仍有未完成节点', failed: '成员错误' }
function definitions(task: TaskItem, free=false): Definition[] {
  return (task.nodes || []).map((node, i) => ({
    id: node.id, title: node.id, task: '', depends_on: [], role: '', model: '', skill_id: '', backend: 'native',
    ...node.document, x: !node.document?.x && !node.document?.y && !(free&&task.events?.some(e=>e.event==='graph_saved'||e.event==='layout_saved')) ? 40 + (i % 3) * 270 : node.document?.x ?? 40, y: free?node.document?.y ?? 150 + Math.floor(i / 3) * 180:Math.max(140, node.document?.y || 150 + Math.floor(i / 3) * 180),
  }))
}
function initial(task: TaskItem): Draft | undefined {
  try { const value = JSON.parse(localStorage.getItem(`lingxi-team-draft-${task.id}`) || 'null'); return Array.isArray(value?.nodes) && typeof value.revision === 'number' ? value : undefined } catch { return undefined }
}
function createsCycle(nodes: Definition[], from: string, to: string): boolean {
  const visited = new Set<string>()
  const visit = (id: string): boolean => id === to || (!visited.has(id) && (visited.add(id), (nodes.find(n => n.id === id)?.depends_on || []).some(visit)))
  return from === to || visit(from)
}

export function TeamCanvas({ task, onChanged, onDirty, onBusy, compact=false, settingsHost, onConfigure, onCopied }: { onCopied?:(task:TaskItem)=>void;compact?:boolean;settingsHost?:HTMLElement|null;onConfigure?:()=>void;task: TaskItem; onChanged: () => Promise<void>; onDirty: (dirty: boolean) => void; onBusy: (busy: boolean) => void }) {
  const [restored] = useState(() => initial(task))
  const [nodes, setNodes] = useState<Definition[]>(() => restored?.nodes || definitions(task,compact))
  const [supervisor,setSupervisor]=useState<Point>(()=>restored?.supervisor||task.document?.supervisor_position||initialSupervisor(nodes))
  const [entries,setEntries]=useState<string[]>(()=>restored?.entries??task.document?.entry_nodes??nodes.filter(n=>!n.depends_on.length).map(n=>n.id))
  const [selected, setSelected] = useState(compact?task.nodes?.[0]?.id||'@supervisor':'')
  const [inspectorOpen,setInspectorOpen]=useState(false)
  const [dirty, setDirty] = useState(!!restored)
  const [baseRevision, setBaseRevision] = useState(restored?.revision ?? task.revision)
  const [source, setSource] = useState('')
  const [history, setHistory] = useState<History[]>([])
  const [historyError, setHistoryError] = useState('')
  const [question, setQuestion] = useState('')
  const [busy, setBusy] = useState(false)
  const actionLock=useRef(false)
  const [options, setOptions] = useState<Options>({ models: [], skills: [], tools: [] })
  const [error, setError] = useState('')
  const [zoom, setZoom] = useState(1)
  const [panMode, setPanMode] = useState(false)
  const [recordsOpen,setRecordsOpen]=useState(false)
  const scroll = useRef<HTMLDivElement>(null)
  const autoFit = useRef(true)
  const nodeGesture = useRef<{x:number;y:number;dragged:boolean}|undefined>(undefined)
  const drag = useRef<{ id: string; pointer: number; x: number; y: number; px: number; py: number } | null>(null)
  const editable = task.status === 'pending' && task.nodes?.every(n => n.status === 'pending')
  const current = nodes.find(n => n.id === selected)
  const liveNode = task.nodes?.find(n => n.id === selected)
  const active = ['running', 'pausing'].includes(task.status)
  const width = Math.max(820, ...nodes.map(n => n.x + 270))
  const height = Math.max(320, ...nodes.map(n => n.y + 155))
  const supervisorX = Math.max(40, Math.min(width - 250, (width - 210) / 2))
  const key = `lingxi-team-draft-${task.id}`
  useEffect(() => {
    const element = scroll.current
    if (!element) return
    const fit = () => { if (autoFit.current) setZoom(Math.max(.25, Math.min(1, (element.clientWidth - 24) / width))) }
    fit(); const observer = new ResizeObserver(fit); observer.observe(element)
    return () => observer.disconnect()
  }, [width])
  useEffect(() => { onDirty(dirty) }, [dirty, onDirty])
  useEffect(() => () => onDirty(false), [onDirty])
  useEffect(() => { onBusy(busy); return () => onBusy(false) }, [busy, onBusy])
  useEffect(() => {
    try { if (dirty) localStorage.setItem(key, JSON.stringify({ nodes, supervisor, entries, revision: baseRevision })); else localStorage.removeItem(key) } catch { setError('本地草稿未能缓存，请先保存到任务再离开。') }
  }, [nodes, supervisor, entries, dirty, baseRevision, key])
  useEffect(() => { if (!dirty && task.revision>=baseRevision) { const next=definitions(task,compact);setNodes(next);setEntries(task.document?.entry_nodes??next.filter(n=>!n.depends_on.length).map(n=>n.id));setSupervisor(task.document?.supervisor_position||initialSupervisor(next));setBaseRevision(task.revision) } }, [task.nodes, task.revision, dirty, compact, baseRevision])
  useEffect(() => { void api<Options>('/v6/team-options').then(setOptions).catch(e => setError(`成员配置目录读取失败：${(e as Error).message}`)) }, [])
  useEffect(() => {
    setHistory([]); setHistoryError(''); setQuestion('')
    if (selected === '@supervisor' || !task.nodes?.some(n => n.id === selected)) return
    let live = true
    const load = () => api<{ items: History[] }>(`/v6/jobs/${task.id}/members/${selected}`).then(value => { if (live) { setHistory(value.items); setHistoryError('') } }).catch(e => { if (live) setHistoryError(`记录读取失败：${(e as Error).message}`) })
    void load()
    const timer = window.setInterval(() => void load(), 2000)
    return () => { live = false; window.clearInterval(timer) }
  }, [task.id, selected, task.nodes?.some(n => n.id === selected)])
  useEffect(() => { const clear = () => { drag.current = null }; window.addEventListener('blur', clear); return () => window.removeEventListener('blur', clear) }, [])
  const updateNode = (id: string, patch: Partial<Definition>) => { setNodes(all => all.map(n => n.id === id ? { ...n, ...patch } : n)); setDirty(true); setError('') }
  const update = (patch: Partial<Definition>) => updateNode(selected, patch)
  const remove = (id=selected) => { if (!editable || busy || nodes.length <= 1 || !nodes.some(n=>n.id===id)) return; setNodes(all => all.filter(n => n.id !== id).map(n => ({ ...n, depends_on: n.depends_on.filter(v => v !== id) }))); setEntries(all=>all.filter(v=>v!==id)); setSelected('@supervisor'); setSource(''); setDirty(true) }
  const save = async () => {
    if(actionLock.current||!dirty)return
    const plan=(items:Definition[])=>JSON.stringify(items.map(n=>Object.fromEntries(Object.entries(n).filter(([k])=>k!=='x'&&k!=='y').sort(([a],[b])=>a.localeCompare(b)))).sort((a,b)=>String(a.id).localeCompare(String(b.id))))
    if(!editable&&(plan(nodes)!==plan(definitions(task,compact))||JSON.stringify([...entries].sort())!==JSON.stringify([...(task.document?.entry_nodes??definitions(task,compact).filter(n=>!n.depends_on.length).map(n=>n.id))].sort()))){setError('任务已开始，成员修改仍在草稿中；请复制草稿后编辑。');return}
    actionLock.current=true;setBusy(true);setError('')
    try {
      const value=await api<TaskItem>(`/v6/jobs/${task.id}/${editable?'graph':'layout'}`,json('PUT',editable?{expected_revision:baseRevision,nodes,entry_nodes:entries,supervisor_position:supervisor}:{expected_revision:baseRevision,positions:Object.fromEntries(nodes.map(n=>[n.id,[n.x,n.y]])),supervisor_position:supervisor}))
      const next=definitions(value,compact);setNodes(next);setEntries(value.document?.entry_nodes??next.filter(n=>!n.depends_on.length).map(n=>n.id));setSupervisor(value.document?.supervisor_position||initialSupervisor(next));setBaseRevision(value.revision);setDirty(false)
      toast.success(editable?'协作计划已保存':'画布布局已保存')
      try{await onChanged()}catch{setError('已保存，列表刷新失败；请稍后重新读取。')}
    }catch(e){setError((e as Error).message)}finally{actionLock.current=false;setBusy(false)}
  }
  const followup = async () => {
    const target = selected
    setBusy(true); setError('')
    try {
      const value = await api<{ items: History[] }>(`/v6/jobs/${task.id}/members/${target}/messages`, json('POST', { text: question.trim() }))
      if (target === selected) { setHistory(value.items); setQuestion('') }
      await onChanged()
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  const connect = (to: string, from=source) => {
    if(!editable||busy||!nodes.some(n=>n.id===to))return
    if(from==='@supervisor'){
      if(nodes.find(n=>n.id===to)?.depends_on.length){setError('请先断开这个智能体的成员前置连线，再接入主调度。');return}
      setEntries(all=>all.includes(to)?all:[...all,to]);setDirty(true);setError('');setSource('');return
    }
    if(!nodes.some(n=>n.id===from))return
    if (createsCycle(nodes, from, to)) { setError('这条依赖会形成循环，请选择其他成员。'); return }
    setEntries(all=>all.filter(id=>id!==to))
    if (!nodes.find(n => n.id === to)?.depends_on.includes(from)) updateNode(to, { depends_on: [...(nodes.find(n => n.id === to)?.depends_on || []), from] })
    setSource('')
  }
  const addMember=(position?:{x:number;y:number},copyId?:string)=>{
    if(!editable||busy||nodes.length>=20)return
    const copy=nodes.find(n=>n.id===copyId),id=`member_${crypto.randomUUID().slice(0,12)}`
    const {x,y}=freePosition({x:Math.min(9700,position?.x??(copy?copy.x+50:40+nodes.length%3*270)),y:Math.min(9700,position?.y??(copy?copy.y+50:150+Math.floor(nodes.length/3)*180))},nodes,supervisor)
    setNodes(all=>[...all,{...(copy||{task:'处理分配的任务',role:'',model:'',skill_id:'',backend:'native',depends_on:[]}),id,title:copy?`${copy.title} 副本`:'新智能体',depends_on:[],x,y}]);setSelected(id);setDirty(true);setError('')
  }
  const copyPlan=async(position?:Point)=>{
    if(actionLock.current)return
    actionLock.current=true;setBusy(true);setError('');let copied:TaskItem|undefined
    try{
      copied=await api<TaskItem>(`/v6/jobs/${task.id}/copy`,json('POST'))
      if(position||dirty){const next=dirty?nodes.map(n=>({...n})):definitions(copied,true);if(position&&next.length<20)next.push({id:`member_${crypto.randomUUID().slice(0,12)}`,title:'新智能体',task:'处理分配的任务',role:'',model:'',skill_id:'',backend:'native',depends_on:[],...freePosition(position,next,supervisor)});copied=await api<TaskItem>(`/v6/jobs/${copied.id}/graph`,json('PUT',{expected_revision:copied.revision,nodes:next,entry_nodes:entries.filter(id=>next.some(n=>n.id===id)),supervisor_position:supervisor}))}
      await onChanged();onCopied?.(copied);toast.success(position?'已复制并添加智能体':'已复制为新任务')
    }catch(e){if(copied){await onChanged().catch(()=>undefined);onCopied?.(copied)}setError((e as Error).message);toast.error('复制未完成',{description:(e as Error).message})}finally{actionLock.current=false;setBusy(false)}
  }
  const visibleHistory = (selected === '@supervisor' ? (task.events || []).filter(e => !e.node_id) as History[] : history).filter(e => eventLabels[e.event])
  const controls=<>    <header className="team-toolbar"><div><h3>协作画布</h3><p>{task.document?.engine === 'agno' ? 'Agno 主团队' : '标准调度'} · {nodes.length} 位成员 · {task.nodes?.filter(n => complete(n.status)).length || 0} 位完成</p></div><div className="team-actions">
      {editable ? <Button variant="outline" size="sm" disabled={busy || nodes.length >= 20} onClick={() => {
        const id = `member_${Date.now().toString(36)}`
        setNodes(all => [...all, { id, title: '新成员', task: '处理分配的任务', role: '', model: '', skill_id: '', backend: 'native', depends_on: [], x: 40 + all.length % 3 * 270, y: 150 + Math.floor(all.length / 3) * 180 }]); setSelected(id); setInspectorOpen(true); setDirty(true)
      }}><Plus />添加成员</Button> : <Button variant="outline" size="sm" disabled={busy} onClick={async () => { setBusy(true); try { await api(`/v6/jobs/${task.id}/copy`, json('POST')); await onChanged(); toast.success('已复制为待开始任务，请从任务列表打开后修改') } catch(e) { setError((e as Error).message) } finally { setBusy(false) } }}><Copy />复制计划</Button>}
      <Button size="sm" disabled={!dirty || busy} onClick={() => void save()}><Save />{busy ? '处理中…' : editable ? '保存编排' : '保存布局'}</Button>
    </div></header>
    <div className="team-canvas-tools"><div className="team-actions"><Button variant="ghost" size="icon" aria-label="缩小画布" disabled={zoom <= .25} onClick={() => { autoFit.current = false; setZoom(v => Math.max(.25, v - .1)) }}><Minus /></Button><output aria-label="画布缩放">{Math.round(zoom * 100)}%</output><Button variant="ghost" size="icon" aria-label="放大画布" disabled={zoom >= 1.6} onClick={() => { autoFit.current = false; setZoom(v => Math.min(1.6, v + .1)) }}><Plus /></Button><Button variant="ghost" size="sm" onClick={() => { autoFit.current = true; setZoom(Math.max(.25, Math.min(1, ((scroll.current?.clientWidth || 820) - 24) / width))); scroll.current?.scrollTo({ top: 0, left: 0 }) }}><Maximize2 />适应</Button><Button variant={panMode ? 'secondary' : 'ghost'} size="sm" aria-pressed={panMode} onClick={() => setPanMode(v => !v)}><Move />平移</Button></div><span className="team-legend"><i />调度关系 <i className="dependency" />前置成果 → 后续成员</span></div>
</>
  const advancedFields=current?<>
      <label>执行后端<Select aria-label="执行后端" disabled={!editable || busy} value={current.backend} onChange={e => update({ backend: e.target.value })}><option value="native">Lingxi 工作助手</option><option value="agentscope">AgentScope 分析与复核</option><option value="camel">CAMEL Workforce</option></Select></label>
      <label>工具范围<Select aria-label="工具范围" disabled={!editable || busy || (!!current.capability && current.capability !== 'text')} value={current.tools == null ? 'default' : current.tools.length ? 'custom' : 'none'} onChange={e => update({ tools: e.target.value === 'default' ? null : e.target.value === 'none' ? [] : options.tools.slice(0, 2) })}><option value="default">沿用工作工具</option><option value="none">禁止宿主工具</option><option value="custom">仅所选工具</option></Select></label>
      {current.tools && current.tools.length > 0 && <fieldset className="team-wide team-tool-list"><legend>分配工具</legend>{options.tools.map(name => <label key={name}><input type="checkbox" disabled={!editable || busy} checked={current.tools!.includes(name)} onChange={e => update({ tools: e.target.checked ? [...current.tools!, name] : current.tools!.filter(t => t !== name) })} />{name}</label>)}</fieldset>}
  </>:null
  const inspector=<aside className="team-inspector">{!compact&&<Button className="team-inspector-close" variant="ghost" size="icon" aria-label="关闭成员检查器" onClick={()=>setInspectorOpen(false)}><X/></Button>}<section>{compact?<details className="task-member-usage"><summary>成员用量</summary><MemberUsage key={task.id+':'+selected} jobId={task.id} nodeId={selected} revision={task.revision}/></details>:<MemberUsage key={task.id+':'+selected} jobId={task.id} nodeId={selected} revision={task.revision}/>}<h4>{current ? compact?current.title:`${current.title} · 成员配置` : '主调度'}</h4>{current ? <div className="team-fields">
      <label>名称<input maxLength={200} disabled={!editable || busy} value={current.title} onChange={e => update({ title: e.target.value })} /></label>
      <label>角色<input maxLength={200} disabled={!editable || busy} value={current.role} onChange={e => update({ role: e.target.value })} /></label>
      <label>模型<Select aria-label="模型" disabled={!editable || busy || (!!current.capability && current.capability !== 'text')} value={current.model} onChange={e => update({ model: e.target.value })}><option value="">沿用工作模型</option>{current.model && !options.models.includes(current.model) && <option value={current.model}>{current.model}</option>}{options.models.map(id => <option key={id}>{id}</option>)}</Select></label>
      <label>技能<Select aria-label="技能" disabled={!editable || busy} value={current.skill_id} onChange={e => update({ skill_id: e.target.value })}><option value="">不指定</option>{current.skill_id && !options.skills.some(s => s.id === current.skill_id) && <option value={current.skill_id}>{current.skill_id}（请检查可用性）</option>}{options.skills.map(s => <option key={s.id} value={s.id} disabled={!s.available}>{s.name}{s.available ? '' : '（未就绪）'}</option>)}</Select></label>
      {compact?<details className="team-wide task-member-advanced"><summary>工具与执行方式</summary><div className="team-fields">{advancedFields}</div></details>:advancedFields}
      <label className="team-wide">任务<textarea maxLength={20000} disabled={!editable || busy} value={current.task} onChange={e => update({ task: e.target.value })} /></label>
      <div className="team-wide team-dependencies"><span>前置成果</span>{entries.includes(current.id)&&<span>主调度{editable&&<button disabled={busy} aria-label="断开主调度入口" onClick={()=>{setEntries(all=>all.filter(id=>id!==current.id));setDirty(true)}}><X/></button>}</span>}{current.depends_on.length ? current.depends_on.map(id => <span key={id}>{nodes.find(n => n.id === id)?.title || id}{editable && <button disabled={busy} aria-label={`移除 ${nodes.find(n => n.id === id)?.title || id} 到 ${current.title} 的依赖`} onClick={() => update({ depends_on: current.depends_on.filter(d => d !== id) })}><X /></button>}</span>) : <small>无前置依赖</small>}</div>
      {editable&&!entries.includes(current.id)&&!current.depends_on.length&&<Button variant="outline" size="sm" disabled={busy} onClick={()=>connect(current.id,'@supervisor')}>接入主调度</Button>}{editable && <Button variant="outline" size="sm" disabled={busy || nodes.length <= 1} onClick={()=>remove()}>移除成员</Button>}
      {complete(liveNode?.status) && <Button variant="outline" size="sm" disabled={busy} onClick={async () => { try { downloadBlob(await apiBlob(`/v6/jobs/${task.id}/artifacts/${selected}`), `${current.title}.md`) } catch(e) { setError((e as Error).message) } }}><Download />下载成果</Button>}
      {liveNode?.error && <p className="team-wide team-error">{liveNode.error}</p>}
    </div> : compact?<div className="team-supervisor-info"><p>{task.document?.review_report || task.document?.goal}</p></div>:<div className="team-supervisor-info"><p>{task.document?.engine === 'agno' ? 'Agno 是唯一主团队调度；各成员按依赖领取任务，子团队按成员配置启动。' : '标准调度根据已保存依赖执行。'}</p><p>模型、Skill 和工具沿用零汐配置。成员之间传递已保存的前置成果。</p><p>{task.document?.progress_report || task.document?.review_report || '选择成员配置任务；执行记录来自实际运行。'}</p></div>}</section>
    </aside>
  const choose=(id:string)=>{setSelected(id);setInspectorOpen(true)}
  const historyContent=<section className="team-history-section"><h4>{selected === '@supervisor' ? '主调度记录' : '成员消息与工具记录'}</h4><div className="team-member-history" role="log" aria-label="实际成员记录">{visibleHistory.map(e => <details key={e.seq}><summary>{eventLabels[e.event]}<time>{e.at}</time></summary><pre>{e.detail}</pre></details>)}{!visibleHistory.length && <p>{task.nodes?.some(n => n.id === selected) || selected === '@supervisor' ? compact?'暂无执行记录':'尚无执行记录。运行后显示实际输入、输出与工具调用。' : '保存成员后开始记录。'}</p>}</div>{historyError && <p className="team-error">{historyError}</p>}{current && (!compact||complete(liveNode?.status)) && <><label className="team-question">针对成果继续对话<textarea disabled={busy || active || !complete(liveNode?.status)} maxLength={12000} value={question} onChange={e => setQuestion(e.target.value)} placeholder={active ? '任务停止后可以追问已完成成员' : '只解释与复核已有成果，不执行工具…'} /></label><Button size="sm" disabled={busy || dirty || active || !question.trim() || !complete(liveNode?.status)} onClick={() => void followup()}>{busy ? '处理中…' : '发送追问'}</Button></>}</section>
  return <>{compact&&settingsHost&&createPortal(<div className="task-canvas-settings" onKeyDown={e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();if(dirty&&!busy)void save()}}}><label className="task-settings-member-select">成员<Select aria-label="选择配置成员" value={selected} onChange={e=>choose(e.target.value)}><option value="@supervisor">主调度</option>{nodes.map(n=><option key={n.id} value={n.id}>{n.title}</option>)}</Select></label>{selected&&inspector}{error&&<p role="alert" className="team-error">{error}</p>}<div className="task-settings-save"><Button disabled={!dirty||busy} onClick={()=>void save()}><Save/>保存更改</Button></div></div>,settingsHost)}{compact?<section className="team-workspace team-workflow" aria-label="多智能体协作画布" onKeyDown={e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();e.stopPropagation();if(dirty&&!busy)void save()}}}>
    {dirty&&baseRevision!==task.revision&&<div className="flow-draft-conflict" role="status"><span>任务已更新，草稿已保留。</span><button type="button" disabled={busy} onClick={()=>void copyPlan()}>复制草稿</button><button type="button" disabled={busy} onClick={()=>{const next=definitions(task,compact);setNodes(next);setEntries(task.document?.entry_nodes??next.filter(n=>!n.depends_on.length).map(n=>n.id));setSupervisor(task.document?.supervisor_position||initialSupervisor(next));setBaseRevision(task.revision);setDirty(false);setError('')}}>放弃草稿并更新</button>{!editable&&<button type="button" disabled={busy} onClick={()=>setBaseRevision(task.revision)}>保留位置并更新</button>}</div>}
    {error&&<div className="flow-state-notice" role="alert">{error}</div>}{editable&&nodes.some(n=>!n.depends_on.length&&!entries.includes(n.id))&&<p className="flow-state-notice" role="status">有智能体尚未连接。请从主调度或其他智能体的输出端口拖线，接入全部成员后再开始。</p>}
    <TaskFlowCanvas entries={entries} id={task.id} root={supervisor} onRootMove={point=>{setSupervisor(point);setDirty(true);setError('')}} nodes={nodes} selected={selected} states={Object.fromEntries((task.nodes||[]).map(n=>[n.id,n.status]))} editable={!!editable} busy={busy} dirty={dirty} onPick={setSelected} onOpen={id=>{setSelected(id);onConfigure?.()}} onMove={(id,x,y)=>updateNode(id,{x,y})} onConnect={(from,to)=>connect(to,from)} onDisconnect={(from,to)=>{if(from==='@supervisor'){if(editable&&!busy){setEntries(all=>all.filter(id=>id!==to));setDirty(true)}return}if(editable&&!busy)updateNode(to,{depends_on:(nodes.find(n=>n.id===to)?.depends_on||[]).filter(v=>v!==from)})}} onAdd={position=>addMember(position)} onCopy={id=>addMember(undefined,id)} onRemove={remove} onSave={()=>void save()} onCopyPlan={position=>void copyPlan(position)}/>
    <div className={`flow-records${recordsOpen?' is-open':''}`}><div className="flow-records-bar"><button type="button" aria-expanded={recordsOpen} onClick={()=>setRecordsOpen(v=>!v)}>执行记录<span>{visibleHistory.length}</span></button>{recordsOpen&&<Button variant="ghost" size="icon" aria-label="收起执行记录" onClick={()=>setRecordsOpen(false)}><X/></Button>}</div>{recordsOpen&&<div className="flow-records-content">{historyContent}</div>}</div>
  </section>:<section className="team-workspace" aria-label="多智能体协作画布" onKeyDown={e => {
    if (e.key === 'Escape') { setSource(''); drag.current = null }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); if (dirty && !busy) void save() }
  }}>
    {compact ? <div className="team-toolbar"><div><p>{nodes.length} 位成员 · {task.nodes?.filter(n=>complete(n.status)).length||0} 位完成</p></div>{dirty&&<Button size="sm" disabled={busy} onClick={()=>void save()}><Save/>保存</Button>}</div> : controls}
    {(!compact||dirty||source)&&<p className="team-hint" aria-live="polite">{dirty ? '有未保存修改；保存后才能开始。草稿会在本机保留。' : source ? `从「${nodes.find(n => n.id === source)?.title}」连出，选择接收成员。Esc 取消。` : editable ? '拖动成员，或选中后用方向键移动；Ctrl+S 保存。' : active ? '暂停会等待正在进行的操作结束；此时可以调整布局，执行计划已锁定。' : '可调整并保存布局；修改执行计划请复制为新任务，已有成果保留。'}</p>}
    {dirty && baseRevision !== task.revision && <div className="team-notice" role="status">任务版本已更新，草稿保留。<button onClick={() => { setNodes(definitions(task,compact)); setBaseRevision(task.revision); setDirty(false); setError('') }}>采用服务器版本</button>{!editable && <button onClick={() => setBaseRevision(task.revision)}>保留位置并更新版本</button>}</div>}
    {dirty && !editable && nodes.some(n => JSON.stringify({ ...n, x: 0, y: 0 }) !== JSON.stringify({ ...definitions(task,compact).find(t => t.id === n.id), x: 0, y: 0 })) && <div className="team-notice">此草稿含执行计划修改。运行后仅保存位置，成员配置以已开始的任务为准。</div>}
    {error && <p role="alert" className="team-error">{error}</p>}
    <div className="team-stage"><div ref={scroll} className={`team-canvas-scroll ${panMode ? 'is-panning' : ''}`} tabIndex={0} aria-label="协作画布，滚动或使用平移模式移动视图" onPointerDown={e => {
      if (!panMode || e.button !== 0 || !scroll.current) return
      drag.current = { id: '@pan', pointer: e.pointerId, x: scroll.current.scrollLeft, y: scroll.current.scrollTop, px: e.clientX, py: e.clientY }; e.currentTarget.setPointerCapture(e.pointerId)
    }} onPointerMove={e => {
      const d = drag.current
      if (!d || d.pointer !== e.pointerId) return
      if(nodeGesture.current&&Math.hypot(e.clientX-nodeGesture.current.x,e.clientY-nodeGesture.current.y)>4)nodeGesture.current.dragged=true
      if (d.id === '@pan' && scroll.current) { scroll.current.scrollLeft = d.x - (e.clientX - d.px); scroll.current.scrollTop = d.y - (e.clientY - d.py); return }
      updateNode(d.id, { x: Math.min(9700, Math.max(0, Math.round(d.x + (e.clientX - d.px) / zoom))), y: Math.min(9700, Math.max(140, Math.round(d.y + (e.clientY - d.py) / zoom))) })
    }} onPointerUp={() => { drag.current = null }} onPointerCancel={() => { drag.current = null }} onLostPointerCapture={() => { drag.current = null }}>
      <div className="team-scaled-frame" style={{ width: width * zoom, height: height * zoom }}><div className="team-canvas" style={{ width, height, transform: `scale(${zoom})` }}>
        <button disabled={busy} className={`team-supervisor ${selected === '@supervisor' ? 'is-selected' : ''}`} style={{ left: supervisorX }} onClick={() => {setSelected('@supervisor');setInspectorOpen(true);onConfigure?.()}} aria-pressed={selected === '@supervisor'}><strong>{task.document?.engine === 'agno' ? 'Agno · 主调度' : '主调度'}</strong><span>{labels[task.status] || task.status}</span>{!compact&&<small>分派任务 · 汇总成果</small>}</button>
        <svg className="team-connections" width="100%" height="100%" aria-label="成员依赖关系"><defs><marker id={`arrow-${task.id}`} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="currentColor" /></marker></defs>
          {nodes.filter(n=>entries.includes(n.id)).map(n => <path key={`supervisor-${n.id}`} className={`team-dispatch state-${task.nodes?.find(t => t.id === n.id)?.status || 'pending'}`} d={`M${supervisorX + 105},96 C${supervisorX + 105},120 ${n.x + 105},115 ${n.x + 105},${n.y}`}><title>主调度分派给 {n.title}</title></path>)}
          {nodes.flatMap(n => n.depends_on.map(id => { const from = nodes.find(x => x.id === id); const done = complete(task.nodes?.find(x => x.id === id)?.status); return from ? <path key={`${id}-${n.id}`} className={`team-dependency ${done ? 'is-ready' : ''} ${n.id === selected || id === selected ? 'is-related' : ''}`} d={`M${from.x + 210},${from.y + 48} C${from.x + 265},${from.y + 48} ${n.x - 55},${n.y + 48} ${n.x},${n.y + 48}`} markerEnd={`url(#arrow-${task.id})`}><title>{from.title} 的成果 → {n.title}{done ? '，前置成果已就绪' : '，等待前置成果'}</title></path> : null }))}
        </svg>
        {nodes.map(n => { const state = task.nodes?.find(x => x.id === n.id)?.status || 'pending'; return <article key={n.id} className={`team-member state-${state} ${selected === n.id ? 'is-selected' : ''}`} style={{ left: n.x, top: Math.max(140, n.y) }}>
          <button className="team-member-title" disabled={busy} aria-label={`选择成员 ${n.title}`} aria-pressed={selected === n.id} onClick={() => {if(nodeGesture.current?.dragged){nodeGesture.current=undefined;return}setSelected(n.id);setInspectorOpen(true);onConfigure?.()}} onPointerDown={e => {
            if (busy || panMode || e.button !== 0 || drag.current) return
            nodeGesture.current={x:e.clientX,y:e.clientY,dragged:false};setSelected(n.id); if(!compact)setInspectorOpen(true); drag.current = { id: n.id, pointer: e.pointerId, x: n.x, y: Math.max(140, n.y), px: e.clientX, py: e.clientY }; e.currentTarget.setPointerCapture(e.pointerId)
          }} onKeyDown={e => { if (['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)) { e.preventDefault(); const step = e.shiftKey ? 40 : 10; updateNode(n.id, { x: Math.min(9700, Math.max(0, n.x + (e.key === 'ArrowRight' ? step : e.key === 'ArrowLeft' ? -step : 0))), y: Math.min(9700, Math.max(140, n.y + (e.key === 'ArrowDown' ? step : e.key === 'ArrowUp' ? -step : 0))) }) } if (e.key === 'Delete') remove() }}>
            <strong>{n.title}</strong><span>{labels[state] || state}</span><small>{n.role || (n.backend === 'agentscope' ? '分析与复核' : n.backend === 'camel' ? '复杂任务团队' : '工作成员')}</small><small className="team-node-model">{n.backend === 'native' ? 'Lingxi' : n.backend === 'camel' ? 'CAMEL' : 'AgentScope'} · {n.model || '默认模型'}</small>
          </button>
          {editable && <div className="team-member-ports"><button disabled={busy} onClick={() => { setSource(source === n.id ? '' : n.id); setError('') }} aria-pressed={source === n.id} aria-label={`从 ${n.title} 连出`}>连出<ArrowRight /></button><button disabled={busy || !source || source === n.id} onClick={() => connect(n.id)} aria-label={`${n.title} 接收依赖`}>接收</button></div>}
        </article> })}
      </div></div>
    </div>
    {!compact && selected && inspectorOpen && inspector}
</div>
    <details className="team-run-log"><summary>{compact?'执行记录':'执行日志与成员追问'} · {visibleHistory.length}</summary>{historyContent}
    </details>
  </section>}</>
}

