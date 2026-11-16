import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { Check, Copy, Eye, RefreshCw, Save, Search } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError, json } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ArtifactPreview, safeHtmlPreview } from './ArtifactPreview'
import { MessageContent } from './MessageContent'
import { useApp } from '@/context/AppContext'
import { artifactReference, type Artifact } from '@/lib/artifacts'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'

export type FileSnapshot = { path: string; text: string; sha256: string; read_only?: boolean; reason?: string; size?: number; mime?: string; preview_kind?: string; location?: { line: number; column: number; request: number } }
type Buffer = { base: FileSnapshot; text: string; remote?: FileSnapshot }
export type FileEditorHandle = { requestClose: () => Promise<boolean> }
const drafts = new Map<string, Buffer>()
let exitGuard = false

export const WorkFileEditor = forwardRef<FileEditorHandle, {
  sessionId: number; projectPath: string; path: string; initial: FileSnapshot; active: boolean; enabled: boolean
  onDirty: (dirty: boolean) => void; onSaved: () => void
}>(function WorkFileEditor({ sessionId, projectPath, path, initial, active, enabled, onDirty, onSaved }, ref) {
  const {activeWorkSessionId,drafts:conversationDrafts,setDraft}=useApp()
  const composer=useRef({sid:activeWorkSessionId,draft:conversationDrafts.work})
  composer.current={sid:activeWorkSessionId,draft:conversationDrafts.work}
  const [preview,setPreview]=useState<Artifact>(),[previewing,setPreviewing]=useState(false),[rendered,setRendered]=useState(false)
  const key = JSON.stringify([sessionId, projectPath, path])
  const [buffer, setBuffer] = useState<Buffer>(() => drafts.get(key) || { base: initial, text: initial.text })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [comparing, setComparing] = useState(false)
  const [closing, setClosing] = useState(false)
  const [finding, setFinding] = useState(false)
  const [query, setQuery] = useState('')
  const closeAnswer = useRef<((answer: boolean) => void) | undefined>(undefined)
  const editor = useRef<HTMLTextAreaElement>(null)
  const serial = useRef(0)
  const busy = useRef(false)
  const alive = useRef(true)
  const current = useRef(buffer)
  current.current = buffer
  const dirty = buffer.text !== buffer.base.text
  const readOnly = !enabled || Boolean(buffer.base.read_only)
  const bytes = new TextEncoder().encode(buffer.text).byteLength
  const endpoint = `/v9/workspace/file?session_id=${sessionId}&path=${encodeURIComponent(path)}&expected_project_path=${encodeURIComponent(projectPath)}`
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false; closeAnswer.current?.(false) }
  }, [])
  useEffect(() => {
    if (dirty) drafts.set(key, buffer)
    else drafts.delete(key)
    onDirty(dirty)
    if (!exitGuard) {
      exitGuard = true
      window.addEventListener('beforeunload', event => {
        if (drafts.size) { event.preventDefault(); event.returnValue = '' }
      })
    }
  }, [buffer, dirty, key, onDirty])
  const receive = useCallback((remote: FileSnapshot) => {
    setBuffer(old => {
      if (old.base.sha256 === remote.sha256 && old.base.read_only === remote.read_only && old.base.mime === remote.mime && old.base.size === remote.size) return old
      return old.text !== old.base.text ? { ...old, remote } : { base: remote, text: remote.text }
    })
  }, [])
  const refresh = useCallback(async () => {
    if (!enabled || busy.current) return
    const request = ++serial.current
    try {
      const remote = await api<FileSnapshot>(endpoint)
      if (alive.current && request === serial.current) { receive(remote); setError('') }
    } catch (failure) {
      if (alive.current && request === serial.current) setError((failure as Error).message)
    }
  }, [enabled, endpoint, receive])
  useEffect(() => {
    receive(initial)
  }, [initial, receive])
  useEffect(() => {
    if (!initial.location || !active || !editor.current) return
    if (current.current.text !== current.current.base.text) { setError('此文件有未保存编辑，搜索位置来自磁盘版本，请先核对内容'); return }
    const lines = initial.text.replace(/\r\n/g, '\n').split('\n'), line = Math.max(0, Math.min(lines.length - 1, initial.location.line - 1))
    const start = lines.slice(0,line).reduce((n,s) => n + s.length + 1, 0) + Array.from(lines[line]).slice(0, initial.location.column - 1).join('').length
    editor.current.focus(); editor.current.setSelectionRange(start, start)
    const height = parseFloat(getComputedStyle(editor.current).lineHeight) || 20
    editor.current.scrollTop = Math.max(0, line * height - editor.current.clientHeight / 2)
  }, [initial.location, active, initial.text])
  useEffect(() => {
    if (!active || !enabled) return
    const check = () => { if (document.visibilityState === 'visible') void refresh() }
    check()
    const timer = window.setInterval(check, 4000)
    window.addEventListener('focus', check)
    return () => { window.clearInterval(timer); window.removeEventListener('focus', check) }
  }, [active, enabled, refresh])
  const save = async () => {
    const value = current.current
    if (busy.current || readOnly || value.remote || value.text === value.base.text) return false
    busy.current = true; setSaving(true); setError(''); ++serial.current
    try {
      const text = value.base.text.includes('\r\n') ? value.text.replace(/\r?\n/g, '\r\n') : value.text
      const saved = await api<{ sha256: string }>(endpoint, json('PUT', { content: text, expected_hash: value.base.sha256, expected_project_path: projectPath }))
      if (!alive.current) return false
      const base = { ...value.base, text, size:new TextEncoder().encode(text).byteLength, sha256: saved.sha256 }
      setBuffer(old => ({ base, text: old.text === value.text ? text : old.text }))
      drafts.delete(key); onSaved(); toast.success('文件已保存')
      return true
    } catch (failure) {
      if (alive.current) {
        setError((failure as Error).message)
        if (failure instanceof ApiError && failure.status === 409) {
          const remote = await api<FileSnapshot>(endpoint).catch(() => undefined)
          if (alive.current && remote) { receive(remote); setComparing(true) }
        }
      }
      return false
    } finally { busy.current = false; if (alive.current) setSaving(false) }
  }
  useImperativeHandle(ref, () => ({ requestClose: async () => {
    if (closeAnswer.current) return false
    if (busy.current) { toast.message('文件正在保存，请稍候'); return false }
    if (current.current.text === current.current.base.text) return true
    setClosing(true)
    return new Promise<boolean>(resolve => { closeAnswer.current = resolve })
  } }))
  const answerClose = (answer: boolean) => { closeAnswer.current?.(answer); closeAnswer.current = undefined; setClosing(false) }
  const discard = () => {
    const remote = current.current.remote || current.current.base
    setBuffer({ base: remote, text: remote.text }); drafts.delete(key)
  }
  const previewOriginal = async () => {
    if(busy.current||previewing||!enabled||dirty||!buffer.base.sha256)return
    setPreviewing(true);setError('')
    try {
      const artifact=await api<Artifact>('/v9/artifacts/from-workspace',json('POST',{
        session_id:sessionId,path,expected_sha256:buffer.base.sha256,expected_project_path:projectPath}))
      if(alive.current)setPreview(artifact)
    }catch(failure){if(alive.current)setError((failure as Error).message)}
    finally{if(alive.current)setPreviewing(false)}
  }
  const insertReference = (reference: string) => {
    if(composer.current.sid!==sessionId){toast.error('工作会话已经切换，请返回原会话引用');return}
    const next=[composer.current.draft.trimEnd(),artifactReference(preview?.name||path,reference)].filter(Boolean).join('\n\n')
    if(next.length>8000){toast.error('加入引用后草稿超过8000字，请先缩短');return}
    setDraft('work',next);toast.success('已加入当前工作草稿')
  }
  const findNext = () => {
    const field = editor.current
    if (!query || !field) return
    const text = field.value.toLocaleLowerCase(), needle = query.toLocaleLowerCase()
    const start = text.indexOf(needle, field.selectionEnd)
    const index = start < 0 ? text.indexOf(needle) : start
    if (index < 0) { toast.message('文件中没有匹配内容'); return }
    field.focus(); field.setSelectionRange(index, index + query.length)
    const line = text.slice(0, index).split('\n').length
    field.scrollTop = Math.max(0, (line - 4) * 21)
  }
  return <section className="workspace-editor" aria-label={`文件编辑器：${path}`}>
    <div className="workspace-panel-heading"><h2>{buffer.base.read_only ? '文件预览' : '文件编辑'}</h2><span role="status">{saving ? '保存中…' : dirty ? '未保存' : buffer.base.read_only ? '只读' : '已保存'}</span>
      {['text/markdown','text/html'].includes(buffer.base.mime||'')&&!buffer.base.read_only&&<Button size="xs" variant="ghost" aria-pressed={rendered} onClick={()=>setRendered(value=>!value)}><Eye/>{rendered?'返回编辑':'渲染预览'}</Button>}
      <Button variant="ghost" size="icon" className="size-7" aria-label="查找文件内容" disabled={Boolean(buffer.base.read_only)} onClick={() => setFinding(value => !value)}><Search /></Button>
      <Button variant="ghost" size="icon" className="size-7" aria-label="刷新文件内容" disabled={!enabled || saving} onClick={() => void refresh()}><RefreshCw /></Button>
      <Button variant="ghost" size="icon" className="size-7" aria-label="复制文件内容" disabled={Boolean(buffer.base.read_only)} onClick={() => void navigator.clipboard.writeText(buffer.text).then(() => toast.success('已复制文件内容')).catch(() => toast.error('复制未完成'))}><Copy /></Button>
      <Button size="sm" variant="outline" disabled={readOnly || !dirty || saving || Boolean(buffer.remote) || bytes > 2 * 1024 * 1024} onClick={() => void save()}><Save />保存</Button>
    </div>
    <div className="workspace-file-path" title={projectPath}>{path}</div>
    {buffer.base.reason && <p className="workspace-editor-note">{buffer.base.reason}</p>}
    {error && <div className="workspace-panel-error" role="alert"><p>{error}</p><Button size="sm" variant="ghost" onClick={() => void refresh()}>重试读取</Button></div>}
    {buffer.remote && <div className="workspace-editor-conflict" role="status"><p>文件已被其他操作修改。你的编辑仍保留，比较后再保存。</p><Button size="sm" variant="outline" onClick={() => setComparing(true)}>比较内容</Button></div>}
    {buffer.base.sha256&&<div className="workspace-file-preview-actions"><Button size="xs" variant="outline" disabled={!enabled||dirty||previewing||saving||!!buffer.remote} onClick={()=>void previewOriginal()}><Eye/>{previewing?'正在准备原件…':'预览原件与引用范围'}</Button><span>{dirty?'当前编辑尚未保存；渲染预览使用编辑中的内容。':'预览使用此版本的原件，引用先加入草稿。'}</span></div>}
    {finding && <form className="workspace-editor-search" onSubmit={event => { event.preventDefault(); findNext() }}><Input autoFocus aria-label="文件内查找" value={query} onChange={event => setQuery(event.target.value)} /><Button size="sm" variant="ghost" type="submit">下一个</Button><Button size="sm" variant="ghost" onClick={() => setFinding(false)}>关闭查找</Button></form>}
    {rendered&&!buffer.base.read_only&&(buffer.base.mime==='text/html'?<iframe className="workspace-rendered-html" sandbox="" referrerPolicy="no-referrer" title="项目HTML安全预览" srcDoc={safeHtmlPreview(buffer.text)}/>:<div className="workspace-rendered-markdown"><MessageContent text={buffer.text}/></div>)}
    {!rendered&&!buffer.base.read_only && <textarea ref={editor} className="workspace-editor-input" aria-label={`编辑 ${path}`} spellCheck={false} wrap="off" readOnly={readOnly || saving} value={buffer.text}
      onChange={event => setBuffer(old => ({ ...old, text: event.target.value }))}
      onKeyDown={event => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); void save() }
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') { event.preventDefault(); setFinding(true) }
      }} />}
    <div className="workspace-editor-status">{buffer.base.read_only?<><span>{buffer.base.mime||'原件'} · 只读</span><span>{buffer.base.size?.toLocaleString()??'未知大小'} 字节</span></>:<><span>UTF-8 · {buffer.base.text.includes('\r\n') ? 'CRLF' : 'LF'}</span><span>{buffer.text.split('\n').length} 行 · {bytes.toLocaleString()} 字节</span></>}{!dirty && !buffer.base.read_only && <Check aria-label="已保存" />}</div>
    {preview&&<ArtifactPreview id={preview.id} sessionId={sessionId} open onOpenChange={open=>{if(!open)setPreview(undefined)}} onReference={insertReference}/>}
    <Dialog open={comparing} onOpenChange={setComparing}><DialogContent className="workspace-file-compare"><DialogHeader><DialogTitle>比较文件改动</DialogTitle><DialogDescription>{path}。确认采用哪份内容后，仍需手动保存；保存前会再次检查磁盘版本。</DialogDescription></DialogHeader><div className="workspace-file-compare-grid"><label>磁盘当前内容<textarea readOnly value={buffer.remote?.text || buffer.base.text} /></label><label>我的未保存编辑<textarea readOnly value={buffer.text} /></label></div><DialogFooter><Button variant="outline" onClick={() => { discard(); setComparing(false) }}>采用磁盘内容</Button><Button disabled={buffer.remote?.read_only} onClick={() => { setBuffer(old => ({ base: old.remote || old.base, text: old.text })); setComparing(false) }}>保留我的内容继续编辑</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={closing} onOpenChange={open => { if (!open) answerClose(false) }}><DialogContent><DialogHeader><DialogTitle>文件有未保存的编辑</DialogTitle><DialogDescription>{path}。关闭前可保存、放弃编辑或继续编辑。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" onClick={() => answerClose(false)}>继续编辑</Button><Button variant="outline" onClick={() => { discard(); answerClose(true) }}>放弃并关闭</Button><Button disabled={saving || Boolean(buffer.remote) || readOnly} onClick={() => void save().then(saved => { if (saved) answerClose(true) })}>保存并关闭</Button></DialogFooter></DialogContent></Dialog>
  </section>
})
