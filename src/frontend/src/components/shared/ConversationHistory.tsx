import { protectMessageUI } from '@/context/SurfaceContext'
import { MessageActions } from './MessageActions'
import { MessageContent } from './MessageContent'
import { CompanionHistory } from './CompanionHistory'
import { useEffect, useState } from 'react'
import { History } from 'lucide-react'
import { toast } from 'sonner'
import { lingxi, type RestorePreview } from '@/api/lingxi'
import { useApp } from '@/context/AppContext'
import type { Message, Mode } from '@/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'

function WorkConversationHistory({ mode, className }: { mode: Mode; className?: string }) {
  const { activeSessionId, activeWorkSessionId, connection, restoreConversation, conversationVersions } = useApp()
  const sid = mode === 'companion' ? activeSessionId : activeWorkSessionId
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<{ items: Array<Message & { distance: number; restore_allowed: boolean; restore_reason: string }>; total: number }>({ items: [], total: 0 })
  const [preview, setPreview] = useState<RestorePreview>()
  const [busy, setBusy] = useState(false)
  const [reload, setReload] = useState(0)
  useEffect(() => { setOpen(false); setPreview(undefined); setData({ items: [], total: 0 }); setQuery(''); setOffset(0) }, [sid])
  useEffect(() => {
    if (!open || !sid || connection !== 'connected') return
    let disposed = false
    const timer = window.setTimeout(() => { void lingxi.history(sid, mode, query, offset).then((value) => { if (!disposed) setData(value) }).catch((error) => { if (!disposed) toast.error('历史读取失败', { description: (error as Error).message }) }) }, 150)
    return () => { disposed = true; window.clearTimeout(timer) }
  }, [connection, mode, offset, open, query, reload, sid, conversationVersions[mode]])
  const run = async (task: () => Promise<void>) => {
    if (busy) return
    setBusy(true)
    try { await task() } catch (error) { toast.error('历史操作未完成', { description: (error as Error).message }) }
    finally { setBusy(false) }
  }
  return <><Button aria-label="对话历史" variant="ghost" size="icon" className={className || 'size-8'} disabled={!sid || connection !== 'connected'} onClick={() => setOpen(true)}><History /></Button>
    <Dialog open={open} onOpenChange={(value) => { if (!busy) setOpen(value) }}><DialogContent className="max-w-3xl"><DialogHeader><DialogTitle>当前对话历史</DialogTitle><DialogDescription>保留目标消息，最多移开它之后的十条消息；回退会同步更新相关记忆与摘要。</DialogDescription></DialogHeader><Input aria-label="搜索对话历史" placeholder="搜索内容、角色或时间" maxLength={500} value={query} onChange={(event) => { setQuery(event.target.value); setOffset(0) }} /><div className="max-h-[55vh] space-y-3 overflow-auto">{data.items.map((message) => <article className="rounded-xl border border-border p-3" key={message.id}><div className="flex items-center justify-between gap-3 text-xs text-muted-foreground"><span>{message.role === 'user' ? '你' : message.speaker?.name || '助手'} · {message.at}</span><Button size="xs" variant="outline" disabled={!message.restore_allowed || busy} title={message.restore_reason} onClick={() => void run(async () => { if (!sid) return; const value = await lingxi.restorePreview(sid, mode, Number(message.id)); if (!value.allowed) throw new Error(value.reason); setPreview(value) })}>回到这里</Button></div><MessageActions message={message} mode={mode} onQuote={() => setOpen(false)} className="mt-2 whitespace-pre-wrap break-words text-xs leading-6"><MessageContent text={message.content} /></MessageActions>{message.restore_reason && <p className="mt-2 text-[10px] text-muted-foreground">{message.restore_reason}</p>}</article>)}{!data.items.length && <p className="py-6 text-center text-xs text-muted-foreground">没有符合条件的消息</p>}</div><DialogFooter><span className="mr-auto text-xs text-muted-foreground">{data.total} 条匹配消息</span><Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>上一页</Button><Button variant="outline" size="sm" disabled={offset + 100 >= data.total} onClick={() => setOffset(offset + 100)}>下一页</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(preview)} onOpenChange={(value) => { if (!value && !busy) setPreview(undefined) }}><DialogContent><DialogHeader><DialogTitle>回到这条消息显示时</DialogTitle><DialogDescription>将移开后续 {preview?.distance} 条消息，并同步回退相关摘要和记忆。{preview?.external_effects_note}</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" disabled={busy} onClick={() => setPreview(undefined)}>取消</Button><Button disabled={busy} onClick={() => void run(async () => { if (!preview) return; await restoreConversation(preview); setPreview(undefined); setReload((value) => value + 1); toast.success('对话与相关记忆已回退') })}>确认回到这里</Button></DialogFooter></DialogContent></Dialog>
  </>
}

function ConversationHistoryBody(props: { mode: Mode; className?: string }) {
  return props.mode === 'companion' ? <CompanionHistory className={props.className} /> : <WorkConversationHistory {...props} />
}

export const ConversationHistory = protectMessageUI(ConversationHistoryBody)
