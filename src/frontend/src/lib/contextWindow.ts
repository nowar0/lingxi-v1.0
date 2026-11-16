export interface ContextPreview {
  note?: string
  initial_context: {
    estimated_input_tokens: number
    available_input_tokens: number
    counter?: string
    system?: string
    messages?: Array<{ role: string; content: string }>
    memory?: { text?: string }
    recent_turns?: number
    complete_turns?: number
    retrieval?: unknown
  }
}

export function contextUsage(preview: ContextPreview) {
  const { estimated_input_tokens: used, available_input_tokens: limit } = preview.initial_context
  if (!Number.isFinite(used) || used < 0 || !Number.isFinite(limit) || limit <= 0) throw new Error('上下文预算数据无效，请刷新重试')
  const percent = Math.round(used / limit * 100)
  return { used, limit, remaining: Math.max(0, limit - used), percent, ring: Math.min(100, percent) }
}

// Reads only: the unsent draft is never sent to the preview endpoint.
export async function loadContextWindow(request: <T>(path: string, options?: RequestInit) => Promise<T>, sid: number, signal?: AbortSignal) {
  const preview = await request<ContextPreview>(`/v3/sessions/${sid}/context?mode=work`, { signal })
  contextUsage(preview)
  return preview
}
