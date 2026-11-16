export const effortLevels = ['auto', 'none', 'enabled', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] as const
export type Effort = typeof effortLevels[number]
export const effortLabels = ['自动', '关闭', '开启', '极低', '低', '中', '高', '极高', '最大']
export const effortNames = ['Auto', 'Off', 'On', 'Minimal', 'Low', 'Medium', 'High', 'XHigh', 'Max']
export interface ReasoningCapability { levels: string[]; source: string; note: string; family?: string }
export function availableEfforts(capability?: ReasoningCapability): Effort[] {
  return ['auto', ...effortLevels.filter((value) => value !== 'auto' && capability?.levels.includes(value))]
}
export function compatibleEffort(value: string | undefined, capability?: ReasoningCapability): Effort {
  return availableEfforts(capability).includes(value as Effort) ? value as Effort : 'auto'
}
export interface WorkPreset {
  id: string; revision: number; name: string; model: string; effort: Effort
  skill_ids: string[]; tools: string[]; brief: string
}
export interface WorkBinding { revision: number; preset: WorkPreset | null }
export type Request = <T>(path: string, options?: RequestInit) => Promise<T>
const json = (method: string, body: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
export function normalizedEffort(value?: string): Effort {
  return effortLevels.includes(value as Effort) ? value as Effort : 'auto'
}
export function modelLabel(model: string) { return model.includes('/') ? model.slice(model.indexOf('/') + 1) : model }
export function workModelChoices(available: { roles: Record<string, string>; models: string[] }) {
  const entries = new Map<string, string>()
  const names: Record<string, string> = { companion: '陪伴模块', work: '工作模块', orchestrator: '编排模块', chat: '对话模型' }
  if (available.roles.work || available.roles.chat) entries.set(available.roles.work ? 'work' : 'chat', '跟随工作模块')
  // Module targets are exposed as role values, but preset validation accepts
  // their role keys. Static provider models accept their full model name.
  for (const [role, target] of Object.entries(available.roles)) {
    if (!entries.has(role)) entries.set(role, `${modelLabel(target)} · ${names[role] || role}`)
  }
  for (const target of available.models) entries.set(target, modelLabel(target))
  return [...entries].map(([value, label]) => ({ value, label }))
}

// The existing backend snapshots presets per work session. Never change the global
// reasoning state or lose a session's assigned skills, tool limits, or instructions.
export async function saveWorkSelection(request: Request, sid: number, expectedRevision: number, model: string, effort: Effort): Promise<WorkBinding> {
  const path = `/v7/sessions/${sid}/preset`
  const current = await request<WorkBinding>(path)
  if (current.revision !== expectedRevision) throw new Error('本会话的模型设置已在其他窗口更新，请刷新后重试')
  const name = `工作区输入栏 · 会话 #${sid}`
  const previous = current.preset
  const document = {
    name, model, effort,
    skill_ids: previous?.skill_ids || [],
    ...(previous ? { tools: previous.tools } : {}),
    brief: previous?.brief || '',
    expected_revision: previous?.name === name ? previous.revision : 0,
  }
  const preset = await request<WorkPreset>(previous?.name === name ? `/v7/presets/${previous.id}` : '/v7/presets', json(previous?.name === name ? 'PUT' : 'POST', document))
  return request<WorkBinding>(path, json('PUT', { preset_id: preset.id, expected_revision: current.revision }))
}
