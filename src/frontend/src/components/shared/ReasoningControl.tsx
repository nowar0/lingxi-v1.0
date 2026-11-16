import { useEffect, useState, type CSSProperties } from 'react'
import { useTheme } from '@/context/ThemeContext'
import { availableEfforts, effortLabels, effortLevels, type Effort, type ReasoningCapability } from '@/lib/workComposerSettings'
import './work-composer.css'

export type EffortPreferences = { motion: 'fast' | 'quick' | 'full'; reachedTicks: 'highlight' | 'dim' | 'hidden' }
export const defaultEffortPreferences: EffortPreferences = { motion: 'full', reachedTicks: 'hidden' }
const storageKey = 'lingxi-effort-slider-v1'
const changedEvent = 'lingxi-effort-slider:settings'
export function readEffortPreferences(): EffortPreferences {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || '{}')
    return { motion: ['fast', 'quick', 'full'].includes(value.motion) ? value.motion : 'full', reachedTicks: ['highlight', 'dim', 'hidden'].includes(value.reachedTicks) ? value.reachedTicks : 'hidden' }
  } catch { return defaultEffortPreferences }
}
export function saveEffortPreferences(value: EffortPreferences) {
  localStorage.setItem(storageKey, JSON.stringify(value))
  window.dispatchEvent(new Event(changedEvent))
}
export function useEffortPreferences() {
  const [preferences, setPreferences] = useState(readEffortPreferences)
  useEffect(() => {
    const update = () => setPreferences(readEffortPreferences())
    window.addEventListener(changedEvent, update); window.addEventListener('storage', update)
    return () => { window.removeEventListener(changedEvent, update); window.removeEventListener('storage', update) }
  }, [])
  return preferences
}

// React adaptation of the existing MIT dsh-codex-effort-slider visual reference.
// The backend supplies a model-specific range; no fixed six-position assumption.
export function ReasoningControl({ value, capability, disabled = false, onCommit }: { value: Effort; capability?: ReasoningCapability; disabled?: boolean; onCommit: (value: Effort) => void }) {
  const levels = availableEfforts(capability)
  const [index, setIndex] = useState(Math.max(0, levels.indexOf(value)))
  const signature = levels.join(',')
  const { reduceMotion } = useTheme()
  const preferences = useEffortPreferences()
  useEffect(() => { if (!disabled) setIndex(Math.max(0, levels.indexOf(value))) }, [value, signature, disabled])
  const safeIndex = Math.min(index, levels.length - 1)
  const pct = levels.length > 1 ? safeIndex / (levels.length - 1) : 0
  const locked = disabled || levels.length < 2
  const label = (level: Effort) => effortLabels[effortLevels.indexOf(level)]
  const commit = (next: number) => { if (!locked && levels[next] && levels[next] !== value) onCommit(levels[next]) }
  return <div className="reasoning-control">
    <div className="work-model-effort-caption"><span>推理强度</span><strong>{label(levels[safeIndex])}</strong></div>
    <div className="work-effort" data-level={safeIndex} data-reduce-motion={reduceMotion} data-motion={preferences.motion} data-reached-ticks={preferences.reachedTicks}>
      <div className="work-effort-track" style={{ '--effort-offset': `calc(14px + ${pct} * (100% - 28px))`, '--effort-energy': pct } as CSSProperties}>
        <div className="work-effort-fill" aria-hidden="true"><div className="work-effort-stars">{Array.from({ length: pct > .5 ? Math.round(pct * 18) : 0 }, (_, i) => <i key={i} style={{ top: `${12 + i * 37 % 76}%`, animationDelay: `${-i * .193}s`, animationDuration: `${2.7 - pct + i % 3 * .12}s` }} />)}</div></div>
        {levels.map((level, i) => <i key={level} className="work-effort-tick" data-reached={i <= safeIndex} style={{ left: `calc(14px + ${levels.length > 1 ? i / (levels.length - 1) : 0} * (100% - 28px))` }} aria-hidden="true" />)}
        <div className="work-effort-knob" aria-hidden="true" />
        <input aria-label="推理强度" aria-valuetext={label(levels[safeIndex])} type="range" min={0} max={Math.max(1, levels.length - 1)} step={1} value={safeIndex} disabled={locked}
          onChange={(event) => setIndex(Number(event.target.value))}
          onPointerUp={(event) => commit(Number(event.currentTarget.value))}
          onKeyUp={(event) => { if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) commit(Number(event.currentTarget.value)) }}
          onBlur={(event) => commit(Number(event.currentTarget.value))} />
      </div>
      <div className="work-effort-labels">{levels.map((level, i) => <button type="button" key={level} disabled={locked} aria-pressed={safeIndex === i} onClick={() => { setIndex(i); commit(i) }}>{label(level)}</button>)}</div>
    </div>
    <p className="work-model-note">{capability?.note || '正在读取模型支持的档位…'}</p>
  </div>
}
