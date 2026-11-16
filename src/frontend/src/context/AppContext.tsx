import { createContext, useCallback, useContext, useEffect, useRef, useState,useSyncExternalStore,useLayoutEffect,useMemo, type ReactNode } from 'react'
import { evolution, sentenceParts } from '@/lib/evolution'
import { toast } from 'sonner'
import { lingxi, type RestorePreview, type MessageMutationResult, type SessionSnapshot } from '@/api/lingxi'
import { demoCharacter } from '@/lib/demo'
import { useConversation, type DraftConflict } from './useConversation'
import { attachSpeechAnalyser } from '@/lib/speechAnalyser'
import type { ToolApproval, Character, ConnectionState, Health, MemoryItem, Message, Mode, ModelModule, ModelProfile, ScheduleItem, Session, TaskItem, UiState, Usage } from '@/types'

interface AppContextValue {
  conversationBatches:Record<Mode,SessionSnapshot['batches']>; decideToolApproval:(mode:Mode,approval:ToolApproval,allowed:boolean)=>Promise<void>
  connection: ConnectionState; health?: Health; usage?: Usage; uiState?: UiState
  dataErrors: Record<string, string>
  sessions: Session[]; workSessions: Session[]; activeSessionId?: number; activeWorkSessionId?: number
  characters: Character[]; activeCharacter: Character; messages: Message[]; workMessages: Message[]
  memories: MemoryItem[]; schedule: ScheduleItem[]; tasks: TaskItem[]; modules: ModelModule[]; profiles: ModelProfile[]
  generating: Record<Mode, boolean>; sending: Record<Mode, boolean>; drafts: Record<Mode, string>
  draftConflicts: Record<Mode, DraftConflict | undefined>; draftBlocked: Record<Mode, boolean>
  pendingInputs: Record<Mode, { content: string; stale?: boolean; accepted?: boolean } | undefined>
  reloadDraftConflict: (mode: Mode) => Promise<void>
  resolveDraft: (mode: Mode, choice: 'local' | 'remote') => Promise<void>
  cancelPendingInput: (mode: Mode) => Promise<void>
  setDraft: (mode: Mode, value: string) => void
  setActiveSessionId: (id: number) => void; setActiveWorkSessionId: (id: number) => void
  createSession: (mode: Mode) => Promise<number>; ensureSession: (mode: Mode) => Promise<number>
  sendMessage: (mode: Mode, content: string, draft?: string) => Promise<void>; stopGenerating: (mode: Mode) => Promise<void>
  retryMessage: (mode: Mode, message: Message) => Promise<void>
  mutateMessage: (mode: Mode, message: Message, content?: string) => Promise<MessageMutationResult>
  undoMessageDeletion: (mode: Mode, sid: number, key: string, version: number) => Promise<void>
  restoreConversation: (preview: RestorePreview) => Promise<void>; conversationVersions: Record<Mode, number>
  speakMessage: (mode: Mode, message: Message, sessionId?: number) => Promise<void>; stopSpeech: () => void; speakingId?: Message['id']
  renameSession: (session: Session, title: string) => Promise<void>; archiveSession: (session: Session) => Promise<void>
  switchCharacter: (id: string) => Promise<void>
  refreshSessions: () => Promise<void>; refreshWorkSessions: () => Promise<void>; refreshCharacters: () => Promise<void>; refreshMemories: () => Promise<void>; refreshTasks: () => Promise<void>
  refreshSchedule: () => Promise<void>; refreshModels: () => Promise<void>
  setMemories: React.Dispatch<React.SetStateAction<MemoryItem[]>>; setSchedule: React.Dispatch<React.SetStateAction<ScheduleItem[]>>
  conversationPaging:Record<Mode,{hasMore:boolean;loadingMore:boolean;loadMore:()=>Promise<void>}>
  sessionHasMore:Record<Mode,boolean>;loadMoreSessions:(mode:Mode)=>Promise<void>
  reconnect: () => Promise<void>
}
export const AppContext = createContext<AppContextValue | null>(null)
type Store={value:AppContextValue;listeners:Set<()=>void>}
const StoreContext=createContext<Store|null>(null)


export function AppProvider({ children }: { children: ReactNode }) {
  const [connection, setConnection] = useState<ConnectionState>('checking')
  const [dataErrors, setDataErrors] = useState<Record<string, string>>({})
  const [health, setHealth] = useState<Health>()
  const [usage, setUsage] = useState<Usage>()
  const [uiState, setUiState] = useState<UiState>()
  const [sessionOffsets,setSessionOffsets]=useState<Record<Mode,number|null>>({companion:null,work:null})
  const [sessions, setSessions] = useState<Session[]>([])
  const [workSessions, setWorkSessions] = useState<Session[]>([])
  const [activeSessionId, setActiveSessionId] = useState<number>()
  const [activeWorkSessionId, setActiveWorkSessionId] = useState<number>()
  const [characters, setCharacters] = useState<Character[]>([])
  const [activeCharacterId, setActiveCharacterId] = useState('lingxi')
  const [memories, setMemories] = useState<MemoryItem[]>([])
  const [schedule, setSchedule] = useState<ScheduleItem[]>([])
  const [tasks, setTasks] = useState<TaskItem[]>([])
  const [modules, setModules] = useState<ModelModule[]>([])
  const [profiles, setProfiles] = useState<ModelProfile[]>([])
  const mounted = useRef(true)
  const loading = useRef(0)
  const stateWrites = useRef<Promise<unknown>>(Promise.resolve())
  const creating = useRef<Partial<Record<Mode, Promise<number>>>>({})
  const selected = useRef<Partial<Record<Mode, number>>>({})
  const companion = useConversation('companion', connection === 'connected', activeSessionId)
  const work = useConversation('work', connection === 'connected', activeWorkSessionId)
  const activeCharacter = characters.find((item) => item.id === activeCharacterId) || characters[0] || demoCharacter
  const [speakingId, setSpeakingId] = useState<Message['id']>()
  const playback = useRef<HTMLAudioElement | undefined>(undefined)
  const speechRequest = useRef(0)
  const speechAbort = useRef<AbortController | undefined>(undefined)
  const speechCache = useRef(new Map<string, string>())
  const speechCleanup = useRef<(() => void) | undefined>(undefined)
  const speechFinish=useRef<(()=>void)|undefined>(undefined)
  const stopSpeech = useCallback(() => {
    speechFinish.current?.();speechFinish.current=undefined; speechRequest.current++; speechAbort.current?.abort(); speechAbort.current = undefined; playback.current?.pause(); playback.current = undefined; speechCleanup.current?.(); speechCleanup.current = undefined; setSpeakingId(undefined)
  }, [])
  const speakMessage = useCallback(async (mode:Mode,message:Message,sessionId?:number)=>{
    stopSpeech()
    if(connection!=='connected')throw new Error('请先连接本地服务')
    if(!modules.find(item=>item.id==='tts')?.enabled)throw new Error('请先启用语音合成')
    const sid=sessionId??selected.current[mode];if(!sid||!message.content.trim())return
    const token=speechRequest.current,controller=new AbortController();speechAbort.current=controller
    const voice=mode==='companion'?activeCharacter.voice_ref||'':''
    const tts=modules.find(item=>item.id==='tts');setSpeakingId(message.id)
    try{
      const options=await evolution.options(sid)
      if(token!==speechRequest.current)return
      const parts=sentenceParts(message.content),pending=new Map<number,Promise<{url?:string;error?:Error}>>()
      const fetchPart=(index:number)=>{
        if(index>=parts.length||pending.has(index)||token!==speechRequest.current)return
        const key=JSON.stringify([sid,message.id,voice,tts?.revision,tts?.model,tts?.voice,options.settings.revision,parts[index]])
        pending.set(index,(async()=>{
          try{let url=speechCache.current.get(key);if(!url){const result=await lingxi.invokeMedia('tts',sid,{text:parts[index],voice},controller.signal);if(!result.media)throw new Error('语音服务没有返回音频');url=result.media.url;speechCache.current.set(key,url);if(speechCache.current.size>96)speechCache.current.delete(speechCache.current.keys().next().value!)}return {url}}
          catch(error){return {error:error as Error}}
        })())
      }
      fetchPart(0);fetchPart(1)
      for(let index=0;index<parts.length;index++){
        fetchPart(index);const result=await pending.get(index)!;pending.delete(index)
        if(token!==speechRequest.current)return
        if(result.error)throw result.error
        fetchPart(index+2)
        const audio=new Audio(result.url);playback.current=audio
        await new Promise<void>((resolve,reject)=>{
          speechFinish.current=resolve
          audio.onended=()=>resolve();audio.onerror=()=>reject(new Error('音频未能播放，请重试'))
          void audio.play().then(async()=>{const cleanup=await attachSpeechAnalyser(audio);if(token===speechRequest.current)speechCleanup.current=cleanup;else cleanup()}).catch(reject)
        })
        speechFinish.current=undefined;speechCleanup.current?.();speechCleanup.current=undefined
        if(token!==speechRequest.current)return
      }
    }finally{if(token===speechRequest.current)stopSpeech()}
  },[activeCharacter.voice_ref,connection,modules,stopSpeech])
  useEffect(() => () => { speechRequest.current++; speechAbort.current?.abort(); playback.current?.pause(); speechCleanup.current?.() }, [])

  useEffect(() => { stopSpeech() }, [activeSessionId, activeWorkSessionId, activeCharacterId, stopSpeech])

  const persistState = useCallback((patch: Partial<Omit<UiState, 'revision'>>) => {
    const next = stateWrites.current.catch(() => undefined).then(async () => {
      const current = await lingxi.uiState()
      const saved = await lingxi.updateUiState(current.revision, patch)
      if (mounted.current) setUiState(saved)
    })
    stateWrites.current = next
    void next.catch((error) => toast.error('当前选择尚未保存', { description: (error as Error).message }))
    return next
  }, [])
  const loadBackend = useCallback(async () => {
    const attempt = ++loading.current
    setConnection(current => current === 'connected' ? current : 'checking')
    try {
      const healthData = await lingxi.health()
      if (!healthData.ok) throw new Error('本地服务未就绪')
      const results = await Promise.allSettled([
        lingxi.usage(), lingxi.uiState(), lingxi.sessions('companion'), lingxi.sessions('work'), lingxi.characters(),
        lingxi.memories('companion'), lingxi.schedule(), lingxi.tasks(), lingxi.modules(), lingxi.profiles(), lingxi.memories('shared'),
      ] as const)
      if (!mounted.current || attempt !== loading.current) return
      const labels = ['用量', '当前选择', '陪伴会话', '工作会话', '人物', '陪伴记忆', '日程', '任务', '模型模块', '模型配置', '共享画像']
      setDataErrors(Object.fromEntries(results.flatMap((result, index) => result.status === 'rejected' ? [[labels[index], String(result.reason?.message || '读取失败')]] : [])))
      const [usageData, ui, companionData, workData, characterData, memoryData, scheduleData, taskData, moduleData, profileData, shared] = results
      setHealth(healthData)
      if (usageData.status === 'fulfilled') setUsage(usageData.value)
      if (ui.status === 'fulfilled') setUiState(ui.value)
      if (characterData.status === 'fulfilled') {
        setCharacters(characterData.value.cards)
        if (ui.status === 'fulfilled') setActiveCharacterId(ui.value.active_character_id || characterData.value.cards[0]?.id || 'lingxi')
      }
      if (companionData.status === 'fulfilled') {
        setSessionOffsets(v=>({...v,companion:companionData.value.next_offset}))
        const requested=ui.status==='fulfilled'?ui.value.active_companion_session_id:undefined
        if(requested&&!companionData.value.sessions.some(v=>v.id===requested)){const found=await lingxi.sessions('companion',0,requested);companionData.value.sessions=found.sessions}
        setSessions(companionData.value.sessions)
        const expected = ui.status === 'fulfilled' ? ui.value.active_companion_session_id : selected.current.companion
        const id = companionData.value.sessions.find(s => s.id === expected)?.id || companionData.value.sessions[0]?.id
        selected.current.companion = id; setActiveSessionId(id)
      }
      if (workData.status === 'fulfilled') {
        setSessionOffsets(v=>({...v,work:workData.value.next_offset}))
        const requested=ui.status==='fulfilled'?ui.value.active_work_session_id:undefined
        if(requested&&!workData.value.sessions.some(v=>v.id===requested)){const found=await lingxi.sessions('work',0,requested);workData.value.sessions=found.sessions}
        setWorkSessions(workData.value.sessions)
        const expected = ui.status === 'fulfilled' ? ui.value.active_work_session_id : selected.current.work
        const id = workData.value.sessions.find(s => s.id === expected)?.id || workData.value.sessions[0]?.id
        selected.current.work = id; setActiveWorkSessionId(id)
      }
      setMemories(current => [
        ...current.filter(item => item.scope === 'work' || item.scope === 'companion' && memoryData.status === 'rejected' || item.scope === 'shared' && shared.status === 'rejected'),
        ...(memoryData.status === 'fulfilled' ? memoryData.value.items : []), ...(shared.status === 'fulfilled' ? shared.value.items : []),
      ])
      if (scheduleData.status === 'fulfilled') setSchedule(scheduleData.value.items)
      if (taskData.status === 'fulfilled') setTasks(taskData.value.items)
      if (moduleData.status === 'fulfilled') setModules(moduleData.value.items)
      if (profileData.status === 'fulfilled') setProfiles(profileData.value.items)
      setConnection('connected')
    } catch (error) {
      if (!mounted.current || attempt !== loading.current) return
      setConnection('offline'); setHealth(undefined)
      toast.error('无法读取零汐本地服务', { description: (error as Error).message })
    }
  }, [])
  useEffect(() => {
    mounted.current = true; void loadBackend()
    return () => { mounted.current = false; loading.current++ }
  }, [loadBackend])
  const selectSession = useCallback((id: number) => {
    selected.current.companion = id; setActiveSessionId(id)
    if (connection === 'connected') void persistState({ active_companion_session_id: id })
  }, [connection, persistState])
  const selectWorkSession = useCallback((id: number) => {
    selected.current.work = id; setActiveWorkSessionId(id)
    if (connection === 'connected') void persistState({ active_work_session_id: id })
  }, [connection, persistState])
  const createSession = useCallback((mode: Mode): Promise<number> => {
    if (connection !== 'connected') return Promise.reject(new Error('请先连接零汐本地服务'))
    if (creating.current[mode]) return creating.current[mode]!
    const result = (async () => {
      const session = await lingxi.createSession(mode, mode === 'companion' ? '新的陪伴对话' : '新的工作会话', crypto.randomUUID())
      selected.current[mode] = session.id
      if (mode === 'companion') {
        companion.adopt(session.id); setSessions((items) => [session, ...items]); setActiveSessionId(session.id)
      } else {
        work.adopt(session.id); setWorkSessions((items) => [session, ...items]); setActiveWorkSessionId(session.id)
      }
      void persistState(mode === 'companion' ? { active_companion_session_id: session.id } : { active_work_session_id: session.id })
      return session.id
    })().finally(() => { delete creating.current[mode] })
    creating.current[mode] = result
    return result
  }, [connection, companion.adopt, work.adopt, persistState])
  const ensureSession = useCallback(async (mode: Mode) => selected.current[mode] || createSession(mode), [createSession])
  useEffect(()=>{
    const handler=(event:Event)=>{const value=(event as CustomEvent<{mode:Mode;id:number}>).detail;void loadBackend().then(()=>{if(value.mode==='work')selectWorkSession(value.id);else selectSession(value.id)})}
    window.addEventListener('lingxi-command-session',handler);return()=>window.removeEventListener('lingxi-command-session',handler)
  },[loadBackend,selectSession,selectWorkSession])
  const sendMessage = useCallback(async (mode: Mode, content: string, draft?: string) => {
    const sid = await ensureSession(mode)
    await (mode === 'companion' ? companion.send : work.send)(sid, content, mode === 'companion' ? activeCharacter : undefined, draft)
  }, [activeCharacter, companion.send, ensureSession, work.send])
  const stopGenerating = useCallback(async (mode: Mode) => { await (mode === 'companion' ? companion.stop : work.stop)() }, [companion.stop, work.stop])
  const retryMessage = useCallback(async (mode: Mode, message: Message) => { await (mode === 'companion' ? companion.retry : work.retry)(message) }, [companion.retry, work.retry])
  const setDraft = useCallback((mode: Mode, text: string) => (mode === 'companion' ? companion.setDraft : work.setDraft)(text), [companion.setDraft, work.setDraft])
  const renameSession = useCallback(async (session: Session, title: string) => {
    const renamed = await lingxi.renameSession(session.id, title)
    ;(session.mode === 'companion' ? setSessions : setWorkSessions)((items) => items.map((item) => item.id === session.id ? renamed : item))
  }, [])
  const archiveSession = useCallback(async (session: Session) => {
    await lingxi.archiveSession(session.id)
    const remaining = (await lingxi.sessions(session.mode)).sessions
    ;(session.mode === 'companion' ? setSessions : setWorkSessions)(remaining)
    if (selected.current[session.mode] === session.id) {
      const next = remaining[0]?.id
      selected.current[session.mode] = next
      ;(session.mode === 'companion' ? setActiveSessionId : setActiveWorkSessionId)(next)
      void persistState(session.mode === 'companion' ? { active_companion_session_id: next || null } : { active_work_session_id: next || null })
    }
  }, [persistState])
  const switchCharacter = useCallback(async (id: string) => {
    await persistState({ active_character_id: id }); setActiveCharacterId(id)
    toast.success(`已切换为 ${characters.find((item) => item.id === id)?.name || id}`)
  }, [characters, persistState])
  const refreshSessions = useCallback(async()=>{if(connection!=='connected')return;const page=await lingxi.sessions('companion',0,selected.current.companion);setSessionOffsets(v=>({...v,companion:page.next_offset}));const items=page.sessions;setSessions(items);const active=selected.current.companion;if(active&&!items.some(s=>s.id===active)){const next=items[0]?.id;selected.current.companion=next;setActiveSessionId(next);await persistState({active_companion_session_id:next||null})}},[connection,persistState])
  const refreshWorkSessions = useCallback(async () => {if(connection!=='connected')return;const page=await lingxi.sessions('work',0,selected.current.work);setSessionOffsets(v=>({...v,work:page.next_offset}));const items=page.sessions;setWorkSessions(items);const active=selected.current.work;if(active&&!items.some(s=>s.id===active)){const next=items[0]?.id;selected.current.work=next;setActiveWorkSessionId(next);await persistState({active_work_session_id:next||null})}},[connection,persistState])
  const refreshCharacters = useCallback(async () => { if (connection === 'connected') setCharacters((await lingxi.characters()).cards) }, [connection])
  const memoryLoad = useRef(0)
  const refreshMemories = useCallback(async () => {
    if (connection !== 'connected') return
    const request = ++memoryLoad.current
    const [companionMemory, shared, workMemory] = await Promise.all([
      lingxi.memories('companion'), lingxi.memories('shared'),
      activeWorkSessionId ? lingxi.memories('work', activeWorkSessionId) : Promise.resolve({ items: [] }),
    ])
    if (request === memoryLoad.current) setMemories([...companionMemory.items, ...shared.items, ...workMemory.items])
  }, [connection, activeWorkSessionId])
  useEffect(() => {
    setMemories((items) => items.filter((item) => item.scope !== 'work'))
    void refreshMemories().catch((error) => toast.error('记忆读取失败', { description: (error as Error).message }))
    return () => { memoryLoad.current++ }
  }, [refreshMemories])
  const refreshTasks = useCallback(async () => { if (connection === 'connected') setTasks((await lingxi.tasks()).items) }, [connection])
  const refreshSchedule = useCallback(async () => { if (connection === 'connected') setSchedule((await lingxi.schedule()).items) }, [connection])
  const refreshModels = useCallback(async () => {
    const [m, p] = await Promise.all([lingxi.modules(), lingxi.profiles()]); setModules(m.items); setProfiles(p.items)
  }, [])
  const restoreConversation = async (preview: RestorePreview) => {
    if (selected.current[preview.mode] !== preview.session_id) throw new Error('当前会话已变化，请重新打开历史')
    const result = await lingxi.restore(preview)
    ;(preview.mode === 'companion' ? companion.apply : work.apply)(preview.session_id, result.view)
    await refreshMemories(); await refreshTasks()
  }
  const mutateMessage: AppContextValue['mutateMessage'] = async (mode, message, content) => {
    const sid = selected.current[mode]
    const conversation = mode === 'companion' ? companion : work
    if (!sid || connection !== 'connected' || typeof message.id !== 'number') throw new Error('请先连接并选择聊天')
    if (conversation.generating || conversation.sending || conversation.messages.some((item) => String(item.id).startsWith('outbox-'))) throw new Error('请先完成或停止当前回复，并处理未确认消息')
    try {
      const result = content === undefined ? await lingxi.deleteMessage(sid,message.id,conversation.version) : await lingxi.editMessage(sid,message.id,conversation.version,content)
      conversation.apply(sid,result.view)
      void refreshMemories().catch((error) => toast.error('记忆刷新失败',{description:(error as Error).message}))
      return result
    } catch (error) {await conversation.refresh(sid).catch(() => {});if ((error as Error).message === 'Not Found') throw new Error('请关闭并重新打开零汐，启用已安装的消息功能');throw error}
  }
  const undoMessageDeletion: AppContextValue['undoMessageDeletion'] = async (mode,sid,key,version) => {
    const result = await lingxi.undoMessageDeletion(sid,key,version)
    ;(mode === 'companion' ? companion : work).apply(sid,result.view)
    void refreshMemories().catch((error) => toast.error('记忆刷新失败',{description:(error as Error).message}))
  }
  const loadMoreSessions=async(mode:Mode)=>{const offset=sessionOffsets[mode];if(offset===null)return;const page=await lingxi.sessions(mode,offset);(mode==='work'?setWorkSessions:setSessions)(items=>[...items,...page.sessions.filter(s=>!items.some(v=>v.id===s.id))]);setSessionOffsets(v=>({...v,[mode]:page.next_offset}))}
  const value: AppContextValue = {
    loadMoreSessions,sessionHasMore:{companion:sessionOffsets.companion!==null,work:sessionOffsets.work!==null},
    conversationPaging:{companion:{hasMore:companion.hasMore,loadingMore:companion.loadingMore,loadMore:companion.loadMore},work:{hasMore:work.hasMore,loadingMore:work.loadingMore,loadMore:work.loadMore}},
    conversationBatches:{companion:companion.batches,work:work.batches},
    decideToolApproval:async(mode,approval,allowed)=>{const sid=selected.current[mode];if(!sid||sid!==approval.snapshot.session_id)throw new Error('当前会话与审批目标不一致');await lingxi.decideToolApproval(sid,approval,allowed);await (mode==='work'?work:companion).refresh(sid)},
    connection, health, usage, uiState, dataErrors, sessions, workSessions, activeSessionId, activeWorkSessionId,
    characters, activeCharacter, messages: companion.messages, workMessages: work.messages,
    memories, schedule, tasks, modules, profiles,
    generating: { companion: companion.generating, work: work.generating }, sending: { companion: companion.sending, work: work.sending },
    drafts: { companion: companion.draft, work: work.draft }, setDraft,
    draftConflicts: { companion: companion.draftConflict, work: work.draftConflict },
    draftBlocked: { companion: companion.draftBlocked, work: work.draftBlocked },
    pendingInputs: { companion: companion.pending, work: work.pending },
    reloadDraftConflict: mode => (mode === 'companion' ? companion : work).reloadDraftConflict(),
    resolveDraft: (mode, choice) => (mode === 'companion' ? companion : work).resolveDraft(choice),
    cancelPendingInput: mode => (mode === 'companion' ? companion : work).cancelPending(),
    setActiveSessionId: selectSession, setActiveWorkSessionId: selectWorkSession, createSession, ensureSession,
    sendMessage, stopGenerating, retryMessage, renameSession, archiveSession, switchCharacter,
    speakMessage, stopSpeech, speakingId,
    mutateMessage, undoMessageDeletion, restoreConversation, conversationVersions: { companion: companion.version, work: work.version },
    refreshSessions, refreshWorkSessions, refreshCharacters, refreshMemories, refreshTasks, refreshSchedule, refreshModels, setMemories, setSchedule, reconnect: loadBackend,
  }
  const store=useRef<Store>({value,listeners:new Set()})
  useLayoutEffect(()=>{store.current.value=value;store.current.listeners.forEach(notify=>notify())})
  return <StoreContext.Provider value={store.current}>{children}</StoreContext.Provider>
}
export function useApp() {
  const override=useContext(AppContext),store=useContext(StoreContext)
  const tracked=useRef(new Set<keyof AppContextValue>())
  const cached=useRef<AppContextValue|null>(null)
  const subscribe=useCallback((notify:()=>void)=>{store?.listeners.add(notify);return()=>{store?.listeners.delete(notify)}},[store])
  const snapshot=useCallback(()=>{const value=override||store?.value;if(!value)throw new Error('useApp 必须在 AppProvider 中使用');const previous=cached.current;if(previous&&[...tracked.current].every(key=>Object.is(previous[key],value[key])))return previous;cached.current=value;return value},[override,store])
  const current=useSyncExternalStore(subscribe,snapshot,snapshot)
  return useMemo(()=>new Proxy(current,{get(target,key: keyof AppContextValue){tracked.current.add(key);return target[key]}}),[current])
}
