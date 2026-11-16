import {UnsavedChanges} from '@/components/shared/UnsavedChanges'
import {NavigationGuard} from '@/components/shared/NavigationGuard'
import {PageErrorBoundary} from '@/components/shared/PageErrorBoundary'
import {UiCommandBridge} from '@/components/shared/UiCommandBridge'
import { Suspense, useEffect, useRef, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'
import { DesktopWindowChrome } from './DesktopWindowChrome'
import { PageLoading } from '@/components/shared/PageLoading'
import { QuickSearch } from './QuickSearch'
import { useApp } from '@/context/AppContext'
import { Button } from '@/components/ui/button'

export function AppShell() {
  const location = useLocation()
  const main = useRef<HTMLElement>(null)
  useEffect(() => { main.current?.scrollTo({ top: 0, behavior: 'instant' }) }, [location.pathname])
  const { dataErrors, connection, reconnect } = useApp()
  const [retrying, setRetrying] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setSearchOpen((current) => !current)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])
  return <div className="app-shell"><UnsavedChanges/><NavigationGuard/><UiCommandBridge/><Sidebar searchOpen={searchOpen} onOpenSearch={() => setSearchOpen(true)} /><div className="app-stage"><Topbar/>{connection === 'connected' && Object.keys(dataErrors).length > 0 && <div className="data-load-notice" role="status"><span title={Object.entries(dataErrors).map(([name, error]) => `${name}：${error}`).join('；')}>部分数据暂未读到：{Object.keys(dataErrors).join('、')}。已读到的功能仍可使用。</span><Button size="xs" variant="outline" disabled={retrying} onClick={() => { setRetrying(true); void reconnect().finally(() => setRetrying(false)) }}>{retrying ? '重试中…' : '重试读取'}</Button></div>}<main ref={main} className="app-main"><PageErrorBoundary resetKey={location.pathname+location.search}><Suspense fallback={<PageLoading />}><Outlet /></Suspense></PageErrorBoundary></main></div><DesktopWindowChrome/><QuickSearch open={searchOpen} onOpenChange={setSearchOpen} /></div>
}
