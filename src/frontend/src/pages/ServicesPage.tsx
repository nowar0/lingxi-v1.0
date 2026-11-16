import { BrowserTools } from '@/components/shared/BrowserTools'
import { WorkbenchDialog } from '@/components/shared/WorkbenchDialog'
import { api } from '@/api/client'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useEffect, useState } from 'react'
import { AppWindow, Monitor, Cable, CircleEllipsis, FolderGit2, RefreshCw, Server, ShieldCheck, SquareTerminal } from 'lucide-react'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
import { lingxi } from '@/api/lingxi'
import { PageHeader } from '@/components/shared/PageHeader'
import { DataModeNotice } from '@/components/shared/DataModeNotice'
import { StatusPill } from '@/components/shared/StatusPill'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'

export function ServicesPage() {
  const { connection, health, activeWorkSessionId } = useApp()
  const [extension, setExtension] = useState<{ ready: boolean; version: string; executables: Record<string, boolean> }>()
  const [tools, setTools] = useState<string>()
  const [browserOpen, setBrowserOpen] = useState(false)
  const [localOpen, setLocalOpen] = useState(false)
  const [browser, setBrowser] = useState<{ installed: boolean; running: boolean }>()
  const refresh = async () => { if (connection === 'connected') { const [extension, browser] = await Promise.all([lingxi.extensionStatus(), api<{ installed: boolean; running: boolean }>('/v6/browser/status')]); setExtension(extension); setBrowser(browser) } }
  useEffect(() => { void refresh().catch((error) => toast.error('服务状态读取失败', { description: (error as Error).message })) }, [connection])
  const services = [
    { id: 'local', name: '零汐本地服务', desc: health ? `${health.name} · v${health.version}` : 'FastAPI 与本地数据服务', icon: Server, status: connection === 'connected' ? '运行中' : '未连接', ok: connection === 'connected' },
    { id: 'git', name: 'Git / Worktree', desc: extension?.executables?.git ? '提交、差异、分支与工作树' : '等待检测本机 Git', icon: FolderGit2, status: extension?.executables?.git ? '可用' : '待检测', ok: Boolean(extension?.executables?.git) },
    { id: 'ssh', name: 'SSH / SFTP', desc: '主机、终端、隧道与远程文件', icon: SquareTerminal, status: extension?.executables?.ssh ? '可用' : '待检测', ok: Boolean(extension?.executables?.ssh) },
    { id: 'browser', name: '专用浏览器', desc: '网页读取与媒体控制；个人浏览器需另配 MCP', icon: Monitor, status: browser?.running ? '运行中' : browser?.installed ? '可打开' : '未检测到 Edge', ok: Boolean(browser?.installed) },
    { id: 'connection', name: '外部连接', desc: '渠道与消息归属尚未确定', icon: Cable, status: '预留', ok: false },
    { id: 'pet', name: '桌宠', desc: '与主界面共用陪伴会话和记忆', icon: AppWindow, status: '预留', ok: false },
  ]
  return <div className="page-container chrome-page"><DataModeNotice /><PageHeader title="服务总览" description="查看本地服务、设备与连接的实际状态。" actions={<Button variant="outline" onClick={() => void refresh().then(() => toast.success('状态已刷新')).catch((error) => toast.error('状态刷新失败', { description: (error as Error).message }))}><RefreshCw />刷新状态</Button>} />
      <div className="service-grid">{services.map((service) => <Card key={service.id} className="service-card"><div className="flex items-start justify-between"><div className="service-icon"><service.icon /></div><StatusPill label={service.status} tone={service.ok ? 'success' : service.status === '预留' ? 'neutral' : 'warning'} /></div><h3>{service.name}</h3><p>{service.desc}</p><Button variant="ghost" size="sm" className="mt-3 -ml-2" aria-label={`查看${service.name}详情`} onClick={() => { if (service.id === 'git') setTools('git'); else if (service.id === 'ssh') setTools('ssh'); else if (service.id === 'browser') setBrowserOpen(true); else if (service.id === 'local') setLocalOpen(true); else toast.info(service.name, { description: '按既定计划作为后续扩展，本轮不标记为已实现。' }) }}>查看详情<CircleEllipsis /></Button></Card>)}</div><Card className="mt-4 p-5"><div className="flex items-center gap-3"><ShieldCheck className="size-5 text-primary" /><div><h3 className="text-sm font-semibold">本机安全边界</h3><p className="mt-1 text-xs text-muted-foreground">仅监听 127.0.0.1；写请求执行同源检查；凭据由系统凭据管理器保存。</p></div><Badge variant="success" className="ml-auto">本地优先</Badge></div></Card>

    <WorkbenchDialog tab={tools} sessionId={activeWorkSessionId} onClose={() => setTools(undefined)} /><BrowserTools open={browserOpen} onOpenChange={setBrowserOpen} /><Dialog open={localOpen} onOpenChange={setLocalOpen}><DialogContent><DialogHeader><DialogTitle>零汐本地服务</DialogTitle></DialogHeader><div className="space-y-2 text-xs"><p>状态：{connection === 'connected' ? '运行中' : '未连接'}</p><p>版本：{health?.version || '未读取'}</p><p>监听范围：127.0.0.1 本机回环</p><p>窗口退出时结束内部服务；个人数据独立保存。</p></div></DialogContent></Dialog>
  </div>
}
