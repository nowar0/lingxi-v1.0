import './work-surface.css'
import { WorkPresetControl } from '@/components/shared/WorkPresets'
import { LingxiLogo } from '@/components/shared/LingxiLogo'
import { WorkConversationMenu,requestWorkMenu } from '@/components/shared/WorkConversationMenu'
import { WorkProjectPicker } from '@/components/shared/WorkProjectPicker'
import { ConversationToolScope } from '@/context/ConversationToolContext'
import { ConversationTools } from '@/components/shared/ConversationTools'
import { WorkTeamPanel } from '@/components/shared/WorkTeamPanel'
import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowUpLeft, Compass, Globe, MessageSquare, Check, ChevronRight, CircleDot, FileCode2, Folder, FolderGit2, GitBranch, GitCommitHorizontal, Layers3, ListChecks, LoaderCircle, MoreHorizontal, PackageCheck, PanelLeft, PanelLeftClose, PanelLeftOpen, PanelRight, Plus, RefreshCw, ShieldCheck, SquareTerminal, TerminalSquare } from 'lucide-react'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
import { api, json } from '@/api/client'

import { WorkToolFrame } from '@/components/shared/WorkToolFrame'
import { WorkSessionRail } from '@/components/shared/WorkSessionRail'
import { WorkspaceFolderPicker } from '@/components/shared/WorkspaceFolderPicker'
import { ConversationHistory } from '@/components/shared/ConversationHistory'
import { MessageList } from '@/components/shared/MessageList'
import { WorkComposer } from '@/components/shared/WorkComposer'
import { DataModeNotice } from '@/components/shared/DataModeNotice'
import { Button } from '@/components/ui/button'
import { Input, Textarea } from '@/components/ui/input'
import { WorkDock } from '@/components/shared/WorkDock'
import { WorkFileEditor, type FileEditorHandle, type FileSnapshot } from '@/components/shared/WorkFileEditor'
import { FileActivity } from '@/components/shared/FileActivity'
import { ArtifactLibrary } from '@/components/shared/ArtifactLibrary'
import { FileExplorer } from '@/components/shared/FileExplorer'
import { SideConversation } from '@/components/shared/SideConversation'
import { WorkStart } from '@/components/shared/WorkStart'
import { BrowserTools } from '@/components/shared/BrowserTools'
import { InteractiveTerminal, WorkflowPanel } from '@/components/shared/AdvancedWorkspaceTools'
import { OfficeWorkspacePanel } from '@/components/shared/OfficeWorkspacePanel'
import { ResearchWorkspacePanel } from '@/components/shared/ResearchWorkspacePanel'
import { Tooltip } from '@/components/ui/tooltip'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import { restoreWorkLayout, saveWorkLayout, workTabsReducer, workStartShortcut, workTabTitle, type WorkPane, type WorkTab, type WorkToolKind } from '@/lib/workspaceTabs'

type WorkRecord = { goal: string; materials: string; constraints: string; tasks: string[]; status: string }
type Context = { revision: number; record: WorkRecord }
type Settings = { path: string; acceptance: string; revision: number }
type FilePreview = FileSnapshot
type TerminalResult = { cwd: string; exit_code: number | null; output: string; limited?: boolean }
type TerminalEntry = { id: string; command: string; cwd: string; status: string; result?: TerminalResult }
type TerminalState = { cwd: string; running: boolean; persistent: boolean; background: boolean }
type CommandPreview = { token: string; command: string; cwd: string; note: string }
type ToolKind = WorkToolKind
const emptyRecord: WorkRecord = { goal: '', materials: '', constraints: '', tasks: [], status: '待开始' }
const toolOptions = [
  { id: 'start', title: '开始', icon: Compass, detail: '文件、终端与浏览器' },
  { id: 'chat', title: '工作对话', icon: MessageSquare, detail: '当前工作会话' },
  { id: 'browser', title: '搜索与浏览器', icon: Globe, detail: '搜索、来源与专用 Edge 操作' },
  { id: 'context', title: '上下文', icon: CircleDot, detail: '目标、材料与限制' },
  { id: 'tasks', title: '任务', icon: ListChecks, detail: '节点进展与审批' },
  { id: 'files', title: '项目文件', icon: Folder, detail: '浏览、编辑与版本比较' },
  { id: 'activity', title: '文件活动', icon: Layers3, detail: '读取、写入与所属步骤' },
  { id: 'artifacts', title: '产物与附件', icon: PackageCheck, detail: '原件、范围引用与版本' },
  { id: 'changes', title: '更改差异', icon: GitCommitHorizontal, detail: '工作区与暂存区' },
  { id: 'terminal', title: '终端', icon: SquareTerminal, detail: 'PowerShell · 执行前确认' },
  { id: 'interactive-terminal', title: '交互终端', icon: TerminalSquare, detail: 'ConPTY · 每次输入前确认' },
  { id: 'workflow', title: '工具流程', icon: ListChecks, detail: '串并行、条件与输出引用' },
  { id: 'office', title: 'Office 与 OCR', icon: FileCode2, detail: '生成、编辑、预览和文字识别' },
  { id: 'git', title: 'Git / Worktree', icon: FolderGit2, detail: '分支、提交与工作树' },
  { id: 'ssh', title: 'SSH / SFTP', icon: TerminalSquare, detail: '远程连接与文件传输' },
  { id: 'review', title: '预算与验收', icon: ShieldCheck, detail: '用量与成果核验' },
] as const
const stateLabel = (state?: string) => ({ completed: '已完成', complete: '已完成', running: '进行中', generating: '生成中', failed: '失败', pending: '待处理', draft: '草稿', approved: '已批准', stopped: '已停止', waiting_approval: '待审批' }[state || ''] || state || '待开始')

function CodeView({ text, diff = false }: { text: string; diff?: boolean }) {
  return <pre className={cn('workspace-code', diff && 'is-diff')} tabIndex={0}><code>{text.split('\n').map((line, index) => <span className={cn('workspace-code-line', diff && line.startsWith('+') && !line.startsWith('+++') && 'is-addition', diff && line.startsWith('-') && !line.startsWith('---') && 'is-deletion', diff && line.startsWith('@@') && 'is-hunk')} key={index}><span className="workspace-line-number" aria-hidden="true">{index + 1}</span><span>{line || ' '}</span></span>)}</code></pre>
}

export function WorkPage() {
  const { activeWorkSessionId, setActiveWorkSessionId, createSession, connection } = useApp()
  const [params]=useSearchParams(); const requested=Number(params.get('session'))
  useEffect(()=>{if(connection!=='connected')return;if(Number.isSafeInteger(requested)&&requested>0)setActiveWorkSessionId(requested);else if(!activeWorkSessionId)void createSession('work')},[requested,connection])
  return <ConversationToolScope identity={activeWorkSessionId}><WorkSurface key={`${activeWorkSessionId || 'new'}:${connection === 'connected'}`} /></ConversationToolScope>
}

function WorkSurface() {
  const navigate = useNavigate()
  const { workSessions, activeWorkSessionId, workMessages, connection, tasks, refreshTasks, conversationVersions, setDraft, sending, generating, activeCharacter, refreshWorkSessions, drafts } = useApp()
  const currentSession = useRef(activeWorkSessionId)
  const [tabState, dispatchTabs] = useReducer(workTabsReducer, activeWorkSessionId, sid => restoreWorkLayout(sid).state)
  const { tabs, active } = tabState
  const [focusedPane,setFocusedPane]=useState<WorkPane>('right')
  const [tabsOverflow,setTabsOverflow]=useState(false)
  const [leftToolbarHost,setLeftToolbarHost]=useState<HTMLDivElement|null>(null)
  const [toolbarHost,setToolbarHost]=useState<HTMLDivElement|null>(null)
  const [toolsOpen, setToolsOpen] = useState(() => restoreWorkLayout(activeWorkSessionId).open)
  const [reveal, setReveal] = useState({ side: 'right' as WorkPane, version: 0 })

  const [railOpen,setRailOpen]=useState(()=>{try{const v=localStorage.getItem('lingxi-work-rail-open');if(v!==null)return v==='true'}catch{}return window.innerWidth>=1200})
  useEffect(()=>{try{localStorage.setItem('lingxi-work-rail-open',String(railOpen))}catch{}},[railOpen])
  const [context, setContext] = useState<Context>({ revision: 0, record: emptyRecord })
  const [settings, setSettings] = useState<Settings>({ path: '', acceptance: '', revision: 0 })
  const projectPath = useRef(settings.path)
  projectPath.current = settings.path
  const previousPath = useRef<string | undefined>(undefined)
  const restoredRoot = useRef(restoreWorkLayout(activeWorkSessionId).root)
  const [settingsLoaded, setSettingsLoaded] = useState(false)
  const [contextError, setContextError] = useState('')
  const [editing, setEditing] = useState(false)
  const [folderPickerOpen, setFolderPickerOpen] = useState(false)
  const [record, setRecord] = useState<WorkRecord>(emptyRecord)
  const [path, setPath] = useState('')
  const [acceptance, setAcceptance] = useState('')
  const [filePreviews, setFilePreviews] = useState<{ [path: string]: FilePreview }>({})
  const fileEditors = useRef(new Map<string, FileEditorHandle>())
  const [dirtyFiles, setDirtyFiles] = useState<Record<string, boolean>>({})
  const [changes, setChanges] = useState<{ status: string; working: string; staged: string }>()
  const [branch, setBranch] = useState('')
  const [terminal, setTerminal] = useState<TerminalEntry[]>([])
  const terminalLog = useRef<HTMLDivElement>(null)
  const terminalFollow = useRef(true)
  const [terminalBehind, setTerminalBehind] = useState(false)
  const [terminalState, setTerminalState] = useState<TerminalState>({ cwd: '', running: false, persistent: false, background: false })
  useLayoutEffect(() => {
    if (terminalFollow.current && terminalLog.current) { terminalLog.current.scrollTop = terminalLog.current.scrollHeight; setTerminalBehind(false) }
    else if (terminal.length) setTerminalBehind(true)
  }, [terminal])
  const [command, setCommand] = useState('')
  const [commandPreview, setCommandPreview] = useState<CommandPreview>()
  const [loading, setLoading] = useState<{ [kind: string]: boolean }>({})
  const [errors, setErrors] = useState<{ [kind: string]: string }>({})
  const [refreshCount, setRefreshCount] = useState(0)
  const sideRequest = useRef<{ signature: string; id: string } | undefined>(undefined)
  const [returnText, setReturnText] = useState<string>()
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const ready = connection === 'connected' && Boolean(activeWorkSessionId)
  const session = workSessions.find((item) => item.id === activeWorkSessionId)
  const sessionTasks = tasks.filter((task) => task.session_id === activeWorkSessionId)
  const currentTask = sessionTasks.find((task) => ['running', 'generating', 'waiting_approval'].includes(task.status)) || sessionTasks[0]
  const projectName = settings.path.split(/[\\/]/).filter(Boolean).at(-1)
  const run = async (task: () => Promise<void>) => {
    if (busyRef.current || !ready) return
    busyRef.current = true; setBusy(true)
    try { await task() } catch (error) { toast.error('工作区操作未完成', { description: (error as Error).message }) }
    finally { busyRef.current = false; setBusy(false) }
  }
  const openTool = (kind: ToolKind, side: WorkPane = 'right') => {
    if (kind === 'chat' && side === 'right') { void createSide(); return }
    const option = toolOptions.find((item) => item.id === kind)!
    const existing = tabs.find(tab => tab.id === kind)
    dispatchTabs({ type: 'open', tab: { id: kind, kind, title: option.title, side } })
    setReveal(value => ({ side: existing?.side || side, version: value.version + 1 }))
    if ((existing?.side || side) === 'right') setToolsOpen(true)
  }
  const createSide = () => run(async () => {
    const sid = activeWorkSessionId
    if (!sid || conversationVersions.work < 0) throw new Error('请等待主会话加载')
    const request = { parent_session_id: sid, expected_version: conversationVersions.work, inherit_record: true }
    const signature = JSON.stringify(request)
    if (sideRequest.current?.signature !== signature) sideRequest.current = { signature, id: crypto.randomUUID().replaceAll('-', '') }
    const data = await api<{ session: { id: number } }>('/v9/workspace/side-chats', json('POST', { ...request, request_id: sideRequest.current.id }))
    if (currentSession.current !== sid) return
    dispatchTabs({ type: 'open', tab: { id: `side:${data.session.id}`, kind: 'side-chat', title: '独立侧聊', sessionId: data.session.id, side: 'right' } })
    setFocusedPane('right'); setToolsOpen(true); sideRequest.current = undefined
    await refreshWorkSessions()
  })
  useEffect(()=>{
    const side=(event:Event)=>{const sid=(event as CustomEvent<number>).detail;dispatchTabs({type:'open',tab:{id:`side:${sid}`,kind:'side-chat',title:'独立侧聊',sessionId:sid,side:'right'}});setToolsOpen(true)}
    const tool=(event:Event)=>{const kind=(event as CustomEvent<string>).detail;if(kind==='refresh-files'){setRefreshCount(v=>v+1);return}if(kind==='edit-workspace'){void edit();return}if(kind==='reminders'){navigate('/tasks');return}if(toolOptions.some(v=>v.id===kind))openTool(kind as ToolKind)}
    window.addEventListener('lingxi-work-open-side',side);window.addEventListener('lingxi-work-tool',tool)
    return()=>{window.removeEventListener('lingxi-work-open-side',side);window.removeEventListener('lingxi-work-tool',tool)}
  },[tabs])
  const closeTab = async (id: string) => {
    const editor = fileEditors.current.get(id)
    if (editor && !await editor.requestClose()) return
    const tab = tabs.find(item => item.id === id)
    if(tab?.kind==='side-chat'&&tab.sessionId){
      try{const value=await api<{origin:{regular:boolean}}>(`/v9/workspace/side-chats/${tab.sessionId}`);if(!value.origin.regular){await api(`/v2/sessions/${tab.sessionId}/archive`,json('POST',{archived:true}));await refreshWorkSessions()}}
      catch(error){toast.error('侧聊未关闭',{description:(error as Error).message});return}
    }
    dispatchTabs({ type: 'close', id })
    if (tab?.side === 'right' && tabs.filter(item => item.side === 'right').length === 1) setToolsOpen(false)
  }
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || document.querySelector('[role="dialog"][data-state="open"]')) return
      const kind = workStartShortcut(event)
      if (kind) { event.preventDefault(); openTool(kind) }
    }
    window.addEventListener('keydown', keydown)
    return () => window.removeEventListener('keydown', keydown)
  }, [tabs])
  const refresh = useCallback(async () => {
    if (!ready || !activeWorkSessionId) return
    const sid = activeWorkSessionId
    try {
      const [s, r] = await Promise.all([api<Settings>(`/v6/work-settings/${sid}`), api<Context>(`/v3/sessions/${sid}/work-record`)])
      if (currentSession.current === sid) { setSettings(s); setSettingsLoaded(true); setContext(r); setContextError('') }
    } catch (error) { if (currentSession.current === sid) setContextError((error as Error).message) }
  }, [activeWorkSessionId, ready])
  useEffect(() => {
    currentSession.current = activeWorkSessionId
    return () => { currentSession.current = undefined }
  }, [activeWorkSessionId])
  useEffect(() => { void refresh() }, [refresh, conversationVersions.work])
  useEffect(() => {
    const changed = (event: Event) => { if ((event as CustomEvent<number>).detail === activeWorkSessionId) void refresh() }
    const requested = (event: Event) => {
      const detail = (event as CustomEvent<{ sessionId: number; checks: Promise<boolean>[] }>).detail
      if (detail.sessionId === activeWorkSessionId) detail.checks.push((async () => {
        for (const editor of fileEditors.current.values()) if (!await editor.requestClose()) return false
        return true
      })())
    }
    window.addEventListener('lingxi-work-project-changed', changed)
    window.addEventListener('lingxi-work-project-request', requested)
    return () => { window.removeEventListener('lingxi-work-project-changed', changed); window.removeEventListener('lingxi-work-project-request', requested) }
  }, [activeWorkSessionId, refresh])
  useEffect(() => {
    if (!ready || !tabs.some(tab => ['git', 'ssh', 'review'].includes(tab.kind) && active[tab.side] === tab.id && (tab.side === 'left' || toolsOpen))) return
    return () => { if (currentSession.current !== undefined) void refresh() }
  }, [active.left, active.right, toolsOpen, ready, refresh])
  useLayoutEffect(() => {
    if (!settingsLoaded || previousPath.current === settings.path) return
    if (previousPath.current === undefined && restoredRoot.current === settings.path) { previousPath.current = settings.path; return }
    previousPath.current = settings.path
    setFilePreviews({})
    setChanges(undefined); setBranch(''); setTerminal([]); setTerminalState({ cwd: '', running: false, persistent: false, background: false }); setCommand(''); setCommandPreview(undefined)
    dispatchTabs({ type: 'drop-files' })
  }, [settings.path, settingsLoaded])
  useEffect(() => {
    if (!settingsLoaded || !activeWorkSessionId) return
    saveWorkLayout(activeWorkSessionId, tabState, settings.path, toolsOpen)
  }, [activeWorkSessionId, settingsLoaded, tabState, settings.path, toolsOpen])
  useEffect(() => {
    if (!ready || !settingsLoaded || !settings.path) return
    let cancelled = false
    for (const tab of tabs.filter(t => t.kind === 'file' && t.path && !filePreviews[t.path])) {
      void api<FilePreview>(`/v9/workspace/file?session_id=${activeWorkSessionId}&path=${encodeURIComponent(tab.path!)}&expected_project_path=${encodeURIComponent(settings.path)}`).then(data => { if (!cancelled) setFilePreviews(items => ({ ...items, [tab.path!]: data })) }).catch(error => { if (!cancelled) setErrors(values => ({ ...values, [tab.id]: `文件恢复失败：${error.message}` })) })
    }
    return () => { cancelled = true }
    // Restore reads are bounded by the saved tab set and revalidate root binding.
  }, [ready, settingsLoaded, settings.path])
  useEffect(() => {
    if (!ready || !settings.path) return
    let disposed = false
    const sid = activeWorkSessionId
    void api<{ branch: string }>(`/v8/workbench/git/status?session_id=${sid}`).then((data) => { if (!disposed) setBranch(data.branch) }).catch(() => { if (!disposed) setBranch('') })
    return () => { disposed = true }
  }, [activeWorkSessionId, ready, settings.path, refreshCount])
  useEffect(() => {
    if (!ready || !settings.path) return
    let disposed = false
    const sid = activeWorkSessionId
    const shown = tabs.filter(tab => active[tab.side] === tab.id && (tab.side === 'left' || toolsOpen) && ['changes', 'terminal'].includes(tab.kind))
    for (const tab of shown) {
      setLoading(values => ({ ...values, [tab.kind]: true }))
      setErrors(values => ({ ...values, [tab.kind]: '' }))
      const load = async () => {
        if (tab.kind === 'changes') {
          const data = await api<{ status: string; working: string; staged: string }>(`/v8/workbench/git/diff?session_id=${sid}`)
          if (!disposed) setChanges(data)
        } else {
          const data = await api<TerminalState & { items: TerminalEntry[] }>(`/v9/terminal/persistent/history?session_id=${sid}`)
          if (!disposed) { setTerminal(data.items.filter(item => item.status !== 'prepared')); setTerminalState(data) }
        }
      }
      void load().catch(error => { if (!disposed) setErrors(values => ({ ...values, [tab.kind]: (error as Error).message })) }).finally(() => { if (!disposed) setLoading(values => ({ ...values, [tab.kind]: false })) })
    }
    return () => { disposed = true }
  }, [activeWorkSessionId, active.left, active.right, ready, settings.path, refreshCount, toolsOpen])
  useEffect(() => {
    if (!ready || !settings.path || !tabs.some(t => t.kind === 'terminal' && active[t.side] === t.id && (t.side === 'left' || toolsOpen))) return
    const controller = new AbortController(); let running = false
    const poll = async () => {
      if (running || document.visibilityState !== 'visible') return
      running = true
      try {
        const data = await api<TerminalState & { items: TerminalEntry[] }>(`/v9/terminal/persistent/history?session_id=${activeWorkSessionId}`, { signal: controller.signal })
        if (!controller.signal.aborted) { setTerminal(data.items.filter(item => item.status !== 'prepared')); setTerminalState(data); setErrors(old => ({ ...old, terminal: '' })) }
      } catch (error) { if (!controller.signal.aborted) setErrors(old => ({ ...old, terminal: (error as Error).message })) }
      finally { running = false }
    }
    void poll(); const timer = window.setInterval(() => void poll(), 800)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [ready, settings.path, active.left, active.right, activeWorkSessionId, toolsOpen, tabs])
  useEffect(() => {
    if (!ready) return
    const timer = window.setInterval(() => { void refreshTasks().catch(() => undefined) }, 3000)
    return () => window.clearInterval(timer)
  }, [ready, refreshTasks])
  const edit = () => { setRecord({ ...context.record }); setPath(settings.path); setAcceptance(settings.acceptance); setEditing(true) }
  const save = () => run(async () => {
    const sid = activeWorkSessionId
    if (!sid) return
    if (path.trim()) await api('/v6/workspace-folders/validate', json('POST', { path: path.trim() }))
    const saved = await api<Settings>(`/v6/work-settings/${sid}`, json('PUT', { path: path.trim(), acceptance, expected_revision: settings.revision }))
    if (currentSession.current !== sid) return
    setSettings(saved)
    try {
      const updated = await api<Context>(`/v3/sessions/${sid}/work-record`, json('PUT', { record, expected_revision: context.revision }))
      if (currentSession.current !== sid) return
      setContext(updated); setEditing(false); setRefreshCount((value) => value + 1); toast.success('已保存', { position: 'top-center' })
    } catch (error) { throw new Error(`项目目录已保存；工作记录尚未保存：${(error as Error).message}`) }
  })
  const openFile = (filePath: string, line?: number, column?: number, expectedSha?: string) => run(async () => {
    const sid = activeWorkSessionId
    const boundPath = settings.path
    const result = await api<FilePreview>(`/v9/workspace/file?session_id=${sid}&path=${encodeURIComponent(filePath)}&expected_project_path=${encodeURIComponent(boundPath)}`)
    if (currentSession.current !== sid || projectPath.current !== boundPath) return
    const id = `file:${filePath}`
    if (expectedSha && expectedSha !== result.sha256) toast.message('文件在搜索后有改动，已打开当前版本，请重新核对位置')
    const location = line && (!expectedSha || expectedSha === result.sha256) ? { line, column: column || 1, request: Date.now() } : undefined
    setFilePreviews((items) => ({ ...items, [filePath]: { ...result, location } }))
    const side = tabs.find(tab => tab.id === id)?.side || tabs.find(tab => tab.kind === 'files')?.side || 'right'
    dispatchTabs({ type: 'open', tab: { id, kind: 'file', title: filePath.split('/').at(-1) || filePath, path: filePath, side } })
    setReveal(value => ({ side, version: value.version + 1 }))
    if (side === 'right') setToolsOpen(true)
  })
  const previewCommand = () => run(async () => {
    if (!command.trim()) return
    const sid = activeWorkSessionId
    const boundPath = settings.path
    const value = await api<CommandPreview>('/v9/terminal/persistent/preview', json('POST', { session_id: sid, command }))
    if (currentSession.current === sid && projectPath.current === boundPath) setCommandPreview(value)
  })
  const confirmCommand = () => run(async () => {
    if (!commandPreview) return
    const sid = activeWorkSessionId
    const result = await api<TerminalEntry>('/v9/terminal/persistent/commit', json('POST', { token: commandPreview.token }))
    if (currentSession.current !== sid) return
    setCommandPreview(undefined); setCommand(''); setTerminalState(old => ({ ...old, running: result.status === 'running', persistent: true }))
    setTerminal((items) => [...items.filter((item) => item.id !== commandPreview.token), result])
    setRefreshCount((value) => value + 1)
  })
  const stopTerminal = () => run(async () => {
    await api('/v9/terminal/persistent/stop', json('POST', { session_id: activeWorkSessionId }))
    setTerminalState(old => ({ ...old, running: false, persistent: false, background: false })); setCommandPreview(undefined); setRefreshCount(n => n+1)
  })

  const panelEmpty = (title: string, description: string, action?: () => void, label?: string) => <div className="workspace-panel-empty"><FolderGit2 /><h3>{title}</h3><p>{description}</p>{action && <Button variant="outline" size="sm" onClick={action} disabled={!ready}>{label}</Button>}</div>

  const renderStart = (side: WorkPane) => <WorkStart onSideChat={()=>void createSide()} recent={workSessions} projectPath={settings.path} art={activeCharacter.art} side={side} onOpen={openTool} />
  const renderChat = () => <section className="workspace-chat" aria-label="工作对话">
          {connection !== 'connected' && <div className="workspace-connection-notice"><DataModeNotice /></div>}
          <ConversationTools mode="work" sessionId={activeWorkSessionId}/>{workMessages.length || generating.work ? <MessageList key={activeWorkSessionId} messages={workMessages} mode="work" preservePosition navigation navigationHidden={toolsOpen} /> : <div className="workspace-welcome workspace-new-chat"><LingxiLogo className="workspace-welcome-icon"/><h2>我们应该在 <WorkProjectPicker sessionId={activeWorkSessionId} path={settings.path} disabled={!ready}>{projectName||'零汐'}</WorkProjectPicker> 中做些什么？</h2></div>}
          <div className="workspace-compose-area"><WorkComposer onContext={edit} header={<div className="workspace-composer-project">{!workMessages.some(message=>message.role==='user')&&<WorkProjectPicker sessionId={activeWorkSessionId} path={settings.path} disabled={!ready}><Folder/><span>{projectName||'选择工作区'}</span></WorkProjectPicker>}<WorkPresetControl sessionId={activeWorkSessionId} disabled={!ready||sending.work||generating.work}/>{!workMessages.some(message=>message.role==='user')&&<button type="button" className="workspace-composer-git" aria-label="打开当前工作区的 Git" disabled={!ready} onClick={()=>openTool('git')}><GitBranch/><span>Git</span></button>}</div>} /></div>
        </section>
  const renderTab = (tab: WorkTab) => {
    const panelLoading = Boolean(loading[tab.kind]), panelError = errors[tab.kind] || ''
    return <>
      {tab.kind === 'chat' && renderChat()}
      {tab.kind === 'side-chat' && tab.sessionId && <SideConversation sessionId={tab.sessionId} onReturn={setReturnText} />}
      {tab.kind === 'start' && renderStart(tab.side)}
      {tab.kind==='files'&&<div className="workspace-file-page">{settings.path&&<div className="workspace-file-toolbar"><button type="button" className="workspace-root-chip" title={settings.path} aria-label="切换工作区" onClick={edit}><Folder/><span>{projectName}</span></button></div>}{!ready?panelEmpty('连接服务后继续','本地文件需要服务连接。'):!settings.path?panelEmpty('项目文件','选择文件夹以查看文件。',edit,'选择工作区'):<div className="workspace-file-layout"><div className="workspace-file-empty"><Folder/><h2>打开文件</h2><p>从工作区目录树中<br/>选择文件</p></div><FileExplorer compact key={settings.path} sessionId={activeWorkSessionId!} active={active[tab.side]===tab.id&&(tab.side==='left'||toolsOpen)} refresh={refreshCount} onOpen={(path,line,column,sha)=>void openFile(path,line,column,sha)}/></div>}</div>}

      {tab.kind === 'browser' && (activeWorkSessionId && ready
        ? <ResearchWorkspacePanel sessionId={activeWorkSessionId} openArtifacts={() => openTool('artifacts', tab.side)}
            legacy={<BrowserTools embedded open={active[tab.side] === tab.id && (tab.side === 'left' || toolsOpen)} enabled={connection === 'connected'} onOpenChange={() => undefined} />} />
        : panelEmpty('先打开工作会话', '搜索和浏览器操作记录属于当前工作会话。'))}
      {tab.kind === 'interactive-terminal' && (activeWorkSessionId && ready
        ? <InteractiveTerminal sessionId={activeWorkSessionId} projectPath={settings.path} active={active[tab.side] === tab.id && (tab.side === 'left' || toolsOpen)} />
        : panelEmpty('先打开工作会话', '每个工作会话有独立的交互终端。'))}
      {tab.kind === 'workflow' && (activeWorkSessionId && ready
        ? <WorkflowPanel sessionId={activeWorkSessionId} /> : panelEmpty('先打开工作会话', '流程按工作会话保存。'))}
      {tab.kind === 'office' && (activeWorkSessionId && ready
        ? <OfficeWorkspacePanel sessionId={activeWorkSessionId} projectPath={settings.path} openArtifacts={() => openTool('artifacts', tab.side)} />
        : panelEmpty('先打开工作会话', 'Office 和 OCR 产物属于当前工作会话。'))}
              {tab.kind === 'context' && <><div className="workspace-panel-heading"><h2>工作上下文</h2><Button variant="ghost" size="sm" disabled={!ready || busy} onClick={edit}>编辑</Button></div>{contextError && <div className="workspace-panel-error"><p>工作记录读取失败：{contextError}</p><Button variant="outline" size="sm" onClick={() => void refresh()}>重试</Button></div>}<dl className="workspace-context"><div><dt>目标</dt><dd>{context.record.goal || currentTask?.document?.goal || '尚未填写。明确本次工作的目标，方便持续推进。'}</dd></div><div><dt>材料</dt><dd>{context.record.materials || '可在对话中补充材料，或从项目文件开始。'}</dd></div><div><dt>限制</dt><dd>{context.record.constraints || '工作上下文独立保存；命令执行需要逐次确认。'}</dd></div><div><dt>验收标准</dt><dd>{settings.acceptance || currentTask?.document?.acceptance || '填写怎样才算完成这次工作。'}</dd></div></dl><div className="workspace-context-project"><FolderGit2 /><div><strong>{projectName || '尚未绑定项目'}</strong><p>{settings.path || '绑定后可查看文件、差异和运行终端。'}</p></div><Button variant="ghost" size="icon" className="size-7" aria-label="编辑项目目录" disabled={!ready || busy} onClick={edit}><ChevronRight /></Button></div><div className="workspace-section-heading">项目工具</div><div className="workspace-tool-links">{toolOptions.filter((item) => ['git', 'ssh', 'review'].includes(item.id)).map((item) => <button key={item.id} onClick={() => openTool(item.id)}><item.icon /><span>{item.title}</span><ChevronRight /></button>)}</div></>}
              {tab.kind === 'tasks' && (activeWorkSessionId ? <WorkTeamPanel sessionId={activeWorkSessionId} /> : panelEmpty('先建立工作会话', '新建或选择工作会话后编排协作任务。'))}
              {['changes', 'terminal'].includes(tab.kind) && <><div className="workspace-panel-heading"><h2>{tab.kind === 'changes' ? '更改差异' : 'PowerShell'}</h2><Tooltip content="刷新当前面板"><Button variant="ghost" size="icon" className="size-7" aria-label="刷新当前工具" disabled={!ready || panelLoading} onClick={() => setRefreshCount((value) => value + 1)}><RefreshCw className={panelLoading ? 'animate-spin' : ''} /></Button></Tooltip></div>{!ready ? panelEmpty('连接服务后继续', '工作文件与终端需要本地服务连接。') : !settings.path ? panelEmpty('先绑定一个项目', '每个工作会话可绑定独立的项目目录。', edit, '绑定项目目录') : <>{panelError && <div className="workspace-panel-error" role="alert"><p>{panelError}</p><Button variant="outline" size="sm" onClick={() => setRefreshCount((value) => value + 1)}>重试读取</Button></div>}{panelLoading && <div className="workspace-loading"><LoaderCircle className="animate-spin" />正在读取{tab.kind === 'changes' ? '更改差异' : '终端记录'}…</div>}

                {tab.kind === 'changes' && !panelLoading && !panelError && <>{changes?.status ? <div className="workspace-change-status"><h3>文件状态</h3><CodeView text={changes.status} /></div> : <p className="workspace-inline-note">工作区没有检测到更改。</p>}{changes?.staged && <div className="workspace-diff-section"><h3>已暂存</h3><CodeView text={changes.staged} diff /></div>}{changes?.working && <div className="workspace-diff-section"><h3>未暂存</h3><CodeView text={changes.working} diff /></div>}<Button variant="outline" className="workspace-panel-action" onClick={() => openTool('git')}><FolderGit2 />打开 Git / Worktree</Button></>}
                {tab.kind === 'terminal' && <div className="workspace-terminal"><div className="workspace-terminal-note"><ShieldCheck /><span>保留目录与变量，每条命令先预览再确认。关闭标签不停止进程。</span></div><div className="workspace-terminal-controls"><code>{terminalState.cwd || settings.path}</code><Button size="xs" variant="outline" disabled={!terminalState.persistent || busy} onClick={() => void stopTerminal()}>{terminalState.running || terminalState.background ? '停止命令与终端' : '关闭终端进程'}</Button></div>{terminalState.background && <p className="workspace-inline-note" role="status">有本终端启动的后台进程；停止终端会一并清理。</p>}<div ref={terminalLog} onScroll={event => { const el = event.currentTarget; terminalFollow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40; if (terminalFollow.current) setTerminalBehind(false) }} className="workspace-terminal-output" role="log" aria-label="PowerShell 执行记录">{!terminal.length && !panelLoading && <div className="workspace-terminal-empty"><SquareTerminal /><p>终端已就绪</p><span>输入命令开始。每条命令单独确认。</span></div>}{terminal.map((item) => <article key={item.id}><div className="workspace-terminal-cwd">{item.cwd}</div><pre className="workspace-terminal-command">PS &gt; {item.command}</pre><pre>{item.result?.output || (item.status === 'running' ? '执行中；刷新不会再次执行。' : item.status === 'complete' ? '命令结束，没有文字输出。' : item.status === 'stopped' ? '命令已停止；已产生的外部效果请按需核对。' : '结果未知，请核对项目状态；不会自动重放。')}</pre><span className={item.result?.exit_code === 0 ? 'workspace-terminal-exit' : 'workspace-terminal-exit is-error'}>{item.status === 'running' ? '执行中' : item.status === 'stopped' ? '已停止' : item.status === 'unknown' ? '结果未知' : `退出码 ${item.result?.exit_code ?? '未知'}`}{item.result?.limited ? ' · 仅保留末尾输出' : ''}</span></article>)}</div>{terminalBehind && <Button size="xs" variant="ghost" onClick={() => { terminalFollow.current = true; if (terminalLog.current) terminalLog.current.scrollTop = terminalLog.current.scrollHeight; setTerminalBehind(false) }}>回到最新输出</Button>}<form className="workspace-terminal-form" onSubmit={(event) => { event.preventDefault(); void previewCommand() }}><label htmlFor="workspace-command"><TerminalSquare />PowerShell 命令</label><Textarea id="workspace-command" value={command} maxLength={4000} disabled={!ready || busy || terminalState.running} onChange={(event) => setCommand(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void previewCommand() } }} placeholder="输入命令，Enter 预览" /><div><span>Shift + Enter 换行 · 不支持交互输入</span><Button type="submit" size="sm" disabled={!ready || busy || terminalState.running || !command.trim()}>{busy ? <LoaderCircle className="animate-spin" /> : <ChevronRight />}预览命令</Button></div></form></div>}
              </>}</>}
              {tab.kind === 'activity' && activeWorkSessionId && <FileActivity sessionId={activeWorkSessionId} active={active[tab.side]===tab.id&&(tab.side==='left'||toolsOpen)} connected={ready} onOpen={path=>void openFile(path)}/>}
              {tab.kind === 'artifacts' && activeWorkSessionId && <ArtifactLibrary sessionId={activeWorkSessionId} active={active[tab.side]===tab.id&&(tab.side==='left'||toolsOpen)} connected={ready}/>}
              {tab.kind === 'file' && tab.path && !filePreviews[tab.path] && <div className="workspace-panel-error" role="status"><p>{errors[tab.id] || '正在重新核对文件和目录权限…'}</p><Button size="sm" variant="outline" disabled={!ready || busy} onClick={() => void openFile(tab.path!)}>重新打开文件</Button></div>}
              {tab.kind === 'file' && tab.path && activeWorkSessionId && filePreviews[tab.path] && <WorkFileEditor ref={handle => { if (handle) fileEditors.current.set(tab.id, handle); else fileEditors.current.delete(tab.id) }} sessionId={activeWorkSessionId} projectPath={settings.path} path={tab.path} initial={filePreviews[tab.path]} enabled={ready} active={active[tab.side] === tab.id && (tab.side === 'left' || toolsOpen)} onDirty={dirty => setDirtyFiles(values => values[tab.id] === dirty ? values : { ...values, [tab.id]: dirty })} onSaved={() => setRefreshCount(value => value + 1)} />}
              {['git', 'ssh', 'review'].includes(tab.kind) && (ready && activeWorkSessionId ? <WorkToolFrame tab={tab.kind} sessionId={activeWorkSessionId} jobId={tab.kind === 'review' ? currentTask?.id : undefined} onClose={() => closeTab(tab.id)} /> : panelEmpty('连接服务后继续', '此工具需要本地工作服务连接。'))}
    </>
  }
  const rightTabs = tabs.filter(tab => tab.side === 'right' && tab.kind !== 'start')
  const openedTabsMenu = <DropdownMenu><DropdownMenuTrigger asChild><Button data-workspace-tab-count variant="ghost" size="icon" className="workspace-open-tabs-count" aria-label={`已打开的右侧标签页，${rightTabs.length} 个`}><span>{rightTabs.length}</span></Button></DropdownMenuTrigger><DropdownMenuContent align="end" className="workspace-open-tabs-menu"><div className="workspace-open-tabs-heading">已打开的标签页</div>{rightTabs.map(tab => { const Icon = toolOptions.find(item => item.id === tab.kind)?.icon || FileCode2; return <DropdownMenuItem key={tab.id} onSelect={() => { dispatchTabs({ type: 'select', id: tab.id }); setToolsOpen(true) }}><Icon /><span>{workTabTitle(tab)}</span>{active.right === tab.id && <Check className="ml-auto" />}</DropdownMenuItem> })}{!rightTabs.length && <DropdownMenuItem onSelect={() => setToolsOpen(true)}><Compass />打开开始页</DropdownMenuItem>}<DropdownMenuItem onSelect={()=>openTool('files')}><FileCode2 />打开文件</DropdownMenuItem><DropdownMenuItem onSelect={()=>void createSide()}><MessageSquare />侧边聊天</DropdownMenuItem></DropdownMenuContent></DropdownMenu>
  return <div className="workspace-page">
    <WorkSessionRail visible={railOpen} onClose={()=>setRailOpen(false)} onOpenChange={setRailOpen}/>

    <div className="workspace-body">
      <header className="workspace-toolbar">
        <div className="workspace-primary-tools"><Tooltip content={railOpen?'收起会话列表':'展开会话列表'}><Button variant="ghost" size="icon" className="size-8" aria-label={railOpen?'隐藏工作会话':'显示工作会话'} aria-expanded={railOpen} onClick={()=>setRailOpen(v=>!v)}>{railOpen?<PanelLeftClose/>:<PanelLeftOpen/>}</Button></Tooltip>
        <div className={`workspace-conversation-title${tabs.some(t=>t.side==='left'&&t.kind!=='chat')?' is-replaced':''}`}><Folder/><h1>{workMessages.length?session?.title||'工作对话':'新聊天'}</h1></div>
        <div className="workspace-left-tabs-host" ref={setLeftToolbarHost}/><div className="workspace-toolbar-actions"><Button variant="ghost" size="icon" className="size-8" aria-label="更多工作会话操作" disabled={!ready} onClick={event=>activeWorkSessionId&&requestWorkMenu(activeWorkSessionId,event.currentTarget,undefined,true,tabs.find(t=>t.kind==='side-chat'&&t.id===active[focusedPane]&&(t.side==='left'||toolsOpen))?.sessionId||tabs.find(t=>t.kind==='side-chat'&&t.id===active.left)?.sessionId||(toolsOpen?tabs.find(t=>t.id===active.right)?.sessionId:undefined))}><MoreHorizontal/></Button><ConversationHistory mode="work" />{(rightTabs.length>0&&(!toolsOpen||tabsOverflow))&&openedTabsMenu}</div></div><div className="workspace-pane-toolbar" ref={setToolbarHost}/>
      </header>
      <WorkDock onTabsOverflow={setTabsOverflow} toolbarHost={toolbarHost} leftToolbarHost={leftToolbarHost} onFocusPane={setFocusedPane} onCreateSideChat={() => void createSide()} state={{ ...tabState, tabs: tabs.map(tab => dirtyFiles[tab.id] ? { ...tab, title: tab.title + ' · 未保存' } : tab) }} dispatch={dispatchTabs} onCloseTab={id => void closeTab(id)} open={toolsOpen} onOpenChange={setToolsOpen} options={toolOptions.filter(item=>!['git','changes'].includes(item.id))} onOpen={openTool} renderTab={renderTab} renderStart={renderStart} projectPath={settings.path} reveal={reveal} />

    </div>
    <WorkConversationMenu/>
    <WorkspaceFolderPicker open={folderPickerOpen} initialPath={path} onOpenChange={setFolderPickerOpen} onSelect={setPath} />

    <Dialog open={returnText !== undefined} onOpenChange={open => { if (!open) setReturnText(undefined) }}><DialogContent><DialogHeader><DialogTitle>带回主对话</DialogTitle><DialogDescription>先编辑结论，再放入主对话草稿；发送仍由你确认。</DialogDescription></DialogHeader><Textarea aria-label="带回主对话的结论" value={returnText || ''} maxLength={7000} rows={10} onChange={e => setReturnText(e.target.value)} /><DialogFooter><Button variant="outline" onClick={() => setReturnText(undefined)}>取消</Button><Button disabled={!returnText?.trim()} onClick={() => { const text = `${drafts.work ? drafts.work + '\n\n' : ''}${returnText}`; if (text.length > 8000) { toast.error('主草稿与结论合计超过 8000 字，请先缩短内容'); return } setDraft('work', text); setReturnText(undefined); toast.success('结论已放入主对话草稿，请检查后发送') }}>加入主对话草稿</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={editing} onOpenChange={(open) => { if (!busy) setEditing(open) }}><DialogContent className="workspace-edit-dialog max-w-2xl"><DialogHeader><DialogTitle>项目与工作记录</DialogTitle><DialogDescription>目录、目标和验收标准仅用于当前工作会话。</DialogDescription></DialogHeader><div className="workspace-edit-fields"><label>项目目录<div className="workspace-folder-input"><Input aria-label="项目目录" placeholder="项目绝对路径" value={path} onChange={(event) => setPath(event.target.value)} /><Button variant="outline" size="sm" disabled={busy} onClick={() => setFolderPickerOpen(true)}>选择文件夹</Button></div></label><label>工作目标<Textarea aria-label="工作目标" placeholder="这次希望完成什么？" maxLength={1200} value={record.goal} onChange={(event) => setRecord({ ...record, goal: event.target.value })} /></label><label>材料<Textarea aria-label="工作材料" placeholder="需要参考的文件、背景或材料" maxLength={1200} value={record.materials} onChange={(event) => setRecord({ ...record, materials: event.target.value })} /></label><label>限制<Textarea aria-label="工作限制" placeholder="需要遵守的边界" maxLength={1000} value={record.constraints} onChange={(event) => setRecord({ ...record, constraints: event.target.value })} /></label><label>验收标准<Textarea aria-label="工作验收标准" placeholder="怎样才算完成？" maxLength={8000} value={acceptance} onChange={(event) => setAcceptance(event.target.value)} /></label></div><DialogFooter><Button variant="outline" disabled={busy} onClick={() => setEditing(false)}>取消</Button><Button disabled={busy || !ready} onClick={() => void save()}>{busy ? '保存中…' : '保存工作记录'}</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(commandPreview)} onOpenChange={(open) => { if (!open && !busy) setCommandPreview(undefined) }}><DialogContent><DialogHeader><DialogTitle>确认执行 PowerShell 命令</DialogTitle><DialogDescription>{commandPreview?.note}</DialogDescription></DialogHeader><div className="workspace-command-confirm"><h3>工作目录</h3><p>{commandPreview?.cwd}</p><h3>即将执行的命令</h3><pre>{commandPreview?.command}</pre></div><DialogFooter><Button variant="outline" disabled={busy} onClick={() => setCommandPreview(undefined)}>取消</Button><Button disabled={busy || !ready} onClick={() => void confirmCommand()}>{busy ? '执行中…' : '确认执行此命令'}</Button></DialogFooter></DialogContent></Dialog>
  </div>
}



