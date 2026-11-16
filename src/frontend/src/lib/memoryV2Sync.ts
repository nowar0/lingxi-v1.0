/** Read-only cross-window change detection. No timers/network before start().
 * The host refresh callback must respect its signal/context before publishing UI:
 * cancellation cannot undo a callback's already performed side effects. No mutation,
 * reinforcement, approval replay, memory body or browser storage exists here.
 */
export type MemorySyncScope = 'companion' | 'work' | 'shared'
export type MemorySyncContext = {
  connected: boolean
  visible: boolean
  scope: MemorySyncScope
  session_id?: number
  layer?: 1 | 2 | 3 | 4
  category?: string
  include_deleted?: boolean
}
export type MemorySyncState = {
  started: boolean
  inflight: boolean
  pending: boolean
  stale: boolean
  manual_restart_required: boolean
  cursor?: string
  last_checked?: number
  error?: string
}
export type MemorySyncChange = {
  cursor: string
  initial: boolean
  context: Readonly<MemorySyncContext>
  signal: AbortSignal
}
export type MemorySyncFetch = (path: string, init: RequestInit) => Promise<Response>
export type MemorySyncScheduler = {
  setTimeout: (callback: () => void, delay: number) => unknown
  clearTimeout: (handle: unknown) => void
}
type WireState = { cursor: string; changed: boolean; read_only: true }
const digest = /^[0-9a-f]{64}$/
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const sameOwner = (context: MemorySyncContext) => JSON.stringify([context.scope, context.session_id ?? null, context.layer ?? null, context.category ?? null, context.include_deleted ?? false])
function contextValue(value: MemorySyncContext): MemorySyncContext {
  if (!object(value) || Object.keys(value).some(key => !['connected','visible','scope','session_id','layer','category','include_deleted'].includes(key)) || typeof value.connected !== 'boolean' || typeof value.visible !== 'boolean' || !['companion','work','shared'].includes(value.scope)) throw new Error('记忆同步范围无效')
  if (value.session_id !== undefined && (!Number.isSafeInteger(value.session_id) || value.session_id < 1)) throw new Error('记忆同步会话无效')
  if (value.layer !== undefined && ![1,2,3,4].includes(value.layer)) throw new Error('记忆同步层级无效')
  if (value.category !== undefined && (typeof value.category !== 'string' || value.category.length > 80 || /[\u0000-\u001f]/.test(value.category))) throw new Error('记忆同步类别无效')
  if (value.include_deleted !== undefined && typeof value.include_deleted !== 'boolean') throw new Error('记忆同步删除范围无效')
  return { ...value }
}
function available(context: MemorySyncContext) { return context.connected && context.visible && (context.scope !== 'work' || context.session_id !== undefined) }
export function parseMemorySyncState(value: unknown, previous?: string): WireState {
  if (!object(value) || Object.keys(value).some(key => !['cursor','changed','read_only'].includes(key)) || typeof value.cursor !== 'string' || !digest.test(value.cursor) || typeof value.changed !== 'boolean' || value.read_only !== true) throw new Error('记忆同步响应不符合只读合同，请手动重试')
  if (previous !== undefined && value.changed !== (value.cursor !== previous)) throw new Error('记忆同步游标与变化标记不一致，请手动重试')
  return value as WireState
}
export function memorySyncPath(context: MemorySyncContext, cursor?: string): string {
  const value = contextValue(context)
  if (value.scope === 'work' && value.session_id === undefined) throw new Error('工作记忆同步需要明确会话')
  if (cursor !== undefined && !digest.test(cursor)) throw new Error('记忆同步游标无效')
  const query = new URLSearchParams({ scope: value.scope, include_deleted: String(value.include_deleted ?? false) })
  if (value.session_id !== undefined) query.set('session_id', String(value.session_id))
  if (value.layer !== undefined) query.set('layer', String(value.layer))
  if (value.category) query.set('category', value.category)
  if (cursor !== undefined) query.set('cursor', cursor)
  return '/v10/memory-sync/change-state?' + query.toString()
}
export function createMemoryV2Sync({ context: initialContext, fetcher, onChange, onStatus, scheduler, now = Date.now, intervalMs = 15000, readTimeoutMs = 10000 }: {
  context: MemorySyncContext
  fetcher: MemorySyncFetch
  onChange: (change: MemorySyncChange) => void | Promise<void>
  onStatus?: (state: Readonly<MemorySyncState>) => void
  scheduler?: MemorySyncScheduler
  now?: () => number
  intervalMs?: number
  readTimeoutMs?: number
}) {
  if (typeof fetcher !== 'function' || typeof onChange !== 'function') throw new Error('记忆同步需要明确的只读传输和刷新处理器')
  if (!Number.isFinite(intervalMs) || intervalMs < 15000 || intervalMs > 60000) throw new Error('记忆同步间隔必须为15至60秒')
  if (!Number.isFinite(readTimeoutMs) || readTimeoutMs < 1 || readTimeoutMs > 15000) throw new Error('记忆同步读取期限必须为1至15000毫秒')
  const timers = scheduler ?? { setTimeout: (callback: () => void, delay: number) => globalThis.setTimeout(callback, delay), clearTimeout: (handle: unknown) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>) }
  let context = contextValue(initialContext), generation = 0
  let state: MemorySyncState = { started: false, inflight: false, pending: false, stale: true, manual_restart_required: false }
  let timer: unknown, hasTimer = false
  let active: { generation: number; controller: AbortController } | undefined
  // Read callbacks from different owners cannot overlap. No callback is retried or
  // treated as authorization; the host decides how to preserve drafts/pending actions.
  let refreshTail: Promise<void> = Promise.resolve()
  function status() { return { ...state } }
  function publish() { try { onStatus?.(Object.freeze(status())) } catch { /* UI observer cannot replay a request. */ } }
  function clearTimer() { if (hasTimer) timers.clearTimeout(timer); hasTimer = false; timer = undefined }
  function invalidate() {
    const previous = active
    generation++; active = undefined; clearTimer()
    state = { ...state, inflight: false, pending: false }
    // Detach first: a host's AbortSignal listener may synchronously inspect/stop us.
    previous?.controller.abort()
  }
  function current(owner: number, controller: AbortController) { return owner === generation && state.started && !controller.signal.aborted && available(context) }
  function schedule() {
    clearTimer()
    if (!state.started || state.manual_restart_required || !available(context) || active) return
    timer = timers.setTimeout(() => { hasTimer = false; timer = undefined; void poll() }, intervalMs); hasTimer = true
  }
  function stopForManual(message: string) {
    state = { ...state, stale: true, manual_restart_required: true, error: message }
    clearTimer()
  }
  async function poll() {
    if (!state.started || state.manual_restart_required || !available(context) || active) return
    clearTimer()
    const owner = generation, controller = new AbortController(), captured = Object.freeze({ ...context }), previous = state.cursor
    const operation = { generation: owner, controller }; active = operation
    state = { ...state, inflight: true }; publish()
    let readTimedOut = false, malformedBody = false
    try {
      if (!current(owner, controller)) return
      // One deadline covers both fetch and body JSON. Promise.race also releases
      // admission when a transport ignores abort, or response.json never settles.
      let deadline: unknown
      let rejectAbort: (() => void) | undefined
      const cancelled = new Promise<never>((_resolve, reject) => {
        rejectAbort = () => reject(new Error('read_cancelled'))
        controller.signal.addEventListener('abort', rejectAbort, { once: true })
        deadline = timers.setTimeout(() => { readTimedOut = true; controller.abort() }, readTimeoutMs)
      })
      let read: { response: Response; payload?: unknown }
      try {
        read = await Promise.race([
          (async () => {
            const response = await fetcher(memorySyncPath(captured, previous), { method: 'GET', credentials: 'same-origin', cache: 'no-store', signal: controller.signal })
            if (!current(owner, controller)) throw new Error('read_cancelled')
            let payload: unknown
            if (response.ok) {
              try { payload = await response.json() }
              catch (error) { malformedBody = true; throw error }
            }
            return { response, payload }
          })(), cancelled,
        ])
      } finally {
        timers.clearTimeout(deadline)
        if (rejectAbort) controller.signal.removeEventListener('abort', rejectAbort)
      }
      const { response } = read
      if (!current(owner, controller)) return
      if (!response.ok) {
        if (response.status === 503) { stopForManual('记忆同步暂不可用（HTTP 503）；请手动重试，现有数据未更改'); return }
        // Permission and invalid-scope failures must not become a permanent polling loop.
        if (response.status >= 400 && response.status < 500) { stopForManual(`记忆同步被拒绝（HTTP ${response.status}），请核对范围后手动重试`); return }
        throw new Error(`记忆同步读取失败（HTTP ${response.status}），上次数据尚未重新核实`)
      }
      let payload: WireState
      try { payload = parseMemorySyncState(read.payload, previous) }
      catch { if (current(owner, controller)) stopForManual('记忆同步响应无效，已停止轮询；请手动重试'); return }
      if (!current(owner, controller)) return
      const initial = previous === undefined
      if (initial || payload.changed) {
        state = { ...state, pending: true }; publish()
        const refresh = refreshTail.catch(() => undefined).then(async () => {
          if (current(owner, controller)) await onChange({ cursor: payload.cursor, initial, context: captured, signal: controller.signal })
        })
        refreshTail = refresh.catch(() => undefined)
        await refresh
        if (!current(owner, controller)) return
      }
      state = { ...state, cursor: payload.cursor, last_checked: now(), stale: false, pending: false, error: undefined }
    } catch {
      if (readTimedOut && owner === generation && state.started && available(context)) {
        state = { ...state, stale: true, pending: false, error: '记忆同步读取超时，上次游标保留；可手动重试，只读检查稍后重试，不提交任何操作' }
      } else if (current(owner, controller)) {
        if (malformedBody) stopForManual('记忆同步响应无效，已停止轮询；请手动重试')
        else state = { ...state, stale: true, pending: false, error: '记忆同步或刷新失败，上次游标保留；只读检查将稍后重试，不提交任何操作' }
      }
    } finally {
      if (active === operation) {
        active = undefined
        if (owner === generation) { state = { ...state, inflight: false, pending: false }; publish(); schedule() }
      }
    }
  }
  return {
    status,
    start() {
      if (state.started && !state.manual_restart_required) return
      invalidate(); state = { ...state, started: true, manual_restart_required: false, error: undefined }; publish(); void poll()
    },
    updateContext(next: MemorySyncContext) {
      const updated = contextValue(next), changed = sameOwner(updated) !== sameOwner(context)
      const same = JSON.stringify(updated) === JSON.stringify(context)
      if (same) return
      invalidate(); context = updated
      if (changed) state = { ...state, cursor: undefined, last_checked: undefined, stale: true, manual_restart_required: false, error: undefined }
      else if (!available(context)) state = { ...state, stale: true }
      publish(); void poll()
    },
    refreshNow() {
      if (!state.started) return
      if (active) return // Including an asynchronous refresh callback; no overlap/replay.
      clearTimer(); state = { ...state, stale: true, manual_restart_required: false, error: undefined }; publish(); void poll()
    },
    stop() {
      invalidate(); state = { ...state, started: false, stale: true }; publish()
    },
  }
}
