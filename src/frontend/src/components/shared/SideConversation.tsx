import { ConversationToolScope } from '@/context/ConversationToolContext'
import { ConversationTools } from '@/components/shared/ConversationTools'
import { useEffect, useRef, useState } from 'react'
import { ArrowUpLeft, RefreshCw, Save, LoaderCircle,MessageCirclePlus } from 'lucide-react'
import { toast } from 'sonner'
import { api, json } from '@/api/client'
import { useApp } from '@/context/AppContext'
import { WorkConversationScope } from '@/context/WorkConversationScope'
import { MessageActionsProvider } from '@/context/MessageActionsContext'
import { MessageList } from './MessageList'
import { WorkComposer } from './WorkComposer'
import { ConversationHistory } from './ConversationHistory'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/input'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'

type Origin = { id: string; revision: number; parent_session_id: number; through_message_id?: number; regular: boolean }
type RecordState = { revision: number; record: { goal: string; materials: string; constraints: string; tasks: string[]; status: string } }

export function SideConversation({ sessionId, onReturn }: { sessionId: number; onReturn: (text: string) => void }) {
  return <WorkConversationScope sessionId={sessionId}><MessageActionsProvider><ConversationToolScope identity={sessionId}><SideBody sessionId={sessionId} onReturn={onReturn} /></ConversationToolScope></MessageActionsProvider></WorkConversationScope>
}
function SideBody({ sessionId, onReturn }: { sessionId: number; onReturn: (text: string) => void }) {
  const { workMessages, connection, refreshWorkSessions } = useApp()
  const [origin, setOrigin] = useState<Origin>()
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(false)
  const [record, setRecord] = useState<RecordState>()
  const latest = [...workMessages].reverse().find(m => m.role === 'assistant' && m.delivery_status === 'complete' && m.content.trim())
  useEffect(() => {
    if (connection !== 'connected') return
    let cancelled = false
    void api<{ origin: Origin }>(`/v9/workspace/side-chats/${sessionId}`).then(data => { if (!cancelled) { setOrigin(data.origin); setError('') } }).catch(failure => { if (!cancelled) setError(failure.message) })
    return () => { cancelled = true }
  }, [sessionId, connection, revision])
  const edit = async () => {
    try { setRecord(await api<RecordState>(`/v3/sessions/${sessionId}/work-record`)); setEditing(true) }
    catch (failure) { toast.error('侧聊工作记录未读取', { description: (failure as Error).message }) }
  }
  const save = async () => {
    if (!record || busy) return
    setBusy(true)
    try { setRecord(await api<RecordState>(`/v3/sessions/${sessionId}/work-record`, json('PUT', { record: record.record, expected_revision: record.revision }))); setEditing(false) }
    catch (failure) { toast.error('侧聊工作记录未保存', { description: (failure as Error).message }) }
    finally { setBusy(false) }
  }
  const historyHost=useRef<HTMLSpanElement>(null)
  const saveRegular=async () => { if (!origin || busy || origin.regular) return; setBusy(true); try { setOrigin(await api<Origin>(`/v9/workspace/side-chats/${sessionId}/save`, json('POST', { expected_revision: origin.revision }))); await refreshWorkSessions(); toast.success('已保存',{position:'top-center'}) } catch (failure) { toast.error('保存未完成', { description: (failure as Error).message }); setRevision(r => r + 1) } finally { setBusy(false) } }
  useEffect(()=>{const act=(event:Event)=>{const value=(event as CustomEvent<{sessionId:number;action:string}>).detail;if(value.sessionId!==sessionId)return;if(value.action==='history')historyHost.current?.querySelector<HTMLButtonElement>('button')?.click();else if(value.action==='save')void saveRegular();else if(value.action==='record')void edit();else if(value.action==='return'&&latest)onReturn(latest.content)};window.addEventListener('lingxi-side-action',act);return()=>window.removeEventListener('lingxi-side-action',act)},[sessionId,origin,busy,latest])
  return <section className="workspace-chat workspace-side-chat" aria-label="独立侧边对话">
    <span ref={historyHost} className="side-history-hidden"><ConversationHistory mode="work" /></span>
    {error ? <div className="workspace-panel-error" role="alert"><p>{error}</p><Button size="sm" variant="outline" onClick={() => setRevision(r => r + 1)}><RefreshCw />重新核对</Button></div> : <><ConversationTools mode="work" sessionId={sessionId}/>{workMessages.length?<MessageList messages={workMessages} mode="work" preservePosition />:<div className="side-chat-empty"><MessageCirclePlus/><h2>侧边聊天</h2><p>关闭侧聊前会一直保留。</p></div>}<WorkComposer compact onContext={() => void edit()} /></>}
    <Dialog open={editing} onOpenChange={open => { if (!busy) setEditing(open) }}><DialogContent><DialogHeader><DialogTitle>侧聊工作记录</DialogTitle><DialogDescription>只修改此侧聊的目标、材料和限制；项目文件与主会话共用。</DialogDescription></DialogHeader>{record && (['goal', 'materials', 'constraints'] as const).map(key => <label className="space-y-2 text-xs" key={key}><span>{{ goal: '目标', materials: '材料', constraints: '限制' }[key]}</span><Textarea value={record.record[key]} maxLength={1000} onChange={e => setRecord({ ...record, record: { ...record.record, [key]: e.target.value } })} /></label>)}<DialogFooter><Button variant="outline" disabled={busy} onClick={() => setEditing(false)}>取消</Button><Button disabled={busy || !record} onClick={() => void save()}>{busy && <LoaderCircle className="animate-spin" />}保存侧聊记录</Button></DialogFooter></DialogContent></Dialog>
  </section>
}
