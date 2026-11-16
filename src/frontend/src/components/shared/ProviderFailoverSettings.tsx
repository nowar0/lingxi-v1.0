import {confirmDiscard,useUnsavedChanges} from './UnsavedChanges'
import { Select } from '@/components/ui/select'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { api, json } from '@/api/client'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'

type Chain = { id: string; revision: number; enabled: boolean; primary: string; fallbacks: string[] }
type Option = { id: string; label: string }
type SwitchEvent = { id: string; role: string; source: string; target: string; reason: string; created_at: string }
const names: Record<string, string> = { companion: '陪伴', work: '工作', orchestrator: '任务调度', chat: '默认对话', hard: '复杂任务' }
const reasons: Record<string, string> = { timeout: '请求超时', connection: '连接失败', rate_limit: '服务限流', server_error: '服务端错误' }

export function ProviderFailoverSettings({ ready }: { ready: boolean }) {
  const [items, setItems] = useState<Chain[]>([])
  const [options, setOptions] = useState<Option[]>([])
  const [events, setEvents] = useState<SwitchEvent[]>([])
  const [role, setRole] = useState('companion')
  const [draft, setDraft] = useState<Chain>();useUnsavedChanges(!!draft&&JSON.stringify(draft)!==JSON.stringify(items.find(v=>v.id===role)))
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const load = async () => {
    setLoading(true); setError('')
    try {
      const [chains, history] = await Promise.all([
        api<{ items: Chain[]; options: Option[] }>('/v6/provider-failover'),
        api<{ items: SwitchEvent[] }>('/v6/provider-failover-events?limit=8'),
      ])
      setItems(chains.items); setOptions(chains.options); setEvents(history.items)
      setDraft(undefined)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '读取失败，请重试') }
    finally { setLoading(false) }
  }
  useEffect(() => { if (ready) void load() }, [ready])
  const current = draft?.id === role ? draft : items.find(item => item.id === role)
  const edit = (change: Partial<Chain>) => { if (current) setDraft({ ...current, ...change }) }
  const save = async () => {
    if (!current) return
    setBusy(true); setError('')
    try {
      await api(`/v6/provider-failover/${encodeURIComponent(role)}`, json('PUT', {
        enabled: current.enabled, fallbacks: current.fallbacks, expected_revision: current.revision,
      }))
      await load(); toast.success('候选链已保存')
    } catch (cause) { setError(cause instanceof Error ? cause.message : '保存失败，请刷新后重试') }
    finally { setBusy(false) }
  }
  return <Card className="settings-section">
    <div className="settings-section-heading"><div><h2>模型故障转移</h2><p>按顺序尝试候选；默认关闭，不重放工具操作</p></div><Button size="sm" variant="outline" disabled={!ready || loading || busy} onClick={() => void confirmDiscard().then(ok=>{if(ok)void load()})}>刷新</Button></div>
    <div className="space-y-4 p-5">
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {!ready ? <p className="text-sm text-muted-foreground">连接本地服务后配置候选。</p> : loading ? <p role="status" className="text-sm text-muted-foreground">正在读取候选链…</p> : current ? <>
        <div className="flex flex-wrap items-end gap-4">
          <label className="grid min-w-0 flex-1 gap-2 text-sm">用途<Select className="h-9 w-full rounded-lg border border-border bg-input px-2" value={role} disabled={busy} onChange={event => {const value=event.target.value;void confirmDiscard().then(ok=>{if(ok){setRole(value);setDraft(undefined)}})}}>{items.map(item => <option key={item.id} value={item.id}>{names[item.id] || item.id}</option>)}</Select></label>
          <label className="flex h-9 items-center gap-2 text-sm"><Switch checked={current.enabled} disabled={busy || !current.primary} onCheckedChange={enabled => edit({ enabled })} />启用</label>
        </div>
        <p className="break-all text-xs text-muted-foreground">首选：{current.primary || '尚未分配，请先在模块分配中选择模型'}</p>
        <div className="space-y-3">{[0, 1, 2].map(index => <label key={index} className="grid gap-2 text-sm">候选 {index + 1}<Select aria-label={`候选 ${index + 1}`} className="h-10 w-full min-w-0 rounded-lg border border-border bg-input px-2" value={current.fallbacks[index] || ''} disabled={busy || !current.primary || (index > 0 && !current.fallbacks[index - 1])} onChange={event => {
          const next = [...current.fallbacks]; next[index] = event.target.value
          edit({ fallbacks: event.target.value ? next : next.filter((_,slot)=>slot!==index) })
        }}><option value="">不设置</option>{current.fallbacks[index] && !options.some(option => option.id === current.fallbacks[index]) && <option value={current.fallbacks[index]}>原候选已不可用，请重新选择</option>}{options.filter(option => !current.fallbacks.includes(option.id) || current.fallbacks[index] === option.id).map(option => <option key={option.id} value={option.id}>{option.label}</option>)}</Select></label>)}</div>
        {!options.length && <p className="text-sm text-muted-foreground">先在 Key 库添加并分配模型，或在模型配置中保存备用服务。</p>}
        <p className="max-w-prose text-xs leading-6 text-muted-foreground">在相同协议的文字模型间，处理尚未输出内容时的超时、连接失败、限流和服务端错误。候选可能产生额外调用费用；已输出内容、参数错误或用户拒绝后会停止。已批准的独立验收始终使用原模型。</p>
        <Button disabled={busy || !draft || (current.enabled && !current.fallbacks.length)} onClick={() => void save()}>{busy ? '正在保存…' : '保存候选链'}</Button>
      </> : <p className="text-sm text-muted-foreground">暂无可配置用途，刷新后重试。</p>}
      <details className="border-t border-border pt-3"><summary className="cursor-pointer text-sm">最近切换记录{events.length ? ` · ${events.length}` : ''}</summary><div className="mt-3 divide-y divide-border">{events.length ? events.map(event => <div key={event.id} className="space-y-1 py-3 text-xs"><p className="font-medium">{names[event.role] || event.role} · {reasons[event.reason] || event.reason}</p><p className="break-all text-muted-foreground">{event.source} → {event.target}</p><p className="text-muted-foreground">{event.created_at} UTC</p></div>) : <p className="py-2 text-xs text-muted-foreground">尚无切换记录。</p>}</div></details>
    </div>
  </Card>
}
