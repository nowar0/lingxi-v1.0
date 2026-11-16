export type Mode = 'companion' | 'work'
export type ConnectionState = 'checking' | 'connected' | 'offline' | 'demo'

export interface ConversationQuestion {
  id: string
  fingerprint: string
  snapshot: { session_id: number; mode: Mode; batch_id: string; attempt_id: string; session_version: number }
  questions: Array<{ id: string; question: string; options: Array<{ label: string; description: string }>; multiple: boolean }>
  answers: Array<{ id: string; selected: string[]; text: string }>
  status: 'waiting' | 'answered' | 'skipped' | 'cancelled' | 'expired'
  expires_at: number
  valid?: boolean
}

export interface Speaker {
  id: string
  revision: number
  name: string
  avatar?: string
}

export interface Message {
  id: number | string
  role: 'user' | 'assistant' | 'system'
  content: string
  at?: string
  speaker?: Speaker
  delivery_status?: string
  batch_id?: string
}

export interface Session {
  id: number
  title: string
  mode: Mode
  created_at?: string
  updated_at?: string
  archived?: boolean
  unread?: number
  pinned?: boolean
}

export interface Character {
  id: string
  name: string
  description: string
  persona: string
  greeting: string
  tags: string[]
  avatar?: string
  art?: string
  revision: number
  expressions?: Record<string, string>
  motions?: Record<string, string>
  voice_ref?: string
  theme_ref?: string
}

export interface MemoryItem {
  id: number | string
  key: string
  value: string
  scope: 'shared' | 'companion' | 'work'
  category: string
  revision: number
  source?: string
  status?: string
  pinned?: boolean
  updated_at?: string
  occurred_at?: string | null
}

export interface ScheduleItem {
  id: string | number
  title: string
  date: string
  done: boolean
  revision: number
}

export interface TaskNode {
  id: string
  document?: { title?: string; task?: string; depends_on?: string[]; role?: string; model?: string; skill_id?: string; backend?: string; tools?: string[] | null; x?: number; y?: number; capability?: string; difficulty?: number; attachment_id?: string }
  status: string
  result?: string
  error?: string
  model?: string
}

export interface TaskItem {
  id: string
  session_id?: number
  title?: string
  status: string
  revision: number
  updated_at?: string
  document?: { goal?: string; acceptance?: string; materials?: string; engine?: string; review_report?: string; progress_report?: string; supervisor_position?: {x:number;y:number}; entry_nodes?: string[] }
  nodes?: TaskNode[]
  approvals?: Array<{ id: string; tool: string; workspace?: string; arguments: unknown }>
  events?: Array<{ at?: string; message?: string; type?: string; event?: string; detail?: string; seq?: number; node_id?: string | null }>
}

export interface Health {
  ok: boolean
  name: string
  version: string
}

export interface UiState {
  active_character_id: string
  active_companion_session_id?: number | null
  active_work_session_id?: number | null
  nickname?: string
  reasoning_effort?: string
  revision: number
}

export interface Usage {
  用量完整?:boolean; 输入已知笔数?:number; 输出已知笔数?:number;
  总未缓存输入?: number
  session_id?: number
  job_id?: string
  node_id?: string
  子智能体?: Array<Usage & {job_id:string;node_id:string}>
  缓存占输入比?: number | null
  总笔数?: number
  总输入?: number
  总输出?: number
  总缓存?: number
  总思考?: number
}

/** video declares native video input, not video generation or verified provider support. */
export type ModelCapability = 'text' | 'vision' | 'video' | 'image' | 'stt' | 'tts'
export type ModelProtocol = 'openai' | 'anthropic' | 'gemini'

export interface ModelModule {
  temperature?: number; timeout?: number; difficulty?: number
  reasoning_effort?: string
  reasoning?: { levels: string[]; source: string; note: string; family?: string }
  profile_id?: string
  id: string
  revision: number
  enabled: boolean
  base_url?: string
  model?: string
  required_capability?: ModelCapability
  local?: boolean
  voice?: string
  credential_ref?: string
  capabilities?: ModelCapability[]
  protocol?: ModelProtocol
  provider_id?: string
  capability_status?: 'unverified' | 'untested'
  capability_source?: string
}

export interface ModelProfile {
  temperature?: number; timeout?: number; enabled?: boolean; local_service_id?: string
  key_id?: string
  id: string
  name: string
  model: string
  base_url: string
  local?: boolean
  revision: number
  capabilities?: ModelCapability[]
  protocol?: ModelProtocol
  provider_id?: string
  capability_status?: 'unverified' | 'untested'
  capability_source?: string
}

export interface ThemeDefinition {
  format: 'lingxi-theme'
  version: 1
  name: string
  character?: string
  appearance: {
    theme: 'light' | 'dark'
    accent: string
    radius: number
    density: 'comfortable' | 'compact'
    fontScale: number
  }
  colors: {
    background: string
    surface: string
    sidebar: string
    text: string
    muted: string
    border: string
    input: string
  }
  scene?: 'night' | 'day' | 'minimal'
}

export interface ToolApproval { id:string; fingerprint:string; expires_at:number; status:string; valid:boolean; scope_note?:string; snapshot:{domain?:Record<string,unknown>;session_id:number;mode:Mode;batch_id:string;attempt_id:string;session_version:number} }
export interface ToolActivity { activity_id:string; tool:string; args:Record<string,unknown>; status:string; scope_note?:string; result?:string; duration_ms?:number|null; approval?:ToolApproval }
