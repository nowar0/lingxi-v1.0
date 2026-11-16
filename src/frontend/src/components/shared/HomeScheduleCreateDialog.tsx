import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api, json } from '@/api/client'
import { useApp } from '@/context/AppContext'
import { ScheduleRule } from './ScheduleRule'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input, Textarea } from '@/components/ui/input'
import { Select } from '@/components/ui/select'

type Kind = 'reminder' | 'work'
type TaskConfig = { revision: number; daily_tokens: number; daily_media_calls: number; reply_tokens: number; reviewer: string; auto_archive_days: number }

export function HomeScheduleCreateDialog({ kind, open, onOpenChange, onSaved }: {
  kind: Kind
  open: boolean
  onOpenChange: (open: boolean) => void
  onSaved: () => void
}) {
  const navigate = useNavigate()
  const { sessions, activeSessionId, tasks, connection, ensureSession } = useApp()
  const [sessionId, setSessionId] = useState(0)
  const [jobId, setJobId] = useState('')
  const [content, setContent] = useState('')
  const [cron, setCron] = useState('0 9 * * *')
  const [messageKind, setMessageKind] = useState<'reminder' | 'briefing'>('reminder')
  const [budget, setBudget] = useState(0)
  const [enabled, setEnabled] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [config, setConfig] = useState<TaskConfig>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const companionSessions = sessions.filter(item => !item.archived)
  const availableJobs = tasks.filter(item => item.session_id)

  useEffect(() => {
    if (!open) return
    setError(''); setContent(''); setCron('0 9 * * *'); setMessageKind('reminder'); setEnabled(false); setConfirmed(false)
    setSessionId(companionSessions.find(item => item.id === activeSessionId)?.id || companionSessions[0]?.id || 0)
    setJobId(availableJobs[0]?.id || '')
    if (kind !== 'work') return
    const controller = new AbortController()
    void api<TaskConfig>('/v7/tasks/settings', { signal: controller.signal }).then(value => { setConfig(value); setBudget(value.daily_tokens) }).catch(cause => { if (!controller.signal.aborted) setError((cause as Error).message) })
    return () => controller.abort()
    // Opening the dialog takes a snapshot of the currently available sessions and jobs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, kind])

  const save = async () => {
    if (busy || connection !== 'connected') return
    setBusy(true); setError('')
    try {
      if (kind === 'reminder') {
        const target = sessionId || await ensureSession('companion')
        await api(`/v6/companion-schedules/${crypto.randomUUID()}`, json('PUT', {
          session_id: target, kind: messageKind, content: content.trim(), cron, enabled: true,
          daily_token_limit: messageKind === 'briefing' ? budget : 0, expected_revision: 0,
        }))
      } else {
        if (!jobId || !config) return
        if (enabled && budget !== config.daily_tokens) {
          setConfig(await api<TaskConfig>('/v7/tasks/settings', json('PUT', {
            daily_tokens: budget, daily_media_calls: config.daily_media_calls, reply_tokens: config.reply_tokens,
            reviewer: config.reviewer, auto_archive_days: config.auto_archive_days, expected_revision: config.revision,
          })))
        }
        await api(`/v7/schedules/${crypto.randomUUID().replaceAll('-', '')}`, json('PUT', {
          job_id: jobId, cron, enabled, confirm_start: enabled && confirmed, expected_revision: 0,
        }))
      }
      onOpenChange(false)
      onSaved()
      toast.success(kind === 'reminder' ? '定时消息已保存' : '工作计划已保存')
    } catch (cause) { setError((cause as Error).message) }
    finally { setBusy(false) }
  }

  return <Dialog open={open} onOpenChange={value => { if (!busy) onOpenChange(value) }}>
    <DialogContent>
      <DialogHeader><DialogTitle>{kind === 'reminder' ? '添加定时消息' : '添加工作计划'}</DialogTitle><DialogDescription>程序运行时执行，错过时间不补跑。</DialogDescription></DialogHeader>
      <div className="home-schedule-create-fields">
        {kind === 'reminder' ? <>
          <label className="customer-field"><span>陪伴会话</span><Select aria-label="定时消息所属会话" value={sessionId} onChange={event => setSessionId(Number(event.target.value))}><option value={0}>{companionSessions.length ? '使用当前会话' : '创建陪伴会话'}</option>{companionSessions.map(item => <option key={item.id} value={item.id}>{item.title || `会话 ${item.id}`}</option>)}</Select></label>
          <label className="customer-field"><span>消息类型</span><Select value={messageKind} onChange={event => setMessageKind(event.target.value as 'reminder' | 'briefing')}><option value="reminder">直接提醒</option><option value="briefing">模型生成播报</option></Select></label>
          <label className="customer-field"><span>{messageKind === 'briefing' ? '播报请求' : '提醒内容'}</span><Textarea aria-label="定时消息内容" required maxLength={2000} value={content} onChange={event => setContent(event.target.value)} /></label>
          {messageKind === 'briefing' && <label className="customer-field"><span>每日 Token 上限</span><Input type="number" min={1} max={1000000} value={budget} onChange={event => setBudget(Number(event.target.value))} /></label>}
        </> : <>
          <label className="customer-field"><span>协作计划</span><Select aria-label="工作计划的任务" value={jobId} onChange={event => setJobId(event.target.value)}><option value="">选择已有任务</option>{availableJobs.map(item => <option key={item.id} value={item.id}>{item.title || item.document?.goal || item.id}</option>)}</Select></label>
          {!availableJobs.length && <p className="home-schedule-create-note">先创建一个工作任务，再安排执行时间。<button type="button" onClick={() => { onOpenChange(false); navigate('/tasks') }}>前往任务</button></p>}
          <label className="home-schedule-create-check"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} /> 启用后按计划自动启动</label>
          {enabled && <><label className="customer-field"><span>全部任务的每日 Token 预算</span><Input type="number" min={1} aria-label="计划任务每日预算" value={budget} onChange={event => setBudget(Number(event.target.value))} /></label><label className="home-schedule-create-check"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> 允许自动启动这份计划</label></>}
        </>}
        <ScheduleRule value={cron} onChange={setCron} disabled={busy} />
        {error && <p role="alert" className="home-schedule-create-error">{error}</p>}
      </div>
      <DialogFooter><Button variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>取消</Button><Button disabled={busy || connection !== 'connected' || (kind === 'reminder' ? !content.trim() || (messageKind === 'briefing' && !budget) : !jobId || !config || (enabled && (!budget || !confirmed)))} onClick={() => void save()}>{busy ? '保存中…' : '保存'}</Button></DialogFooter>
    </DialogContent>
  </Dialog>
}
