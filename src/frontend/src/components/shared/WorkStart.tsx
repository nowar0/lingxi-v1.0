import { Compass, FolderClosed, Globe, MessageSquarePlus, TerminalSquare } from 'lucide-react'
import type { WorkPane, WorkToolKind } from '@/lib/workspaceTabs'

export function WorkStart({ side, onOpen, recent, projectPath, onSideChat }: { onSideChat?:()=>void; recent?: {id:number;title:string}[]; projectPath?:string; art?: string; side: WorkPane; onOpen: (kind: WorkToolKind, side?: WorkPane) => void }) {
  return <div className="workspace-start">
    <div className="workspace-start-actions"><div className="workspace-start-heading"><Compass size={20} aria-hidden="true"/><h2>工具</h2></div><p className="workspace-start-context" title={projectPath}>{projectPath?.split(/[\\/]/).filter(Boolean).at(-1)||'选择工作区'}</p>
      <button onClick={() => onOpen('files', side)}><FolderClosed className="is-files" /><span><strong>文件</strong><small>浏览会话工作区的文件</small></span><kbd>Ctrl + P</kbd></button>
      {onSideChat&&<button onClick={onSideChat}><MessageSquarePlus/><span><strong>侧边聊天</strong><small>创建独立的侧聊会话</small></span></button>}
      <button onClick={() => onOpen('terminal', side)}><TerminalSquare className="is-terminal" /><span><strong>终端</strong><small>在会话工作区运行命令</small></span><kbd>Ctrl + `</kbd></button>
      <button onClick={() => onOpen('browser', side)}><Globe className="is-browser" /><span><strong>浏览器</strong><small>浏览网页</small></span><kbd>Ctrl + T</kbd></button>

    </div>
  </div>
}
