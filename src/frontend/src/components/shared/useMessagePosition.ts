import { useLayoutEffect, useRef } from 'react'

type Position = { top: number; atBottom: boolean }
const positions = new Map<string, Position>()

export function useMessagePosition({ enabled, sessionKey, ready, content }: { enabled: boolean; sessionKey: string; ready: boolean; content: unknown }) {
  const ref = useRef<HTMLDivElement>(null)
  const restored = useRef(false)
  const follow = useRef(true)
  const save = () => {
    const node = ref.current
    if (!enabled || !node || !restored.current) return
    const atBottom = node.scrollHeight - node.clientHeight - node.scrollTop <= 32
    follow.current = atBottom
    positions.set(sessionKey, { top: node.scrollTop, atBottom })
  }
  useLayoutEffect(() => {
    if (!enabled || !ready || !ref.current) return
    const node = ref.current
    if (!restored.current) {
      const saved = positions.get(sessionKey)
      follow.current = saved?.atBottom ?? true
      node.scrollTop = saved && !saved.atBottom ? saved.top : node.scrollHeight
      restored.current = true
    } else if (follow.current) node.scrollTop = node.scrollHeight
  }, [content, enabled, ready, sessionKey])
  useLayoutEffect(() => {
    const node = ref.current
    if (!enabled || !node) return
    const observer = new ResizeObserver(() => { if (restored.current && follow.current) node.scrollTop = node.scrollHeight })
    observer.observe(node)
    if (node.firstElementChild) observer.observe(node.firstElementChild)
    return () => { save(); observer.disconnect() }
    // Each normal-chat MessageList is keyed by its session, so cleanup saves that session's last position.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, sessionKey])
  return { ref, save }
}
