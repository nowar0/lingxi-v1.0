/** Shared transport for embedded work tools. Business results retain their typed ok/error contract. */
export function requestError(value: unknown, fallback = '操作失败，请重试'): string {
  if (value instanceof Error) return value.message || fallback;
  if (typeof value === 'string') return value.trim() && !value.trim().startsWith('<') ? value.slice(0, 400) : fallback;
  if (value && typeof value === 'object') {
    const data = value as Record<string, unknown>;
    if (typeof data.code === 'string' && data.code.startsWith('ui_auth')) return '连接已过期，请重新打开零汐';
    for (const key of ['message', 'detail', 'error']) {
      if (data[key] !== undefined) return requestError(data[key], fallback);
    }
  }
  return fallback;
}

export async function api(path: string, method = 'GET', body?: unknown, signal?: AbortSignal) {
  const readOnly = method.toUpperCase() === 'GET';
  const deadline = AbortSignal.timeout(readOnly ? 60000 : 180000);
  try {
    const response = await fetch(path, {
      method, cache: 'no-store', credentials: 'same-origin',
      headers: body === undefined ? {} : {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: signal ? AbortSignal.any([signal, deadline]) : deadline,
    });
    const raw = await response.text();
    let data: any = null;
    try { data = raw ? JSON.parse(raw) : null; }
    catch { if (response.ok) throw new Error('服务响应不完整，请刷新当前状态'); }
    if (!response.ok) throw new Error(requestError(data ?? raw, `请求失败（HTTP ${response.status}）`));
    return data;
  } catch (error) {
    if (signal?.aborted) throw error;
    if (deadline.aborted) throw new Error(readOnly ? '读取超时，请重试' : '处理结果尚未确认，请先刷新状态再重试');
    if (error instanceof TypeError) throw new Error('连接已中断，请检查连接后重试');
    throw error;
  }
}
