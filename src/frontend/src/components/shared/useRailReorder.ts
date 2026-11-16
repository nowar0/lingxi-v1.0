import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react'

export type RailModule = 'character' | 'schedule' | 'activity'
const defaults: RailModule[] = ['character', 'schedule', 'activity']
const storeKey = 'lingxi-companion-rail:v1'
const ease = 'cubic-bezier(.23,1,.32,1)'
const quietMotion = () => document.documentElement.classList.contains('reduce-motion') || matchMedia('(prefers-reduced-motion: reduce)').matches

function loadOrder(): RailModule[] {
  try {
    const saved = JSON.parse(localStorage.getItem(storeKey) || localStorage.getItem('lingxi-companion-cards:v1') || '{}')
    const known = Array.isArray(saved.order) ? saved.order.map((id: string) => id === 'mood' ? 'character' : id) : []
    return [...new Set<RailModule>([...known.filter((id: RailModule) => defaults.includes(id)), ...defaults])]
  } catch { return [...defaults] }
}

type Drag = { id: RailModule; pointer: number; ghost: HTMLElement; node: HTMLElement; x: number; y: number; left: number; top: number; order: RailModule[]; clientX: number; clientY: number }

export function useRailReorder() {
  const [order, setOrder] = useState(loadOrder)
  const [announcement, announce] = useState('')
  const current = useRef(order)
  const rail = useRef<HTMLElement>(null)
  const nodes = useRef(new Map<RailModule, HTMLDivElement>())
  const moves = useRef(new Map<RailModule, Animation>())
  const before = useRef(new Map<RailModule, number>())
  const drag = useRef<Drag | null>(null)
  const press = useRef<{ timer: number; x: number; y: number; pointer: number } | null>(null)
  const frame = useRef(0)
  const settling = useRef(new Set<HTMLElement>())
  const suppressClick = useRef(0)

  useEffect(() => { try { localStorage.setItem(storeKey, JSON.stringify({ order })) } catch { /* Ordering still works for this window. */ } }, [order])

  const reorder = (next: RailModule[]) => {
    if (next.every((id, index) => id === current.current[index])) return
    before.current = new Map([...nodes.current].map(([id, node]) => [id, node.getBoundingClientRect().top]))
    moves.current.forEach((animation) => animation.cancel())
    moves.current.clear()
    current.current = next
    setOrder(next)
  }
  useLayoutEffect(() => {
    for (const [id, node] of nodes.current) {
      const previous = before.current.get(id)
      const delta = previous === undefined ? 0 : previous - node.getBoundingClientRect().top
      if (quietMotion() || Math.abs(delta) < .5 || drag.current?.id === id) continue
      const animation = node.animate([{ transform: `translateY(${delta}px)` }, { transform: 'translateY(0)' }], { duration: 240, easing: ease })
      moves.current.set(id, animation)
      animation.onfinish = () => { if (moves.current.get(id) === animation) moves.current.delete(id) }
    }
    before.current.clear()
  }, [order])

  const cancelPress = () => { if (press.current) window.clearTimeout(press.current.timer); press.current = null }
  const finish = (cancelled = false) => {
    cancelPress()
    cancelAnimationFrame(frame.current); frame.current = 0
    const active = drag.current
    if (!active) return
    drag.current = null
    try { active.node.releasePointerCapture(active.pointer) } catch { /* The browser may already have released it. */ }
    document.body.classList.remove('companion-rail-dragging')
    active.node.classList.remove('is-rail-dragging')
    if (cancelled) reorder(active.order)
    suppressClick.current = performance.now() + 200
    if (quietMotion() || cancelled) active.ghost.remove()
    else {
      const start = active.ghost.getBoundingClientRect()
      const target = active.node.getBoundingClientRect()
      settling.current.add(active.ghost)
      const animation = active.ghost.animate([
        { transform: `translate3d(${start.left - active.left}px,${start.top - active.top}px,0)`, opacity: 1 },
        { transform: `translate3d(${target.left - active.left}px,${target.top - active.top}px,0)`, opacity: 0 },
      ], { duration: 200, easing: ease })
      animation.onfinish = () => { active.ghost.remove(); settling.current.delete(active.ghost) }
    }
    announce(cancelled ? '已取消换位' : `已移动到第 ${current.current.indexOf(active.id) + 1} 位`)
  }
  const begin = (id: RailModule, pointer: number, x: number, y: number) => {
    const node = nodes.current.get(id)
    if (!node) return
    cancelPress()
    moves.current.get(id)?.cancel()
    const rect = node.getBoundingClientRect()
    const ghost = node.cloneNode(true) as HTMLElement
    ghost.querySelectorAll('[id]').forEach((element) => element.removeAttribute('id'))
    ghost.removeAttribute('id')
    ghost.setAttribute('aria-hidden', 'true'); ghost.inert = true
    ghost.classList.add('companion-rail-ghost')
    Object.assign(ghost.style, { left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px` })
    document.body.append(ghost)
    node.querySelectorAll('.companion-module-body').forEach((body, index) => {
      const copy = ghost.querySelectorAll('.companion-module-body')[index]
      if (copy) copy.scrollTop = body.scrollTop
    })
    node.classList.add('is-rail-dragging')
    document.body.classList.add('companion-rail-dragging')
    node.focus({ preventScroll: true })
    drag.current = { id, pointer, ghost, node, x, y, left: rect.left, top: rect.top, order: [...current.current], clientX: x, clientY: y }
    try { node.setPointerCapture(pointer) } catch { /* Global pointer listeners also keep the drag attached. */ }
    window.getSelection()?.removeAllRanges()
    announce('已拿起模块，拖动换位，按 Escape 取消')
  }
  const update = () => {
    frame.current = 0
    const active = drag.current
    if (!active || !rail.current) return
    const rect = rail.current.getBoundingClientRect()
    const dy = active.clientY - active.y
    const dx = Math.max(-14, Math.min(14, active.clientX - active.x))
    active.ghost.style.transform = `translate3d(${dx}px,${dy}px,0)`
    const rest = current.current.filter((id) => id !== active.id)
    // Use layout positions, excluding a neighbour's in-flight transform, to avoid reorder oscillation.
    const index = rest.filter((id) => {
      const node = nodes.current.get(id)!
      return active.clientY > rect.top + node.offsetTop + node.offsetHeight / 2
    }).length
    const next = [...rest]; next.splice(index, 0, active.id); reorder(next)
  }
  // Listeners delegate to the latest render without detaching during a reorder.
  const actions = useRef({ finish, cancelPress, update })
  actions.current = { finish, cancelPress, update }
  useEffect(() => {
    const move = (event: PointerEvent) => {
      if (press.current?.pointer === event.pointerId && Math.hypot(event.clientX - press.current.x, event.clientY - press.current.y) > 7) actions.current.cancelPress()
      if (drag.current?.pointer !== event.pointerId) return
      event.preventDefault()
      drag.current.clientX = event.clientX; drag.current.clientY = event.clientY
      if (!frame.current) frame.current = requestAnimationFrame(() => actions.current.update())
    }
    const end = (event: PointerEvent) => {
      if (press.current?.pointer === event.pointerId) actions.current.cancelPress()
      if (drag.current?.pointer === event.pointerId) {
        // Consume the final pointer position even when pointerup arrives before the next frame.
        drag.current.clientX = event.clientX; drag.current.clientY = event.clientY
        if (event.type !== 'pointercancel') actions.current.update()
        actions.current.finish(event.type === 'pointercancel')
      }
    }
    const escape = (event: globalThis.KeyboardEvent) => { if (event.key === 'Escape' && (drag.current || press.current)) { event.preventDefault(); actions.current.finish(true) } }
    const cancel = () => actions.current.finish(true)
    window.addEventListener('pointermove', move, { passive: false })
    window.addEventListener('pointerup', end); window.addEventListener('pointercancel', end)
    window.addEventListener('keydown', escape); window.addEventListener('blur', cancel); window.addEventListener('resize', cancel)
    return () => {
      window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', end); window.removeEventListener('pointercancel', end)
      window.removeEventListener('keydown', escape); window.removeEventListener('blur', cancel); window.removeEventListener('resize', cancel)
      actions.current.cancelPress(); cancelAnimationFrame(frame.current)
      drag.current?.ghost.remove(); settling.current.forEach((node) => node.remove())
      moves.current.forEach((animation) => animation.cancel()); document.body.classList.remove('companion-rail-dragging')
    }
  }, [])

  const onPointerDown = (id: RailModule, event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || !event.isPrimary || drag.current) return
    const target = event.target as Element
    if (target.closest('button,a,input,textarea,select,label,img,svg,[contenteditable]')) return
    // Only inspect the hit container: scrolled text clipped behind a header is not a hit.
    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT)
    for (let node; (node = walker.nextNode());) {
      if (!node.textContent?.trim()) continue
      const range = document.createRange(); range.selectNodeContents(node)
      if ([...range.getClientRects()].some((rect) => event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom)) return
    }
    const { pointerId, clientX, clientY } = event
    cancelPress()
    press.current = { pointer: pointerId, x: clientX, y: clientY, timer: window.setTimeout(() => begin(id, pointerId, clientX, clientY), 420) }
  }
  const onKeyDown = (id: RailModule, event: KeyboardEvent) => {
    if (!event.altKey || !['ArrowUp', 'ArrowDown'].includes(event.key) || drag.current) return
    event.preventDefault()
    const index = current.current.indexOf(id)
    const nextIndex = Math.max(0, Math.min(order.length - 1, index + (event.key === 'ArrowUp' ? -1 : 1)))
    const next = [...current.current]; next.splice(index, 1); next.splice(nextIndex, 0, id); reorder(next)
    announce(`已移动到第 ${nextIndex + 1} 位`)
  }
  return { order, rail, nodes, announcement, onPointerDown, onKeyDown, cancelPress, blockClick: () => performance.now() < suppressClick.current }
}
