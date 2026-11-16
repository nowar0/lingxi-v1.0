export const SIDEBAR_ICON_WIDTH = 64
export const SIDEBAR_MIN_WIDTH = 216
export const SIDEBAR_MAX_WIDTH = 320
export const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value))
export const clampWidth = (width: number) => clamp(width, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH)

export function sidebarProgress(width: number) {
  const progress = clamp((width - 96) / (SIDEBAR_MIN_WIDTH - 96), 0, 1)
  return progress * progress * (3 - 2 * progress)
}

export function releaseLayout(width: number, velocity: number, previousWidth: number) {
  const projected = width + clamp(velocity * 0.06, -28, 28)
  return projected < (SIDEBAR_ICON_WIDTH + SIDEBAR_MIN_WIDTH) / 2
    ? { width: previousWidth, collapsed: true }
    : { width: clampWidth(width), collapsed: false }
}

export function stepSidebarSpring(position: number, velocity: number, target: number, elapsed: number) {
  // Response ≈ 0.3 s, damping ratio ≈ 0.83: one restrained, momentum-driven settle.
  const duration = Math.min(elapsed, 0.04)
  const steps = Math.max(1, Math.ceil(duration / (1 / 240)))
  const dt = duration / steps
  for (let i = 0; i < steps; i++) {
    velocity += ((target - position) * 420 - velocity * 34) * dt
    position += velocity * dt
    if (position < SIDEBAR_ICON_WIDTH) { position = SIDEBAR_ICON_WIDTH; velocity = Math.max(0, velocity) }
    if (position > SIDEBAR_MAX_WIDTH) { position = SIDEBAR_MAX_WIDTH; velocity = Math.min(0, velocity) }
  }
  const settled = Math.abs(target - position) < 0.08 && Math.abs(velocity) < 2
  return { position: settled ? target : position, velocity: settled ? 0 : velocity, settled }
}
