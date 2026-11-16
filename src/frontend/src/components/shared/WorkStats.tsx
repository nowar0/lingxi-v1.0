import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { Database, Gauge, X } from 'lucide-react'
import { useApp } from '@/context/AppContext'
import { api } from '@/api/client'
import type { Usage } from '@/types'
import { ContextWindow } from './ContextWindow'
import './work-stats.css'

const compact = (value: number) => new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(value)
const amount = (value?: number | null) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString() : '—'

function StatsPopover({ open, onOpenChange, label, icon, summary, children }: { open: boolean; onOpenChange: (open: boolean) => void; label: string; icon: ReactNode; summary: string; children: ReactNode }) {
  const trigger = useRef<HTMLButtonElement>(null)
  const [position, setPosition] = useState<CSSProperties>({})
  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const bounds = trigger.current?.getBoundingClientRect()
      if (!bounds) return
      const width = Math.min(340, window.innerWidth - 24), bottom = Math.max(12, window.innerHeight - bounds.top + 8)
      setPosition({ left: Math.max(12, Math.min(window.innerWidth - width - 12, bounds.left)), bottom, maxHeight: Math.max(80, window.innerHeight - bottom - 12) })
    }
    place(); window.addEventListener('resize', place); window.addEventListener('scroll', place, true)
    const observer = new ResizeObserver(place)
    if (trigger.current?.parentElement) observer.observe(trigger.current.parentElement)
    return () => { observer.disconnect(); window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true) }
  }, [open])
  return <Dialog.Root open={open} onOpenChange={onOpenChange} modal={false}>
    <Dialog.Trigger asChild><button ref={trigger} type="button" className="work-stat-pill" aria-label={`查看${label}`}>{icon}<span>{summary}</span></button></Dialog.Trigger>
    <Dialog.Portal><Dialog.Content className="work-stat-popover" style={position} aria-describedby={undefined} onCloseAutoFocus={event => { event.preventDefault(); if (document.activeElement === document.body && !document.querySelector('.work-stats-strip [data-state="open"]')) trigger.current?.focus() }}>
      <header><Dialog.Title>{icon}{label}</Dialog.Title><Dialog.Close aria-label={`关闭${label}`}><X /></Dialog.Close></header>
      {children}
    </Dialog.Content></Dialog.Portal>
  </Dialog.Root>
}

export function WorkStats({ sessionId, connected, generating, revision, turnCount }: { sessionId?: number | null; connected: boolean; generating: boolean; revision: string; turnCount: number }) {
  const { conversationBatches } = useApp()
  const [performance,setPerformance]=useState<{model_ms:number|null;ttft_ms:number|null;tps:number|null}>()
  const [active, setActive] = useState<'session' | 'tokens' | 'context'>()
  const [latestUsage, setLatestUsage] = useState<{ sid: number; value: Usage }>()
  const [loading, setLoading] = useState(false), [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  useEffect(() => { setActive(undefined); setLatestUsage(undefined);setPerformance(undefined); setError('') }, [sessionId, connected])
  useEffect(() => {
    if (!connected || !sessionId) { setLoading(false); return }
    const controller = new AbortController();setLoading(true);setError('')
    void api<{model_ms:number|null;ttft_ms:number|null;tps:number|null}>(`/v6/manage/performance/${sessionId}`,{signal:controller.signal}).then(v=>{if(!controller.signal.aborted)setPerformance(v)}).catch(()=>{})
    api<Usage>(`/v6/sessions/${sessionId}/usage`, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) setLatestUsage({ sid: sessionId, value })
    }).catch((error: Error) => { if (!controller.signal.aborted) setError(error.message) }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [connected, sessionId, revision, active, refresh])
  const usage = connected && latestUsage?.sid === sessionId ? latestUsage?.value : undefined
  const total = usage && typeof usage.总输入 === 'number' && typeof usage.总输出 === 'number' ? usage.总输入 + usage.总输出 : undefined
  const cache = usage?.缓存占输入比
  const tools = conversationBatches.work.flatMap(batch => batch.details?.tools || [])
  const durations = tools.map(tool => tool.duration_ms).filter((value): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0)
  const toolDuration = durations.length ? `${(durations.reduce((sum,value) => sum + value, 0) / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })}秒` : '未记录'
  const change = (name: 'session' | 'tokens' | 'context') => (open: boolean) => setActive(current => open ? name : current === name ? undefined : current)
  return <div className="work-composer-status work-stats-strip" aria-label="工作统计">
    <StatsPopover open={active === 'session'} onOpenChange={change('session')} label="会话统计" icon={<Gauge />} summary={`${turnCount} 轮 ${tools.length} 步`}>
      <dl><div><dt>模型用时</dt><dd>{performance?.model_ms!=null?`${(performance.model_ms/1000).toFixed(1)} 秒`:'—'}</dd></div><div><dt>工具调用用时{durations.length > 0 && durations.length < tools.length ? '（已记录部分）' : ''}</dt><dd>{toolDuration}</dd></div><div><dt>平均首字延迟</dt><dd>{performance?.ttft_ms!=null?`${(performance.ttft_ms/1000).toFixed(2)} 秒`:'—'}</dd></div><div><dt>平均输出速度</dt><dd>{performance?.tps!=null?`${performance.tps.toFixed(1)} tok/s`:'—'}</dd></div></dl>
    </StatsPopover>
    <StatsPopover open={active === 'tokens'} onOpenChange={change('tokens')} label="Token 用量" icon={<Database />} summary={`${total === undefined ? '—' : compact(total)} tok · 缓存${typeof cache === 'number' ? `命中 ${cache}%` : '未上报'}`}>
      <div className="work-stat-total"><span>本会话累计</span><strong>{amount(total)} tok{usage?.用量完整===false&&<small> · 部分未上报</small>}</strong></div>
      {loading && <p role="status">正在更新用量…</p>}
      <dl><div><dt>缓存命中</dt><dd>{typeof cache === 'number' ? `${cache}%` : '未上报'}</dd></div><div><dt>未缓存输入</dt><dd>{amount(usage?.总未缓存输入)} tok</dd></div><div><dt>缓存读取</dt><dd>{amount(usage?.总缓存)} tok</dd></div><div><dt>输出</dt><dd>{amount(usage?.总输出)} tok</dd></div></dl>

      {!!usage?.子智能体?.length&&<details className="work-agent-usage"><summary>子智能体用量</summary>{usage.子智能体.map(agent=><div key={agent.job_id+':'+agent.node_id}><span>{agent.node_id||'主调度'}</span><span>{amount((agent.总输入||0)+(agent.总输出||0))} tok</span></div>)}</details>}
      {!connected && <p role="status">本地服务未连接。</p>}
      {error && <div role="alert"><p>更新失败：{error}。{usage ? '当前保留已读取记录。' : ''}</p><button type="button" onClick={() => setRefresh(value => value + 1)}>重新读取</button></div>}
    </StatsPopover>
    <ContextWindow sessionId={sessionId} connected={connected} generating={generating} revision={revision} open={active === 'context'} onOpenChange={change('context')} />
  </div>
}
