import {ComposerLimit} from './ComposerLimit'
import { ConversationQuestions } from './ConversationQuestionCard'
import { ComposerMenu, composerSuggestionKey } from './ComposerMenu'
import { protectMessageUI, useMessageTokens } from '@/context/SurfaceContext'
import { useRef, useState } from 'react'
import { ImagePlus, LoaderCircle, Mic, Paperclip, Plus, Send, Square, Volume2, X } from 'lucide-react'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
import { Button } from '@/components/ui/button'
import { Tooltip } from '@/components/ui/tooltip'
import { useComposerMedia } from './useComposerMedia'
import { ComposerAttachments } from './ComposerAttachments'
import { artifactAccept } from '@/lib/artifacts'
import { ImageGenerationDialog } from './ImageGenerationDialog'
import { QuotePreview } from './QuotePreview'
import { DraftRecovery } from './DraftRecovery'
import { useMessageActions } from '@/context/MessageActionsContext'
import { withMessageQuote } from '@/lib/messageActions'

function CompanionComposerBody() {
  const messageTokens = useMessageTokens()
  const { sendMessage, stopGenerating, generating, sending, connection, activeCharacter, drafts, setDraft, messages, speakMessage, stopSpeech, speakingId, activeSessionId } = useApp()
  const media = useComposerMedia('companion')
  const [imageOpen, setImageOpen] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const {currentQuote,clearQuote} = useMessageActions()
  const quote = currentQuote('companion')
  const draft = drafts.companion
  const connected = connection === 'connected'
  const latest = [...messages].reverse().find((message) => message.role === 'assistant' && message.content)
  const stopping = generating.companion && !draft.trim() && !media.items.length
  const showSeparateStop = generating.companion && (!!draft.trim() || media.items.length > 0)
  const submit = async () => {
    if (stopping) { try { await stopGenerating('companion') } catch (error) { toast.error((error as Error).message) }; return }
    if ((!draft.trim() && !media.items.length) || sending.companion || !connected) return
    try { const prepared=media.prepare(withMessageQuote(draft,quote));await sendMessage('companion', prepared.content, draft);media.sent(prepared);clearQuote('companion',quote) } catch (error) { !(error as Error&{notified?:boolean}).notified&&toast.error('消息未完成发送', { description: (error as Error).message }) }
  }
  return <div className="composer-wrap companion-composer-wrap" data-message-ui="true" style={messageTokens}><ConversationQuestions mode="companion" /><div className="composer companion-composer" onDragOver={event=>{if(event.dataTransfer.types.includes('Files'))event.preventDefault()}} onDrop={media.drop} onPaste={media.paste}>
    <QuotePreview mode="companion" /><DraftRecovery mode={"companion"} />
    <ComposerAttachments media={media} />
    <input aria-label="选择媒体附件" className="hidden" ref={fileInput} type="file" multiple accept={artifactAccept} onChange={(event) => { void media.chooseMany(Array.from(event.target.files || [])); event.target.value = '' }} />
    <textarea data-composer-mode={'companion'} aria-label="消息输入" rows={2} maxLength={8000} placeholder={`和${activeCharacter.name}说点什么…`} value={draft} onChange={(event) => setDraft('companion', event.target.value)} onKeyDown={(event) => { if(composerSuggestionKey(event))return; if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit() } }} />
    <ComposerLimit mode="companion" draft={draft} quoted={withMessageQuote(draft,quote)} media={media}/>
    <div className="companion-composer-toolbar"><ComposerMenu mode="companion" onFile={() => fileInput.current?.click()} fileBusy={media.busy} onImage={() => setImageOpen(true)} />
      <div className="companion-composer-state" role="status">{media.busy && <LoaderCircle className="size-3 animate-spin" />}{media.recording ? '正在录音 · 点击停止' : media.busy ? '正在处理媒体…' : !connected ? '服务未连接' : ''}</div>
      <div className="companion-send-actions">
        <Tooltip content={media.recording ? '停止录音' : '语音输入'}><Button variant="ghost" size="icon" aria-label={media.recording ? '停止录音' : '语音输入'} aria-pressed={media.recording} disabled={media.busy || !connected} onClick={() => void media.record()}>{media.recording ? <Square className="text-red-500" /> : <Mic />}</Button></Tooltip>
        {showSeparateStop && <Tooltip content="停止当前回复"><Button variant="ghost" size="icon" aria-label="停止当前回复" onClick={()=>void stopGenerating('companion').catch(error=>toast.error(error.message))}><Square/></Button></Tooltip>}
        <Tooltip content={stopping ? '停止生成' : '发送消息'}><Button size="icon" className="companion-send-button" aria-label={stopping ? '停止生成' : '发送消息'} disabled={!connected || sending.companion || (!stopping && (media.busy || (!draft.trim() && !media.items.length)))} onClick={() => void submit()}>{stopping ? <Square className="size-3 fill-current" /> : <Send />}</Button></Tooltip>
      </div>
    </div>
  </div><ImageGenerationDialog mode="companion" open={imageOpen} onOpenChange={setImageOpen} /></div>
}

export const CompanionComposer = protectMessageUI(CompanionComposerBody)
