import { useCallback, useEffect, useRef, useState } from 'react'
import { LoaderCircle, RefreshCw } from 'lucide-react'
import { api } from '@/api/client'
import { useApp } from '@/context/AppContext'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/dialog'
import { capabilityRequestSequence, capabilityStateLabel, capabilityVerificationLabel, loadCapabilityStatus } from '@/lib/capabilityStatus'
import type { CapabilityStatus } from '@/lib/capabilityStatus'
import './feature-status.css'

/** Mounted only in the explicit status section; reads host inventory, never packages. */
export function CapabilityStatusPanel() {
  const { connection } = useApp()
  const [value, setValue] = useState<CapabilityStatus>()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [updatedAt, setUpdatedAt] = useState<number>()
  const [selectedId, setSelectedId] = useState<string>()
  const sequence = useRef(capabilityRequestSequence())
  const abort = useRef<AbortController | undefined>(undefined)
  const connected = connection === 'connected'
  const refresh = useCallback(async () => {
    abort.current?.abort()
    const request = sequence.current.begin()
    if (connection !== 'connected') { setLoading(false); return }
    const controller = new AbortController(); abort.current = controller
    setLoading(true); setError('')
    try {
      const next = await loadCapabilityStatus(api, connection, AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]))
      if (sequence.current.current(request) && !controller.signal.aborted && next) { setValue(next); setUpdatedAt(Date.now()) }
    } catch (cause) {
      if (sequence.current.current(request) && !controller.signal.aborted) setError(cause instanceof Error ? cause.message : '能力状态读取失败；没有更改任何功能')
    } finally {
      if (sequence.current.current(request) && !controller.signal.aborted) setLoading(false)
    }
  }, [connection])
  useEffect(() => {
    void refresh()
    return () => { sequence.current.invalidate(); abort.current?.abort() }
  }, [refresh])
  const stale = !connected || loading || Boolean(error)
  const selected = value?.items.find(item => item.id === selectedId)
  const recordNames: Record<string,string> = { source_tests:'源码测试',frozen_exe:'应用版本',formal_user_data:'正式数据',external_services:'外部服务' }
  return <section className="feature-status-panel" aria-busy={loading}>
    <header className="customer-panel-heading feature-status-heading"><h2>功能状态</h2><Button size="sm" variant="outline" disabled={!connected || loading} onClick={() => void refresh()}>{loading ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}刷新</Button></header>
    {!connected && <p role="status" className="feature-status-message">{connection === 'demo' ? '演示模式下暂无本机状态。' : '本地服务未连接。'}{value && '以下为上次结果。'}</p>}
    {loading && <p role="status" className="feature-status-message">正在更新…</p>}
    {error && <p role="alert" className="feature-status-message">状态读取失败，请重试。{value && '以下为上次结果。'}</p>}
    {value && <div className="feature-status-grid" role="region" aria-label="功能列表" tabIndex={0}>{value.items.map(item => <article key={item.id} className="feature-status-row">
      <div className="feature-status-summary"><h3>{item.name}</h3><span>{stale ? '待确认' : capabilityStateLabel(item)}</span></div>
      <Button size="sm" variant="ghost" aria-label={`查看${item.name}详情`} onClick={() => setSelectedId(item.id)}>详情</Button>
    </article>)}</div>}
    {connected && !loading && !error && value && !value.items.length && <p className="feature-status-message">暂无功能状态。</p>}
    <Dialog open={Boolean(selected)} onOpenChange={open => { if (!open) setSelectedId(undefined) }}>
      <DialogContent className="feature-status-dialog">
        <DialogHeader><DialogTitle>{selected?.name}</DialogTitle><DialogDescription>{selected && (stale ? '上次结果 · 当前未核实' : capabilityStateLabel(selected))}</DialogDescription></DialogHeader>
        {selected && <div className="feature-status-details">
          <p>{selected.note}</p>
          {error && <p role="alert">{error}</p>}
          <dl className="feature-status-facts"><div><dt>功能</dt><dd>{selected.implemented ? '已提供' : '尚未提供'}</dd></div><div><dt>安装</dt><dd>{selected.installed ? '已安装' : '未安装'}</dd></div><div><dt>启用</dt><dd>{selected.enabled ? '已启用' : '未启用'}</dd></div></dl>
          <p className="feature-status-message">启用状态不代表服务已连接或已完成测试。</p>
          <details className="personal-disclosure"><summary>运行信息</summary><dl className="feature-status-facts"><div><dt>标识</dt><dd>{selected.id}</dd></div><div><dt>状态</dt><dd>{selected.state}</dd></div><div><dt>数据目录</dt><dd>{value?.data_root}</dd></div>{updatedAt && <div><dt>更新时间</dt><dd>{new Date(updatedAt).toLocaleString()}</dd></div>}</dl><h3>测试记录</h3><dl className="feature-status-facts">{Object.entries(value?.verification || {}).map(([key,status]) => <div key={key}><dt>{recordNames[key] || key}</dt><dd>{capabilityVerificationLabel(status)}</dd></div>)}</dl></details>
        </div>}
      </DialogContent>
    </Dialog>
  </section>
}
