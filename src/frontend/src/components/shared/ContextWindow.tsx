import {Link} from 'react-router-dom'
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { api } from '@/api/client'
import { contextUsage, loadContextWindow, type ContextPreview } from '@/lib/contextWindow'
import './context-window.css'

export function ContextWindow({ sessionId, connected, revision, generating, open: controlledOpen, onOpenChange }: { sessionId?: number | null; connected: boolean; revision: string; generating: boolean; open?: boolean; onOpenChange?: (open: boolean) => void }) {
  const [localOpen, setLocalOpen] = useState(false)
  const open = controlledOpen ?? localOpen
  const setOpen = (value: boolean) => { setLocalOpen(value); onOpenChange?.(value) }
  const [snapshot, setSnapshot] = useState<{ sid: number; revision: string; generating: boolean; preview: ContextPreview }>()
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [position, setPosition] = useState<CSSProperties>({})
  const trigger = useRef<HTMLButtonElement>(null)
  const preview = connected && snapshot && snapshot.sid === sessionId && snapshot.revision === revision && snapshot.generating === generating ? snapshot.preview : undefined
  const usage = preview ? contextUsage(preview) : undefined
  const bytes = (text: string) => new TextEncoder().encode(text).length
  const systemTokens = preview ? bytes(preview.initial_context.system || '') : 0
  const messageTokens = preview?.initial_context.messages?.filter(message => message.role !== 'system').reduce((sum, message) => sum + bytes(message.content || ''), 0) || 0
  const segments = usage ? [
    { label: '系统提示词', value: systemTokens },
    { label: '工具定义', value: Math.max(0, usage.used - systemTokens - messageTokens) },
    { label: '对话消息', value: messageTokens },
  ] : []
  useEffect(() => { setOpen(false); setSnapshot(undefined); setError('') }, [sessionId, connected])
  useEffect(() => {
    if (!connected || !sessionId) { setLoading(false); return }
    const controller = new AbortController()
    setLoading(true); setError('')
    loadContextWindow(api, sessionId, controller.signal).then((value) => {
      if (!controller.signal.aborted) setSnapshot({ sid: sessionId, revision, generating, preview: value })
    }).catch((failure: Error) => { if (!controller.signal.aborted) setError(failure.message) }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [sessionId, connected, revision, generating, refresh, open])
  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const bounds = trigger.current?.getBoundingClientRect()
      if (!bounds) return
      const width = Math.min(340, window.innerWidth - 24)
      const bottom = Math.max(12, window.innerHeight - bounds.top + 8)
      setPosition({ left: Math.max(12, Math.min(window.innerWidth - width - 12, bounds.right - width)), bottom, maxHeight: Math.max(80, window.innerHeight - bottom - 12) })
    }
    place()
    window.addEventListener('resize', place); window.addEventListener('scroll', place, true)
    const observer = new ResizeObserver(place)
    if (trigger.current?.parentElement) observer.observe(trigger.current.parentElement)
    return () => { observer.disconnect(); window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true) }
  }, [open])
  return <Dialog.Root open={open} onOpenChange={setOpen} modal={false}>
    <Dialog.Trigger asChild><button type="button" ref={trigger} className="work-context-trigger work-stat-pill" disabled={!connected || !sessionId} aria-label="查看上下文窗口" title="查看上下文窗口">
      <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7" /><circle className="work-context-ring" cx="10" cy="10" r="7" pathLength="100" strokeDasharray={`${usage?.ring || 0} 100`} /></svg>
      <span>{usage ? `${usage.percent}%` : '上下文 —'}</span>
    </button></Dialog.Trigger>
    <Dialog.Portal><Dialog.Content className="work-context-popover is-compact" style={position} aria-busy={loading} onCloseAutoFocus={event => { event.preventDefault(); if (document.activeElement === document.body && !document.querySelector('.work-stats-strip [data-state="open"]')) trigger.current?.focus() }}>
      <Dialog.Title className="sr-only">上下文用量</Dialog.Title>
      <Dialog.Description className="sr-only">后端保守估算的输入预算；未发送草稿不计入。工具定义项包含协议余量。</Dialog.Description>
      {loading&&!preview&&<p role="status">正在读取…</p>}
      {error&&<div role="alert"><p>读取失败：{error}</p><button type="button" onClick={()=>setRefresh(value=>value+1)}>重新读取</button></div>}
      {preview&&usage&&<>
        <div className="work-context-summary"><span>上下文已用 <strong>{usage.percent}%</strong></span><span>~{new Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(usage.used)} / {new Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(usage.limit)}</span></div>
        <div className="work-context-bar" role="progressbar" aria-label="输入预算使用比例" aria-valuemin={0} aria-valuemax={100} aria-valuenow={usage.ring}>{segments.map((segment,index)=><i className={`work-context-segment is-segment-${index}`} key={segment.label} style={{width:`${Math.min(100,segment.value/usage.limit*100)}%`}}/>)}</div>
        <dl className="work-context-breakdown">{segments.map((segment,index)=><div key={segment.label}><dt><i className={`is-segment-${index}`}/>{segment.label}</dt><dd>~{new Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(segment.value)}</dd></div>)}</dl>
      </>}
      <Link to="/settings?tab=conversations" onClick={()=>setOpen(false)}>上下文与记忆设置</Link>
    </Dialog.Content></Dialog.Portal>
  </Dialog.Root>
}
