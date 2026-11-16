import { api, apiBlob, json } from './client'
import type { ConversationQuestion, ToolApproval, ToolActivity, Character, Health, MemoryItem, Message, Mode, ModelModule, ModelProfile, ScheduleItem, Session, TaskItem, UiState, Usage } from '@/types'

export interface SessionSnapshot {
  session_id: number
  session_version: number
  event_revision?: number
  mode: Mode
  messages: Message[]
  batches: Array<{ id: string; status: string; error?: string; assistant_id?: number; details?: { tools:ToolActivity[]; approvals?:ToolApproval[]; questions?:ConversationQuestion[] } }>
  has_more?:boolean
  oldest_id?:number
  queued: number
}

export interface MessageMutationResult { view: SessionSnapshot; undo_id?: string }

export interface RestorePreview {
  session_id: number; mode: Mode; target_id: number; distance: number; session_version: number
  latest_message_id: number; allowed: boolean; reason: string; external_effects_note: string
}

export interface MemoryPageResult {
  items: MemoryItem[]
  total: number
  offset: number
  limit: number
}

export interface MemorySearchResult {
  items: MemoryItem[]
  semantic_status: string
  semantic_note?: string
  note?: string
  total?: number
  total_records?: number
  semantic_chunks?: number
  semantic_backend?: string
}

export interface MemoryPagination { offset?: number; limit?: number }

type StoredMemory = Omit<MemoryItem, 'source'> & { source?: unknown }
const memoryForUi = (item: StoredMemory): MemoryItem => {
  const source = item.source
  return { ...item, source: typeof source === 'string' ? source : source && typeof source === 'object'
    ? String((source as Record<string, unknown>).origin || '对话记录') : '手动添加' }
}

const taskForUi = (item: TaskItem): TaskItem => ({
  ...item,
  title: item.title || item.document?.goal,
  status: item.status === 'complete' ? 'completed' : item.status,
  nodes: item.nodes?.map((node) => ({ ...node, status: node.status === 'complete' ? 'completed' : node.status })),
})

export const lingxi = {
  answerQuestions: (question:ConversationQuestion, answers:ConversationQuestion['answers'], skipped:boolean, requestId:string) => api<{ok:boolean;view:SessionSnapshot}>(`/v3/sessions/${question.snapshot.session_id}/questions/${question.id}`,json('POST',{fingerprint:question.fingerprint,request_id:requestId,answers,skipped})),
  decideToolApproval: (sid:number, approval:ToolApproval, allowed:boolean) => api(`/v3/sessions/${sid}/approvals/${approval.id}`,json('POST',{fingerprint:approval.fingerprint,allowed,batch_id:approval.snapshot.batch_id,attempt_id:approval.snapshot.attempt_id,session_version:approval.snapshot.session_version})),
  health: () => api<Health>('/health'),
  usage: () => api<Usage>('/v1/usage'),
  models: () => api<{ roles: Record<string, string>; models: string[]; reasoning?: Record<string, import('@/lib/workComposerSettings').ReasoningCapability> }>('/v1/models'),
  uiState: () => api<UiState>('/v2/ui-state'),
  updateUiState: (revision: number, patch: Partial<Omit<UiState, 'revision'>>) =>
    api<UiState>('/v2/ui-state', json('PATCH', { expected_revision: revision, ...patch })),

  sessions: async (mode:Mode,offset=0,selected?:number)=>{const page=await api<{sessions:Session[];next_offset:number|null}>(`/v2/sessions?mode=${mode}&offset=${offset}&limit=100&include_archived=false`);if(selected&&!page.sessions.some(s=>s.id===selected)){try{const active=await api<Session>(`/v2/sessions/${selected}`);if(active.mode===mode&&!active.archived)page.sessions.push(active)}catch(error){if(!(error instanceof Error)||!('status' in error)||error.status!==404)throw error}}return page},
  createSession: (mode: Mode, title: string, creationId: string) =>
    api<Session>('/v2/sessions', json('POST', { mode, title, creation_id: creationId })),
  renameSession: (id: number, title: string) => api<Session>(`/v2/sessions/${id}`, json('PATCH', { title })),
  archiveSession: (id: number, archived = true) => api(`/v2/sessions/${id}/archive`, json('POST', { archived })),
  messages: (id: number, mode: Mode) => api<{ messages: Message[] }>(`/v2/sessions/${id}/messages?mode=${mode}`),
  draft: (id: number) => api<{ content: string; revision: number }>(`/v2/sessions/${id}/draft`),
  saveDraft: (id: number, content: string, revision: number) => api<{ content: string; revision: number }>(`/v2/sessions/${id}/draft`, json('PUT', { content, expected_revision: revision })),
  snapshot: (id: number, mode: Mode,before?:number) => api<SessionSnapshot>(`/v3/sessions/${id}/view?mode=${mode}${before?'&before='+before:''}`),
  send: (id: number, mode: Mode, content: string, clientMessageId: string, character?: Character, version?: number) =>
    api(`/v3/sessions/${id}/inputs`, json('POST', {
      mode, content, client_message_id: clientMessageId,
      ...(version === undefined ? {} : { expected_version: version }),
      ...(mode === 'companion' && character ? { character_id: character.id, character_revision: character.revision } : {}),
    })),
  cancelInput: (id: number, mode: Mode, clientId: string) => api<{ cancelled: boolean; already_processed?: boolean }>(`/v3/sessions/${id}/inputs/${clientId}/cancel`, json('POST', { mode })),
  run: (id: number, mode: Mode) => api<SessionSnapshot>(`/v3/sessions/${id}/run`, json('POST', { mode })),
  retry: (id: number, mode: Mode, batchId: string) => api<SessionSnapshot>(`/v3/sessions/${id}/run`, json('POST', { mode, retry_batch_id: batchId })),
  stop: (id: number, mode: Mode) => api<SessionSnapshot>(`/v3/sessions/${id}/stop`, json('POST', { mode })),
  eventsUrl: (id: number, mode: Mode) => `/v3/sessions/${id}/events?mode=${mode}`,
  history: (id: number, mode: Mode, query: string, offset: number,kind='all') => api<{ items: Array<Message & { distance: number; restore_allowed: boolean; restore_reason: string }>; total: number }>(`/v5/sessions/${id}/history?mode=${mode}&q=${encodeURIComponent(query)}&offset=${offset}&limit=100&kind=${encodeURIComponent(kind)}`),
  restorePreview: (id: number, mode: Mode, targetId: number) => api<RestorePreview>(`/v5/sessions/${id}/restore-preview?mode=${mode}&target_id=${targetId}`),
  restore: (preview: RestorePreview) => api<{ view: SessionSnapshot }>(`/v5/sessions/${preview.session_id}/restore`, json('POST', { mode: preview.mode, target_id: preview.target_id, expected_version: preview.session_version, expected_latest_message_id: preview.latest_message_id })),

  editMessage: (sid: number, mid: number, version: number, content: string) => api<MessageMutationResult>(`/v8/sessions/${sid}/messages/${mid}`, json('PUT', {expected_version:version,content})),
  deleteMessage: (sid: number, mid: number, version: number) => api<MessageMutationResult>(`/v8/sessions/${sid}/messages/${mid}`, json('DELETE', {expected_version:version})),
  undoMessageDeletion: (sid: number, key: string, version: number) => api<MessageMutationResult>(`/v8/sessions/${sid}/message-actions/${key}/undo`, json('POST', {expected_version:version})),

  characters: () => api<{ cards: Character[] }>('/v2/characters'),
  createCharacter: (payload: Partial<Character>) => api<Character>('/v2/characters', json('POST', payload)),
  updateCharacter: (character: Character, payload: Partial<Character>) =>
    api<Character>(`/v2/characters/${character.id}`, json('PATCH', { ...payload, expected_revision: character.revision })),
  archiveCharacter: (id: string) => api(`/v2/characters/${id}/archive`, json('POST', {})),
  exportCharacter: (id: string, includeCompanion: boolean, includeShared: boolean, includeChat: boolean, sessionId?: number) =>
    apiBlob(`/v2/characters/${id}/export?include_companion=${includeCompanion}&include_shared=${includeShared}&include_chat=${includeChat}${includeChat && sessionId ? `&session_id=${sessionId}` : ''}`),
  previewCardImport: (data: string) => api<{ warnings?: string[]; import_id: string; card: Character; included: string[]; conflicts: Record<string, Array<{ key: string; current: string | null; incoming: string }>>; chat_count: number; preview_assets?: Record<string, string> }>(
    '/v2/card-imports/preview', json('POST', { data })),
  commitCardImport: (token: string, choices: Record<string, unknown>) => api(`/v2/card-imports/${token}/commit`, json('POST', choices)),

  memories: async (scope: string, sessionId?: number, { offset = 0, limit = 200 }: MemoryPagination = {}): Promise<MemoryPageResult> => {
    const data = await api<Omit<MemoryPageResult, 'items'> & { items: StoredMemory[] }>(`/v4/memory?scope=${encodeURIComponent(scope)}&offset=${offset}&limit=${limit}${sessionId !== undefined ? `&session_id=${sessionId}` : ''}`)
    return { ...data, items: data.items.map(memoryForUi) }
  },
  searchMemories: async (scope: string, query: string, sessionId?: number): Promise<MemorySearchResult> => {
    const data = await api<Omit<MemorySearchResult, 'items'> & { items: StoredMemory[] }>(`/v4/memory/search?scope=${encodeURIComponent(scope)}&q=${encodeURIComponent(query)}&semantic=true${sessionId !== undefined ? `&session_id=${sessionId}` : ''}`)
    // Preserve search status, notes and counts; total_records is not a hit count.
    return { ...data, items: data.items.map(memoryForUi) }
  },
  createMemory: (payload: { scope: string; key: string; value: string; category: string; pinned: boolean; session_id?: number }) =>
    api<MemoryItem>('/v4/memory', json('POST', payload)),
  updateMemory: (item: MemoryItem, patch: Partial<MemoryItem>) =>
    api<MemoryItem>(`/v4/memory/${item.id}`, json('PUT', { value: item.value, category: item.category, pinned: Boolean(item.pinned), occurred_at: item.occurred_at || null, ...patch, expected_revision: item.revision })),
  deleteMemory: (item: MemoryItem) => api(`/v4/memory/${item.id}?expected_revision=${item.revision}`, json('DELETE')),
  restoreMemory: (item: MemoryItem) => api<MemoryItem>(`/v4/memory/${item.id}/restore`, json('POST', { expected_revision: item.revision })),

  schedule: () => api<{ items: ScheduleItem[] }>('/v3/schedule'),
  createSchedule: (title: string, date: string, clientId?: string) => api<{ items: ScheduleItem[] }>('/v3/schedule', json('POST', { title, date, client_id: clientId })),
  deleteSchedule: (item: ScheduleItem) => api<{ items: ScheduleItem[] }>(`/v3/schedule/${item.id}?expected_revision=${item.revision}`, json('DELETE')),
  updateSchedule: (item: ScheduleItem, done: boolean) => api<{ items: ScheduleItem[] }>(`/v3/schedule/${item.id}`, json('PATCH', { done, expected_revision: item.revision })),

  tasks: async () => {
    const data = await api<{ items: TaskItem[]; projects: string[] }>('/v7/tasks')
    return { ...data, items: data.items.map(taskForUi) }
  },
  task: async (id: string) => taskForUi(await api<TaskItem>(`/v6/jobs/${id}`)),
  taskAction: async (task: TaskItem, action: string, nodeId?: string) =>
    taskForUi(await api<TaskItem>(`/v6/jobs/${task.id}/action`, json('POST', { action, expected_revision: task.revision, node_id: nodeId }))),

  modules: () => api<{ items: ModelModule[] }>('/v6/modules'),
  profiles: () => api<{ items: ModelProfile[] }>('/v6/model-profiles'),
  testModule: (id: string) => api<{ connected: boolean; capability_verified: boolean; note?: string }>(`/v6/modules/${id}/test`, json('POST', {})),
  uploadMedia: (sessionId: number, mime: string, name: string, data: string, signal?: AbortSignal) => api<{ id: string; url: string; mime: string; size: number }>('/v6/media', { ...json('POST', { session_id: sessionId, mime, name, data }), signal }),
  invokeMedia: (moduleId: string, sessionId: number, options: { text?: string; attachment_id?: string; voice?: string } = {}, signal?: AbortSignal) => api<{ text?: string; media?: { id: string; url: string; mime: string }; model: string }>(`/v6/modules/${moduleId}/invoke`, { ...json('POST', { session_id: sessionId, ...options }), signal }),
  extensionStatus: () => api<{ ready: boolean; version: string; executables: Record<string, boolean> }>('/v7/status'),
  backup: (preferences: Record<string, unknown>) => apiBlob('/v6/data/backup', json('POST', { preferences })),
}
