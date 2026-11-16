import { ConversationQuestions } from './ConversationQuestionCard'
import { protectMessageUI, useMessageTokens } from '@/context/SurfaceContext'
import { ImageGenerationDialog } from './ImageGenerationDialog'
import { QuotePreview } from './QuotePreview'
import { DraftRecovery } from './DraftRecovery'
import { useMessageActions } from '@/context/MessageActionsContext'
import { withMessageQuote } from '@/lib/messageActions'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { useRef, useState } from 'react'
import { useComposerMedia } from './useComposerMedia'
import { ComposerAttachments } from './ComposerAttachments'
import { artifactAccept } from '@/lib/artifacts'
import { ArrowUp, AtSign, Image, Mic, Paperclip, Square, Volume2, X, LoaderCircle } from 'lucide-react'
import { toast } from 'sonner'
import type { Mode } from '@/types'
import { Button } from '@/components/ui/button'
import { Tooltip } from '@/components/ui/tooltip'
import { useApp } from '@/context/AppContext'
import { cn } from '@/lib/utils'

function ChatComposerBody({ mode, className, placeholder }: { mode: Mode; className?: string; placeholder?: string }) {
  const messageTokens = useMessageTokens()
  const { sendMessage, stopGenerating, generating, sending, connection, activeCharacter, drafts, setDraft, messages, workMessages, speakMessage, stopSpeech, speakingId, activeSessionId, activeWorkSessionId } = useApp()
  const {currentQuote,clearQuote} = useMessageActions()
  const quote = currentQuote(mode)
  const draft = drafts[mode]
  const media = useComposerMedia(mode)
  const [imageOpen, setImageOpen] = useState(false)
  const currentSession=mode==='companion'?activeSessionId:activeWorkSessionId
  const input = useRef<HTMLInputElement>(null)
  const latest = [...(mode === 'companion' ? messages : workMessages)].reverse().find((message) => message.role === 'assistant' && message.content)
  const choose = () => input.current?.click()
  const submit = async () => {
    if ((!draft.trim() && !media.items.length) || sending[mode] || connection !== 'connected') return
    try { const prepared=media.prepare(withMessageQuote(draft,quote));await sendMessage(mode, prepared.content, draft);media.sent(prepared);clearQuote(mode,quote) } catch (error) { toast.error('消息未完成发送', { description: (error as Error).message }) }
  }
  return <div data-message-ui="true" style={messageTokens} className={cn('composer-wrap', className)}><ConversationQuestions mode={mode} /><div className="composer" onDragOver={event=>{if(event.dataTransfer.types.includes('Files'))event.preventDefault()}} onDrop={media.drop} onPaste={media.paste}>
    <QuotePreview mode={mode} /><DraftRecovery mode={mode} />
    <ComposerAttachments media={media} />
    <input aria-label="选择媒体附件" className="hidden" ref={input} type="file" multiple accept={artifactAccept} onChange={(event) => { void media.chooseMany(Array.from(event.target.files || [])); event.target.value = '' }} />
    <textarea data-composer-mode={mode} aria-label="消息输入" rows={1} maxLength={8000} placeholder={placeholder || (mode === 'companion' ? `和${activeCharacter.name}说点什么…` : '描述目标、材料或下一步…')} value={draft} onChange={(event) => setDraft(mode, event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit() } }} />
    <div className="flex items-center gap-1">
      
      <Tooltip content="添加附件"><Button variant="ghost" size="icon" className="size-7" aria-label="添加附件" disabled={media.busy || connection !== 'connected'} onClick={choose}><Paperclip /></Button></Tooltip>
      {mode === 'companion' ? <><DropdownMenu><DropdownMenuTrigger asChild><Button variant="ghost" size="icon" className="size-7" aria-label="图片操作" disabled={media.busy || connection !== 'connected'}><Image /></Button></DropdownMenuTrigger><DropdownMenuContent><DropdownMenuItem onSelect={choose}>上传并识别图片</DropdownMenuItem><DropdownMenuItem onSelect={() => setImageOpen(true)}>生成图片</DropdownMenuItem></DropdownMenuContent></DropdownMenu><Tooltip content="语音输入"><Button variant="ghost" size="icon" className="size-7" aria-label={media.recording ? '停止录音' : '语音输入'} disabled={media.busy || connection !== 'connected'} onClick={() => void media.record()}>{media.recording ? <Square className="text-red-500" /> : <Mic />}</Button></Tooltip></> : <Tooltip content="引用上下文"><Button variant="ghost" size="icon" className="size-7" onClick={() => setDraft(mode, `${draft}@当前项目 `)}><AtSign /></Button></Tooltip>}
      {latest && <Tooltip content={speakingId !== undefined ? '停止朗读' : '朗读最新回复'}><Button aria-label={speakingId !== undefined ? '停止朗读' : '朗读最新回复'} variant="ghost" size="icon" className="size-7" onClick={() => speakingId !== undefined ? stopSpeech() : void speakMessage(mode, latest).catch((error) => toast.error('朗读失败', { description: (error as Error).message }))}>{speakingId !== undefined ? <Square /> : <Volume2 />}</Button></Tooltip>}
      {media.busy && <LoaderCircle className="size-3 animate-spin" />}
      <span className="ml-1 hidden text-[10px] text-muted-foreground sm:inline">{media.recording ? '正在录音 · 最长 120 秒' : connection !== 'connected' ? '服务未连接' : mode === 'work' ? '工作模型' : '陪伴模型'}</span>
      <Button size="icon" className="ml-auto size-8 rounded-[10px]" onClick={() => generating[mode] && !draft.trim() && !media.items.length ? void stopGenerating(mode).catch((error) => toast.error((error as Error).message)) : void submit()} disabled={connection !== 'connected' || sending[mode] || (!(generating[mode] && !draft.trim() && !media.items.length) && (media.busy || (!draft.trim() && !media.items.length)))} aria-label={generating[mode] && !draft.trim() && !media.items.length ? '停止生成' : '发送消息'}>{generating[mode] && !draft.trim() && !media.items.length ? <Square className="size-3 fill-current" /> : <ArrowUp />}</Button>
    </div>
  </div><ImageGenerationDialog mode={mode} open={imageOpen} onOpenChange={setImageOpen} /><div className="mt-1.5 text-center text-[9px] text-muted-foreground/65">Enter 发送 · Shift + Enter 换行{mode === 'work' ? ' · 文件与命令写入需要人工审批' : ' · 零汐可能会犯错，请注意辨别'}</div></div>
}


export const ChatComposer = protectMessageUI(ChatComposerBody)
