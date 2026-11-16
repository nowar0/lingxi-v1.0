import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'
import { useTheme } from '@/context/ThemeContext'
import { clamp, clampWidth, releaseLayout, sidebarProgress, SIDEBAR_ICON_WIDTH, SIDEBAR_MAX_WIDTH, SIDEBAR_MIN_WIDTH, stepSidebarSpring } from './sidebarMotion'
export { SIDEBAR_ICON_WIDTH, SIDEBAR_MAX_WIDTH } from './sidebarMotion'

const STORAGE_KEY = 'lingxi-sidebar-layout-v1'
function initialLayout() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')
    if (saved && typeof saved.width === 'number' && Number.isFinite(saved.width) && typeof saved.collapsed === 'boolean') {
      return { width: clampWidth(saved.width), collapsed: saved.collapsed }
    }
  } catch { /* 外观缓存不可用时仍可拖动。 */ }
  return { width: SIDEBAR_MIN_WIDTH, collapsed: window.innerWidth < 600 }
}

export function useSidebarLayout() {
  const [layout, setLayout] = useState(initialLayout)
  const [compactWindow,setCompactWindow]=useState(()=>window.innerWidth<1000)
  const [compactExpanded,setCompactExpanded]=useState(false)
  useEffect(()=>{const resize=()=>{setCompactWindow(window.innerWidth<1000);setCompactExpanded(false)};const toggle=()=>setCompactExpanded(v=>!v);window.addEventListener('resize',resize);window.addEventListener('lingxi-toggle-nav',toggle);return()=>{window.removeEventListener('resize',resize);window.removeEventListener('lingxi-toggle-nav',toggle)}},[])
  const effectiveCollapsed=layout.collapsed || (compactWindow && !compactExpanded)
  const [resizing, setResizing] = useState(false)
  const { reduceMotion } = useTheme()
  const [systemReduceMotion, setSystemReduceMotion] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  const reduced = useRef(reduceMotion || systemReduceMotion)
  const sidebarRef = useRef<HTMLElement>(null)
  const handleRef = useRef<HTMLDivElement>(null)
  const width = effectiveCollapsed ? SIDEBAR_ICON_WIDTH : layout.width
  const motion = useRef({ position: width, target: width, velocity: 0 })
  const frame = useRef<number | null>(null)
  const initialized = useRef(false)
  const inertia = useRef(0)
  const drag = useRef<{ pointerId: number; x: number; width: number; lastX: number; lastTime: number; velocity: number } | null>(null)

  const cancelFrame = useCallback(() => {
    if (frame.current !== null) cancelAnimationFrame(frame.current)
    frame.current = null
  }, [])
  const paint = useCallback((position: number, velocity: number, resistance = 0, elapsed?: number) => {
    const element = sidebarRef.current
    if (!element) return
    element.style.setProperty('--sidebar-width', `${position.toFixed(3)}px`)
    element.style.setProperty('--sidebar-progress', sidebarProgress(position).toFixed(5))
    const targetInertia = reduced.current ? 0 : clamp(-velocity * 0.0025 + resistance, -4, 4)
    inertia.current = elapsed === undefined ? targetInertia : inertia.current + (targetInertia - inertia.current) * (1 - Math.exp(-26 * elapsed))
    if (Math.abs(inertia.current - targetInertia) < 0.01) inertia.current = targetInertia
    element.style.setProperty('--sidebar-inertia', `${inertia.current.toFixed(3)}px`)
    handleRef.current?.setAttribute('aria-valuenow', String(Math.round(position)))
  }, [])
  const animateTo = useCallback((target: number, releaseVelocity?: number) => {
    motion.current.target = target
    if (releaseVelocity !== undefined) motion.current.velocity = clamp(releaseVelocity, -1000, 1000)
    if (reduced.current) {
      cancelFrame()
      motion.current = { position: target, target, velocity: 0 }
      paint(target, 0)
      return
    }
    if (frame.current !== null) return
    let lastTime = performance.now()
    const tick = (time: number) => {
      frame.current = null
      const state = motion.current
      const elapsed = Math.min((time - lastTime) / 1000, 0.04)
      const next = stepSidebarSpring(state.position, state.velocity, state.target, elapsed)
      lastTime = time
      motion.current = { position: next.position, target: state.target, velocity: next.velocity }
      paint(next.position, next.velocity, 0, elapsed)
      if (!next.settled || Math.abs(inertia.current) > 0.01) frame.current = requestAnimationFrame(tick)
    }
    frame.current = requestAnimationFrame(tick)
  }, [cancelFrame, paint])

  useLayoutEffect(() => {
    if (!initialized.current) {
      initialized.current = true
      motion.current = { position: width, target: width, velocity: 0 }
      paint(width, 0)
    } else animateTo(width)
  }, [width, animateTo, paint])
  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)')
    const change = () => setSystemReduceMotion(media.matches)
    media.addEventListener('change', change)
    return () => media.removeEventListener('change', change)
  }, [])
  useLayoutEffect(() => {
    reduced.current = reduceMotion || systemReduceMotion
    if (reduced.current && !drag.current) animateTo(motion.current.target, 0)
  }, [reduceMotion, systemReduceMotion, animateTo])
  useEffect(() => cancelFrame, [cancelFrame])
  useEffect(() => {
    if (resizing) return
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(layout)) } catch { /* 不影响当前窗口。 */ }
  }, [layout, resizing])

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (!event.isPrimary || event.button !== 0) return
    event.preventDefault()
    cancelFrame()
    const position = motion.current.position
    drag.current = { pointerId: event.pointerId, x: event.clientX, width: position, lastX: event.clientX, lastTime: performance.now(), velocity: 0 }
    motion.current.velocity = 0
    event.currentTarget.setPointerCapture(event.pointerId)
    setResizing(true)
  }
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const start = drag.current
    if (!start || start.pointerId !== event.pointerId) return
    const now = performance.now()
    const elapsed = now - start.lastTime
    if (elapsed > 0) {
      const velocity = clamp((event.clientX - start.lastX) / elapsed * 1000, -1400, 1400)
      start.velocity = start.velocity * 0.35 + velocity * 0.65
    }
    start.lastX = event.clientX
    start.lastTime = now
    const proposed = start.width + event.clientX - start.x
    const position = clamp(proposed, SIDEBAR_ICON_WIDTH, SIDEBAR_MAX_WIDTH)
    const overshoot = proposed - position
    const resistance = overshoot / (1 + Math.abs(overshoot) / 24) * 0.15
    motion.current.position = position
    motion.current.velocity = start.velocity
    paint(position, start.velocity, resistance)
  }
  const stopResize = (event: PointerEvent<HTMLDivElement>) => {
    const start = drag.current
    if (!start || start.pointerId !== event.pointerId) return
    const velocity = event.type === 'pointerup' && performance.now() - start.lastTime < 70 ? start.velocity : 0
    const next = releaseLayout(motion.current.position, velocity, layout.width)
    drag.current = null
    setResizing(false)
    setLayout(next)
    animateTo(next.collapsed ? SIDEBAR_ICON_WIDTH : next.width, velocity)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', ' '].includes(event.key)) return
    event.preventDefault()
    setLayout(current => {
      if (event.key === 'Enter' || event.key === ' ') return { ...current, collapsed: !current.collapsed }
      if (event.key === 'Home') return { ...current, collapsed: true }
      if (event.key === 'End') return { width: SIDEBAR_MAX_WIDTH, collapsed: false }
      if (event.key === 'ArrowRight') return { width: current.collapsed ? current.width : clampWidth(current.width + 16), collapsed: false }
      return current.collapsed || current.width <= SIDEBAR_MIN_WIDTH
        ? { ...current, collapsed: true }
        : { width: clampWidth(current.width - 16), collapsed: false }
    })
  }

  return {
    sidebarRef, width, collapsed: effectiveCollapsed, resizing,
    resizeHandleProps: { ref: handleRef, onPointerDown, onPointerMove, onPointerUp: stopResize, onPointerCancel: stopResize, onLostPointerCapture: stopResize, onKeyDown },
  }
}
