import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowRight, BookOpen, BriefcaseBusiness, Check, ChevronRight, ListChecks, MessageCircleMore, Plus, Sparkles, Trash2, UsersRound, Zap } from 'lucide-react'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
import { useScheduleActions } from '@/components/shared/useScheduleActions'
import { ScheduleManager } from '@/components/settings/CustomerSettings'
import { HomeScheduleCreateDialog } from '@/components/shared/HomeScheduleCreateDialog'
import { formatNumber, relativeDay } from '@/lib/format'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { DataModeNotice } from '@/components/shared/DataModeNotice'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import './home-page.css'

const taskLabels: Record<string, string> = {
  running: '执行中', generating: '执行中', completed: '已完成', pending: '待开始', waiting: '等待处理',
  paused: '已暂停', pausing: '等待暂停', review: '待验收', failed: '执行失败', blocked: '需要处理',
  interrupted: '已中断', needs_configuration: '待配置', cancelled: '已取消',
}

export function HomePage() {
  const navigate = useNavigate()
  const [scheduleParams,setScheduleParams] = useSearchParams()
  const scheduleView = scheduleParams.get('schedule') === 'reminder' ? 'reminder' : scheduleParams.get('schedule') === 'work' ? 'work' : 'todo'
  const { activeCharacter, connection, health, usage, modules, sessions, workSessions, tasks, schedule, dataErrors, reconnect } = useApp()
  const scheduleActions = useScheduleActions()
  const [taskOpen, setTaskOpen] = useState(false)
  const [planOpen, setPlanOpen] = useState(false)
  const [planRevision, setPlanRevision] = useState(0)
  const [title, setTitle] = useState('')
  const [date, setDate] = useState(() => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10))
  const connected = connection === 'connected'
  const writable = connected || connection === 'demo'
  const activeTasks = tasks.filter(task => !['completed', 'cancelled'].includes(task.status))
  const companionConfigured = modules.some(item => item.id === 'companion' && item.enabled && item.model)
  const localState = connected ? '运行中' : connection === 'checking' ? '连接中' : '未连接'
  const entries = [
    { label: '陪伴消息', note: dataErrors['陪伴会话'] ? '会话暂未读取' : `${sessions.length} 段对话`, icon: MessageCircleMore, path: '/companion' },
    { label: '工作空间', note: dataErrors['工作会话'] ? '会话暂未读取' : `${workSessions.length} 个项目会话`, icon: BriefcaseBusiness, path: '/work' },
    { label: '记忆库', note: '你允许记住的事', icon: BookOpen, path: '/memory' },
    { label: '人物卡', note: '角色、素材与卡包', icon: UsersRound, path: '/settings?tab=characters' },
  ]
  const addSchedule = async () => {
    if (!title.trim() || !date) return
    if (await scheduleActions.create(title, date)) {
      setTitle('')
      setTaskOpen(false)
      toast.success('事项已保存')
    }
  }
  const retry = () => void reconnect().catch(error => toast.error('重新读取失败', { description: error.message }))

  return <div className="page-container chrome-page home-page home-dashboard">
    <DataModeNotice />
    <section className="home-welcome" aria-labelledby="home-welcome-title">
      <div className="home-welcome-copy">
        <span className="home-partner-label"><Sparkles aria-hidden="true" />你的本地数字伙伴</span>
        <h1 id="home-welcome-title">你好，<br /><span>欢迎回来。</span></h1>
        <p>我在。今天想从哪里聊起？</p>
        <div className="home-welcome-actions">
          <button className="home-action home-action-primary" onClick={() => navigate('/companion')}>继续陪伴<ArrowRight aria-hidden="true" /></button>
          <button className="home-action home-action-secondary" onClick={() => navigate('/work')}><BriefcaseBusiness aria-hidden="true" />进入工作区</button>
        </div>
      </div>
      <img key={activeCharacter.art} src={activeCharacter.art || './assets/lingxi-stage.png'} alt={activeCharacter.name} className="home-welcome-art" onError={event => { if (!event.currentTarget.src.endsWith('/assets/lingxi-stage.png')) event.currentTarget.src = './assets/lingxi-stage.png' }} />
      <div className="home-partner-presence"><strong>{activeCharacter.name}</strong><span><i className={connected ? 'is-online' : ''} aria-hidden="true" />{connected ? '此刻陪伴着你' : connection === 'checking' ? '正在连接本地服务' : '等待重新连接'}</span></div>
    </section>

    {/* Middle row 2:1, bottom row 7:5; both share one 24px gutter. */}
    <div className="home-overview-grid">
      <section className="home-panel home-entries-panel" aria-labelledby="home-entries-title">
        <header className="home-panel-heading"><div><h2 id="home-entries-title">常用入口</h2><p>从你现在想做的事情开始</p></div><span className="home-space-count">{String(entries.length).padStart(2, '0')} 个空间</span></header>
        <nav className="home-entry-grid" aria-label="常用入口">{entries.map(item => <button className="home-entry" key={item.path} onClick={() => navigate(item.path)}>
          <span className="home-entry-icon"><item.icon aria-hidden="true" /></span><ChevronRight className="home-entry-arrow" aria-hidden="true" />
          <strong>{item.label}</strong><small>{item.note}</small>
        </button>)}</nav>
      </section>

      <section className="home-panel home-status-panel" aria-labelledby="home-status-title">
        <header className="home-panel-heading"><div><h2 id="home-status-title">真实状态</h2><p>配置、连接与调用分别呈现</p></div><span className="home-runtime-state"><i className={connected ? 'is-online' : ''} aria-hidden="true" />{localState}</span></header>
        <div className="home-service-list">
          <button className="home-service-row" onClick={() => navigate('/services')} aria-label="查看本地服务状态"><span className="home-service-icon"><Zap aria-hidden="true" /></span><span className="home-service-copy"><strong>本地服务</strong><small>{health ? `${health.name} · v${health.version}` : '未取得服务信息'}</small></span><span className={connected ? 'home-service-value is-online' : 'home-service-value'}>{connected ? '可用' : localState}</span></button>
          <button className="home-service-row" onClick={() => navigate('/connections?tab=assignments')} aria-label="设置陪伴模型"><span className="home-service-icon"><Sparkles aria-hidden="true" /></span><span className="home-service-copy"><strong>陪伴模型</strong><small>{dataErrors['模型模块'] ? '模型配置暂未读取' : '读取当前模块配置'}</small></span><span className="home-service-value">{dataErrors['模型模块'] ? '读取失败' : connected ? companionConfigured ? '已配置' : '待配置' : '待连接'}</span></button>
        </div>
        <dl className="home-usage-metrics">{[
          { label: '输入 tokens', value: usage?.总输入 },
          { label: '输出 tokens', value: usage?.总输出 },
          { label: '累计调用', value: usage?.总笔数 },
        ].map(item => <div key={item.label}><dt>{item.label}</dt><dd>{usage && !dataErrors['用量'] ? formatNumber(item.value ?? 0) : '—'}</dd></div>)}</dl>
        {(dataErrors['模型模块'] || dataErrors['用量']) && <p className="home-load-error">部分状态读取失败。<button onClick={retry}>重新读取</button></p>}
      </section>
    </div>

    <div className="home-activity-grid">
      <section className="home-panel home-tasks-panel" aria-labelledby="home-tasks-title">
        <header className="home-panel-heading"><div><h2 id="home-tasks-title">正在进行</h2><p>任务进度与下一步清晰可见</p></div><button className="home-panel-link" onClick={() => navigate('/tasks')}>全部任务<ChevronRight aria-hidden="true" /></button></header>
        {dataErrors['任务'] ? <p className="home-panel-empty home-load-error">任务暂未读取。<button onClick={retry}>重新读取</button></p> : activeTasks.length ? <div className="home-active-tasks">{activeTasks.slice(0, 3).map(task => {
          const completed = task.nodes?.filter(node => node.status === 'completed').length || 0
          const total = task.nodes?.length || 0
          const progress = total ? completed / total * 100 : 0
          return <button className="home-task-row" key={task.id} onClick={() => navigate(`/tasks?job=${encodeURIComponent(task.id)}`)}>
            <ListChecks aria-hidden="true" /><span className="home-task-content"><span className="home-task-heading"><strong>{task.title || task.document?.goal || '未命名任务'}</strong><span>{taskLabels[task.status] || task.status}</span></span><span className="home-task-progress"><Progress value={progress} /><small>{completed}/{total} 个节点</small></span></span><ChevronRight aria-hidden="true" />
          </button>
        })}</div> : <p className="home-panel-empty">{connection === 'checking' ? '正在读取任务…' : connected ? '现在没有进行中的任务' : '连接本地服务后查看任务'}</p>}
      </section>

      <section className="home-panel home-schedule-panel" aria-labelledby="home-schedule-title">
        <header className="home-panel-heading home-schedule-heading"><div><h2 id="home-schedule-title">日程与待办</h2><p>{scheduleView==='todo'?'按日期记录事项': '程序运行时执行，错过不补跑'}</p></div><div className="home-schedule-actions"><div className="home-schedule-tabs" role="group" aria-label="日程类型">{([{id:'todo',label:'待办'},{id:'reminder',label:'定时消息'},{id:'work',label:'工作计划'}] as const).map(item=><button key={item.id} type="button" aria-pressed={scheduleView===item.id} onClick={()=>setScheduleParams(item.id==='todo'?{}:{schedule:item.id})}>{item.label}</button>)}</div>{scheduleView==='todo'?<Dialog open={taskOpen} onOpenChange={setTaskOpen}><DialogTrigger asChild><button className="home-add-schedule" aria-label="添加事项" title={writable ? '添加事项' : '请先连接本地服务'} disabled={!writable}><Plus aria-hidden="true" /></button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>添加一个事项</DialogTitle><DialogDescription>事项会保存在零汐本地数据中。</DialogDescription></DialogHeader><label className="grid gap-2 text-sm">事项名称<Input maxLength={200} disabled={scheduleActions.saving} value={title} onChange={event => setTitle(event.target.value)} /></label><label className="grid gap-2 text-sm">日期<Input type="date" disabled={scheduleActions.saving} value={date} onChange={event => setDate(event.target.value)} /></label><DialogFooter><Button disabled={!title.trim() || !date || scheduleActions.saving || !writable} onClick={() => void addSchedule().catch(error => toast.error('事项保存结果未确认，可重试核对', { description: error.message }))}>{scheduleActions.saving ? '保存中…' : '保存事项'}</Button></DialogFooter></DialogContent></Dialog>:<button className="home-add-schedule" aria-label={scheduleView==='reminder'?'添加定时消息':'添加工作计划'} title={connected ? undefined : '请先连接本地服务'} disabled={!connected} onClick={()=>setPlanOpen(true)}><Plus aria-hidden="true" /></button>}</div></header>
        {scheduleView==='todo' ? (dataErrors['日程'] ? <p className="home-panel-empty home-load-error">日程暂未读取。<button onClick={retry}>重新读取</button></p> : !schedule.length ? <p className="home-panel-empty">{connection==='checking'?'正在读取待办…':writable?'暂无待办，点击右上角添加事项':'连接本地服务后查看待办'}</p> : <div className="home-schedule-list">{schedule.map(item => <div className="home-schedule-row" key={item.id}>
          <button className="home-schedule-check" aria-label={`${item.done ? '标为未完成' : '完成事项'}：${item.title}`} aria-pressed={Boolean(item.done)} disabled={!writable || scheduleActions.working[String(item.id)]} onClick={() => void scheduleActions.toggle(item).catch(error => toast.error('事项更新失败', { description: error.message }))}>{Boolean(item.done) && <Check aria-hidden="true" />}</button><span className="home-schedule-copy"><strong className={item.done ? 'is-done' : undefined}>{item.title}</strong><small>{relativeDay(item.date)}</small></span><button className="home-schedule-delete" aria-label={`删除事项 ${item.title}`} disabled={!writable || scheduleActions.working[String(item.id)]} onClick={() => void scheduleActions.remove(item).catch(error => toast.error('事项删除结果未确认', { description: error.message }))}><Trash2 aria-hidden="true" /></button>
        </div>)}</div>) : <ScheduleManager key={`${scheduleView}-${planRevision}`} initialKind={scheduleView} embedded />}
        {scheduleView!=='todo' && <HomeScheduleCreateDialog kind={scheduleView} open={planOpen} onOpenChange={setPlanOpen} onSaved={()=>setPlanRevision(value=>value+1)} />}
      </section>
    </div>
  </div>
}
