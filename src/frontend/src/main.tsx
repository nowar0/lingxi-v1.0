import {hydrateBrowserPreferences} from '@/lib/browserPreferences'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { establishHostSession } from './api/client'
import './index.css'

const root = createRoot(document.getElementById('root')!)
// The launcher capability shares the URL fragment with HashRouter. Capture and
// exchange it BEFORE any router render/effect can interpret or replace the hash.
// Awaiting this also prevents an initial reload from discarding an in-flight
// exchange. Never manufacture a cookie or continue a failed bootstrap offline.
async function startApplication() {
  try {
    await establishHostSession()
    await hydrateBrowserPreferences()
  } catch {
    root.render(<main role="alert" className="p-8"><h1>本地会话认证未完成</h1><p>请关闭此窗口并从零汐启动器重新打开。未批准插件执行或模型发送。</p></main>)
    return
  }
  root.render(<StrictMode><App /></StrictMode>)
}
void startApplication()
