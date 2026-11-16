import { Select } from '@/components/ui/select'
import { TaskQualityPanel } from '@/components/shared/TaskQualityPanel'
import { useEffect, useState } from 'react'
import { api, json } from '@/api/client'
import { lingxi } from '@/api/lingxi'
import { useApp } from '@/context/AppContext'
import type { TaskItem } from '@/types'
import { TeamCanvas } from './TeamCanvas'
import { Button } from '@/components/ui/button'
import { Play, Pause, Square, RotateCcw, Plus } from 'lucide-react'
import { toast } from 'sonner'

export function WorkTeamPanel({ sessionId }: { sessionId: number }) {
  const { tasks, refreshTasks } = useApp()
  const choices = tasks.filter(t => t.session_id === sessionId)
  const [id, setId] = useState('')
  const taskId = id || choices[0]?.id
  const [detail, setDetail] = useState<TaskItem>()
  const [goal, setGoal] = useState('')
  const [acceptance, setAcceptance] = useState('逐项说明完成内容、成果与未完成项')
  const [creating, setCreating] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [memberBusy, setMemberBusy] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [lastGood, setLastGood] = useState(0)
  const load = async () => { if (taskId) { const value = await lingxi.task(taskId); setDetail(old => old?.id === value.id && old.revision > value.revision ? old : value); setLastGood(Date.now()); setError('') } await refreshTasks() }
  useEffect(() => {
    if (!taskId) return
    let live = true, reading = false
    const poll = async () => {
      if (reading) return
      reading = true
      try { const value = await lingxi.task(taskId); if (live) { setDetail(old => old?.id === value.id && old.revision > value.revision ? old : value); setLastGood(Date.now()); setError('') } } catch(e) { if (live) setError((e as Error).message) } finally { reading = false }
    }
    void poll(); const timer = window.setInterval(() => void poll(), 2000)
    return () => { live = false; window.clearInterval(timer) }
  }, [taskId])
  const task = detail?.id === taskId ? detail : undefined
  const act = async (action: string, node?: string) => {
    if (!task || busy || memberBusy) return
    setBusy(true)
    try { setDetail(await lingxi.taskAction(task, action, node)); await refreshTasks() } catch(e) { toast.error((e as Error).message); void load() } finally { setBusy(false) }
  }
  return <div className="work-team-panel"><div className="team-toolbar"><label className="work-team-picker">协作任务<Select aria-label="当前协作任务" value={taskId || ''} disabled={busy || memberBusy} onChange={e => { setId(e.target.value); setDetail(undefined) }}>{choices.map(t => <option key={t.id} value={t.id}>{t.title || t.document?.goal || t.id}</option>)}</Select></label><Button variant="outline" size="sm" onClick={() => setCreating(v => !v)}><Plus />新建协作任务</Button></div>
    {(creating || !choices.length) && <form className="work-team-create" onSubmit={async e => { e.preventDefault(); if (busy || !goal.trim()) return; setBusy(true); try { const created = await api<TaskItem>('/v6/jobs', json('POST', { engine: 'agno', session_id: sessionId, goal: goal.trim(), acceptance, nodes: [{ id: 'main', title: '执行成员', task: goal.trim() }] })); await refreshTasks(); setId(created.id); setDetail(created); setCreating(false); setGoal('') } catch(e) { toast.error((e as Error).message) } finally { setBusy(false) } }}><label>任务目标<textarea required maxLength={12000} value={goal} onChange={e => setGoal(e.target.value)} /></label><label>验收标准<input required maxLength={8000} value={acceptance} onChange={e => setAcceptance(e.target.value)} /></label><Button size="sm" disabled={busy || !goal.trim()}>创建协作计划</Button></form>}
    {error && <p className="team-error" role="status">状态读取失败：{error}。最后更新 {lastGood ? new Date(lastGood).toLocaleTimeString() : '尚未成功'}；已保留画布草稿。</p>}
    {task && <><div className="team-toolbar"><div><h3>{task.document?.goal}</h3><p>任务 {task.id.slice(0,8)} · {({pending:'待开始',running:'执行中',pausing:'正在暂停',paused:'已暂停',blocked:'需要处理',review:'待验收',completed:'已完成',complete:'已完成',cancelled:'已取消'} as Record<string,string>)[task.status] || task.status} · {lastGood ? new Date(lastGood).toLocaleTimeString() : ''}</p></div><div className="team-actions">{task.status === 'running' ? <Button variant="outline" size="sm" disabled={busy} onClick={() => void act('pause')}><Pause />暂停</Button> : ['pending','paused','blocked'].includes(task.status) ? <Button size="sm" disabled={busy || memberBusy || dirty || !!error} onClick={() => void act('start')}><Play />{task.status === 'pending' ? '开始' : '恢复'}</Button> : task.status === 'review' ? <Button size="sm" disabled={busy || memberBusy || dirty} onClick={() => void act('accept')}>确认完成</Button> : null}{!['complete','completed','cancelled'].includes(task.status) && <Button variant="ghost" size="sm" disabled={busy || memberBusy} onClick={() => void act('cancel')}><Square />取消</Button>}</div></div>
    <TaskQualityPanel task={task} onCreated={value=>{setId(value.id);setDetail(value)}}/><TeamCanvas key={task.id} task={task} onDirty={setDirty} onBusy={setMemberBusy} onChanged={load} />
    <div className="work-team-results">{task.approvals?.map(a => <section key={a.id} className="team-notice"><strong>等待已有工具审批：{a.tool}</strong><p>{a.workspace}</p><pre>{JSON.stringify(a.arguments,null,2)}</pre><div className="team-actions">{[true,false].map(allowed => <Button key={String(allowed)} size="sm" variant={allowed ? 'default' : 'outline'} disabled={busy} onClick={async () => { setBusy(true); try { await api(`/v1/approvals/${a.id}`,json('POST',{allowed})); await load() } catch(e) { toast.error((e as Error).message) } finally { setBusy(false) } }}>{allowed ? '批准本次操作' : '拒绝'}</Button>)}</div></section>)}
    {task.nodes?.filter(n => ['failed','interrupted','needs_configuration'].includes(n.status)).map(n => <div key={n.id} className="team-notice"><strong>{n.document?.title || n.id}</strong><p>{n.error}</p><Button variant="outline" size="sm" disabled={busy || memberBusy || !['paused','blocked'].includes(task.status)} onClick={() => void act('retry',n.id)}><RotateCcw />重试此节点</Button></div>)}
    {(task.document?.review_report || task.document?.progress_report) && <section><h4>成果汇总</h4><pre>{task.document.review_report || task.document.progress_report}</pre></section>}</div></>}
  </div>
}
