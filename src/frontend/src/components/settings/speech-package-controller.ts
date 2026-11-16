export type ModelPackage = { id: string; name: string; kind: string; size: number; installed: boolean; runtime_ready: boolean; version: string; active_version?: string; pointer_revision: number; versions: string[]; license: string; device_hint: string; source: string; job?: { id: string; status: string; phase: string; error: string; completed_bytes: number; current_bytes: number; total_bytes: number } }
export type SpeechEngine = { engine: string; name: string; kind: string; ready: boolean; installed: boolean; loading?: boolean; device: string; issues: string[]; last_error?: string }
export type EngineOptions = { revision: number; device: string; hotwords: string[] }
export type Device = { cpu: boolean; cuda: boolean; gpu: string | null; vram_mb: number | null }
export type SpeechSnapshot = { packages: ModelPackage[]; engines: SpeechEngine[]; options: Record<string, EngineOptions>; optionErrors?: Record<string, string>; device?: Device; error: string; busy: string }
export const emptySpeechSnapshot = (): SpeechSnapshot => ({ packages: [], engines: [], options: {}, error: '', busy: '' })
type IO = {
  api: <T>(path: string, options?: RequestInit) => Promise<T>
  blob: (path: string, options?: RequestInit) => Promise<Blob>
  changed: (snapshot: SpeechSnapshot) => void
  modelsChanged: () => Promise<unknown>
  success: (message: string) => void
  audio?: () => HTMLAudioElement
  createUrl?: (blob: Blob) => string
  revokeUrl?: (url: string) => void
  setTimer?: typeof setTimeout
  clearTimer?: typeof clearTimeout
}
const body = (value?: unknown, method = 'POST'): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: value === undefined ? undefined : JSON.stringify(value) })
const isAbort = (error: unknown) => (error as Error)?.name === 'AbortError'
const message = (error: unknown) => error instanceof Error ? error.message : '操作失败，请重试'
const activeDownload = (item: ModelPackage) => ['queued', 'downloading', 'preparing_runtime'].includes(item.job?.status || '')

/** Owns requests, audio and the activation queue for one mounted panel. */
export function createSpeechPackagesController(io: IO) {
  let state = emptySpeechSnapshot(), live = false, read: AbortController | undefined, reading: Promise<void> | undefined
  let action: AbortController | undefined, running: Promise<boolean> | undefined, timer: ReturnType<typeof setTimeout> | undefined
  let audio: HTMLAudioElement | undefined, audioUrl = '', audioGeneration = 0, previewing = false
  const pending = new Map<string, { job: string; revision: number }>(), stopping = new Set<string>()
  const later = io.setTimer || setTimeout, clear = io.clearTimer || clearTimeout
  const emit = (patch: Partial<SpeechSnapshot>) => { if (live) { state = { ...state, ...patch }; io.changed(state) } }
  const get = <T>(path: string, signal: AbortSignal) => io.api<T>(path, { signal: AbortSignal.any([signal, AbortSignal.timeout(15000)]) })
  const stopAudio = () => {
    audioGeneration++
    const previous = audio; audio = undefined
    if (previous) { previous.onended = null; previous.onerror = null; previous.pause(); previous.removeAttribute('src'); previous.load() }
    if (audioUrl) (io.revokeUrl || URL.revokeObjectURL)(audioUrl)
    audioUrl = ''
  }
  const unschedule = () => { if (timer !== undefined) clear(timer); timer = undefined }
  const schedule = () => {
    unschedule()
    if (!live || state.busy) return
    timer = later(() => { timer = undefined; void refresh().catch(error => { if (!isAbort(error)) emit({ error: message(error) }) }).finally(schedule) }, state.packages.some(activeDownload) || state.engines.some(e => e.loading) ? 2000 : 15000)
  }
  const invalidate = () => { read?.abort(); read = undefined; reading = undefined }
  const loadOptions = async (signal: AbortSignal) => {
    const errors: Record<string, string> = {}
    const entries = await Promise.all(['sensevoice-small', 'cosyvoice2', 'indextts2.5'].map(async id => {
      try { return [id, await get<EngineOptions>(`/v6/speech/${id}/settings`, signal)] as const }
      catch (error) { if (isAbort(error)) throw error; errors[id] = '设置读取失败，请刷新重试'; return undefined }
    }))
    return { values: Object.fromEntries(entries.filter((v): v is NonNullable<typeof v> => !!v)), errors }
  }
  const refresh = (settings = false): Promise<void> => {
    if (!live) return Promise.resolve()
    if (reading) return reading
    const controller = new AbortController(); read = controller
    const operation = (async () => {
      const [p, e, options] = await Promise.all([
        get<{ items: ModelPackage[] }>('/v6/model-packages', controller.signal),
        get<{ items: SpeechEngine[] }>('/v6/speech/engines', controller.signal),
        settings ? loadOptions(controller.signal) : Promise.resolve(undefined),
      ])
      if (live && read === controller && !controller.signal.aborted) {
        emit({ packages: p.items, engines: e.items, ...(options ? { options: options.values, optionErrors: options.errors } : {}) })
        pump()
      }
    })().catch(error => { controller.abort(); throw error })
    reading = operation
    void operation.finally(() => { if (read === controller) { read = undefined; reading = undefined } }).catch(() => {})
    return operation
  }
  const run = (id: string, task: (signal: AbortSignal) => Promise<unknown>, refreshAfter = true): Promise<boolean> => {
    if (!live || state.busy) return Promise.resolve(false)
    unschedule(); invalidate(); emit({ busy: id, error: '' })
    const controller = new AbortController(); action = controller
    const operation = (async () => {
      try {
        await task(controller.signal)
        if (!live || controller.signal.aborted) return false
        if (refreshAfter) await refresh()
        return true
      } catch (error) {
        if (live) emit({ error: isAbort(error) ? '已停止本次操作' : message(error) })
        return false
      } finally {
        if (action === controller) { action = undefined; running = undefined; emit({ busy: '' }); schedule(); pump() }
      }
    })()
    running = operation
    return operation
  }
  const activate = async (id: string, signal: AbortSignal, capturedRevision?: number) => {
    const engine = state.engines.find(e => e.engine === id)
    if (!engine) throw new Error('引擎信息尚未就绪，请刷新后重试')
    const revision = capturedRevision ?? (await get<{ items: Array<{ id: string; revision: number }> }>('/v6/modules', signal)).items.find(m => m.id === engine.kind)?.revision ?? 0
    await io.api(`/v6/speech/${id}/activate`, { ...body({ expected_revision: revision }), signal })
    if (!live || signal.aborted) return
    await io.modelsChanged()
    if (live && !signal.aborted) io.success('已启用 ' + engine.name)
  }
  function pump() {
    if (!live || state.busy || stopping.size) return
    for (const [id, intent] of pending) {
      const item = state.packages.find(p => p.id === id)
      if (!item?.job) continue
      if (item.job.id !== intent.job || ['failed', 'paused'].includes(item.job.status)) { pending.delete(id); continue }
      if (item.job.status === 'complete') {
        // Keep the intent until the operation lock is acquired. Busy work must not consume it.
        void run(id, async signal => { pending.delete(id); await activate(id, signal, intent.revision) })
        break
      }
    }
  }
  const preview = (id: string) => run(id, async signal => {
    stopAudio(); const generation = audioGeneration; previewing = true
    try {
      const blob = await io.blob(`/v6/speech/${id}/synthesize`, { ...body({ text: '你好，老师。今天也一起努力吧。' }), signal })
      if (!live || signal.aborted || generation !== audioGeneration) return
      const url = (io.createUrl || URL.createObjectURL)(blob), next = io.audio ? io.audio() : new Audio()
      audio = next; audioUrl = url; next.src = url
      next.onended = () => { if (audio === next) stopAudio() }
      next.onerror = () => { if (audio === next) { stopAudio(); emit({ error: '试听音频未能播放，请重试' }) } }
      try { await next.play() } catch (error) { if (audio === next) stopAudio(); throw error }
    } finally { previewing = false }
  })
  return {
    snapshot: () => state,
    start: async () => { live = true; try { await refresh(true) } catch (error) { if (!isAbort(error)) emit({ error: message(error) }) } finally { schedule() } },
    dispose: () => { live = false; unschedule(); invalidate(); action?.abort(); stopAudio(); pending.clear() },
    refresh: () => run('refresh', async () => { await refresh(true) }, false),
    activate: (id: string) => run(id, signal => activate(id, signal)),
    preview,
    stopAudio: () => { stopAudio(); if (previewing) action?.abort() },
    stop: async (id: string) => {
      if (!live || stopping.has(id)) return
      stopping.add(id); pending.delete(id); stopAudio()
      if (state.busy === id) action?.abort()
      try {
        await running
        await run('stop:' + id, async signal => { await io.api(`/v6/speech/${id}/stop`, { ...body(), signal }) })
      } finally { stopping.delete(id); pump() }
    },
    detectDevice: () => run('device', async signal => emit({ device: await io.api<Device>('/v6/speech/device-check', { ...body(), signal }) })),
    download: (item: ModelPackage) => run(item.id, async signal => {
      const engine = state.engines.find(e => e.engine === item.id)
      const revision = engine ? (await get<{ items: Array<{ id: string; revision: number }> }>('/v6/modules', signal)).items.find(m => m.id === engine.kind)?.revision ?? 0 : 0
      const job = await io.api<{ id: string }>('/v6/model-packages/download', { ...body({ id: item.id, version: item.version }), signal })
      if (live && !signal.aborted && engine) pending.set(item.id, { job: job.id, revision })
    }),
    pause: (item: ModelPackage) => run(item.id, async signal => { pending.delete(item.id); await io.api(`/v6/model-packages/downloads/${item.job!.id}/pause`, { ...body(), signal }) }),
    saveOptions: (id: string, patch: Partial<EngineOptions>) => run(id, async signal => {
      const prior = state.options[id]
      if (!prior) throw new Error('设置尚未读取，请刷新后重试')
      const saved = await io.api<EngineOptions>(`/v6/speech/${id}/settings`, { ...body({ device: prior.device, hotwords: prior.hotwords, ...patch, expected_revision: prior.revision }, 'PUT'), signal })
      emit({ options: { ...state.options, [id]: saved } })
    }),
    switchVersion: (item: ModelPackage, version: string) => run(item.id, async signal => { await io.api(`/v6/model-packages/${item.id}/activate`, { ...body({ version, expected_revision: item.pointer_revision }), signal }) }),
  }
}
