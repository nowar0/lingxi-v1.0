import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
import { lingxi } from '@/api/lingxi'
import type { Mode } from '@/types'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/input'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'

export function ImageGenerationDialog({ mode, open, onOpenChange }: { mode: Mode; open: boolean; onOpenChange: (open: boolean) => void }) {
  const { ensureSession, modules, drafts, setDraft, activeSessionId, activeWorkSessionId, connection } = useApp()
  const [text, setText] = useState(''), [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ url: string; sessionId: number }>()
  const module = modules.find(item => item.id === 'image')
  const sid = mode === 'companion' ? activeSessionId : activeWorkSessionId
  const sequence = useRef(0), creating = useRef(false), live = useRef(true)
  const controller = useRef<AbortController | undefined>(undefined)
  const previous = useRef(sid), locked = useRef(false)
  const current = useRef(sid); current.current = sid
  const cancel = () => { sequence.current++; controller.current?.abort(); controller.current = undefined; locked.current = false }
  useEffect(() => { live.current = true; return () => { live.current = false; cancel() } }, [])
  useEffect(() => {
    const firstSession = previous.current === undefined && sid !== undefined && creating.current
    previous.current = sid
    if (firstSession) return
    cancel(); setBusy(false); setResult(undefined)
  }, [sid, module?.revision])
  const changeOpen = (value: boolean) => {
    if (!value && locked.current) { cancel(); setBusy(false); toast.message('已停止等待生图结果', { description: '已提交的外部生图可能仍在处理，请核对后再发起新请求。' }) }
    onOpenChange(value)
  }
  const generate = async () => {
    if (!text.trim() || locked.current || connection !== 'connected') return
    locked.current = true; setBusy(true); setResult(undefined); creating.current = !current.current
    const request = ++sequence.current, owner = new AbortController()
    controller.current = owner
    try {
      const target = await ensureSession(mode)
      if (!live.current || sequence.current !== request || owner.signal.aborted) return
      const value = await lingxi.invokeMedia('image', target, { text }, owner.signal)
      if (!live.current || sequence.current !== request || owner.signal.aborted || current.current !== target) return
      if (!value.media) throw new Error('生图服务没有返回图片')
      setResult({ url: value.media.url, sessionId: target })
    } catch (error) { if (live.current && sequence.current === request && !owner.signal.aborted) toast.error('生图未完成', { description: (error as Error).message }) }
    finally { creating.current = false; if (sequence.current === request) { locked.current = false; if (live.current) setBusy(false) } }
  }
  return <Dialog open={open} onOpenChange={changeOpen}><DialogContent className="max-w-2xl"><DialogHeader><DialogTitle>生成图片</DialogTitle><DialogDescription>{module?.model || '尚未配置生图模块'} · {module?.local ? '本地服务' : 'Key 接口'}。提交后会调用当前配置的生图服务。</DialogDescription></DialogHeader><Textarea aria-label="生图提示词" placeholder="输入准备好的正向提示词，原样提交给生图服务" maxLength={12000} value={text} onChange={event => setText(event.target.value)} disabled={busy} />{result && <a href={result.url} target="_blank" rel="noreferrer"><img alt="实际生成的图片" src={result.url} className="max-h-[45vh] w-full rounded-xl object-contain" /></a>}<DialogFooter>{result && <Button variant="outline" onClick={() => {
    if (current.current !== result.sessionId) { toast.error('当前会话已变化，请返回原会话插入图片'); return }
    const next = [drafts[mode], `[生成图片](${result.url})`].filter(Boolean).join('\n\n')
    if (next.length > 8000) { toast.error('草稿超过 8000 字，请先缩短'); return }
    setDraft(mode, next); changeOpen(false)
  }}>插入当前草稿</Button>}{busy ? <Button variant="outline" onClick={() => changeOpen(false)}>停止等待</Button> : <Button disabled={connection !== 'connected' || !module?.enabled || !text.trim()} onClick={() => void generate()}>确认生成图片</Button>}</DialogFooter></DialogContent></Dialog>
}
