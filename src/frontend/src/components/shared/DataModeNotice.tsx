import { FlaskConical, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useApp } from '@/context/AppContext'

export function DataModeNotice() {
  const { connection, reconnect } = useApp()
  if (connection === 'connected') return null
  return <div className="mb-4 flex items-center gap-2 rounded-xl border border-amber-500/15 bg-amber-500/[0.065] px-3 py-2 text-[11px] leading-4 text-amber-700 dark:text-amber-300">
    <FlaskConical className="size-3.5 shrink-0" /><span>{connection === 'checking' ? '正在连接零汐本地服务…' : '本地服务未连接。已显示的内容仅供查看，重新连接后才能提交操作。'}</span>
    <Button variant="ghost" size="xs" className="ml-auto h-6 text-amber-700 hover:bg-amber-500/10 dark:text-amber-300" onClick={() => void reconnect()}><RefreshCw className="size-3" />重新连接</Button>
  </div>
}
