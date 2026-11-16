import { PanelLeft } from 'lucide-react'
import { useLocation } from 'react-router-dom'

const titles: Record<string, [string, string]> = {
  '/': ['首页', '你的本地数字伙伴'], '/companion': ['消息', '与你相处·日常对话与陪伴'],
  '/work': ['工作空间', '独立任务上下文·理性协作'], '/skills': ['技能中心', '工作技能、插件与 MCP'],
  '/tasks': ['任务与执行', '目标、授权与执行记录'], '/memory': ['记忆', '你允许记住的事'],
  '/services': ['服务总览', '本地设备、服务与连接状态'], '/connections': ['连接中心', '本机服务、MCP 与后续渠道'], '/settings': ['设置', '外观、模型与数据管理'],
}

export function Topbar() {
  const location = useLocation()
  const [title, description] = titles[location.pathname] || titles['/']
  return <header className="app-topbar"><button className="compact-nav-toggle" aria-label="展开或收起导航" onClick={()=>window.dispatchEvent(new Event('lingxi-toggle-nav'))}><PanelLeft size={18}/></button>
    <div className="topbar-context"><span className="topbar-title">{title}</span><span className="topbar-divider" aria-hidden="true">/</span><span className="topbar-description">{description}</span></div>
  </header>
}
