/** Read-only runtime capability inventory. Never discovers, installs or executes a package. */
export type CapabilityItem = { id: string; name: string; state: string; implemented: boolean; installed: boolean; enabled: boolean; note: string }
export type CapabilityStatus = { source: 'runtime'; data_root: string; items: CapabilityItem[]; verification: Record<string, string> }
export type CapabilityRequest = <T>(path: string, options?: RequestInit) => Promise<T>
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
function invalid(): never { throw new Error('能力状态合同无效；未把未知状态当作已安装或已启用，请刷新或更新本地服务') }

export function parseCapabilityStatus(value: unknown): CapabilityStatus {
  if (!object(value) || value.source !== 'runtime' || !text(value.data_root) || !Array.isArray(value.items) || !object(value.verification) || Object.values(value.verification).some(item => typeof item !== 'string')) invalid()
  const seen = new Set<string>()
  for (const raw of value.items) {
    if (!object(raw) || !['id', 'name', 'state', 'note'].every(key => text(raw[key])) || !['implemented', 'installed', 'enabled'].every(key => typeof raw[key] === 'boolean')) invalid()
    if (seen.has(raw.id as string) || raw.enabled && (!raw.installed || !raw.implemented) || raw.installed && !raw.implemented) invalid()
    if (raw.state === 'installed_enabled' && (!raw.implemented || !raw.installed || !raw.enabled)) invalid()
    if (raw.state === 'installed_disabled' && (!raw.implemented || !raw.installed || raw.enabled)) invalid()
    if (['deferred', 'planned', 'unavailable'].includes(String(raw.state)) && (raw.installed || raw.enabled)) invalid()
    // Optional extensions stay compatible but are not projected into fictitious UI fields.
    seen.add(raw.id as string)
  }
  return value as CapabilityStatus
}

export async function loadCapabilityStatus(request: CapabilityRequest, connection: string, signal?: AbortSignal): Promise<CapabilityStatus | undefined> {
  if (connection !== 'connected') return undefined
  return parseCapabilityStatus(await request('/v11/features', { signal }))
}

export function capabilityStateLabel(item: CapabilityItem): string {
  if (item.state === 'deferred') return '用户延期 · 未安装'
  if (item.state === 'planned') return '后期规划 · 未安装'
  if (item.state === 'unavailable') return '不可用 · 未安装'
  if (item.state === 'installed_enabled') return '已安装 · 已启用'
  if (item.state === 'installed_disabled') return '已安装 · 已停用'
  if (item.state === 'configured_program_local') return '已配置 · 未独立存储'
  return `未识别状态 · ${item.state}`
}

export function capabilityVerificationLabel(value: string): string {
  if (value === 'not_tested') return '未验收'
  if (value === 'see-delivery-report') return '需查对应交付报告；不等于已验收'
  return `服务端记录：${value}`
}

export function capabilityRequestSequence() {
  let sequence = 0
  return { begin: () => ++sequence, current: (value: number) => value === sequence, invalidate: () => { sequence++ } }
}
