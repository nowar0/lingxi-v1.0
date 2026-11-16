import { useEffect, useRef, useState } from 'react'
import { ArrowUpRight, Search } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'

const commands = [
  { label: '回到首页', path: '/' }, { label: '继续消息对话', path: '/companion' },
  { label: '打开工作区', path: '/work' }, { label: '打开技能中心', path: '/skills' }, { label: '查看任务', path: '/tasks' },
  { label: '查看记忆', path: '/memory' }, { label: '打开连接中心', path: '/connections' }, { label: '打开模型模块', path: '/connections?tab=assignments' }, { label: '打开设置', path: '/settings' },
  { label: '导入主题', path: '/settings?tab=appearance' }, { label: '管理人物卡包', path: '/settings?tab=characters' },
]

export function QuickSearch({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const navigate = useNavigate()
  const [selected,setSelected]=useState(0)
  const [query, setQuery] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const results = commands.filter(item => item.label.toLowerCase().includes(query.trim().toLowerCase()))
  useEffect(()=>setSelected(0),[query,open])
  const choose=(index:number)=>{const item=results[index];if(item){navigate(item.path);onOpenChange(false)}}
  useEffect(() => { if (!open) setQuery('') }, [open])

  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent className="app-search-dialog top-[18%] max-w-none translate-y-0 gap-0 overflow-hidden p-0" onOpenAutoFocus={(event) => { event.preventDefault(); inputRef.current?.focus() }}>
    <DialogHeader className="sr-only"><DialogTitle>快速前往</DialogTitle><DialogDescription>搜索页面与常用操作</DialogDescription></DialogHeader>
    <div className="app-search-input-row"><Search aria-hidden="true" className="size-[18px] shrink-0 text-muted-foreground" /><input ref={inputRef} autoFocus role="combobox" aria-expanded={open} aria-controls="quick-search-results" aria-activedescendant={results[selected]?`quick-search-${selected}`:undefined} aria-label="搜索页面或操作" onKeyDown={event=>{if(event.nativeEvent.isComposing)return;if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();setSelected(value=>(value+(event.key==='ArrowDown'?1:-1)+results.length)%Math.max(1,results.length))}else if(event.key==='Enter'){event.preventDefault();choose(selected)}}} className="h-14 min-w-0 flex-1 bg-transparent text-sm outline-none" placeholder="搜索页面或操作…" value={query} onChange={(event) => setQuery(event.target.value)} /></div>
    <div id="quick-search-results" role="listbox" className="app-search-results">{results.length ? results.map((item,index) => <button role="option" id={`quick-search-${index}`} aria-selected={selected===index} onMouseEnter={()=>setSelected(index)} key={item.path} className="app-search-result" onClick={() => { navigate(item.path); onOpenChange(false) }}><span className="min-w-0 truncate">{item.label}</span><ArrowUpRight aria-hidden="true" className="ml-auto size-3.5 shrink-0 text-muted-foreground" /></button>) : <p className="px-3 py-8 text-center text-xs text-muted-foreground">没有找到匹配的页面或操作</p>}</div>
    <div className="flex items-center justify-between border-t border-border/60 px-4 py-2 text-[11px] text-muted-foreground"><span>快速前往零汐空间</span><kbd>Esc 关闭</kbd></div>
  </DialogContent></Dialog>
}
