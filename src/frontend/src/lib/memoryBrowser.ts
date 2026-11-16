import type { MemoryItem } from '../types/index'

export type MemoryScope = MemoryItem['scope']
export const MEMORY_PAGE_SIZE = 50

// These are the existing stage7.CATEGORIES values, not new memory semantics.
export const memoryCategories = [
  ['fact', '事实'],
  ['preference', '偏好'],
  ['event', '事件'],
  ['task', '事项'],
  ['communication_preference', '沟通偏好'],
  ['writing_preference', '写作偏好'],
  ['legacy', '迁移'],
] as const
export const memoryCategoryNames: Record<string, string> = Object.fromEntries(memoryCategories)

export const memoryOwnerKey = (scope: MemoryScope, sessionId?: number) => scope === 'work' ? `work:${sessionId ?? 'none'}` : scope
export interface DeletedMemory { ownerKey: string; item: MemoryItem }
export const deletedMemoryForOwner = (deleted: DeletedMemory | undefined, ownerKey: string) => deleted?.ownerKey === ownerKey ? deleted.item : undefined
export const lastMemoryPageOffset = (total: number, limit = MEMORY_PAGE_SIZE) => Math.max(0, Math.ceil(total / limit) - 1) * limit

export interface MemorySource { messages?: Array<{ id: number; content: string; role: string }>; note?: string }
export interface MemoryVersion { id?: number; revision?: number; value?: string; status?: string }
export interface MemoryHistory { source: MemorySource; versions: MemoryVersion[]; versionsNote?: string }
type ReadApi = <T>(path: string) => Promise<T>

export async function loadMemoryHistory(read: ReadApi, id: MemoryItem['id']): Promise<MemoryHistory> {
  const path = `/v4/memory/${encodeURIComponent(String(id))}`
  const [source, versions] = await Promise.allSettled([
    read<MemorySource>(`${path}/source`),
    read<{ versions: MemoryVersion[] }>(`${path}/versions`),
  ])
  const sourceError = source.status === 'rejected' ? source.reason as { status?: number; message?: string } : undefined
  return {
    source: source.status === 'fulfilled' ? source.value : { note: sourceError?.status === 404
      ? '来源原文已不可用（记忆可能已删除）；历史版本仍可查看。'
      : `来源读取失败：${sourceError?.message || '请稍后重试'}；不影响历史版本显示。` },
    versions: versions.status === 'fulfilled' ? versions.value.versions : [],
    versionsNote: versions.status === 'rejected' ? `历史版本读取失败：${String(versions.reason?.message || '请稍后重试')}` : undefined,
  }
}

export function memorySearchNotice(result: { semantic_status: string; semantic_note?: string; note?: string }) {
  const status = result.semantic_status
  if (status === 'complete') return [result.semantic_note, result.note].filter(Boolean).join('；')
  const fallback = status === 'disabled' ? '语义检索已关闭，当前仅显示关键词匹配。' : '语义检索暂不可用，已降级为关键词匹配。'
  return [fallback, result.semantic_note, result.note].filter(Boolean).join('；')
}
