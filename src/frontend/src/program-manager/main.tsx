import { createRoot } from 'react-dom/client'
import { ProgramManagerView } from '@/components/settings/ProgramManagerView'
import { createProgramManagerClient } from '@/lib/programManager'
import { beginManagerSession, ManagerSessionError } from './session'
import '../index.css'
import './manager.css'

const element=document.getElementById('root')
if(!element)throw new Error('Manager root is unavailable')
const root=createRoot(element)
const renderError=()=>root.render(<main className="page-container chrome-page"><h1>零汐 · 外置便携程序管理器</h1><p role="alert">{new ManagerSessionError().message}</p><p>没有生成或提交新计划；已提交动作的结果请通过宿主核对。普通聊天、模型和Key接口未启动。</p></main>)
let client:ReturnType<typeof createProgramManagerClient>|undefined
// This promise is created once outside React effects. StrictMode/remount cannot
// consume the one-use bootstrap twice; no token is stored in browser persistence.
const session=beginManagerSession({location:window.location,clearFragment:pathname=>window.history.replaceState(null,'',pathname),onExpired:()=>{client?.invalidate();renderError()}})
root.render(<main className="page-container chrome-page"><h1>零汐 · 外置便携程序管理器</h1><p role="status">正在验证本次宿主启动能力；未读取程序或用户数据目录。</p></main>)
void session.then(fetcher=>{
  client=createProgramManagerClient({fetcher})
  root.render(<main className="page-container chrome-page"><header className="mb-5"><h1 className="text-xl font-semibold">零汐 · 外置便携程序管理器</h1><p className="mt-2 text-sm text-muted-foreground">只管理宿主明确授权的便携根目录；不会自动启动聊天服务、迁移默认数据或停止原程序。</p></header><ProgramManagerView connected client={client}/></main>)
}).catch(renderError)
