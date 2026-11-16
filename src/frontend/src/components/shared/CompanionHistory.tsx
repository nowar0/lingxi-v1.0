import { protectMessageUI } from '@/context/SurfaceContext'
import { MessageActions } from './MessageActions'
import { useEffect, useState } from 'react'
import { History, Search } from 'lucide-react'
import { toast } from 'sonner'
import { lingxi, type RestorePreview } from '@/api/lingxi'
import { useApp } from '@/context/AppContext'
import type { Message } from '@/types'
import { formatTime } from '@/lib/format'
import { MessageContent } from './MessageContent'
import { CharacterAvatar } from './CharacterAvatar'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import './companion-history.css'

type HistoryMessage = Message & { distance: number; restore_allowed: boolean; restore_reason: string }
type Kind = 'all' | 'images' | 'audio' | 'files' | 'links'
const tabs: { id: Kind; label: string }[] = [{ id: 'all', label: '全部' }, { id: 'images', label: '图片' }, { id: 'audio', label: '音频' }, { id: 'files', label: '文件' }, { id: 'links', label: '链接' }]
function dateLabel(at?: string) {
  const date = at ? new Date(at) : undefined
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleDateString('zh-CN') : '未标注日期'
}

function CompanionHistoryBody({ className }: { className?: string }) {
  const { activeSessionId: sid, sessions, connection, restoreConversation, activeCharacter, uiState, conversationVersions } = useApp()
  const title = sessions.find((item) => item.id === sid)?.title || '陪伴对话'
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState<Kind>('all')
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<{ items: HistoryMessage[]; total: number }>({ items: [], total: 0 })
  const [preview, setPreview] = useState<RestorePreview>()
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  const serverOffset = offset
  useEffect(() => { setOpen(false); setPreview(undefined); setData({ items: [], total: 0 }); setQuery(''); setKind('all'); setOffset(0) }, [sid])
  useEffect(() => {
    if (!open || !sid || connection !== 'connected') return
    let disposed = false
    setLoading(true); setError('')
    const timer = window.setTimeout(() => { void (async () => {
      const first=await lingxi.history(sid,'companion',query,serverOffset,kind)
      if(!disposed)setData(first)
    })().catch((reason) => { if (!disposed) setError((reason as Error).message) }).finally(() => { if (!disposed) setLoading(false) }) }, 150)
    return () => { disposed = true; window.clearTimeout(timer) }
  }, [connection, kind, open, query, reload, serverOffset, sid, conversationVersions.companion])
  const run = async (task: () => Promise<void>) => {
    if (busy) return
    setBusy(true)
    try { await task() } catch (reason) { toast.error('历史操作未完成', { description: (reason as Error).message }) }
    finally { setBusy(false) }
  }
  const items = data.items
  return <><Button aria-label="对话历史" variant="ghost" size="icon" className={className || 'size-8'} disabled={!sid || connection !== 'connected'} onClick={() => setOpen(true)}><History /></Button>
    <Dialog open={open} onOpenChange={(value) => { if (!busy) setOpen(value) }}><DialogContent className="companion-history-dialog"><DialogHeader className="companion-history-heading"><DialogTitle title={title}>{title}</DialogTitle><DialogDescription className="sr-only">这段聊天的历史记录，可搜索内容、按类型筛选，或预览回退到一条消息。</DialogDescription></DialogHeader>
      <div className="companion-history-search"><Search aria-hidden="true" /><Input aria-label="搜索对话历史" placeholder="搜索聊天记录" maxLength={500} value={query} onChange={(event) => { setQuery(event.target.value); setOffset(0) }} /></div>
      <div className="companion-history-tabs" role="tablist" aria-label="聊天记录类型">{tabs.map((tab) => <button key={tab.id} role="tab" aria-selected={kind === tab.id} aria-controls="companion-history-results" onClick={() => { setKind(tab.id); setOffset(0) }}>{tab.label}</button>)}</div>
      <div className="companion-history-results" id="companion-history-results" role="tabpanel" aria-label="聊天记录" aria-busy={loading} key={`${sid}:${query}:${kind}:${offset}`}>
        {loading ? <div className="companion-history-empty" role="status">正在读取聊天记录…</div> : error ? <div className="companion-history-empty"><p>聊天记录未能读取：{error}</p><Button variant="outline" size="sm" onClick={() => setReload((value) => value + 1)}>重试</Button></div> : !items.length ? <p className="companion-history-empty">没有符合条件的消息</p> : items.map((message, index) => {
          const date = dateLabel(message.at)
          const user = message.role === 'user'
          const name = user ? uiState?.nickname || '你' : message.speaker?.name || activeCharacter.name
          return <div key={message.id}>{(index === 0 || date !== dateLabel(items[index - 1].at)) && <h3 className="companion-history-date">{date}</h3>}<article className="companion-history-row"><CharacterAvatar src={user ? undefined : message.speaker?.avatar || activeCharacter.avatar} name={name} className="size-9 rounded-full" /><div className="companion-history-message"><header><span>{name}</span><time dateTime={message.at}>{formatTime(message.at)}</time><Button className="companion-history-restore" size="xs" variant="ghost" disabled={!message.restore_allowed || busy} title={message.restore_reason || '预览回到这条消息之后'} aria-label={`回到这条消息：${message.content.slice(0, 24)}`} onClick={() => void run(async () => { if (!sid) return; const value = await lingxi.restorePreview(sid, 'companion', Number(message.id)); if (!value.allowed) throw new Error(value.reason); setPreview(value) })}>回到这里</Button></header><MessageActions message={message} mode="companion" className="companion-history-content" onQuote={() => setOpen(false)}><MessageContent text={message.content} /></MessageActions></div></article></div>
        })}
      </div><DialogFooter className="companion-history-footer"><span>{loading ? '正在读取…' : `${data.total} 条匹配消息`}</span>{data.total > 100 && <><Button variant="outline" size="sm" disabled={!offset || loading} onClick={() => setOffset(Math.max(0, offset - 100))}>上一页</Button><Button variant="outline" size="sm" disabled={offset + 100 >= data.total || loading} onClick={() => setOffset(offset + 100)}>下一页</Button></>}</DialogFooter>
    </DialogContent></Dialog>
    <Dialog open={Boolean(preview)} onOpenChange={(value) => { if (!value && !busy) setPreview(undefined) }}><DialogContent><DialogHeader><DialogTitle>回到这条消息显示时</DialogTitle><DialogDescription>将移开后续 {preview?.distance} 条消息，并同步回退相关摘要和记忆。{preview?.external_effects_note}</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" disabled={busy} onClick={() => setPreview(undefined)}>取消</Button><Button disabled={busy} onClick={() => void run(async () => { if (!preview) return; await restoreConversation(preview); setPreview(undefined); setReload((value) => value + 1); toast.success('对话与相关记忆已回退') })}>确认回到这里</Button></DialogFooter></DialogContent></Dialog>
  </>
}

export const CompanionHistory = protectMessageUI(CompanionHistoryBody)
