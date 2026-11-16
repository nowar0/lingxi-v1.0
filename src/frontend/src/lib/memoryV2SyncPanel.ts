/** UI policy only: never reads or mutates memory, drafts, approvals or cursors. */
import type { MemorySyncState } from './memoryV2Sync'

export type MemorySyncProtection = { editOpen: boolean; actionOpen: boolean; versionsOpen: boolean; restoreOpen: boolean; busy: boolean }
export function memorySyncProtectionActive(value: MemorySyncProtection): boolean {
  return value.editOpen || value.actionOpen || value.versionsOpen || value.restoreOpen || value.busy
}
export function memorySyncPanelDecision(value: MemorySyncProtection, alreadyPending: boolean, initial = false): 'defer' | 'reload' | 'retain-pending' | 'verify-baseline' {
  if (alreadyPending) return 'retain-pending'
  // The first cursor proves no external mutation: an open draft only means that
  // its initial list/cursor baseline still needs a deliberate readonly recheck.
  if (initial && memorySyncProtectionActive(value)) return 'verify-baseline'
  return memorySyncProtectionActive(value) ? 'defer' : 'reload'
}
export function memorySyncWritesReady(state: Readonly<MemorySyncState> | undefined, baselinePending: boolean): boolean {
  return !baselinePending && state?.last_checked !== undefined && !state.stale && !state.manual_restart_required
}
export function memorySyncPanelStatus(state: Readonly<MemorySyncState> | undefined, connected: boolean, visible: boolean): string {
  if (!connected) return '本地服务未连接，检查已暂停。'
  if (!visible) return '页面隐藏，检查已暂停。'
  if (state?.error) return state.error
  if (state?.manual_restart_required) return '检查暂不可用，请重试。'
  if (!state?.started || state.last_checked === undefined) return '正在进行首次检查…'
  if (state.inflight || state.pending) return '正在检查更新…'
  if (state.stale) return '上次变化检查尚未重新核实。'
  return '已是最新 · '+new Date(state.last_checked).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'})
}
