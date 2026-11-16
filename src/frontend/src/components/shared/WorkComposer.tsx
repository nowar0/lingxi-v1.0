import {ComposerLimit} from './ComposerLimit'
import { ConversationQuestions } from './ConversationQuestionCard'
import { PermissionPicker } from './PermissionPicker'
import { evolution, type OptionsView } from '@/lib/evolution'
import { ComposerMenu, composerSuggestionKey } from './ComposerMenu'
import { protectMessageUI, useMessageTokens } from '@/context/SurfaceContext'
import { QuotePreview } from './QuotePreview'
import { DraftRecovery } from './DraftRecovery'
import { WorkStats } from './WorkStats'
import { useMessageActions } from '@/context/MessageActionsContext'
import { withMessageQuote } from '@/lib/messageActions'
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import * as Popover from '@radix-ui/react-dialog'
import { useNavigate } from 'react-router-dom'
import { ArrowUp, ArrowUpRight, AtSign, Check, ChevronDown, FileText, LoaderCircle, Paperclip, Plus, RefreshCw, Send, ShieldCheck, Square, Volume2, X, RotateCcw, ChevronRight, ArrowLeft, Zap } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { lingxi } from '@/api/lingxi'
import { useApp } from '@/context/AppContext'
import { ReasoningControl } from './ReasoningControl'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { useComposerMedia } from './useComposerMedia'
import { ComposerAttachments } from './ComposerAttachments'
import { artifactAccept } from '@/lib/artifacts'
import { effortLevels, effortNames, effortLabels, modelLabel, compatibleEffort, saveWorkSelection, workModelChoices, type Effort, type WorkBinding, type ReasoningCapability } from '@/lib/workComposerSettings'
import './work-composer.css'

function WorkComposerBody({ onContext, header, compact=false }: { onContext: () => void; header?: ReactNode; compact?:boolean }) {
  const messageTokens = useMessageTokens()
  const { activeWorkSessionId: sid, connection, drafts, setDraft, sendMessage, sending, generating, stopGenerating, workMessages, modules, profiles, usage, speakMessage, speakingId, stopSpeech } = useApp()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [modelView,setModelView]=useState<'effort'|'models'>('effort')
  const [modelQuery,setModelQuery]=useState('')
  useEffect(()=>{if(open){setModelView('effort');setModelQuery('')}},[open])
  const [binding, setBinding] = useState<WorkBinding>()
  const [sessionOptions,setSessionOptions]=useState<OptionsView>()
  const [choices, setChoices] = useState<{ value: string; label: string }[]>([])
  const [modelCapabilities, setModelCapabilities] = useState<Record<string, ReasoningCapability>>({})
  const [defaultModel, setDefaultModel] = useState('work')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  const trigger = useRef<HTMLButtonElement>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const file = useRef<HTMLInputElement>(null)
  const alive = useRef(true)
  const writeLock = useRef(false)
  const media = useComposerMedia('work')
  const { currentQuote, clearQuote } = useMessageActions()
  const quote = currentQuote('work')
  const draft = drafts.work
  const workModule = modules.find((item) => item.id === 'work')
  const profile = profiles.find((item) => item.id === workModule?.profile_id)
  const model = sessionOptions?.settings.model || binding?.preset?.model || defaultModel
  const capability = modelCapabilities[model]
  const effort = compatibleEffort(sessionOptions?.settings.effort&&sessionOptions.settings.effort!=='auto'?sessionOptions.settings.effort:binding?.preset?.effort || workModule?.reasoning_effort, capability)
  const level = effortLevels.indexOf(effort)
  const displayModel = model === defaultModel ? profile?.name || workModule?.model || '工作模型' : choices.find((choice) => choice.value === model)?.label || modelLabel(model)
  const ready = connection === 'connected'
  const locked = loading || saving || generating.work || sending.work || !ready || !binding || !choices.length
  const latest = [...workMessages].reverse().find((message) => message.role === 'assistant' && message.content)
  const turnCount = workMessages.filter((message) => message.role === 'user').length
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    let cancelled = false
    setLoading(true); setError(''); setBinding(undefined)
    if (!sid || !ready) { setLoading(false); return }
    Promise.all([api<WorkBinding>(`/v7/sessions/${sid}/preset`), lingxi.models(), evolution.options(sid)]).then(([current, available, options]) => {
      if (cancelled) return
      const fallback = available.roles.work ? 'work' : 'chat'
      setDefaultModel(fallback)
      const entries = workModelChoices(available)
      if (current.preset?.model && !entries.some((item) => item.value === current.preset!.model)) entries.push({ value: current.preset.model, label: `${modelLabel(current.preset.model)}（当前配置）` })
      setSessionOptions(options); setBinding(current); setChoices(entries.map(item=>({...item,label:available.roles[item.value]?modelLabel(available.roles[item.value]):item.label}))); setModelCapabilities(available.reasoning || {})
    }).catch((failure) => { if (!cancelled) setError((failure as Error).message) }).finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [sid, ready, reload, modules])
  useEffect(() => {
    const input = textarea.current
    if (!input) return
    const resize = () => { input.style.height = '62px'; if (input.value) input.style.height = `${Math.min(200, Math.max(62, input.scrollHeight))}px` }
    resize(); let width = input.clientWidth
    const observer = new ResizeObserver(() => { if (input.clientWidth !== width) { width = input.clientWidth; resize() } })
    observer.observe(input)
    return () => observer.disconnect()
  }, [draft])
  const change = async (nextModel: string, nextEffort: Effort) => {
    if (!sid || locked || writeLock.current || nextModel === model && nextEffort === effort) return
    writeLock.current = true; setSaving(true); setError('')
    try {
      if(sessionOptions&&(sessionOptions.settings.model||sessionOptions.settings.effort!=='auto')){const saved=await evolution.saveOptions(sid,{...sessionOptions.settings,model:nextModel,effort:nextEffort});if(alive.current)setSessionOptions(saved)}
      else {const saved = await saveWorkSelection(api, sid, binding!.revision, nextModel, nextEffort);if (alive.current) setBinding(saved)}
    } catch (failure) {
      if (alive.current) { setError('保存失败，请重试。'); setReload((value) => value + 1) }
      toast.error('模型设置未保存，请重试')
    } finally { writeLock.current = false; if (alive.current) setSaving(false) }
  }
  useEffect(()=>{const changed=()=>setReload(value=>value+1);window.addEventListener('lingxi-options-changed',changed);return()=>window.removeEventListener('lingxi-options-changed',changed)},[])
  const submit = async () => {
    if ((!draft.trim() && !media.items.length) || !ready || sending.work || loading || saving || writeLock.current) return
    try { const prepared = media.prepare(withMessageQuote(draft, quote)); await sendMessage('work', prepared.content, draft); media.sent(prepared); clearQuote('work', quote) } catch (failure) { !(failure as Error&{notified?:boolean}).notified&&toast.error('消息未完成发送', { description: (failure as Error).message }) }
  }
  const bounds = trigger.current?.getBoundingClientRect()
  const position: CSSProperties = bounds ? { left: Math.max(12, Math.min(window.innerWidth - 344, bounds.right - 320)), bottom: Math.max(12, window.innerHeight - bounds.top + 8) } : {}
  const stop = generating.work && !draft.trim() && !media.items.length
  return <div data-message-ui="true" style={messageTokens} className={`composer-wrap work-composer-wrap${compact?' is-compact':''}`}>
    <ConversationQuestions mode="work" />
    <div className={`composer work-composer${header?' has-project-header':''}`} onDragOver={event=>{if(event.dataTransfer.types.includes('Files'))event.preventDefault()}} onDrop={media.drop} onPaste={media.paste}>{header}<QuotePreview mode="work" /><DraftRecovery mode={"work"} />
      <ComposerAttachments media={media} work />
      <input ref={file} className="hidden" type="file" aria-label="选择媒体附件" multiple accept={artifactAccept} onChange={(event) => { void media.chooseMany(Array.from(event.target.files || [])); event.target.value = '' }} />
      <textarea ref={textarea} data-composer-mode="work" aria-label="工作消息输入" rows={2} maxLength={8000} placeholder={compact?'与零汐一起工作':'发消息或创建任务，/ 调用指令，@ 文件或对话'} value={draft} onChange={(event) => setDraft('work', event.target.value)} onKeyDown={(event) => { if(composerSuggestionKey(event))return; if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit() } }} />
    <ComposerLimit mode="work" draft={draft} quoted={withMessageQuote(draft,quote)} media={media}/>
      <div className="work-composer-controls">
        <ComposerMenu mode="work" onFile={() => file.current?.click()} fileBusy={media.busy} onContext={onContext} onModel={() => setOpen(true)} />
        
        <PermissionPicker mode="work" compact={compact}/>
        {media.busy && <LoaderCircle className="work-composer-busy animate-spin" aria-label="处理附件中" />}
        <Popover.Root open={open} onOpenChange={setOpen} modal={false}><Popover.Trigger asChild><button type="button" ref={trigger} className="work-composer-model" aria-label="模型与推理强度" disabled={!ready}><span className="work-model-name">{loading?'读取模型…':displayModel}</span><span className="work-model-strength">{effortLabels[level]}</span>{saving?<LoaderCircle className="animate-spin"/>:<ChevronDown/>}</button></Popover.Trigger><Popover.Portal><Popover.Content data-ui-surface="message" className={`work-model-popover work-model-customer ${modelView==='effort'?'is-effort':'is-models'}`} style={{...messageTokens,...position}}>
          <Popover.Title className="sr-only">模型与推理强度</Popover.Title><Popover.Description className="sr-only">当前会话</Popover.Description>
          {modelView==='effort'?<><button type="button" className="work-current-model" aria-label="选择工作模型" disabled={saving||loading} onClick={()=>setModelView('models')}>{displayModel}<ChevronRight/></button>{effort!=='auto'&&<button type="button" className="work-effort-reset" aria-label="重置推理强度" disabled={locked} onClick={()=>void change(model,'auto')}><RotateCcw/></button>}<ReasoningControl key={`${binding?.revision}:${reload}:${model}`} value={effort} capability={capability} disabled={locked} onCommit={value=>void change(model,value)}/></>:<><div className="work-model-list-heading"><button type="button" aria-label="返回推理强度" onClick={()=>setModelView('effort')}><ArrowLeft/></button><input aria-label="搜索工作模型" placeholder="选择模型" value={modelQuery} onChange={event=>setModelQuery(event.target.value)}/></div><div className="work-model-list" role="listbox" aria-label="工作模型列表">{choices.filter(choice=>choice.label.toLowerCase().includes(modelQuery.toLowerCase())).map(choice=><button type="button" role="option" key={choice.value} aria-selected={model===choice.value} disabled={locked} onClick={()=>void change(choice.value,compatibleEffort(effort,modelCapabilities[choice.value])).then(()=>setModelView('effort'))}><span>{choice.value===defaultModel?'默认':choice.label}{choice.value===defaultModel&&<small>{profile?.name||workModule?.model||'工作模型'}</small>}</span>{model===choice.value&&<Check/>}</button>)}</div></>}
          {saving&&<p className="work-model-save-state" role="status">正在保存…</p>}{error&&<p className="work-model-error" role="alert">{error}<button type="button" onClick={()=>setReload(v=>v+1)}>重试</button></p>}
        </Popover.Content></Popover.Portal></Popover.Root>
        {generating.work&&!!draft.trim()||generating.work&&!!media.items.length?<Button variant="ghost" size="icon" aria-label="停止当前回复" onClick={()=>void stopGenerating('work').catch(error=>toast.error(error.message))}><Square/></Button>:null}<Button size="icon" className="work-composer-send" aria-label={stop ? '停止生成' : '发送工作消息'} disabled={!ready || sending.work || (!stop && (loading || saving || media.busy || (!draft.trim() && !media.items.length)))} onClick={() => stop ? void stopGenerating('work').catch((failure) => toast.error((failure as Error).message)) : void submit()}>{stop ? <Square className="fill-current" /> : compact?<ArrowUp/>:<Send />}</Button>
      </div>
    </div>
    {!compact&&<WorkStats sessionId={sid} connected={ready} generating={generating.work} turnCount={turnCount} revision={generating.work ? 'generating' : workMessages.map((message) => `${message.id}:${message.delivery_status}:${message.content}`).join('\u0000')} />}
  </div>
}

export const WorkComposer = protectMessageUI(WorkComposerBody)
