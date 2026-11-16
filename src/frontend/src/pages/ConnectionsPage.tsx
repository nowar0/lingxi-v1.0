import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ArrowUpRight, Bot, KeyRound, ListChecks, Plug, RefreshCw, Server } from 'lucide-react'
import { useApp } from '@/context/AppContext'
import { api } from '@/api/client'
import { ModelSettings } from '@/components/shared/ModelSettings'
import { KeyLibrary } from '@/components/shared/KeyLibrary'
import { LocalModelsPanel } from '@/components/shared/LocalModelsPanel'
import { SkillsPanel } from '@/components/skills/SkillsPanel'
import { Button } from '@/components/ui/button'
import { ManagementNav, ManagementSurface } from '@/components/shared/ManagementSurface'
type Channel = { id: string; name: string; description: string; state: string }
const sections = [{id:'providers',label:'模型服务',icon:KeyRound},{id:'profiles',label:'模型库',icon:Bot},{id:'assignments',label:'功能分配',icon:ListChecks},{id:'mcp',label:'MCP 服务',icon:Plug},{id:'local',label:'运行服务',icon:Server}]
export function ConnectionsPage() {
  const { connection, health, reconnect } = useApp()
  const [params, setParams] = useSearchParams()
  const requested = params.get('tab') === 'models' ? 'profiles' : params.get('tab')
  const section = sections.some(s => s.id === requested) ? requested! : 'providers'
  const [channels, setChannels] = useState<Channel[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [modelSource, setModelSource] = useState<'cloud' | 'local'>('cloud')
  const status = connection === 'connected' ? '已连接' : connection === 'checking' ? '连接中' : '未连接'
  useEffect(() => { let disposed = false; if (connection !== 'connected') { setChannels([]); return }; void api<{ items: Channel[] }>('/v11/connections').then(value => { if (!disposed) { setChannels(value.items); setError('') } }).catch(reason => { if (!disposed) setError(reason instanceof Error ? reason.message : '连接列表读取失败') }); return () => { disposed = true } }, [connection])
  return <ManagementSurface title="连接中心" className="connection-page" navigation={<ManagementNav label="连接中心分区" items={sections} value={section} onChange={tab => setParams(tab === 'providers' ? {} : { tab })} />} footer={<span className="management-connection-status"><i className={`local-connection-dot is-${connection}`} style={{ background: connection === 'connected' ? '#10b981' : '#ef4444' }} aria-hidden="true" /><span>本地{status}</span></span>} actions={<Button variant="ghost" size="sm" disabled={busy} onClick={() => { setBusy(true); void reconnect().finally(() => setBusy(false)) }}><RefreshCw className={busy ? 'animate-spin' : ''} />检查连接</Button>}>
    <div className="management-view" key={section}>{section === 'providers' ? <KeyLibrary ready={connection === 'connected'} /> : section === 'profiles' ? <div className="model-library-view"><nav className="model-source-tabs" aria-label="模型来源"><button type="button" aria-pressed={modelSource==='cloud'} onClick={()=>setModelSource('cloud')}>云端</button><button type="button" aria-pressed={modelSource==='local'} onClick={()=>setModelSource('local')}>本地</button></nav>{modelSource==='cloud'?<ModelSettings section="profiles" source="cloud" />:<LocalModelsPanel />}</div> : section === 'assignments' ? <ModelSettings section="modules" /> : section === 'mcp' ? <SkillsPanel section="plugins" /> : <>
      <header className="management-section-title"><h2>本地服务</h2></header>
      <section className="management-local-detail" aria-label="本机连接"><div><Server /><div><h3>零汐本地服务</h3><p>{health?.version ? `版本 ${health.version}` : '从零汐启动器连接本地服务'}</p></div><span className="personal-state">{status}</span></div><Link to="/services">查看服务详情<ArrowUpRight size={14} /></Link></section>
      <details className="personal-disclosure connection-future" aria-label="后续连接渠道"><summary>其他渠道</summary><p className="personal-muted">以下渠道暂未接入。</p>{error && <p role="alert" className="personal-error">{error}</p>}{!channels.length && !error && <p className="personal-muted">{connection === 'connected' ? '暂无其他渠道' : '连接本地服务后查看'}</p>}{channels.map(channel => <div className="connection-channel" key={channel.id}><div><h3>{channel.name}</h3><p>{channel.description}</p></div><span className="personal-state">暂未接入</span></div>)}</details>
    </>}</div>
  </ManagementSurface>
}
