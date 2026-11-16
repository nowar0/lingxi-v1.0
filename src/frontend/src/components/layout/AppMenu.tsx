import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Bell, Moon, Settings, Sun, Wifi, RefreshCw } from 'lucide-react'
import { useApp } from '@/context/AppContext'
import { useTheme } from '@/context/ThemeContext'
import { Tooltip } from '@/components/ui/tooltip'
import { CharacterAvatar } from '@/components/shared/CharacterAvatar'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'

export function AppMenu({ collapsed, searchOpen }: { collapsed: boolean; searchOpen: boolean; onOpenSearch: () => void }) {
  const { activeCharacter, connection, health, reconnect } = useApp()
  const { mode, setMode } = useTheme()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [view, setView] = useState<'main' | 'notifications'>('main')
  const firstItemRef = useRef<HTMLDivElement>(null)
  const backItemRef = useRef<HTMLDivElement>(null)
  const openingSearch = useRef(false)
  const isDark = mode === 'dark' || (mode === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)

  useEffect(() => {
    if (searchOpen && open) { openingSearch.current = true; setOpen(false) }
  }, [searchOpen, open])
  useEffect(() => {
    if (open) (view === 'notifications' ? backItemRef.current : firstItemRef.current)?.focus()
  }, [open, view])

  return <DropdownMenu modal={false} open={open} onOpenChange={(value) => { setOpen(value); if (value) { openingSearch.current = false; setView('main') } }}>
    <Tooltip content={collapsed ? '应用菜单' : ''} side="right"><DropdownMenuTrigger asChild>
      <button className="app-menu-trigger account-trigger" aria-label="应用菜单">
        <CharacterAvatar src={activeCharacter.avatar} name={activeCharacter.name} className="account-avatar rounded-full border-0 shadow-none" />
        <span className="account-name">{activeCharacter.name}</span>
      </button>
    </DropdownMenuTrigger></Tooltip>
    <DropdownMenuContent side="top" align="start" sideOffset={8} collisionPadding={12} className="app-corner-menu" aria-label={view === 'main' ? '应用菜单' : '通知与状态'} onCloseAutoFocus={(event) => { if (openingSearch.current) { event.preventDefault(); openingSearch.current = false } }}>
      {view === 'main' ? <>
        <div className="flex min-w-0 items-center gap-2.5 px-3 py-3"><CharacterAvatar src={activeCharacter.avatar} name={activeCharacter.name} className="account-avatar menu-account-avatar rounded-full border-0 shadow-none" /><span className="min-w-0 truncate text-[13px] font-semibold">{activeCharacter.name}</span></div>
        <DropdownMenuSeparator />

        <DropdownMenuItem ref={firstItemRef} className="app-corner-menu-item" onSelect={() => navigate('/settings')}><Settings /><span>设置</span></DropdownMenuItem>
        <DropdownMenuItem className="app-corner-menu-item" onSelect={(event) => { event.preventDefault(); setMode(isDark ? 'light' : 'dark') }}>{isDark ? <Sun /> : <Moon />}<span>{isDark ? '切换为白天' : '切换为夜晚'}</span></DropdownMenuItem>
        <DropdownMenuItem className="app-corner-menu-item" onSelect={(event) => { event.preventDefault(); setView('notifications') }}><Bell /><span>通知</span></DropdownMenuItem>
      </> : <>
        <DropdownMenuItem ref={backItemRef} className="app-corner-menu-item" onSelect={(event) => { event.preventDefault(); setView('main') }}><ArrowLeft /><span>返回</span></DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuLabel className="px-3 py-2">通知与状态</DropdownMenuLabel>
        <div className="flex items-center gap-2.5 px-3 py-4 text-xs text-muted-foreground"><Bell className="size-4 shrink-0" /><span>暂无新通知</span></div>
        <DropdownMenuSeparator />
        <div className="flex items-start gap-2.5 px-3 py-3 text-xs"><Wifi className="mt-0.5 size-4 shrink-0 text-muted-foreground" /><div className="min-w-0"><div>{connection === 'connected' ? '本地服务连接正常' : connection === 'checking' ? '正在连接本地服务' : '本地服务未连接'}</div>{health?.version && <div className="mt-1 text-[11px] text-muted-foreground">{health.name} · {health.version}</div>}</div></div>
        {connection === 'demo' && <DropdownMenuItem className="app-corner-menu-item" onSelect={() => void reconnect()}><RefreshCw /><span>重新检测真实服务</span></DropdownMenuItem>}
      </>}
    </DropdownMenuContent>
  </DropdownMenu>
}
