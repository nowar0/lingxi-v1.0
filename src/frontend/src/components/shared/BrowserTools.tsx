import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { api, json } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/dialog'

export function BrowserTools({ open, onOpenChange, embedded = false, enabled = true }: { open: boolean; onOpenChange: (value: boolean) => void; embedded?: boolean; enabled?: boolean }) {
  const [url, setUrl] = useState('')
  const [status, setStatus] = useState<{ installed: boolean; running: boolean; note: string }>()
  const [result, setResult] = useState<{ title?: string; url?: string; text?: string; closed?: boolean }>()
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (open && enabled) void api<NonNullable<typeof status>>('/v6/browser/status').then(setStatus).catch((error) => toast.error('浏览器状态读取失败', { description: (error as Error).message })) }, [open, enabled])
  const operate = async (action: string) => {
    if (busy || !enabled) return
    setBusy(true)
    try { setResult(await api('/v6/browser/operate', json('POST', { action, url: action === 'open' ? url : '' }))); setStatus(await api('/v6/browser/status')) }
    catch (error) { toast.error('浏览器操作未完成', { description: (error as Error).message }) }
    finally { setBusy(false) }
  }
  const content = <><label className="text-xs">网页地址</label><Input aria-label="专用浏览器地址" placeholder="https://example.com" value={url} onChange={(event) => setUrl(event.target.value)} /><div className="flex flex-wrap gap-2"><Button size="sm" disabled={!enabled || busy || !url.trim() || !status?.installed} onClick={() => void operate('open')}>打开网页</Button><Button size="sm" variant="outline" disabled={!enabled || busy || !status?.running} onClick={() => void operate('read')}>读取当前页面</Button><Button size="sm" variant="outline" disabled={!enabled || busy || !status?.running} onClick={() => void operate('play')}>播放</Button><Button size="sm" variant="outline" disabled={!enabled || busy || !status?.running} onClick={() => void operate('pause')}>暂停</Button><Button size="sm" variant="ghost" disabled={!enabled || busy || !status?.running} onClick={() => void operate('close')}>关闭专用浏览器</Button></div><p className="text-xs text-muted-foreground">{!enabled ? '连接本地服务后可用' : !status ? '正在读取浏览器状态' : status.installed ? status.running ? '专用浏览器运行中' : '尚未打开专用浏览器' : '未检测到 Microsoft Edge'}</p>{result && <div className="workspace-browser-result space-y-2 overflow-auto text-xs"><h3 className="font-semibold">{result.closed ? '浏览器已关闭' : result.title}</h3><p className="break-all text-muted-foreground">{result.url}</p><pre className="whitespace-pre-wrap break-words">{result.text}</pre></div>}</>
  if (embedded) return <section className="workspace-browser"><div className="workspace-panel-heading"><h2>浏览器</h2></div><p className="workspace-browser-description">{status?.note || '使用独立的浏览器资料。'}</p>{content}</section>
  return <Dialog open={open} onOpenChange={(value) => { if (!busy) onOpenChange(value) }}><DialogContent className="max-w-2xl"><DialogHeader><DialogTitle>零汐专用浏览器</DialogTitle><DialogDescription>{status?.note || '使用独立的浏览器资料，不连接个人浏览器。'}</DialogDescription></DialogHeader>{content}</DialogContent></Dialog>
}
