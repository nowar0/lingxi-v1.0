import { useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTheme } from '@/context/ThemeContext'

export function WorkToolFrame({ tab, sessionId, jobId, onClose }: { tab: string; sessionId: number; jobId?: string; onClose: () => void }) {
  const frame = useRef<HTMLIFrameElement>(null)
  const theme = useTheme()
  const navigate = useNavigate()
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
      if (event.data?.type === 'lingxi-tools-open' && event.data.tab === 'appearance') navigate('/settings')
    }
    addEventListener('message', receive)
    return () => removeEventListener('message', receive)
  }, [navigate, onClose])
  const title = tab === 'git' ? 'Git 与 Worktree' : tab === 'ssh' ? 'SSH 与 SFTP' : '预算与验收'
  return <iframe ref={frame} title={title} onLoad={syncTheme} className="workspace-tool-frame" src={`/ui/workbench-tools.html?tab=${tab}&session_id=${sessionId}${jobId ? `&job_id=${encodeURIComponent(jobId)}` : ''}`} />
}
