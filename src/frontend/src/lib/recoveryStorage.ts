type RecoveryBridge = { read: (key?: string) => Record<string, string | null>; set: (key: string, value: string | null) => { ok?: boolean; error?: string } }
const bridge = () => (window as unknown as { LingxiShell?: { recovery?: RecoveryBridge } }).LingxiShell?.recovery
export const recoveryStorage = {
  getItem(key: string) {
    const host = bridge()
    if (host) {
      const current = host.read(key)
      if (typeof current.error === 'string') throw new Error(current.error)
      return current[key] ?? null
    }
    return localStorage.getItem(key)
  },
  setItem(key: string, value: string) {
    const result = bridge()?.set(key, value)
    if (result?.error) throw new Error(result.error)
    try { localStorage.setItem(key, value) } catch (error) { if (!result?.ok) throw error }
  },
  removeItem(key: string) {
    const result = bridge()?.set(key, null)
    if (result?.error) throw new Error(result.error)
    try { localStorage.removeItem(key) } catch (error) { if (!result?.ok) throw error }
  },
}
