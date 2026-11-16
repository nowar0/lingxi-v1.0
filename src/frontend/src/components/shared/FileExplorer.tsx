import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, File, Folder, LoaderCircle, RefreshCw, Search, X } from 'lucide-react'
import { api } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

type Entry = { path: string; directory: boolean }
type Match = { path: string; line?: number; column?: number; preview: string; sha256?: string }
type Results = { items: Match[]; more: boolean; next_offset?: number; partial: boolean; reason: string; visited: number; skipped: number; unreadable: number; ignored: string }
type Directory = { items: Entry[]; error?: string; loading?: boolean; limited?: boolean }

export function FileExplorer({ sessionId, active, onOpen, refresh, compact=false }: { sessionId: number; active: boolean; refresh: number; compact?:boolean; onOpen: (path: string, line?: number, column?: number, sha?: string) => void }) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(['.']))
  const [dirs, setDirs] = useState<Record<string, Directory>>({})
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState('filename')
  const [scope, setScope] = useState('.')
  const [ignore, setIgnore] = useState('')
  const [searching, setSearching] = useState(false)
  const [results, setResults] = useState<Results>()
  const [error, setError] = useState('')
  const [searchVersion, setSearchVersion] = useState(0)
  const [offset, setOffset] = useState(0)
  const [selection, setSelection] = useState('')
  const [watchError, setWatchError] = useState('')
  const visibleDirs = useMemo(() => [...expanded].filter(path => path === '.' || path.split('/').slice(0,-1).every((_,i,parts) => expanded.has(parts.slice(0,i+1).join('/')))), [expanded])
  const alive = useRef(true)
  const inflight = useRef(new Set<string>())
  const signatures = useRef(new Map<string, string>())
  const requestSerial = useRef(0)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const load = useCallback(async (path: string) => {
    if (inflight.current.has(path)) return
    inflight.current.add(path)
    setDirs(old => ({ ...old, [path]: { ...old[path], items: old[path]?.items || [], loading: true, error: '' } }))
    try {
      const data = await api<{ items: Entry[]; limit: number; truncated?: boolean }>(`/v9/workspace/files?session_id=${sessionId}&path=${encodeURIComponent(path)}`)
      if (alive.current) setDirs(old => ({ ...old, [path]: { items: data.items.sort((a,b) => Number(b.directory)-Number(a.directory) || a.path.localeCompare(b.path)), limited: data.truncated || data.items.length >= data.limit } }))
    } catch (failure) { if (alive.current) setDirs(old => ({ ...old, [path]: { items: old[path]?.items || [], error: (failure as Error).message } })) }
    finally { inflight.current.delete(path) }
  }, [sessionId])
  useEffect(() => { for (const path of visibleDirs) void load(path) }, [visibleDirs, load, refresh])
  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    let busy = false
    const check = async () => {
      if (busy || document.visibilityState !== 'visible') return
      busy = true
      try {
        const params = new URLSearchParams({ session_id: String(sessionId) })
        for (const path of visibleDirs) params.append('directories', path)
        const data = await api<{ items: { path: string; signature?: string; error?: string }[] }>(`/v9/workspace/watch?${params}`, { signal: controller.signal })
        if (controller.signal.aborted) return
        setWatchError('')
        for (const item of data.items) {
          if (item.error) { setDirs(old => ({ ...old, [item.path]: { items: old[item.path]?.items || [], error: item.error } })); continue }
          if (signatures.current.has(item.path) && signatures.current.get(item.path) !== item.signature) void load(item.path)
          signatures.current.set(item.path, item.signature!)
        }
      } catch (failure) { if (!controller.signal.aborted) setWatchError(`自动检查暂不可用：${(failure as Error).message}；可手动刷新。`) }
      finally { busy = false }
    }
    void check(); const timer = window.setInterval(() => void check(), 2500)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [active, visibleDirs, load, sessionId])
  useEffect(() => {
    const serial = ++requestSerial.current
    if (!searchVersion || !query.trim()) { setResults(undefined); setSearching(false); return }
    const controller = new AbortController()
    setSearching(true); setError(''); setResults(undefined)
    const params = new URLSearchParams({ session_id: String(sessionId), q: query.trim(), mode, path: scope, offset: String(offset) })
    for (const pattern of ignore.split(',').map(p => p.trim()).filter(Boolean)) params.append('ignore', pattern)
    void api<Results>(`/v9/workspace/search?${params}`, { signal: controller.signal }).then(data => { if (!controller.signal.aborted && serial === requestSerial.current) setResults(data) }).catch(failure => { if (!controller.signal.aborted && serial === requestSerial.current) setError(failure.message) }).finally(() => { if (serial === requestSerial.current) setSearching(false) })
    return () => controller.abort()
  }, [query, mode, scope, ignore, searchVersion, offset, sessionId])
  useEffect(()=>{if(!compact)return;const timer=window.setTimeout(()=>{setOffset(0);setSearchVersion(query.trim()?1:0)},180);return()=>clearTimeout(timer)},[query,compact])
  const changed = () => { setSearchVersion(0); setOffset(0); setError('') }
  const open = (item: Match) => { setSelection(item.path); onOpen(item.path,item.line,item.column,item.sha256) }
  const tree = (path: string, depth = 0): React.ReactNode => {
    const directory = dirs[path]
    if (!directory) return null
    return <div role="group">{directory.error && <div className="workspace-panel-error"><p>{directory.error}</p><Button variant="ghost" size="xs" onClick={() => void load(path)}>重试此目录</Button></div>}{directory.items.map(item => <div key={item.path}><button role="treeitem" aria-expanded={item.directory ? expanded.has(item.path) : undefined} aria-selected={!item.directory && selection === item.path} className="workspace-tree-row" style={{ paddingLeft: 12 + depth * 14 }} onClick={() => { if (!item.directory) return open({ ...item, preview: '' }); setExpanded(old => { const next = new Set(old); if (next.has(item.path)) next.delete(item.path); else if (next.size < 50) next.add(item.path); return next }) }} onKeyDown={event => { if (['ArrowDown','ArrowUp','Home','End'].includes(event.key)) { const peers = Array.from(event.currentTarget.closest('[role=tree]')!.querySelectorAll<HTMLButtonElement>('[role=treeitem]')); const i = peers.indexOf(event.currentTarget); const next = event.key === 'Home' ? peers[0] : event.key === 'End' ? peers.at(-1) : peers[Math.max(0,Math.min(peers.length-1,i+(event.key === 'ArrowDown' ? 1 : -1)))]; event.preventDefault(); next?.focus(); return } if (!item.directory) return; if (event.key === 'ArrowRight' && !expanded.has(item.path) && expanded.size < 50) { event.preventDefault(); setExpanded(old => new Set([...old,item.path])) } if (event.key === 'ArrowLeft' && expanded.has(item.path)) { event.preventDefault(); setExpanded(old => { const next = new Set(old); next.delete(item.path); return next }) } }}>{item.directory ? expanded.has(item.path) ? <ChevronDown /> : <ChevronRight /> : <File />}{item.directory && <Folder className="is-folder" />}<span title={item.path}>{item.path.split('/').at(-1)}</span></button>{item.directory && expanded.has(item.path) && tree(item.path, depth+1)}</div>)}{directory.loading && <p className="workspace-inline-note"><LoaderCircle className="inline size-3 animate-spin" />正在读取目录…</p>}{directory.limited && <p className="workspace-inline-note">本目录仅显示前500项，可缩小搜索范围。</p>}{!directory.items.length && !directory.error && !directory.loading && <p className="workspace-inline-note">空目录</p>}</div>
  }
  return <div className="workspace-explorer"><form className="workspace-search-form" onSubmit={e => { e.preventDefault(); setOffset(0); setSearchVersion(v => v+1) }}><label className="workspace-file-search"><Search /><input aria-label="搜索项目文件或内容" placeholder={compact?'筛选文件…':'搜索文件名或内容'} value={query} maxLength={200} onChange={e => { setQuery(e.target.value); changed() }} />{query && <button type="button" aria-label="清除文件搜索" onClick={() => { setQuery(''); changed() }}><X /></button>}</label><details className="workspace-explorer-options"><summary>搜索选项</summary><div className="workspace-search-options"><select aria-label="文件搜索方式" value={mode} onChange={e => { setMode(e.target.value); changed() }}><option value="filename">文件名</option><option value="content">正文内容</option></select><select aria-label="文件搜索范围" value={scope} onChange={e => { setScope(e.target.value); changed() }}>{[...expanded].map(path => <option key={path} value={path}>{path === '.' ? '整个项目' : path}</option>)}</select><Button size="xs" type="submit" disabled={!query.trim() || searching}>{searching ? '搜索中…' : '搜索'}</Button>{searching && <Button size="xs" variant="ghost" type="button" onClick={() => { requestSerial.current++; setSearchVersion(0); setSearching(false) }}>取消</Button>}</div><details className="workspace-search-advanced"><summary>忽略模式</summary><Input className="h-7 text-xs" aria-label="忽略模式" value={ignore} onChange={e => { setIgnore(e.target.value); changed() }} placeholder="忽略模式，逗号分隔，如 *.log,cache/*" maxLength={2000} /></details></details></form>{error && <div className="workspace-panel-error" role="alert"><p>{error}</p><Button variant="outline" size="sm" onClick={() => setSearchVersion(v => v+1)}>重试搜索</Button></div>}{results ? <div className="workspace-search-results"><p className="workspace-inline-note">本页{results.items.length}项 · 扫描{results.visited}项{results.skipped ? ` · 跳过${results.skipped}个非适用文件` : ''}{results.unreadable ? ` · ${results.unreadable}项不可读` : ''}</p>{results.partial && <p className="workspace-panel-error" role="status">{results.reason}</p>}{results.items.map((item,i) => <button className="workspace-search-hit" key={`${item.path}:${item.line}:${i}`} onClick={() => open(item)}><span>{item.path}{item.line ? `:${item.line}:${item.column}` : ''}</span><code>{item.preview}</code></button>)}{!results.items.length && <p className="workspace-inline-note">没有匹配项。可调整范围或忽略模式。</p>}<div className="workspace-search-options"><Button variant="ghost" size="xs" disabled={!offset || searching} onClick={() => setOffset(n => Math.max(0,n-50))}>上一页</Button><Button variant="ghost" size="xs" disabled={!results.more || searching} onClick={() => setOffset(results.next_offset || 0)}>下一页</Button></div><p className="workspace-file-hint">忽略：{results.ignored}。正文限UTF-8、单文件2MiB；结果可能随外部修改变化。</p></div> : <><div className="workspace-file-location"><span>项目文件</span><Button size="icon" variant="ghost" className="size-7" aria-label="刷新展开目录" onClick={() => { for (const path of visibleDirs) void load(path) }}><RefreshCw /></Button></div>{watchError && <p className="workspace-inline-note" role="status">{watchError}</p>}<div role="tree" aria-label="项目文件树" className="workspace-file-tree">{tree('.')}</div><p className="workspace-file-hint">仅检查展开目录的变化；选中文件在标签页编辑。最多展开50个目录。</p></>}</div>
}
