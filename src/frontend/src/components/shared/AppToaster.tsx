import { useEffect, useRef } from 'react'
import { Toaster, toast, useSonner, type ToastT } from 'sonner'

const DEFAULT_DURATION = 3000
type ToastTimer = { handle: ReturnType<typeof setTimeout>; toast: ToastT }

export function AppToaster() {
  const { toasts } = useSonner()
  const timers = useRef(new Map<ToastT['id'], ToastTimer>())

  useEffect(() => {
    const activeIds = new Set(toasts.map((item) => item.id))
    for (const [id, timer] of timers.current) {
      if (!activeIds.has(id)) {
        clearTimeout(timer.handle)
        timers.current.delete(id)
      }
    }
    for (const item of toasts) {
      const existing = timers.current.get(item.id)
      const duration = item.duration ?? DEFAULT_DURATION
      if (item.type === 'loading' || duration === Infinity) {
        if (existing) clearTimeout(existing.handle)
        timers.current.delete(item.id)
        continue
      }
      if (existing) {
        existing.toast = item
        continue
      }
      // Each toast keeps its own deadline; new toasts and hover never reset it.
      const timer: ToastTimer = {
        toast: item,
        handle: setTimeout(() => {
          timers.current.delete(item.id)
          toast.dismiss(item.id)
          timer.toast.onAutoClose?.(timer.toast)
        }, Math.max(0, duration)),
      }
      timers.current.set(item.id, timer)
    }
  }, [toasts])

  useEffect(() => () => {
    for (const timer of timers.current.values()) clearTimeout(timer.handle)
    timers.current.clear()
  }, [])

  return <Toaster position="bottom-right" duration={Infinity} richColors closeButton toastOptions={{ className: 'lingxi-toast' }} />
}
