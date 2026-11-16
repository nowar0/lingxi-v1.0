import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Activity, CalendarDays, Check, Plus, RefreshCw, UsersRound, X } from 'lucide-react'
import { toast } from 'sonner'
import { useScheduleActions } from './useScheduleActions'
import { useApp } from '@/context/AppContext'
import { relativeDay } from '@/lib/format'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { useRailReorder, type RailModule } from './useRailReorder'
import './companion-rail.css'

const labels: Record<RailModule, string> = { character: '角色卡', schedule: '日程与待办', activity: '模型活动' }
const localDate = () => { const now = new Date(); return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}` }

export function CompanionRail() {
  const navigate = useNavigate()
  const { activeCharacter, connection, schedule, refreshSchedule, messages, generating, sending, modules, profiles, uiState } = useApp()
  const sort = useRailReorder()
  const [adding, setAdding] = useState(false)
  const [title, setTitle] = useState('')
  const [date, setDate] = useState(localDate)
  const scheduleActions = useScheduleActions()
  const saving = scheduleActions.saving
  const [refreshing, setRefreshing] = useState(false)
  const connected = connection === 'connected'
  const configured = modules.find((item) => item.id === 'companion')
  const profile = profiles.find((item) => item.id === configured?.profile_id)
  const model = configured?.model || profile?.model
  const lastMessage = messages.at(-1)
  const failed = lastMessage?.delivery_status === 'failed'
  const busy = generating.companion || sending.companion
  const state = !connected ? '待连接' : generating.companion ? '正在回复' : sending.companion ? '正在发送' : failed ? '需要重试' : '空闲'
  const statusDescription = !connected ? '连接本地服务后查看活动' : generating.companion ? '正在生成当前会话的回复' : sending.companion ? '正在确认本条输入' : failed ? '请使用消息旁的重试按钮' : '等待下一条消息'
  const recent = messages.filter((item) => item.role !== 'system').slice(-3).reverse()
  const items = [...schedule].sort((a, b) => Number(a.done) - Number(b.done) || a.date.localeCompare(b.date))
  const report = (message: string, error: unknown) => toast.error(message, { description: (error as Error).message })
  const addSchedule = async () => {
    if (!connected || !title.trim() || !date) return
    try { if (await scheduleActions.create(title, date)) { setTitle(''); setAdding(false); toast.success('事项已保存') } }
    catch (error) { report('事项保存结果未确认，可重试核对', error) }
  }
  const toggle = async (id: string | number) => {
    const item = schedule.find(entry => entry.id === id)
    if (!connected || !item) return
    try { await scheduleActions.toggle(item) } catch (error) { report('事项更新失败', error) }
  }
  const content = (id: RailModule) => {
    if (id === 'character') return <div className="companion-module-body companion-character-body">
      <div className="companion-character-art"><img draggable={false} src={activeCharacter.art || './assets/lingxi-stage.png'} alt={activeCharacter.name} /></div>
      <div className="companion-character-copy"><h2>{activeCharacter.name}</h2><p title={activeCharacter.description}>{activeCharacter.description || '此刻，陪伴在你身边。'}</p>
        <div className="companion-character-actions"><Button variant="outline" size="sm" onClick={() => navigate('/settings?tab=characters')}><UsersRound />切换伙伴</Button></div>
      </div>
    </div>
    if (id === 'schedule') return <>
      <header className="companion-module-header"><h2><CalendarDays aria-hidden="true" />日程与待办</h2><div className="companion-module-actions"><Button variant="ghost" size="icon" className="companion-rail-icon" aria-label="刷新日程" disabled={!connected || refreshing} onClick={async () => { setRefreshing(true); try { await refreshSchedule() } catch (error) { report('日程刷新失败', error) } finally { setRefreshing(false) } }}><RefreshCw className={refreshing ? 'animate-spin' : ''} /></Button><Button variant="outline" size="xs" disabled={!connected || saving} aria-expanded={adding} onClick={() => setAdding(!adding)}><Plus />添加</Button></div></header>
      <div className="companion-module-body companion-schedule-body">
      {adding && <form className="companion-schedule-form" onSubmit={(event) => { event.preventDefault(); void addSchedule() }}><label className="sr-only" htmlFor="rail-schedule-title">事项名称</label><Input id="rail-schedule-title" autoFocus maxLength={200} placeholder="要做什么？" value={title} disabled={saving} onChange={(event) => setTitle(event.target.value)} /><label className="sr-only" htmlFor="rail-schedule-date">事项日期</label><Input id="rail-schedule-date" type="date" required value={date} disabled={saving} onChange={(event) => setDate(event.target.value)} /><div><Button type="button" variant="ghost" size="xs" disabled={saving} onClick={() => setAdding(false)}><X />取消</Button><Button size="xs" disabled={saving || !title.trim() || !date}>{saving ? '保存中…' : '保存事项'}</Button></div></form>}
      {items.length ? <ul className="companion-schedule-list">{items.map((item) => <li key={item.id}><button className={`companion-schedule-check ${item.done ? 'is-done' : ''}`} aria-label={`${item.done ? '标为未完成' : '完成事项'}：${item.title}`} aria-pressed={Boolean(item.done)} disabled={!connected || scheduleActions.working[String(item.id)]} onClick={() => void toggle(item.id)}>{Boolean(item.done) && <Check aria-hidden="true" />}</button><span className={item.done ? 'is-done' : ''} title={item.title}>{item.title}</span><time dateTime={item.date} title={item.date}>{relativeDay(item.date)}</time></li>)}</ul> : <p className="companion-rail-empty">{connected ? '还没有待办事项，留点空白也很好。' : '连接本地服务后查看日程。'}</p>}
      </div>
    </>
    return <>
      <header className="companion-module-header"><h2><Activity aria-hidden="true" />模型活动</h2></header>
      <div className="companion-module-body companion-activity-body">
      <div className="companion-model-status" role="status"><span className={`companion-model-dot ${busy && connected ? 'is-busy' : ''} ${failed ? 'is-failed' : ''}`} /><div><strong>{state}</strong><p>{statusDescription}</p></div></div>
      <div className="companion-model-config"><span>{model || (connected ? '陪伴模型待配置' : '尚未读取模型配置')}</span>{connected && model && <small>推理 · {({ low: '低', medium: '中', high: '高', xhigh: '最大', max: '最大' } as Record<string, string>)[uiState?.reasoning_effort || ''] || '跟随配置'}</small>}</div>
      {recent.length ? <ul className="companion-activity-list">{recent.map((message) => <li key={message.id}><span className={`companion-activity-mark ${message.delivery_status === 'failed' ? 'is-failed' : ''}`} /><span>{message.delivery_status === 'failed' ? '发送或生成未完成' : message.role === 'user' ? message.delivery_status === 'sending' ? '正在发送消息' : '收到你的消息' : generating.companion && message.id === lastMessage?.id ? '正在生成回复' : message.delivery_status === 'cancelled' ? '已停止生成' : '生成了一条回复'}</span><time>{message.at ? new Date(message.at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''}</time></li>)}</ul> : <p className="companion-model-foot">还没有活动记录</p>}
      </div>
    </>
  }

  return <aside className="context-rail companion-context-rail" ref={sort.rail} aria-label="消息侧栏模块" onScrollCapture={sort.cancelPress}>
    <p id="companion-rail-help" className="sr-only">长按模块空白处后拖动换位；聚焦模块后也可按 Alt 加上下方向键。按 Escape 取消拖动。</p>
    {sort.order.map((id) => <Card key={id} className={`companion-module companion-module-${id}`} ref={(node) => { if (node) sort.nodes.current.set(id, node); else sort.nodes.current.delete(id) }} role="region" aria-label={labels[id]} aria-describedby="companion-rail-help" tabIndex={0} onPointerDown={(event) => sort.onPointerDown(id, event)} onKeyDown={(event) => sort.onKeyDown(id, event)} onClickCapture={(event) => { if (sort.blockClick()) { event.preventDefault(); event.stopPropagation() } }}>{content(id)}</Card>)}
    <p className="sr-only" role="status" aria-live="polite">{sort.announcement}</p>
  </aside>
}
