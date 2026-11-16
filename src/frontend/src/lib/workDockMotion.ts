export const DOCK_MIN = 320
export const DOCK_MAX = 760
export function revealDockPane(side: 'left' | 'right', singlePane: boolean, current: { open: boolean; expanded: boolean }) {
  return { open: side === 'right' ? true : singlePane ? false : current.open, expanded: side === 'left' ? false : current.expanded }
}
const clamp = (n: number, min: number, max: number) => Math.max(min, Math.min(max, n))
export function releaseDock(width: number, velocity: number, previous: number, max = DOCK_MAX, min = DOCK_MIN) {
  const projected = width + clamp(velocity * .06, -28, 28)
  return projected < min / 2 ? { width: previous, open: false } : { width: clamp(width, Math.min(min, max), max), open: true }
}
// Same response and damping as the main navigation; right dock has a zero-width stop.
export function stepDockSpring(position: number, velocity: number, target: number, elapsed: number, max = DOCK_MAX) {
  const duration = Math.min(Math.max(elapsed, 0), .04)
  const steps = Math.max(1, Math.ceil(duration / (1 / 240))), dt = duration / steps
  for (let i = 0; i < steps; i++) {
    velocity += ((target - position) * 420 - velocity * 34) * dt
    position += velocity * dt
    if (position < 0) { position = 0; velocity = Math.max(0, velocity) }
    if (position > max) { position = max; velocity = Math.min(0, velocity) }
  }
  const settled = Math.abs(target - position) < .08 && Math.abs(velocity) < 2
  return { position: settled ? target : position, velocity: settled ? 0 : velocity, settled }
}
