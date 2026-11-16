import { Select } from '@/components/ui/select'
import { TaskAcceptancePanel } from '@/components/shared/TaskAcceptancePanel'
import { TaskSettingsDrawer } from '@/components/shared/TaskSettingsDrawer'
import './task-detail.css'
import { TaskQualityPanel } from '@/components/shared/TaskQualityPanel'
import { TeamCanvas } from '@/components/shared/TeamCanvas'
import { downloadBlob } from '@/lib/utils'
import { apiBlob } from '@/api/client'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Check, ChevronRight, Circle, CircleDashed, ListChecks, LoaderCircle, PanelLeftClose, PanelLeftOpen, Pause, Play, Plus, RotateCcw, Settings2, Square, X } from 'lucide-react'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
import { lingxi } from '@/api/lingxi'
import { api, json } from '@/api/client'
import type { TaskItem } from '@/types'
import { cn } from '@/lib/utils'
import { PageHeader } from '@/components/shared/PageHeader'
import { Tooltip } from '@/components/ui/tooltip'
import { DataModeNotice } from '@/components/shared/DataModeNotice'
import { SearchInput } from '@/components/shared/SearchInput'
import { EmptyState } from '@/components/shared/EmptyState'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/input'
import './task-workflow.css'

const statusMap: Record<string, { label: string; tone: 'default' | 'success' | 'warning' | 'danger' | 'secondary' }> = {
  running: { label: '正在执行', tone: 'default' }, generating: { label: '正在执行', tone: 'default' }, completed: { label: '已完成', tone: 'success' },
  waiting: { label: '等待处理', tone: 'warning' }, pending: { label: '待开始', tone: 'secondary' }, paused: { label: '已暂停', tone: 'secondary' },
  blocked: { label: '需要处理', tone: 'warning' }, pausing: { label: '等待暂停', tone: 'warning' }, review: { label: '待验收', tone: 'warning' }, interrupted: { label: '已中断', tone: 'danger' }, needs_configuration: { label: '待配置', tone: 'warning' },
  failed: { label: '执行失败', tone: 'danger' }, cancelled: { label: '已取消', tone: 'secondary' },
}

function teamEventLabel(event: NonNullable<TaskItem['events']>[number]) {
  if (event.event === 'team_started') return '团队开始工作'
  if (event.event === 'team_failed') return '团队执行失败，请检查模型连接'
  if (event.event === 'team_incomplete') return '仍有节点未完成，请查看节点结果'
  try {
    const detail = JSON.parse(event.detail || '{}') as { kind?: string; engine?: string }
    const labels: Record<string, string> = {
      TeamRunStarted: '主团队开始调度', TeamRunCompleted: '主团队完成汇总', TeamRunError: '主团队遇到错误',
      TeamToolCallStarted: '主团队分派任务', TeamToolCallCompleted: '主团队收到执行结果',
      RunStarted: '执行成员开始工作', RunCompleted: '执行成员完成工作',
      ToolCallStarted: '执行任务节点', ToolCallCompleted: '节点结果已返回',
      analyst_started: '分析成员开始工作', reviewer_started: '复核成员开始工作',
      workforce_started: '复杂任务团队开始工作', task_created: '创建子任务', task_decomposed: '完成任务拆解',
      task_completed: '子任务完成', TaskAssignedEvent: '子任务已分派', TaskStartedEvent: '子任务开始执行',
      AllTasksCompletedEvent: '全部子任务已完成', WorkerCreatedEvent: '执行成员已加入', failed: '子团队执行失败',
    }
    return labels[detail.kind || ''] || ''
  } catch { return '' }
}

export function TasksPage() {
  const { tasks, connection, activeWorkSessionId, ensureSession, refreshTasks } = useApp()
  const [searchParams] = useSearchParams()
  const requestedJob = searchParams.get('job')
  const [detailOpen,setDetailOpen]=useState(Boolean(requestedJob))
  const [view,setView]=useState<'process'|'canvas'|'review'>('canvas')
  const [settingsOpen,setSettingsOpen]=useState(false)
  const [settingsTab,setSettingsTab]=useState<'member'|'task'>('member')
  const [settingsHost,setSettingsHost]=useState<HTMLDivElement|null>(null)
  const [listWidth,setListWidth]=useState(280)
  const [listOpen,setListOpen]=useState(()=>{try{const value=localStorage.getItem('lingxi-task-list-open');if(value!==null)return value==='true'}catch{}return true})
  useEffect(()=>{try{localStorage.setItem('lingxi-task-list-open',String(listOpen))}catch{}},[listOpen])
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [selectedId, setSelectedId] = useState(()=>requestedJob||tasks[0]?.id)
  const [focusedNodeId, setFocusedNodeId] = useState<string>()
  const nodeRefs = useRef<Record<string, HTMLDivElement | null>>({})
  const [localTasks, setLocalTasks] = useState<TaskItem[]>([])
  const [overrides, setOverrides] = useState<Record<string, string>>({})
  const [open, setOpen] = useState(false)
  const [goal, setGoal] = useState('')
  const [engine, setEngine] = useState('agno')
  const [backend, setBackend] = useState('native')
  const [detail, setDetail] = useState<TaskItem>()
  const [busy, setBusy] = useState(false)
  const [graphDirty, setGraphDirty] = useState(false)
  const [canvasBusy, setCanvasBusy] = useState(false)
  const [lastGood, setLastGood] = useState(0)
  const [pollError, setPollError] = useState(false)
  const run = async (task: () => Promise<void>) => {
    if (busy || canvasBusy || connection !== 'connected') return
    setBusy(true)
    try { await task() } catch (error) { toast.error('任务操作未完成', { description: (error as Error).message }) }
    finally { setBusy(false) }
  }
  const allTasks = useMemo(() => [...localTasks, ...tasks].map((task) => overrides[task.id] ? { ...task, status: overrides[task.id] } : task), [tasks, localTasks, overrides])
  const filtered = allTasks.filter((task) => (filter === 'all' || (filter === 'waiting' ? ['pending','paused','blocked','review','pausing'].includes(task.status) : task.status === filter)) && `${task.title}${task.document?.goal}`.toLowerCase().includes(query.toLowerCase()))
  const listed = filtered.find((task) => task.id === selectedId) || (requestedJob ? undefined : filtered[0])
  const selected = detail?.id === listed?.id ? detail : listed
  useEffect(() => {
    if (!requestedJob) return
    setSelectedId(requestedJob)
    setDetail(undefined)
    setQuery('')
    setFilter('all')
    setDetailOpen(true)
  }, [requestedJob])
  useEffect(() => {
    if (view !== 'process' || !focusedNodeId || !selected) return
    const frame = requestAnimationFrame(() => {
      const target = nodeRefs.current[focusedNodeId]
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'center' })
        target.focus({ preventScroll: true })
        setFocusedNodeId(undefined)
      }
    })
    return () => cancelAnimationFrame(frame)
  }, [view, focusedNodeId, selected])
  useEffect(()=>{
    if(connection!=='connected')return
    let reading=false,disposed=false
    const refresh=async()=>{if(reading||disposed)return;reading=true;try{await refreshTasks()}catch{if(!disposed)setPollError(true)}finally{reading=false}}
    void refresh();const timer=window.setInterval(()=>void refresh(),2000)
    return()=>{disposed=true;window.clearInterval(timer)}
  },[connection,refreshTasks])
  useEffect(() => {
    if (!listed?.id || connection !== 'connected') return
    let disposed = false, reading=false
    const update = async () => {
      if(reading)return;reading=true
      try { const value = await lingxi.task(listed.id); if (!disposed) { setDetail(old => old?.id === value.id && old.revision > value.revision ? old : value); setLastGood(Date.now()); setPollError(false) } }
      catch { if (!disposed) setPollError(true) }
      finally{reading=false}
    }
    void update()
    const timer = window.setInterval(() => { void update() }, 2000)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [listed?.id, connection, refreshTasks])
  const action = async (task: TaskItem, actionName: string, nodeId?: string) => {
    if (connection === 'demo') { setOverrides((items) => ({ ...items, [task.id]: actionName === 'start' ? 'running' : actionName === 'pause' ? 'paused' : actionName === 'cancel' ? 'cancelled' : task.status })); toast.info('演示状态已更新，未执行真实任务'); return }
    try { setDetail(await lingxi.taskAction(task, actionName, nodeId)); await refreshTasks(); toast.success('任务状态已更新') } catch (error) { toast.error('任务操作失败', { description: (error as Error).message }) }
  }
  const create = async () => {
    if (!goal.trim()) return
    const sessionId = await ensureSession('work')
    const payload = { engine, session_id: sessionId, goal, entry_nodes: [], materials: '', acceptance: '由用户确认结果', nodes: [{ id: 'main', title: '执行任务', task: goal, backend, depends_on: [], skill_id: '', capability: 'text', difficulty: 1, attachment_id: '' }] }
    if (connection === 'demo') {
      const task: TaskItem = { id: `demo-${Date.now()}`, session_id: activeWorkSessionId, title: goal.slice(0, 24), status: 'pending', revision: 0, document: { goal }, nodes: [{ id: 'main', status: 'pending', document: { title: '执行任务', task: goal } }] }
      setLocalTasks((items) => [task, ...items]); setSelectedId(task.id)
    } else { const created = await api<TaskItem>('/v6/jobs', json('POST', payload)); setQuery('');setFilter('all');setSelectedId(created.id); await refreshTasks() }
    setOpen(false); setGoal(''); toast.success('任务已创建', { description: '可以开始执行。' })
  }
  const listToggle = <Tooltip content={listOpen ? "收起任务列表" : "展开任务列表"}><Button variant="ghost" size="icon" className="task-list-toggle size-8" aria-label={listOpen ? "隐藏任务列表" : "显示任务列表"} aria-controls="task-list-panel" aria-expanded={listOpen} onClick={() => setListOpen(value => !value)}>{listOpen ? <PanelLeftClose /> : <PanelLeftOpen />}</Button></Tooltip>
  return <div className="page-container chrome-page task-page"><DataModeNotice /><PageHeader title="任务与执行" actions={<Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button disabled={connection !== 'connected'}><Plus />新建任务</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>创建工作任务</DialogTitle><DialogDescription>任务创建后保持待开始，执行与写入仍需你的操作和审批。</DialogDescription></DialogHeader><div className="grid grid-cols-2 gap-3"><label className="text-xs">任务调度<Select aria-label="任务调度" className="mt-2 w-full rounded-md border border-border bg-background p-2" value={engine} onChange={(event) => setEngine(event.target.value)}><option value="native">标准任务</option><option value="agno">Agno 团队</option></Select></label><label className="text-xs">执行方式<Select aria-label="执行方式" className="mt-2 w-full rounded-md border border-border bg-background p-2" value={backend} onChange={(event) => setBackend(event.target.value)}><option value="native">工作助手</option><option value="agentscope">AgentScope 分析与复核</option><option value="camel">CAMEL 复杂任务团队</option></Select></label></div><Textarea placeholder="描述要完成的目标…" value={goal} onChange={(event) => setGoal(event.target.value)} /><DialogFooter><Button variant="outline" disabled={busy || !goal.trim()} onClick={() => void run(async () => { const sid = await ensureSession('work'); const created = await api<TaskItem>('/v6/jobs-plan', json('POST', { engine, backend, session_id: sid, goal, materials: '', acceptance: '由用户确认结果' })); setQuery('');setFilter('all');setSelectedId(created.id); await refreshTasks(); setOpen(false); setGoal(''); toast.success('任务已拆分，尚未开始执行') })}>AI 拆分任务（调用模型）</Button><Button disabled={busy || !goal.trim()} onClick={() => void run(create)}>创建任务</Button></DialogFooter></DialogContent></Dialog>} />
    <div className={cn("task-layout",detailOpen && "has-task-selection",!listOpen && "is-list-collapsed")} style={{"--task-list-width":`${listWidth}px`} as React.CSSProperties}>{!listOpen && <div className="task-list-reopen">{listToggle}</div>}<Card id="task-list-panel" className="task-list-panel"><div className="border-b border-border/60 p-3"><div className="task-list-search-row">{listOpen && listToggle}<SearchInput placeholder="搜索任务" value={query} onChange={(event) => setQuery(event.target.value)} /></div><Tabs value={filter} onValueChange={setFilter} className="mt-3"><TabsList className="grid w-full grid-cols-4"><TabsTrigger value="all">全部</TabsTrigger><TabsTrigger value="running">执行中</TabsTrigger><TabsTrigger value="waiting">等待</TabsTrigger><TabsTrigger value="completed">完成</TabsTrigger></TabsList></Tabs></div><div className="task-list-scroll">{filtered.map((task) => { const status = statusMap[task.status] || { label: task.status, tone: 'secondary' as const }; return <button key={task.id} className={cn('task-list-item', selected?.id === task.id && 'is-active')} onClick={() => {setSelectedId(task.id);setDetailOpen(true)}}><div className="flex items-start justify-between gap-2"><span className="line-clamp-1 text-xs font-semibold">{task.title || task.document?.goal || '未命名任务'}</span><Badge variant={status.tone}>{status.label}</Badge></div><p className="mt-2 line-clamp-2 text-xs leading-4 text-muted-foreground">{task.document?.goal || '暂无目标说明'}</p><div className="mt-3 flex items-center justify-between text-xs text-muted-foreground"><span>{task.nodes?.length || 0} 个节点</span><span>查看详情<ChevronRight className="ml-1 inline size-3" /></span></div></button>})}{!filtered.length && <EmptyState icon={ListChecks} title={allTasks.length?"没有匹配的任务":"还没有任务"} description={allTasks.length?"尝试调整搜索词或状态筛选。":"创建一个任务，开始安排工作。"} />}</div><div role="separator" aria-label="调整任务列表宽度" aria-orientation="vertical" tabIndex={0} className="task-pane-resize" onKeyDown={e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();setListWidth(v=>Math.min(360,Math.max(260,v+(e.key==='ArrowRight'?16:-16))))}}} onPointerDown={e=>{e.currentTarget.setPointerCapture(e.pointerId);e.currentTarget.dataset.drag=String(e.clientX);e.currentTarget.dataset.width=String(listWidth)}} onPointerMove={e=>{if(e.currentTarget.hasPointerCapture(e.pointerId))setListWidth(Math.min(360,Math.max(260,Number(e.currentTarget.dataset.width)+e.clientX-Number(e.currentTarget.dataset.drag))))}} onPointerUp={e=>{if(e.currentTarget.hasPointerCapture(e.pointerId))e.currentTarget.releasePointerCapture(e.pointerId)}} /></Card>
      <Card className={cn("task-detail-panel",view==='canvas' && "is-canvas-view",view==='review' && "is-review-view")}>{selected ? <><div className="task-detail-header"><Button className="task-back" variant="ghost" size="sm" onClick={()=>setDetailOpen(false)}>返回任务列表</Button><div><div className="mb-2 flex items-center gap-2"><Badge variant={(statusMap[selected.status] || {}).tone || 'secondary'}>{(statusMap[selected.status] || {}).label || selected.status}</Badge><span className="text-xs text-muted-foreground">{selected.nodes?.length || 0} 位成员</span></div><h2 className="text-lg font-semibold tracking-tight">{selected.title || '任务详情'}</h2>{selected.document?.goal !== selected.title && <p className="mt-2 max-w-2xl text-xs leading-5 text-muted-foreground">{selected.document?.goal}</p>}</div><div className="task-detail-actions"><Button variant="ghost" size="sm" aria-label="打开任务设置" onClick={()=>setSettingsOpen(true)}><Settings2/>设置</Button>{['paused','blocked','pausing','pending','review'].includes(selected.status) && <><Button variant="ghost" size="sm" disabled={busy || canvasBusy} onClick={() => void run(() => action(selected, 'cancel'))}>取消任务</Button></>}{selected.status === 'running' ? <><Button variant="outline" size="sm" disabled={busy} onClick={() => void run(() => action(selected, 'pause'))}><Pause />暂停</Button><Button variant="outline" size="sm" disabled={busy} onClick={() => void run(() => action(selected, 'cancel'))}><Square />取消</Button></> : ['pending','paused','blocked','review'].includes(selected.status) && <Button size="sm" disabled={busy || canvasBusy || graphDirty || pollError || !['pending', 'paused', 'blocked', 'review'].includes(selected.status)} onClick={() => selected.status === 'review' ? setView('review') : void run(() => action(selected, 'start'))}><Play />{selected.status === 'review' ? '验收' : selected.status === 'pending' ? '开始' : '恢复执行'}</Button>}</div></div>
        {pollError && <p className="team-notice" role="status">状态刷新失败；最后更新 {lastGood ? new Date(lastGood).toLocaleTimeString() : '尚未成功'}。连接恢复后自动刷新，草稿保留。</p>}

        {view!=='canvas' && (graphDirty || canvasBusy) && <p className="team-notice" role="status">{canvasBusy ? '成员操作进行中，完成后可继续任务操作。' : '画布有未保存修改，请回到协作画布保存后再开始。'}</p>}<div className="task-view-switch"><button aria-pressed={view==='canvas'} onClick={()=>setView('canvas')}>协作画布</button><button aria-pressed={view==='process'} onClick={()=>setView('process')}>进度与成果</button><button aria-pressed={view==='review'} onClick={()=>setView('review')}>验收</button></div>
{(selected.approvals?.length || selected.nodes?.some(n=>['failed','interrupted','needs_configuration'].includes(n.status))) ? <div className="task-urgent" role="status"><strong>需要处理</strong>{selected.approvals?.map((approval) => <div key={approval.id} className="mb-4 rounded-xl border border-border p-3 text-xs"><strong>等待审批：{approval.tool}</strong><p className="mt-2 break-all text-muted-foreground">工作目录：{approval.workspace || '无'}</p><pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(approval.arguments, null, 2)}</pre><div className="mt-3 flex gap-2">{[true, false].map((allowed) => <Button key={String(allowed)} size="sm" variant={allowed ? 'default' : 'outline'} disabled={busy} onClick={() => void run(async () => { await api(`/v1/approvals/${approval.id}`, json('POST', { allowed })); setDetail(await lingxi.task(selected.id)) })}>{allowed ? '批准本次操作' : '拒绝'}</Button>)}</div></div>)}{selected.nodes?.filter(n=>['failed','interrupted','needs_configuration'].includes(n.status)).map(n=><p key={n.id}>{n.document?.title || n.id}：{n.error || '请检查此节点配置或执行结果'} <Button size="xs" variant="outline" disabled={busy} onClick={()=>{setFocusedNodeId(n.id);setView('process')}}>查看节点</Button></p>)}</div>:null}
        <TeamCanvas key={`canvas-${selected.id}`} task={selected} settingsHost={settingsHost} onConfigure={()=>{setSettingsTab('member');setSettingsOpen(true)}} onCopied={value=>{setQuery('');setFilter('all');setSelectedId(value.id);setDetail(value);setView('canvas')}} compact onDirty={setGraphDirty} onBusy={setCanvasBusy} onChanged={async () => { setDetail(await lingxi.task(selected.id)); await refreshTasks() }} />{view==='review'&&<TaskAcceptancePanel key={`acceptance-${selected.id}`} task={selected} onChanged={async()=>{setDetail(await lingxi.task(selected.id));await refreshTasks()}}/>}<div className="task-detail-body"><section>{selected.document?.review_report && <section className="mb-6"><div className="section-title">成果汇总</div><pre className="mt-3 whitespace-pre-wrap break-words text-xs leading-6">{selected.document.review_report}</pre></section>}<div className="section-title">执行过程<span>{selected.nodes?.filter((node) => node.status === 'completed').length || 0}/{selected.nodes?.length || 0}</span></div><div className="task-timeline">{(selected.nodes || []).map((node) => <div key={node.id} className="task-step" tabIndex={-1} ref={element=>{nodeRefs.current[node.id]=element}}><div className={cn('task-step-icon', `is-${node.status}`)}>{node.status === 'completed' ? <Check /> : node.status === 'running' ? <LoaderCircle className="animate-spin" /> : node.status === 'failed' ? <X /> : <Circle />}</div><div className="min-w-0 flex-1"><div className="flex items-center justify-between gap-3"><h3>{node.document?.title || node.id}</h3><span>{(statusMap[node.status] || {}).label || node.status}</span></div><p>{node.document?.task}</p>{node.result && <div className="mt-3"><pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-lg bg-secondary/50 p-3 text-xs">{node.result}</pre>{node.status === 'completed' && <Button size="xs" variant="outline" className="mt-2" onClick={() => void run(async () => downloadBlob(await apiBlob(`/v6/jobs/${selected.id}/artifacts/${node.id}`), `${node.document?.title || node.id}.md`))}>下载成果</Button>}</div>}{node.error && <div className="mt-2 rounded-lg bg-red-500/10 p-2 text-xs text-red-500">{node.error}</div>}{['failed', 'interrupted', 'needs_configuration'].includes(node.status) && <Button variant="ghost" size="xs" className="mt-2" disabled={busy} onClick={() => void run(() => action(selected, 'retry', node.id))}><RotateCcw />重试此节点</Button>}</div></div>)}{!selected.nodes?.length && <div className="rounded-xl border border-dashed border-border p-6 text-center text-xs text-muted-foreground">任务尚未拆分节点</div>}</div></section>
</div></> : <EmptyState icon={CircleDashed} title="选择一个任务" description="查看执行节点、授权范围与结果。" />}</Card>
    </div>
    {selected&&<TaskSettingsDrawer key={selected.id} open={settingsOpen} onOpenChange={setSettingsOpen}>
      <div className="task-settings-tabs"><button aria-pressed={settingsTab==='member'} onClick={()=>setSettingsTab('member')}>智能体</button><button aria-pressed={settingsTab==='task'} onClick={()=>setSettingsTab('task')}>任务</button></div>
      <div ref={setSettingsHost} className="task-member-settings-host" hidden={settingsTab!=='member'}/>
      <div className="task-settings-options" hidden={settingsTab!=='task'}>
      <section className="task-settings-section"><TaskQualityPanel compact task={selected} onCreated={value=>{setSelectedId(value.id);setDetail(value);setSettingsOpen(false)}}/></section>
      <section className="task-settings-section"><h4>验收标准</h4><p>{selected.document?.acceptance||'由你确认成果'}</p><Button variant="ghost" size="sm" onClick={()=>{setSettingsOpen(false);setView('review')}}>进入任务验收</Button></section>
      <details className="task-settings-section"><summary>团队记录</summary><div role="log" aria-label="团队执行记录">{(selected.events||[]).filter(event=>teamEventLabel(event)).slice(-30).map((event,index)=><p key={event.seq||index}>{teamEventLabel(event)}</p>)}</div></details>
      <details className="task-settings-section"><summary>任务信息</summary><dl><div><dt>任务编号</dt><dd>{selected.id}</dd></div><div><dt>工作会话</dt><dd>#{selected.session_id||'—'}</dd></div><div><dt>版本</dt><dd>{selected.revision}</dd></div></dl></details>
      </div>
    </TaskSettingsDrawer>}
  </div>
}
