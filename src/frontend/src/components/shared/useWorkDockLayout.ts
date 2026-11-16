import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'
import { useTheme } from '@/context/ThemeContext'
import { DOCK_MAX, DOCK_MIN, releaseDock, stepDockSpring } from '@/lib/workDockMotion'

export function useWorkDockLayout(open: boolean, onOpenChange: (value: boolean) => void) {
  const rootRef = useRef<HTMLDivElement>(null)
  const handleRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(() => {
    try { const n = Number(localStorage.getItem('lingxi-work-tool-width')); if (Number.isFinite(n) && n >= DOCK_MIN && n <= DOCK_MAX) return n } catch { /* Optional layout cache. */ }
    return 390
  })
  const [singlePane, setSinglePane] = useState(false)
  const [maxWidth, setMaxWidth] = useState(DOCK_MAX)
  const [present,setPresent]=useState(open)
  const [resizing, setResizing] = useState(false)
  const { reduceMotion } = useTheme()
  const [systemReduced, setSystemReduced] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  const reduced = useRef(reduceMotion || systemReduced)
  const motion = useRef({ position: open ? width : 0, velocity: 0, target: open ? width : 0 })
  const frame = useRef<number | undefined>(undefined)
  const initialized = useRef(false)
  const drag = useRef<{ id: number; x: number; width: number; lastX: number; lastTime: number; velocity: number } | undefined>(undefined)
  const bound = useRef(maxWidth)
  bound.current = maxWidth
  reduced.current = reduceMotion || systemReduced
  const cancel = useCallback(() => { if (frame.current !== undefined) cancelAnimationFrame(frame.current); frame.current = undefined }, [])
  const paint = useCallback((position: number) => {
    rootRef.current?.style.setProperty('--work-dock-width', `${position.toFixed(2)}px`)
    rootRef.current?.closest<HTMLElement>('.workspace-body')?.style.setProperty('--work-pane-header-width',`${position.toFixed(2)}px`)
    rootRef.current?.style.setProperty('--work-dock-opacity', '1')
    handleRef.current?.setAttribute('aria-valuenow', String(Math.round(position)))
  }, [])
  const animate = useCallback((target: number, velocity?: number) => {
    motion.current.target = target
    if (velocity !== undefined) motion.current.velocity = velocity
    if (reduced.current) { cancel(); motion.current = { position: target, target, velocity: 0 }; paint(target); setPresent(target>0); return }
    if (frame.current !== undefined) return
    let last = performance.now()
    const tick = (now: number) => {
      frame.current = undefined
      const state = motion.current
      const next = stepDockSpring(state.position, state.velocity, state.target, (now - last) / 1000, bound.current)
      last = now; motion.current = { ...next, target: state.target }; paint(next.position)
      if(next.settled&&state.target===0)setPresent(false)
      if (!next.settled) frame.current = requestAnimationFrame(tick)
    }
    frame.current = requestAnimationFrame(tick)
  }, [cancel, paint])
  useLayoutEffect(() => {
    const root = rootRef.current
    if (!root) return
    const measure = () => { setSinglePane(root.clientWidth <= 720); setMaxWidth(Math.max(DOCK_MIN, Math.min(DOCK_MAX, root.clientWidth - 320))) }
    measure(); const observer = new ResizeObserver(measure); observer.observe(root)
    return () => observer.disconnect()
  }, [])
  useLayoutEffect(() => {
    if(open)setPresent(true)
    const target = open ? Math.min(width, maxWidth) : 0
    if (!initialized.current) { initialized.current = true; motion.current = { position: target, target, velocity: 0 }; paint(target) }
    else animate(target)
  }, [open, width, maxWidth, reduceMotion, systemReduced, animate, paint])
  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)')
    const change = () => setSystemReduced(media.matches)
    media.addEventListener('change', change); return () => media.removeEventListener('change', change)
  }, [])
  useEffect(() => cancel, [cancel])
  useEffect(() => { if (!resizing) { try { localStorage.setItem('lingxi-work-tool-width', String(width)) } catch { /* Optional cache. */ } } }, [width, resizing])
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (!event.isPrimary || event.button !== 0 || singlePane) return
    event.preventDefault(); cancel()
    drag.current = { id: event.pointerId, x: event.clientX, width: motion.current.position, lastX: event.clientX, lastTime: performance.now(), velocity: 0 }
    event.currentTarget.setPointerCapture(event.pointerId); setResizing(true)
  }
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const start = drag.current
    if (!start || start.id !== event.pointerId) return
    const now = performance.now(), elapsed = now - start.lastTime
    if (elapsed > 0) start.velocity = start.velocity * .35 + Math.max(-1400, Math.min(1400, (start.lastX - event.clientX) / elapsed * 1000)) * .65
    start.lastX = event.clientX; start.lastTime = now
    motion.current.position = Math.max(0, Math.min(maxWidth, start.width + start.x - event.clientX))
    paint(motion.current.position)
  }
  const stop = (event: PointerEvent<HTMLDivElement>) => {
    const start = drag.current
    if (!start || start.id !== event.pointerId) return
    const velocity = event.type === 'pointerup' && performance.now() - start.lastTime < 70 ? start.velocity : 0
    const next = releaseDock(motion.current.position, velocity, width, maxWidth)
    drag.current = undefined; setResizing(false); setWidth(next.width); onOpenChange(next.open)
    animate(next.open ? next.width : 0, velocity)
    if(handleRef.current?.hasPointerCapture(event.pointerId))handleRef.current.releasePointerCapture(event.pointerId)
  }
  useEffect(()=>{if(!resizing)return;const move=(event:globalThis.PointerEvent)=>onPointerMove(event as unknown as PointerEvent<HTMLDivElement>);const end=(event:globalThis.PointerEvent)=>stop(event as unknown as PointerEvent<HTMLDivElement>);document.addEventListener('pointermove',move,true);document.addEventListener('pointerup',end,true);document.addEventListener('pointercancel',end,true);return()=>{document.removeEventListener('pointermove',move,true);document.removeEventListener('pointerup',end,true);document.removeEventListener('pointercancel',end,true)}},[resizing,width,maxWidth,animate,onOpenChange])
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', ' '].includes(event.key)) return
    event.preventDefault()
    if (event.key === 'Enter' || event.key === ' ') onOpenChange(!open)
    else if (event.key === 'Home') onOpenChange(false)
    else if (event.key === 'End') { setWidth(maxWidth); onOpenChange(true) }
    else if (event.key === 'ArrowLeft') { setWidth(Math.min(maxWidth, width + 24)); onOpenChange(true) }
    else if (width <= DOCK_MIN) onOpenChange(false)
    else setWidth(Math.max(DOCK_MIN, width - 24))
  }
  return { rootRef, singlePane, resizing, present, width, maxWidth, handleProps: { ref: handleRef, onPointerDown, onPointerMove, onPointerUp: stop, onPointerCancel: stop, onLostPointerCapture: stop, onKeyDown } }
}
