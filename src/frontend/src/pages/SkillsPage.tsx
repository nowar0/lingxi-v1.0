import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Activity, Plug, Wrench } from 'lucide-react'
import { api } from '@/api/client'
import { ManagementNav, ManagementSurface } from '@/components/shared/ManagementSurface'
import { SkillsPanel } from '@/components/skills/SkillsPanel'
import { PersonalPluginsPanel } from '@/components/skills/PersonalPluginsPanel'
import { CapabilityStatusPanel } from '@/components/skills/CapabilityStatusPanel'
const sections = [{ id: 'catalog', label: '技能目录', icon: Wrench }, { id: 'plugins', label: '本地插件', icon: Plug }, { id: 'status', label: '功能状态', icon: Activity }]
export function SkillsPage() {
  const [params, setParams] = useSearchParams()
  const section = sections.some(item => item.id === params.get('tab')) ? params.get('tab')! : 'catalog'
  const [suggestedRoots, setSuggestedRoots] = useState<string[]>([])
  useEffect(() => { let disposed = false; void api<{ directories: string[] }>('/v11/plugin-host/sources').then(value => { if (!disposed) setSuggestedRoots(value.directories) }).catch(() => undefined); return () => { disposed = true } }, [])
  return <ManagementSurface title="技能中心" className={`skills-page${section === 'status' ? ' skills-status-page' : ''}`} navigation={<ManagementNav label="技能中心分区" items={sections} value={section} onChange={tab => setParams(tab === 'catalog' ? {} : { tab })} />}>
    <div className="management-view" key={section}>{section === 'status' ? <CapabilityStatusPanel /> : section === 'plugins' ? <PersonalPluginsPanel suggestedRoots={suggestedRoots} /> : <SkillsPanel section="skills" />}</div>
  </ManagementSurface>
}
