import { useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTheme } from '@/context/ThemeContext'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'

export function WorkbenchDialog({ tab, sessionId, jobId, onClose }: { tab?: string; sessionId?: number; jobId?: string; onClose: () => void }) {
  const frame = useRef<HTMLIFrameElement>(null)
  const navigate = useNavigate()
  const theme = useTheme()
  const syncTheme = () => {
    const styles = getComputedStyle(frame.current?.closest(".workspace-page") || document.documentElement)
    const mapping = { paper: '--card', ink: '--foreground', muted: '--muted-foreground', line: '--border', soft: '--secondary', accent: '--primary' }
    const colors = Object.fromEntries(Object.entries(mapping).map(([key, variable]) => [key, styles.getPropertyValue(variable).trim()]))
    frame.current?.contentWindow?.postMessage({ type: 'lingxi-tools-theme', colors, dark: document.documentElement.classList.contains('dark') }, location.origin)
  }
  useEffect(() => { syncTheme() }, [theme])
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if (event.origin !== location.origin || event.source !== frame.current?.contentWindow) return
      if (event.data?.type === 'lingxi-tools-close') onClose()
      if (event.data?.type === 'lingxi-tools-open' && event.data.tab === 'appearance') { onClose(); navigate('/settings') }
    }
    addEventListener('message', receive)
    return () => removeEventListener('message', receive)
  }, [navigate, onClose])
  return <Dialog open={Boolean(tab)} onOpenChange={(open) => { if (!open) onClose() }}><DialogContent className="flex h-[85vh] max-w-[min(1200px,calc(100%-32px))] flex-col p-4"><DialogHeader><DialogTitle>工作工具</DialogTitle></DialogHeader>{tab && <iframe ref={frame} title="Git、SSH 与预算验收工作工具" onLoad={syncTheme} className="min-h-0 w-full flex-1 border-0" src={`/ui/workbench-tools.html?tab=${encodeURIComponent(tab)}&session_id=${sessionId || ''}${jobId ? `&job_id=${encodeURIComponent(jobId)}` : ''}`} />}</DialogContent></Dialog>
}
