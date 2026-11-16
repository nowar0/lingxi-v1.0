import {Link} from 'react-router-dom'
import { Select } from '@/components/ui/select'
import { WorkSkillsPicker } from './WorkSkillsPicker'
import { PermissionPicker } from './PermissionPicker'
import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { ArrowLeft, AtSign, BookOpen, Download, FileText, Globe, ImagePlus, ListChecks, LoaderCircle, MessageSquare, Minimize2, Paperclip, Plus, Send, Settings2, ShieldCheck, Target, Terminal, X } from 'lucide-react'
import { toast } from 'sonner'
import { api, apiBlob, json } from '@/api/client'
import { lingxi } from '@/api/lingxi'
import { useApp } from '@/context/AppContext'
import { useConversationToolPanel } from '@/context/ConversationToolContext'
import { artifactReference, type Artifact } from '@/lib/artifacts'
import { downloadBlob } from '@/lib/utils'
import type { Message, Mode, Session } from '@/types'
import './composer-menu.css'
import { WorkPresetPicker } from './WorkPresets'

type Controls = { revision: number; goal: string; plan_mode: boolean; permission: 'ask' | 'readonly' | 'workspace' | 'full'; auto_compact: boolean; proactive_compact: boolean; auto_ratio: number; proactive_ratio: number; keep_recent_turns: number; summary_model: string }
type Command = { name: string; argument: string; description: string }
type CompactStatus = { context_capacity?:{tokens:number;source:string;notice:string}; summary?: { id: string; content: unknown }; estimated_history_tokens: number; history_budget_tokens: number; jobs: { id: string; status: string; error: string; trigger: string }[] }

export function composerSuggestionKey(event: KeyboardEvent<HTMLTextAreaElement>) {
  if (event.nativeEvent.isComposing) return false
  const menu = event.currentTarget.closest('.composer')?.querySelector<HTMLElement>('[data-composer-suggestions]')
  if (!menu || !['Enter', 'ArrowDown', 'ArrowUp'].includes(event.key)) return false
  const choices = [...menu.querySelectorAll<HTMLButtonElement>('[data-composer-pick]:not(:disabled)')]
  if (!choices.length) return false
  event.preventDefault()
  if (event.key === 'Enter') choices[0].click()
  else choices[event.key === 'ArrowUp' ? choices.length - 1 : 0].focus()
  return true
}

export function ComposerMenu({ mode, onFile, fileBusy, onImage, onModel, onContext }: { mode: Mode; onFile: () => void; fileBusy: boolean; onImage?: () => void; onModel?: () => void; onContext?: () => void }) {
  const { connection, activeSessionId, activeWorkSessionId, drafts, setDraft, ensureSession, generating, sending } = useApp()
  const sid = mode === 'work' ? activeWorkSessionId : activeSessionId
  const draft = drafts[mode]
  const panel = useConversationToolPanel()
  const id = useId()
  const trigger = useRef<HTMLButtonElement>(null), drawer = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false), [view, setView] = useState('main')
  const [controls, setControls] = useState<Controls>(), [compact, setCompact] = useState<CompactStatus>()
  const [commands, setCommands] = useState<Command[]>([]), [models, setModels] = useState<string[]>([])
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [feedback, setFeedback] = useState('')
  const [referenceType, setReferenceType] = useState<'files' | 'sessions'>(mode==='work'?'files':'sessions'), [query, setQuery] = useState('')
  const [files, setFiles] = useState<{ path: string; directory?: boolean }[]>([]), [sessions, setSessions] = useState<Session[]>([]), [messages, setMessages] = useState<Message[]>([])
  const [referenceSession, setReferenceSession] = useState<number>(), [loadingRefs, setLoadingRefs] = useState(false)
  const source = useRef({ sid, draft, mode, open, view }); source.current = { sid, draft, mode, open, view }
  const epoch = useRef(0), lock = useRef(false), feedbackId = useRef<string>(''), compactId = useRef<string>('')
  const connected = connection === 'connected', locked = busy || generating[mode] || sending[mode]
  const close = (focus = true) => { setOpen(false); if (focus) trigger.current?.focus() }
  const show = (next: string) => { setView(next); setOpen(true); setError('') }
  useEffect(() => { epoch.current++; setOpen(false); setBusy(false); setView('main'); setControls(undefined); setError(''); setMessages([]); feedbackId.current=''; compactId.current='' }, [sid, connected])
  useEffect(() => {
    if (!connected) return
    const controller = new AbortController()
    void api<{ items: Command[] }>('/v6/commands', { signal: controller.signal }).then(v => setCommands(v.items)).catch(() => {})
    return () => controller.abort()
  }, [connected])
  useEffect(() => {
    if (!open || !sid || !connected) return
    const controller = new AbortController()
    const refresh = () => void api<Controls>(`/v6/sessions/${sid}/composer`, { signal: controller.signal }).then(v=>{if(!controller.signal.aborted)setControls(v)}).catch(e => { if (!controller.signal.aborted) setError(e.message) })
    refresh()
    window.addEventListener('lingxi-options-changed', refresh)
    if (view === 'compact') {
      const poll = () => void api<CompactStatus>(`/v6/sessions/${sid}/composer/compaction`, { signal: controller.signal }).then(v=>{if(!controller.signal.aborted)setCompact(v)}).catch(e => { if (!controller.signal.aborted) setError(e.message) })
      poll(); const timer=window.setInterval(poll,1800)
      void api<{models:string[]}>(`/v6/sessions/${sid}/options/catalog`,{signal:controller.signal}).then(v=>setModels(v.models)).catch(()=>{})
      return () => { controller.abort(); window.clearInterval(timer); window.removeEventListener('lingxi-options-changed',refresh) }
    }
    return () => { controller.abort(); window.removeEventListener('lingxi-options-changed',refresh) }
  }, [open, sid, connected, view])
  useEffect(() => {
    if (!connected) return
    if (/^\/[a-z]*$/.test(draft)) { setView('commands'); setOpen(true); return }
    const match = draft.match(/(?:^|\s)@([^\n@]{0,100})$/)
    if (match) { setQuery(match[1]); setView('references'); setOpen(true); return }
    if (source.current.view === 'commands' || source.current.view === 'references') setOpen(false)
  }, [draft, connected])
  useEffect(() => {
    if (!open || view !== 'references' || !connected || !sid) return
    const controller = new AbortController(); setLoadingRefs(true); setError('')
    const timer = window.setTimeout(() => {
      const request = referenceType === 'sessions' ? api<{sessions:Session[]}>(`/v2/sessions?mode=${mode}&limit=100&include_archived=false`,{signal:controller.signal}).then(v => {if(!controller.signal.aborted)setSessions(v.sessions.filter(s => s.id !== sid))}) : query.trim()
        ? api<{items:{path:string}[]}>(`/v9/workspace/search?session_id=${sid}&mode=filename&q=${encodeURIComponent(query.trim())}&limit=30`,{signal:controller.signal}).then(v=>{if(!controller.signal.aborted)setFiles(v.items)})
        : api<{items:{path:string;directory:boolean}[]}>(`/v9/workspace/files?session_id=${sid}`,{signal:controller.signal}).then(v=>{if(!controller.signal.aborted)setFiles(v.items.filter(f=>!f.directory))})
      void request.catch(e=>{if(!controller.signal.aborted)setError(e.message)}).finally(()=>{if(!controller.signal.aborted)setLoadingRefs(false)})
    },150)
    return () => { window.clearTimeout(timer); controller.abort() }
  }, [open,view,query,referenceType,sid,connected,mode])
  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const composer = trigger.current?.closest('.composer'), pane = trigger.current?.closest('.workspace-chat,.workspace-side-chat,.companion-chat-panel,.chat-center')
      if (composer && drawer.current) drawer.current.style.maxHeight = `${Math.max(120,Math.min(550,composer.getBoundingClientRect().top-(pane?.getBoundingClientRect().top||0)-12))}px`
    }
    place(); window.addEventListener('resize',place)
    const observer=new ResizeObserver(place);if(trigger.current?.closest('.composer'))observer.observe(trigger.current.closest('.composer')!)
    const outside=(e:PointerEvent)=>{if(!drawer.current?.contains(e.target as Node)&&!trigger.current?.contains(e.target as Node))close(false)}
    const escape=(e:globalThis.KeyboardEvent)=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();close()}}
    document.addEventListener('pointerdown',outside);document.addEventListener('keydown',escape,true)
    return()=>{observer.disconnect();window.removeEventListener('resize',place);document.removeEventListener('pointerdown',outside);document.removeEventListener('keydown',escape,true)}
  },[open])
  const run = async (operation: (target: number, valid:()=>boolean) => Promise<void>) => {
    if (lock.current || !connected) return
    lock.current=true;setBusy(true);setError('');const captured=epoch.current
    try { const target=sid || await ensureSession(mode); if(captured!==epoch.current&&sid)return; await operation(target,()=>captured===epoch.current&&source.current.sid===target) }
    catch(e){if(captured===epoch.current)setError((e as Error).message)}
    finally{lock.current=false;if(captured===epoch.current)setBusy(false)}
  }
  const save = (patch: Partial<Controls>) => void run(async (target,valid)=>{
    const current=controls || await api<Controls>(`/v6/sessions/${target}/composer`)
    const {revision,...settings}=current
    const value=await api<Controls>(`/v6/sessions/${target}/composer`,json('PUT',{...settings,...patch,expected_revision:revision}))
    if(valid()){setControls(value);toast.success(mode==='work'?'已保存':'会话设置已保存',mode==='work'?{position:'top-center'}:undefined)}
  })
  const tool = (tab: string) => { panel?.setTab(tab); close() }
  const chooseCommand = (command: Command) => { setDraft(mode,command.name+(command.argument?' ':''));close(false);trigger.current?.closest('.composer')?.querySelector('textarea')?.focus() }
  const insert = (text: string) => {
    const current=source.current.draft.replace(/(?:^|\s)@([^\n@]{0,100})$/,'').trimEnd()
    const next=[current,text].filter(Boolean).join('\n\n')
    if(next.length>8000)throw Error('引用和草稿合计超过8000字，请缩短后重试')
    setDraft(mode,next);close(false);trigger.current?.closest('.composer')?.querySelector('textarea')?.focus()
  }
  const referenceFile=(path:string)=>void run(async (target,valid)=>{
    const info=await api<{sha256:string;text:string;reason?:string}>(`/v9/workspace/file?session_id=${target}&path=${encodeURIComponent(path)}`)
    const settings=await api<{path:string}>(`/v6/work-settings/${target}`)
    if(!valid()||!source.current.open)return
    const artifact=await api<Artifact>('/v9/artifacts/from-workspace',json('POST',{session_id:target,path,expected_sha256:info.sha256,expected_project_path:settings.path}))
    if(valid()&&source.current.open)insert(artifactReference(artifact.name,artifact.url))
  })
  useEffect(()=>{
    if(mode!=='work')return
    const action=(event:Event)=>{const detail=(event as CustomEvent<{sessionId:number;action:string}>).detail;if(detail?.sessionId!==sid)return
      if(['goal','plan','compact','permission','feedback','commands'].includes(detail.action)){show(detail.action);return}
      if(detail.action==='model'){close(false);onModel?.();return}
      if(detail.action==='context'){close(false);onContext?.();return}
      if(detail.action==='export'){void run(async target=>{downloadBlob(await apiBlob(`/v6/sessions/${target}/composer/export.zip`),`零汐-会话-${target}.zip`);close()});return}
      tool(detail.action)
    }
    window.addEventListener('lingxi-work-composer-action',action);return()=>window.removeEventListener('lingxi-work-composer-action',action)
  })
  const row=(label:string,alias:string,description:string,Icon:typeof Plus,action:()=>void,disabled=false)=><button type="button" className="composer-menu-row" title={description} disabled={disabled} onClick={action}><Icon aria-hidden="true"/><span>{label}<small>{alias}</small></span><em>{description}</em></button>
  return <>
    <button ref={trigger} type="button" className="composer-drawer-trigger" aria-label="添加与指令" aria-expanded={open} aria-controls={id} onClick={()=>open?close():show('main')}><Plus className={open?'is-open':''}/></button>
    {open&&<div ref={drawer} id={id} role="dialog" aria-label="添加与会话工具" className="composer-action-drawer" data-menu-view={view} data-composer-menu-mode={mode} data-ui-surface="message" data-composer-suggestions={['commands','references'].includes(view)?'true':undefined} onKeyDown={event=>{
      if(!['ArrowDown','ArrowUp','Home','End'].includes(event.key)||event.target instanceof HTMLInputElement||event.target instanceof HTMLSelectElement||event.target instanceof HTMLTextAreaElement)return
      const buttons=[...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')],index=buttons.indexOf(document.activeElement as HTMLButtonElement)
      if(!buttons.length)return;event.preventDefault();buttons[event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length].focus()
    }}>
      <header className="composer-menu-header">{view!=='main'&&<button type="button" aria-label="返回添加菜单" onClick={()=>show('main')}><ArrowLeft/></button>}<strong>{{main:'添加',plan:'计划模式',commands:'指令',references:'引用文件或对话',goal:'会话目标',feedback:'会话反馈',permission:'权限',compact:'上下文压缩',presets:'工作预设',skills:'技能'}[view]||'会话工具'}</strong><button type="button" aria-label="关闭添加菜单" onClick={()=>close()}><X/></button></header>
      {connected&&!sid&&<p className="composer-menu-note">先建立会话后可保存目标与配置；文件添加会建立当前会话。</p>}
      {error&&<p role="alert" className="composer-menu-error">{error}</p>}
      {view==='presets'&&mode==='work'&&<WorkPresetPicker sessionId={sid} disabled={!connected||locked} onSelected={()=>close()}/>} 
      {view==='skills'&&mode==='work'&&<WorkSkillsPicker onSelected={()=>close()}/>}
      {view==='main'&&<>{mode==='work'&&<>
        {row('文件','','上传文件、图片、音频或视频',Paperclip,()=>{close();onFile()},!connected||fileBusy)}
        {row('技能','','选择当前会话使用的技能',BookOpen,()=>show('skills'),!connected)}
        {row('引用','','选择文件或对话片段',AtSign,()=>show('references'),!connected||!sid)}
        {row('全部指令','','查看可用指令',Terminal,()=>show('commands'),!connected)}
      </>}
      {mode!=='work'&&<>
        {row('文件','file','上传文件、图片、音频或视频',Paperclip,()=>{close();onFile()},!connected||fileBusy)}
        {row('引用','mention','选择项目文件或其他会话片段',AtSign,()=>show('references'),!connected||!sid)}
        {onImage&&row('图片','image','生成图片',ImagePlus,()=>{close();onImage()},!connected||fileBusy)}
        {row('目标','goal','设置或查看当前会话长期目标',Target,()=>show('goal'),!connected)}
        {row(controls?.plan_mode?'退出计划':'计划','plan',controls?.plan_mode?'恢复普通会话，原审批规则保留':'先分析与制定计划，只允许只读工具',ListChecks,()=>save({plan_mode:!controls?.plan_mode}),!connected||locked)}
        {row('反馈','feedback','保存关于当前会话的本地反馈',Send,()=>show('feedback'),!connected)}
        <div className="composer-menu-group">指令</div>
        {row('压缩','compact','手动、主动提前和自动压缩',Minimize2,()=>show('compact'),!connected)}
        {row('权限','permission','查看或修改当前会话执行权限',ShieldCheck,()=>show('permission'),!connected)}
        {row('模型','model','选择本会话模型与思考强度',Settings2,()=>{if(onModel){close();onModel()}else tool('settings')},!connected||!sid)}
        {row('下载日志','export','将当前会话与配置摘要导出为 ZIP',Download,()=>void run(async target=>{downloadBlob(await apiBlob(`/v6/sessions/${target}/composer/export.zip`),`零汐-会话-${target}.zip`);close()}),!connected||!sid||busy)}
        <div className="composer-menu-group">会话工具</div>
        {row('全部指令','commands','/ 调用本地指令',Terminal,()=>show('commands'),!connected)}
        {row('会话设置','settings','模型、温度、语音与工具组',Settings2,()=>tool('settings'),!connected||!sid)}
        {row('转工作','work','创建建议后确认拆分，任务不会自动执行',ListChecks,()=>tool('handoff'),!connected||!sid)}
        {row('网页搜索','search','搜索并查看真实来源',Globe,()=>tool('search'),!connected||!sid)}
        {row('定时消息','reminders','管理定时消息',BookOpen,()=>tool('reminders'),!connected||!sid)}
        {onContext&&row('工作记录','context','编辑项目、目标、材料与限制',FileText,()=>{close();onContext()},!connected)}
      </>}
      </>}
      {view==='commands'&&<div className="composer-menu-commands">{commands.filter(c=>!draft.startsWith('/')||draft.includes(' ')||c.name.startsWith(draft)).map(command=><button type="button" className="composer-menu-row" key={command.name} data-composer-pick onClick={()=>chooseCommand(command)}><Terminal/><span>{command.name}<small>{command.argument}</small></span><em>{command.description}</em></button>)}{!commands.length&&<p>正在读取当前程序支持的指令…</p>}</div>}
      {view==='plan'&&<div className="composer-menu-form"><p>先分析和制定计划，仅使用读取工具。</p><button type="button" disabled={locked||!controls} onClick={()=>save({plan_mode:!controls?.plan_mode})}>{controls?.plan_mode?'退出计划模式':'启用计划模式'}</button></div>}
      {view==='goal'&&<form className="composer-menu-form" onSubmit={e=>{e.preventDefault();save({goal:controls?.goal||''})}}><label>当前会话长期目标<textarea maxLength={2000} value={controls?.goal||''} onChange={e=>setControls(value=>value?{...value,goal:e.target.value}:value)}/></label><p>目标随本会话保存并进入上下文；不会授予额外权限或自动执行任务。</p><button type="submit" disabled={locked||!controls}>保存目标</button></form>}
      {view==='feedback'&&<form className="composer-menu-form" onSubmit={e=>{e.preventDefault();void run(async target=>{feedbackId.current ||=crypto.randomUUID();await api(`/v6/sessions/${target}/composer/feedback`,json('POST',{text:feedback,request_id:feedbackId.current}));setFeedback('');feedbackId.current='';toast.success('反馈已保存在本地，没有外发');close()})}}><label>关于当前会话的反馈<textarea maxLength={4000} value={feedback} onChange={e=>{setFeedback(e.target.value);feedbackId.current=''}}/></label><p>仅保存在本地，随当前会话日志导出。</p><button disabled={!feedback.trim()||busy}>保存反馈</button></form>}
      {view==='permission'&&<div className="composer-menu-form"><PermissionPicker mode={mode} inline/></div>}
      {view==='compact'&&<div className="composer-menu-form"><p>{mode==='work'?'生成摘要并保留近期对话，原始消息不删除。':'使用摘要保留目标、约束、进度、决定和相关文件，并保留近期完整问答。原始消息不删除。'}</p>{compact&&<p>历史估算 {compact.estimated_history_tokens.toLocaleString()} / {compact.history_budget_tokens.toLocaleString()} tok（保守估算）</p>}
        <button type="button" disabled={locked||!sid||compact?.jobs.some(j=>['pending','running'].includes(j.status))} onClick={()=>void run(async target=>{compactId.current ||=crypto.randomUUID();const result=await api<{note:string}>(`/v6/sessions/${target}/composer/compact`,json('POST',{request_id:compactId.current}));toast.info(result.note);compactId.current='';setCompact(await api(`/v6/sessions/${target}/composer/compaction`))})}>{mode==='work'?'立即压缩':'立即压缩（调用摘要模型）'}</button>
        {controls&&<><label className="composer-menu-check"><input type="checkbox" checked={controls.auto_compact} disabled={locked} onChange={e=>save({auto_compact:e.target.checked})}/>达到阈值自动压缩</label><label className="composer-menu-check"><input type="checkbox" checked={controls.proactive_compact} disabled={locked} onChange={e=>save({proactive_compact:e.target.checked})}/>{mode==='work'?'提前压缩':'在较低阈值主动提前压缩'}</label><label>自动阈值<input type="number" min={60} max={95} step={5} value={Math.round(controls.auto_ratio*100)} onChange={e=>setControls({...controls,auto_ratio:Number(e.target.value)/100})}/></label><label>主动提前阈值<input type="number" min={30} max={85} step={5} value={Math.round(controls.proactive_ratio*100)} onChange={e=>setControls({...controls,proactive_ratio:Number(e.target.value)/100})}/></label><label>摘要后仍保留原文（轮）<input type="number" min={1} max={10} value={controls.keep_recent_turns} onChange={e=>setControls({...controls,keep_recent_turns:Number(e.target.value)})}/></label><label>摘要模型<Select value={controls.summary_model} onChange={e=>setControls({...controls,summary_model:e.target.value})}><option value="">沿用当前会话模型</option>{models.map(model=><option key={model}>{model}</option>)}</Select></label><button type="button" disabled={locked} onClick={()=>save({})}>保存压缩设置</button></>}
        <Link to="/settings?tab=conversations" onClick={()=>close(false)}>打开上下文与记忆默认设置</Link>{compact?.context_capacity&&<p>当前模型容量：{compact.context_capacity.tokens.toLocaleString()} tokens · {compact.context_capacity.source==='service'?'服务元数据':compact.context_capacity.source==='manual'?'手动设置':'尚未确认，使用全局后备值'}</p>}<p className="text-xs">摘要后保留轮数与近期原文上限不相加；一轮是一组输入和回复。压缩保留原聊天。</p>
        <p>{mode==='work'?'压缩会调用模型并计入用量，完成后用于后续对话。':'提前压缩会额外调用模型并记录用量；正在生成时使用已有上下文，完整摘要确认成功后用于后续请求。'}</p>{compact?.jobs.slice(0,3).map(job=><p key={job.id} role="status">{({manual:'手动',proactive:'主动提前',automatic:'自动'} as Record<string,string>)[job.trigger]||'摘要'} · {({pending:'等待中',running:'生成摘要中',complete:'已完成',failed:'失败，原上下文保留',stale:'来源已变化，结果未应用',interrupted:'已中断，原上下文保留'} as Record<string,string>)[job.status]||job.status}{job.error&&`：${job.error}`}</p>)}{compact?.summary&&<details><summary>查看当前摘要</summary><pre>{JSON.stringify(compact.summary.content,null,2)}</pre></details>}
      </div>}
      {view==='references'&&<div className="composer-reference-picker" aria-busy={loadingRefs}><div className="composer-reference-tabs">{mode==='work'&&<button type="button" aria-pressed={referenceType==='files'} onClick={()=>{setReferenceType('files');setMessages([]);setReferenceSession(undefined)}}><FileText/>项目文件</button>}<button type="button" aria-pressed={referenceType==='sessions'||mode==='companion'} onClick={()=>{setReferenceType('sessions');setMessages([]);setReferenceSession(undefined)}}><MessageSquare/>对话片段</button></div><input aria-label="搜索文件或对话" placeholder="搜索文件名或会话标题…" value={query} onChange={e=>setQuery(e.target.value)}/>{loadingRefs&&<p role="status">正在读取…</p>}{referenceType==='files'&&mode==='work'?files.map(file=><button type="button" className="composer-menu-row" key={file.path} data-composer-pick disabled={loadingRefs||busy} onClick={()=>referenceFile(file.path)}><FileText/><span>{file.path}</span></button>):referenceSession!==undefined?<><button type="button" className="composer-menu-row" onClick={()=>{setReferenceSession(undefined);setMessages([])}}><ArrowLeft/>返回会话列表</button>{!messages.length&&<p className="composer-menu-note">这段对话还没有可引用的消息。</p>}{messages.filter(m=>m.content.trim()).slice(-20).map(message=><button type="button" className="composer-reference-message" key={message.id} data-composer-pick disabled={busy} onClick={()=>{try{insert(`> 引用${mode==='work'?'工作':'陪伴'}会话 #${referenceSession} · ${message.role==='user'?'用户':'助手'}\n${message.content.slice(0,5000).split('\n').map(line=>'> '+line).join('\n')}${message.content.length>5000?'\n> （节选）':''}`)}catch(e){setError((e as Error).message)}}}>{message.role==='user'?'用户':'助手'}：{message.content.slice(0,180)}</button>)}</>:sessions.filter(s=>s.title.includes(query)).map(item=><button type="button" className="composer-menu-row" key={item.id} data-composer-pick disabled={loadingRefs||busy} onClick={()=>void run(async(_target,valid)=>{const data=await lingxi.snapshot(item.id,mode);if(valid()&&source.current.open){setReferenceSession(item.id);setMessages(data.messages)}})}><MessageSquare/><span>{item.title||'未命名会话'}</span></button>)}<p className="composer-menu-note">先选择要引用的片段，再由你发送；不会把整个会话自动交给模型。</p></div>}
      {busy&&<p role="status" className="composer-menu-note"><LoaderCircle className="animate-spin"/>正在处理…</p>}
    </div>}
  </>
}
