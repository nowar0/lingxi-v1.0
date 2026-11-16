export type WorkPane = 'left' | 'right'
export type WorkToolKind = 'start' | 'chat' | 'context' | 'tasks' | 'files' | 'activity' | 'artifacts' | 'changes' | 'terminal' | 'interactive-terminal' | 'workflow' | 'office' | 'browser' | 'git' | 'ssh' | 'review'
export type WorkTab = { id: string; kind: WorkToolKind | 'file' | 'side-chat'; title: string; path?: string; sessionId?: number; side: WorkPane }
export type WorkTabState = { tabs: WorkTab[]; active: { left: string; right: string } }
export type WorkTabAction = { type: 'open'; tab: WorkTab } | { type: 'select' | 'close'; id: string } | { type: 'move'; id: string; side: WorkPane } | { type: 'drop-files' } | { type: 'reorder'; id: string; beforeId: string }
export const initialWorkTabs: WorkTabState = { tabs: [{ id: 'chat', kind: 'chat', title: '工作对话', side: 'left' }, { id: 'start', kind: 'start', title: '开始', side: 'right' }], active: { left: 'chat', right: 'start' } }
export const workTabTitle = (tab: WorkTab) => tab.title
function validSelection(tabs: WorkTab[], active: WorkTabState['active']) {
  return Object.fromEntries((['left', 'right'] as const).map(side => [side, tabs.some(tab => tab.side === side && tab.id === active[side]) ? active[side] : tabs.find(tab => tab.side === side)?.id || ''])) as WorkTabState['active']
}

/** One reducer keeps the selected tab and the open set consistent under batched updates. */
export function workTabsReducer(state: WorkTabState, action: WorkTabAction): WorkTabState {
  if (action.type === 'drop-files') {
    const tabs = state.tabs.filter((tab) => tab.kind !== 'file')
    return { tabs, active: validSelection(tabs, state.active) }
  }
  if (action.type === 'open') {
    const found = state.tabs.find(tab => tab.id === action.tab.id)
    const tab = found || action.tab
    return { tabs: found ? state.tabs : [...state.tabs, tab], active: { ...state.active, [tab.side]: tab.id } }
  }
  const index = state.tabs.findIndex((tab) => tab.id === action.id)
  if (index < 0) return state
  const tab = state.tabs[index]
  if (action.type === 'select') return { ...state, active: { ...state.active, [tab.side]: action.id } }
  if (action.type === 'reorder') {
    const destination = state.tabs.find(item => item.id === action.beforeId)
    if (!destination || destination.id === tab.id || destination.side !== tab.side) return state
    const tabs = state.tabs.filter(item => item.id !== tab.id)
    tabs.splice(tabs.findIndex(item => item.id === destination.id), 0, tab)
    return { ...state, tabs }
  }
  if (action.type === 'move') {
    if (tab.side === action.side || tab.kind === 'chat' && action.side === 'right') return state
    const tabs = state.tabs.map(item => item.id === tab.id ? { ...item, side: action.side } : item)
    return { tabs, active: validSelection(tabs, { ...state.active, [action.side]: tab.id }) }
  }
  const tabs = state.tabs.filter((tab) => tab.id !== action.id)
  const peers = state.tabs.filter(item => item.side === tab.side)
  const peerIndex = peers.findIndex(item => item.id === tab.id)
  const nextPeers = peers.filter(item => item.id !== tab.id)
  const active = state.active[tab.side] === action.id ? { ...state.active, [tab.side]: nextPeers[Math.max(0, peerIndex - 1)]?.id || '' } : state.active
  return { tabs, active: validSelection(tabs, active) }
}

export function workStartShortcut(event: { key: string; code?: string; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean; altKey: boolean }) {
  if (!(event.ctrlKey || event.metaKey) || event.shiftKey || event.altKey) return undefined
  if (event.key.toLowerCase() === 'p') return 'files'
  if (event.key.toLowerCase() === 't') return 'browser'
  if (event.code === 'Backquote' || event.key === '`') return 'terminal'
  return undefined
}

const layoutKinds = new Set(['start', 'chat', 'context', 'tasks', 'files', 'activity', 'artifacts', 'changes', 'terminal', 'interactive-terminal', 'workflow', 'office', 'browser', 'git', 'ssh', 'review', 'file', 'side-chat'])
const layoutKey = (sessionId: number) => `lingxi-work-layout-v1:${sessionId}`
export function parseWorkLayout(raw: string | null): { state: WorkTabState; root: string; open: boolean } {
  const fallback = { state: initialWorkTabs, root: '', open: false }
  try {
    if (!raw || raw.length > 100_000) return fallback
    const data = JSON.parse(raw)
    if (data.version !== 1 || typeof data.root !== 'string' || data.root.length > 2000 || !Array.isArray(data.tabs) || data.tabs.length > 50) return fallback
    const ids = new Set<string>()
    const tabs: WorkTab[] = data.tabs.map((item: WorkTab) => {
      if (!item || typeof item.id !== 'string' || item.id.length > 2200 || !layoutKinds.has(item.kind) || !['left', 'right'].includes(item.side) || ids.has(item.id)) throw new Error('无效标签')
      ids.add(item.id)
      if (item.kind === 'file') {
        if (typeof item.path !== 'string' || item.path.length > 2000 || item.path.includes('\0') || item.id !== `file:${item.path}`) throw new Error('无效文件标签')
        return { id: item.id, kind: item.kind, path: item.path, title: item.path.split(/[\\/]/).at(-1) || item.path, side: item.side }
      }
      if (item.kind === 'side-chat') {
        if (!Number.isSafeInteger(item.sessionId) || item.sessionId! <= 0 || item.id !== `side:${item.sessionId}`) throw new Error('无效侧聊')
        return { id: item.id, kind: item.kind, sessionId: item.sessionId, title: '独立侧聊', side: item.side }
      }
      if (item.id !== item.kind) throw new Error('无效工具标签')
      // Main conversation always remains on the left. Legacy moved-chat
      // layouts become a normal main chat, never a silently shared side chat.
      return { id: item.id, kind: item.kind, title: typeof item.title === 'string' ? item.title.slice(0, 80) : item.kind, side: item.kind === 'chat' ? 'left' : item.side }
    })
    const active = { left: typeof data.active?.left === 'string' ? data.active.left : '', right: typeof data.active?.right === 'string' ? data.active.right : '' }
    return { state: { tabs, active: validSelection(tabs, active) }, root: data.root, open: typeof data.open === 'boolean' ? data.open : fallback.open }
  } catch { return fallback }
}
export function restoreWorkLayout(sessionId?: number) {
  try { return parseWorkLayout(sessionId ? localStorage.getItem(layoutKey(sessionId)) : null) }
  catch { return parseWorkLayout(null) }
}
export function saveWorkLayout(sessionId: number, state: WorkTabState, root: string, open: boolean) {
  // Persist only the navigation contract. Drafts and files have separate
  // ownership; credentials, command previews and approvals never enter it.
  const tabs = state.tabs.map(({ id, kind, path, sessionId, title, side }) => ({ id, kind, path, sessionId, title, side }))
  try { localStorage.setItem(layoutKey(sessionId), JSON.stringify({ version: 1, root, open, tabs, active: state.active })) }
  catch { /* Server-owned chat/drafts remain available when layout storage is full. */ }
}
