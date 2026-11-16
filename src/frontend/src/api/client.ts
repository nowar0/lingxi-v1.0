let hostSessionReady: Promise<void> | undefined
let bootstrapToken: string | undefined
let authenticatedAt = 0
const renewalInterval = 6 * 60 * 60 * 1000

type HostSessionBridge = { hostSession?: () => Promise<{ token: string }> }

/** Recover through the app-owned bridge; capabilities never enter persistent storage. */
export function establishHostSession(): Promise<void> {
  const now = Date.now()
  if (hostSessionReady && (!authenticatedAt || (now >= authenticatedAt && now - authenticatedAt < renewalInterval))) return hostSessionReady
  const fragment = new URLSearchParams(window.location.hash.slice(1))
  const offered = fragment.get('lingxi-bootstrap')
  if (offered) {
    bootstrapToken = offered
    window.history.replaceState(null, '', window.location.pathname + window.location.search)
  }
  const shell = (window as unknown as { LingxiShell?: HostSessionBridge }).LingxiShell
  if (!bootstrapToken && !shell?.hostSession) return Promise.resolve()
  authenticatedAt = 0
  const attempt = (async () => {
    const token = shell?.hostSession ? (await shell.hostSession()).token : bootstrapToken!
    if (!/^[A-Za-z0-9_-]{43,128}$/.test(token)) throw new Error('连接认证无效，请重新打开零汐')
    const response = await fetch('/v11/ui-auth/exchange', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token, renew: true }), credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000),
    })
    if (!response.ok) throw new Error('连接认证失败，请检查连接后重试')
    authenticatedAt = Date.now()
  })()
  hostSessionReady = attempt
  void attempt.catch(() => { if (hostSessionReady === attempt) { hostSessionReady = undefined; authenticatedAt = 0 } })
  return attempt
}

export class ApiError extends Error {
  status: number
  payload: unknown

  constructor(message: string, status: number, payload: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

function errorMessage(payload: unknown, status: number) {
  if (typeof payload === 'string') {
    const trimmed = payload.trim()
    if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
      try { return errorMessage(JSON.parse(trimmed), status) } catch { /* plain server text */ }
    }
    if (trimmed && !trimmed.startsWith('<')) return trimmed.slice(0, 400)
  }
  if (payload && typeof payload === 'object') {
    const data = payload as Record<string, unknown>
    if (typeof data.detail === 'string') return data.detail
    if (data.detail && typeof data.detail === 'object' && !Array.isArray(data.detail)) {
      const detail = data.detail as Record<string, unknown>
      if (typeof detail.code === 'string' && detail.code.startsWith('ui_auth')) return '连接认证已过期，请检查连接后重试'
      if (typeof detail.message === 'string') return detail.message
    }
    if (typeof data.error === 'string') return data.error
    if (data.error && typeof data.error === 'object' && typeof (data.error as Record<string, unknown>).message === 'string') {
      return String((data.error as Record<string, unknown>).message)
    }
    if (Array.isArray(data.detail)) return data.detail.slice(0, 4).map((item) => typeof item.msg === 'string' ? item.msg : '请求参数有误').join('；')
  }
  return `请求失败（HTTP ${status}）`
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  await establishHostSession()
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const readOnly=!options.method||options.method.toUpperCase()==='GET'
  const deadline=AbortSignal.timeout(readOnly?15000:180000)
  const readSignal=options.signal?AbortSignal.any([options.signal,deadline]):deadline
  let response: Response
  let payload: unknown
  try {
    response = await fetch(path, { ...options, signal: readSignal, headers, cache: 'no-store', credentials: 'same-origin' })
    const contentType = response.headers.get('content-type') || ''
    payload = response.status === 204 ? null : contentType.includes('application/json')
      ? await response.json().catch(() => { if (response.ok) throw new Error('服务响应不完整，请刷新当前状态'); return null })
      : await response.text()
  } catch (error) {
    if (deadline.aborted) throw new Error(readOnly ? '读取超时，请重试' : '处理结果尚未确认，请先检查当前状态再重试')
    if (error instanceof TypeError) throw new Error('连接已中断，请检查连接后重试')
    throw error
  }
  if (response.status === 401) { hostSessionReady = undefined; authenticatedAt = 0 }

  if (!response.ok) throw new ApiError(errorMessage(payload, response.status), response.status, payload)
  if (payload && typeof payload === 'object' && (payload as Record<string, unknown>).ok === false && (payload as Record<string, unknown>).error) {
    throw new ApiError(errorMessage(payload, response.status), response.status, payload)
  }
  return payload as T
}

export async function apiBlob(path: string, options: RequestInit = {}) {
  await establishHostSession()
  const deadline=AbortSignal.timeout(180000)
  const response = await fetch(path, { ...options,signal:options.signal?AbortSignal.any([options.signal,deadline]):deadline, cache: 'no-store', credentials: 'same-origin' })
  if (response.status === 401) { hostSessionReady = undefined; authenticatedAt = 0 }
  if (!response.ok) {
    const payload = response.headers.get('content-type')?.includes('application/json') ? await response.json().catch(() => null) : await response.text()
    throw new ApiError(errorMessage(payload, response.status), response.status, payload)
  }
  return response.blob()
}

export const json = (method: string, body?: unknown): RequestInit => ({
  method,
  body: body === undefined ? undefined : JSON.stringify(body),
})
