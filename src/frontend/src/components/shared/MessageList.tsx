import { protectMessageUI, useMessageTokens } from '@/context/SurfaceContext'
import { ToolActivities } from './ToolActivities'
import { MessageActions } from './MessageActions'
import { useMessagePosition } from './useMessagePosition'
import { MessageContent } from './MessageContent'
import { useEffect, useRef } from 'react'
import { Check, ListChecks, RefreshCcw, Sparkles, Volume2, Square } from 'lucide-react'
import { toast } from 'sonner'
import type { Message, Mode } from '@/types'
import { cn } from '@/lib/utils'
import { formatTime } from '@/lib/format'
import { CharacterAvatar } from './CharacterAvatar'
import { Button } from '@/components/ui/button'
import { useApp } from '@/context/AppContext'
import { WorkMessageNavigator } from './WorkMessageNavigator'
import { SelectionActions } from './SelectionActions'

function MessageListBody({ messages, mode, emptyText, preservePosition = false, navigation=false, navigationHidden=false }: { messages: Message[]; mode: Mode; emptyText?: string; preservePosition?: boolean; navigation?:boolean; navigationHidden?:boolean }) {
  const messageTokens = useMessageTokens()
  const bottom = useRef<HTMLDivElement>(null)
  const { conversationPaging,activeCharacter, generating, sending, retryMessage, connection, conversationBatches, activeSessionId, activeWorkSessionId, conversationVersions, speakMessage, stopSpeech, speakingId, modules } = useApp()
  const waitingQuestion = conversationBatches[mode].some(b=>b.status==='generating' && b.details?.questions?.some(q=>q.status==='waiting' && q.valid))
  const awaitingReply = !waitingQuestion && (generating[mode] || (mode === 'companion' && sending[mode]))
  const position = useMessagePosition({ enabled: preservePosition, sessionKey: `${mode}:${mode === 'companion' ? activeSessionId : activeWorkSessionId}`, ready: connection !== 'connected' || conversationVersions[mode] >= 0, content: messages })
  useEffect(() => { if (!preservePosition) bottom.current?.scrollIntoView({ block: 'end' }) }, [messages.length, awaitingReply, preservePosition])
  const paging=conversationPaging[mode]
  const content=<div className="message-scroll" data-message-ui="true" style={messageTokens} ref={position.ref} onScroll={position.save}><div className="message-column">
    {paging.hasMore&&<div className="message-history-load"><Button variant="ghost" size="sm" disabled={paging.loadingMore} onClick={()=>{const node=position.ref.current;const height=node?.scrollHeight||0;const top=node?.scrollTop||0;void paging.loadMore().then(()=>requestAnimationFrame(()=>{if(node)node.scrollTop=top+node.scrollHeight-height})).catch(error=>toast.error('历史加载失败',{description:error.message}))}}>{paging.loadingMore?'正在加载…':'加载更早消息'}</Button></div>}
    {!messages.length && !awaitingReply && <div className="flex min-h-64 flex-col items-center justify-center text-center"><CharacterAvatar src={activeCharacter.avatar} name={activeCharacter.name} className="mb-4 size-14 rounded-2xl" /><h3 className="text-sm font-medium">{emptyText || '从这里开始新的对话'}</h3></div>}
    {messages.map((message) => {
      const details=conversationBatches[mode].find(b=>b.id===message.batch_id)?.details
      if (message.role==='assistant' && !message.content.trim() && details?.questions?.some(q=>q.status==='waiting' && q.valid) && !details.tools.some(t=>t.tool!=='request_user_input')) return null
      if (mode === 'companion' && message.role === 'assistant') {
        if (!message.content.trim() && !conversationBatches[mode].find(b=>b.id===message.batch_id)?.details?.tools.length) {
          if (!['failed', 'stopped'].includes(message.delivery_status || '')) return null
          return <div key={message.id} className="flex items-center gap-2 py-2 text-xs text-muted-foreground" role="status"><span>{message.delivery_status === 'stopped' ? '回复已停止' : '回复失败'}</span><Button variant="ghost" size="xs" aria-label="重试回复" disabled={awaitingReply || connection !== 'connected'} onClick={() => void retryMessage(mode, message).catch((error) => toast.error('重试失败', { description: (error as Error).message }))}><RefreshCcw className="size-3" />重试</Button></div>
        }
      }
      return <article key={message.id} data-message-id={String(message.id)} className={cn('message-row', message.role === 'user' && 'is-user')}>
      {message.role !== 'user' && <CharacterAvatar src={message.speaker?.avatar || (mode === 'companion' ? activeCharacter.avatar : undefined)} name={message.speaker?.name || (mode === 'work' ? '工作' : activeCharacter.name)} className="mt-1 size-8 rounded-[10px]" />}
      <div className={cn('min-w-0 max-w-[78%]', message.role === 'user' && 'flex flex-col items-end')}>
        <div className="mb-1.5 flex items-center gap-2 text-[10px] text-muted-foreground"><span>{message.role === 'user' ? '你' : message.speaker?.name || (mode === 'work' ? '工作 Agent' : activeCharacter.name)}</span><span>{formatTime(message.at)}</span>{message.delivery_status === 'failed' && <span className="text-red-500">回复或发送失败</span>}{message.delivery_status === 'generating' && message.role === 'assistant' && <span>{details?.questions?.some(q=>q.status==='waiting' && q.valid)?'等待回答':'生成中'}</span>}{message.delivery_status === 'stopped' && <span>{message.role === 'assistant' ? '已停止 · 保留部分内容' : '本轮已停止'}</span>}</div>
        <MessageActions message={message} mode={mode} className={cn('message-bubble', message.role === 'user' ? 'is-user' : 'is-assistant')}><MessageContent text={message.content} /></MessageActions>
        {message.role==='assistant'&&<ToolActivities mode={mode} batchId={message.batch_id}/> }
        {message.role==='assistant'&&message.content.trim()&&message.delivery_status!=='generating'&&<div className="message-playback-row"><Button variant="ghost" size="xs" className="message-playback" aria-label={speakingId===message.id?'停止朗读此回复':'朗读此回复'} aria-pressed={speakingId===message.id} disabled={speakingId!==message.id&&(connection!=='connected'||!modules.find(item=>item.id==='tts')?.enabled)} onClick={()=>speakingId===message.id?stopSpeech():void speakMessage(mode,message).catch(error=>toast.error('朗读失败',{description:error.message}))}>{speakingId===message.id?<Square/>:<Volume2/>}<span>{speakingId===message.id?'停止':'朗读'}</span></Button></div>}
        {['failed', 'stopped'].includes(message.delivery_status || '') && <Button aria-label="重试消息" variant="ghost" size="xs" className="mt-1" disabled={generating[mode] || connection !== 'connected'} onClick={() => void retryMessage(mode, message).catch((error) => toast.error('重试失败', { description: (error as Error).message }))}><RefreshCcw className="size-3" />重试</Button>}
      </div>
    </article>})}
    {connection === 'demo' && mode === 'companion' && messages.length > 1 && <div className="ml-10 mr-auto my-2 w-[min(430px,85%)] rounded-xl border border-primary/10 bg-primary/[0.055] p-3.5"><div className="flex items-center gap-2 text-xs font-medium"><ListChecks className="size-4 text-primary" />对话中的任务<span className="ml-auto text-[10px] font-normal text-primary">界面演示</span></div><p className="mt-2 text-xs leading-5 text-muted-foreground">梳理新 UI 的页面、交互和真实接口边界</p><div className="mt-3 flex items-center gap-2 text-[10px] text-muted-foreground"><span className="size-1.5 rounded-full bg-emerald-500" />资料与接口盘点已完成</div></div>}
    {connection === 'demo' && mode === 'work' && messages.length > 1 && <div className="ml-10 mr-auto my-2 w-[min(500px,88%)] overflow-hidden rounded-xl border border-border/75 bg-card/70"><button className="flex w-full items-center gap-2 px-3.5 py-3 text-left text-xs"><Sparkles className="size-4 text-primary" /><span className="font-medium">已读取项目接口与页面结构</span><span className="ml-auto text-[10px] text-muted-foreground">3 个步骤</span></button><div className="border-t border-border/60 bg-secondary/35 px-3.5 py-2.5 font-mono text-[10px] leading-5 text-muted-foreground"><div><Check className="mr-1 inline size-3 text-emerald-500" />扫描 FastAPI 路由与现有调用</div><div><Check className="mr-1 inline size-3 text-emerald-500" />整理会话与任务状态边界</div></div></div>}
    {awaitingReply && <div className={cn('message-row', mode === 'companion' && 'pl-10')} role="status" aria-label="正在生成回复">{mode !== 'companion' && <CharacterAvatar name={activeCharacter.name} className="size-8 rounded-[10px]" />}<div className={cn('flex h-9 items-center gap-1 px-4', mode !== 'companion' && 'rounded-xl bg-card/70')}><span className="typing-dot" /><span className="typing-dot [animation-delay:120ms]" /><span className="typing-dot [animation-delay:240ms]" /></div></div>}
    <div ref={bottom} />
    <SelectionActions scrollRef={position.ref} mode={mode}/>
  </div></div>
  return navigation&&mode==='work'?<div className="work-message-view">{content}<WorkMessageNavigator messages={messages} sessionId={activeWorkSessionId} scrollRef={position.ref} enabled={!navigationHidden}/></div>:content
}

export const MessageList = protectMessageUI(MessageListBody)
