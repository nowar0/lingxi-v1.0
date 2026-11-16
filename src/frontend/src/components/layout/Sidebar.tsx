import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { BookOpen, Blocks, PanelsTopLeft, PanelLeft, Home, MessageCircleMore, ListChecks, Cable } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Tooltip } from '@/components/ui/tooltip'
import { useApp } from '@/context/AppContext'
import { SIDEBAR_ICON_WIDTH, SIDEBAR_MAX_WIDTH, useSidebarLayout } from './useSidebarLayout'
import { AppMenu } from './AppMenu'


const groups = [
 {label:'日常使用',items:[{to:'/',label:'首页',icon:Home},{to:'/companion',label:'消息',icon:MessageCircleMore}]},
 {label:'工作与管理',items:[{to:'/work',label:'工作',icon:PanelsTopLeft},{to:'/tasks',label:'任务',icon:ListChecks,badge:true},{to:'/memory',label:'记忆',icon:BookOpen},{to:'/skills',label:'技能中心',icon:Blocks},{to:'/connections',label:'连接中心',icon:Cable}]},
]

export function Sidebar({ searchOpen, onOpenSearch }: { searchOpen: boolean; onOpenSearch: () => void }) {
  const { sidebarRef, width, collapsed, resizing, resizeHandleProps } = useSidebarLayout()
  const { tasks } = useApp()
  const navigate = useNavigate()
  const location = useLocation()
  const running = tasks.filter((task) => ['running', 'generating'].includes(task.status)).length
  return <aside ref={sidebarRef} className={cn('app-sidebar', collapsed && 'is-collapsed', resizing && 'is-resizing')}>
    <div className="sidebar-inner">
    <div className="sidebar-header flex h-[68px] shrink-0 items-center gap-1 px-3">
      <button className="sidebar-brand flex min-w-0 flex-1 items-center gap-3 text-left" onClick={() => navigate('/')}>
        <img src="./assets/lingxi-brand.svg" alt="零汐" className="h-8 w-8 shrink-0 object-contain" />
        <div className="sidebar-label min-w-0"><div className="text-[17px] font-semibold tracking-[0.18em]">零汐</div></div>
      </button>
      <button type="button" className="sidebar-compact-toggle" aria-label="展开或收起导航" onClick={()=>window.dispatchEvent(new Event('lingxi-toggle-nav'))}><PanelLeft size={17}/></button>
    </div>
    <nav id="lingxi-main-navigation" className="flex-1 overflow-y-auto px-3 pb-3" aria-label="主要导航">
      {groups.map((group) => <div className="mb-4" key={group.label}>
        {group.label && <div className="sidebar-group-label px-3 text-[10px] font-medium tracking-[0.12em] text-muted-foreground/70">{group.label}</div>}
        <div className="space-y-1">{group.items.map((item) => <Tooltip key={item.to} content={collapsed ? item.label : ''} side="right"><NavLink to={item.to} aria-label={item.label} end={item.to === '/'} className={cn('nav-item', location.pathname === item.to && 'is-active')}>
          <item.icon className="size-[17px] shrink-0" strokeWidth={1.8} /><span className="sidebar-nav-label whitespace-nowrap">{item.label}</span>
          {item.badge && running > 0 && <span className="sidebar-nav-badge rounded-full bg-primary/10 px-1.5 py-0.5 text-[9px] text-primary">{running}</span>}
        </NavLink></Tooltip>)}</div>
      </div>)}
    </nav>

    <div className="p-3">
      <AppMenu collapsed={collapsed} searchOpen={searchOpen} onOpenSearch={onOpenSearch} />
    </div>
    </div>
    <div role="separator" aria-label="调整导航宽度" aria-orientation="vertical" aria-controls="lingxi-main-navigation" aria-valuemin={SIDEBAR_ICON_WIDTH} aria-valuemax={SIDEBAR_MAX_WIDTH} aria-valuenow={width} aria-valuetext={collapsed ? `仅图标，${width} 像素` : `文字导航，${width} 像素`} tabIndex={0} className="sidebar-resize-handle" title="拖动调整宽度，继续向左拖动可收起" {...resizeHandleProps} />
  </aside>
}
