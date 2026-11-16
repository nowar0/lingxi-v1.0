import {PluginHostSecurity} from './PluginHostSecurity'
import { Select } from '@/components/ui/select'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { FileArchive, FolderOpen, Play, Plus, RefreshCw, Search, X } from 'lucide-react'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
import { createPluginPackagesClient, pluginErrorMessage } from '@/lib/pluginPackages'
import type { LifecycleAction, PluginPackage, PluginPreview, ValueKind } from '@/lib/pluginPackages'
import { Button } from '@/components/ui/button'
import { Input, Textarea } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { PackageUiPanel } from './PackageUiPanel'
import { PackageMcpPanel } from './PackageMcpPanel'
import { PluginDataPanel } from './PluginDataPanel'

type Source = { action: 'install' | 'update' | 'restore'; item?: PluginPackage; path: string }
type Pending = { preview: PluginPreview; label: string }

export function PersonalPluginsPanel({ suggestedRoots = [] }: { suggestedRoots?: readonly string[] }) {
  const { connection } = useApp()
  const client = useMemo(() => createPluginPackagesClient(), [])
  const [items, setItems] = useState<PluginPackage[]>([])
  const [selectedId, setSelectedId] = useState<string>()
  const [query, setQuery] = useState('')
  const [source, setSource] = useState<Source>()
  const [pending, setPending] = useState<Pending>()
  const [value, setValue] = useState<{ kind: ValueKind; text: string }>()
  const [tool, setTool] = useState(''), [args, setArgs] = useState('{}')
  const [result, setResult] = useState<unknown>()
  const [busy, setBusy] = useState(false), [loading, setLoading] = useState(false), [error, setError] = useState('')
  const lock = useRef(false), mounted = useRef(true), request = useRef(0)
  const ready = connection === 'connected'
  const selected = items.find(item => item.id === selectedId)
  const filtered = items.filter(item => `${item.name} ${item.id}`.toLowerCase().includes(query.toLowerCase()))
  const refresh = useCallback(async () => {
    if (!ready) return
    const seq = ++request.current; setLoading(true)
    try { const next = await client.list(); if (mounted.current && request.current === seq) { setItems(next.items); setError('') } }
    catch (reason) { if (mounted.current && request.current === seq) setError(pluginErrorMessage(reason)) }
    finally { if (mounted.current && request.current === seq) setLoading(false) }
  }, [client, ready])
  useEffect(() => { mounted.current = true; void refresh(); return () => { mounted.current = false; request.current++ } }, [refresh])
  useEffect(() => { setTool(''); setArgs('{}'); setValue(undefined); setResult(undefined); setPending(undefined) }, [selectedId])
  const run = async (operation: () => Promise<void>) => {
    if (lock.current || !ready) return
    lock.current = true; setBusy(true); setError('')
    try { await operation() } catch (reason) { if (mounted.current) setError(pluginErrorMessage(reason)) }
    finally { lock.current = false; if (mounted.current) setBusy(false) }
  }
  const action = (item: PluginPackage, name: LifecycleAction, label: string) => void run(async () => {
    const preview = await client.previewAction(item.id, name, item.revision)
    if (['enable', 'disable'].includes(name) && !preview.blocked.length) { await client.commit(preview); await refresh() }
    else setPending({ preview, label })
  })
  const commit = () => {
    if (!pending) return
    const operation = pending; setPending(undefined)
    void run(async () => {
      if (operation.preview.snapshot.action === 'call') { const output = await client.execute(operation.preview); setResult(output.result) }
      else { const changed = await client.commit(operation.preview); setSelectedId(changed.id); setSource(undefined); setValue(undefined); toast.success(`${operation.label}完成`) }
      await refresh()
    })
  }
  const select = (id: string) => { if (!busy) setSelectedId(id) }
  return <div className="personal-plugin-library"><details className="personal-disclosure"><summary>高级维护：插件宿主隔离与目录</summary><PluginHostSecurity disabled={busy||!ready} onChanged={()=>void refresh()}/></details>
    <header className="customer-panel-heading"><div><h2>本地插件</h2><p>{items.filter(item => item.installed).length} 个已安装</p></div><Button size="sm" disabled={!ready || busy} onClick={() => setSource({ action: 'install', path: '' })}><Plus />安装插件</Button></header><div className="personal-toolbar"><label className="personal-search"><Search size={16} /><input aria-label="搜索插件" placeholder="搜索插件" value={query} onChange={event => setQuery(event.target.value)} /></label><Button variant="ghost" size="icon" aria-label="刷新插件" disabled={!ready || busy || loading} onClick={() => void refresh()}><RefreshCw className={loading ? 'animate-spin' : ''} /></Button></div>
    {error && <p className="personal-error" role="alert">{error}</p>}
    {!ready && <p className="personal-empty">连接本地服务后可管理插件。</p>}
    <div className={`personal-library-split${selected ? ' has-selection' : ''}`}>
      <section className="personal-plugin-list" aria-label="本地插件列表">{loading && !items.length ? <p className="personal-empty">正在读取插件…</p> : !filtered.length ? <div className="personal-empty"><FolderOpen size={24} /><h3>{query ? '没有匹配的插件' : '还没有本地插件'}</h3><p>{query ? '换个名称试试。' : '支持插件 ZIP 或文件夹。'}</p></div> : filtered.map(item => <button key={item.id} className={`personal-plugin-row${selectedId === item.id ? ' is-selected' : ''}`} onClick={() => select(item.id)}><div><strong>{item.name}</strong><span>{item.version}</span></div><p>{item.manifest.description || item.id}</p><span className="personal-state">{!item.installed ? '已卸载 · 数据保留' : item.missing || item.files_mismatch ? '文件待修复' : item.loaded ? '运行中' : item.enabled ? '已启用' : '已停用'}</span></button>)}</section>
      {selected && <section className="personal-plugin-detail" aria-label={`${selected.name}详情`}><header><div><h2>{selected.name}</h2><p>{selected.id} · {selected.version}</p></div><Button variant="ghost" aria-label="关闭插件详情" disabled={busy} onClick={() => setSelectedId(undefined)}><X /></Button></header><p>{selected.manifest.description}</p><div className="personal-actions">{selected.installed ? <><Button disabled={busy || selected.loaded || selected.missing || selected.files_mismatch} variant={selected.enabled ? 'outline' : 'default'} onClick={() => action(selected, selected.enabled ? 'disable' : 'enable', selected.enabled ? '停用' : '启用')}>{selected.enabled ? '停用插件' : '启用插件'}</Button><Button variant="outline" disabled={busy || selected.loaded} onClick={() => setSource({ action: 'update', item: selected, path: '' })}>更新</Button><Button variant="outline" disabled={busy || selected.loaded || !selected.previous_digest} onClick={() => action(selected, 'rollback', '回滚')}>回滚</Button></> : <Button disabled={busy} onClick={() => setSource({ action: 'restore', item: selected, path: '' })}>重新安装</Button>}</div>{selected.error && <p role="alert" className="personal-error">{selected.error}</p>}{selected.blocked.length > 0 && <p className="personal-error">依赖待处理：{selected.blocked.map(block => block.code).join('、')}</p>}
      {selected.installed && <>
        {Boolean(selected.manifest.ui || selected.manifest.config_schema) && <PackageUiPanel configOnly={Boolean(selected.manifest.config_schema && !selected.manifest.ui)} pluginId={selected.id} revision={selected.revision} digest={selected.digest} installed={selected.installed} enabled={selected.enabled} disabled={busy || !ready} onChanged={() => void refresh()} />}
        {Boolean(selected.manifest.tools.length) && <details className="personal-disclosure"><summary>运行工具</summary><div className="personal-form"><label>工具<Select value={tool} onChange={event => { setTool(event.target.value); setResult(undefined) }}><option value="">选择工具</option>{selected.manifest.tools.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}</Select></label><label>参数 JSON<Textarea value={args} onChange={event => setArgs(event.target.value)} aria-label="插件工具参数" className="font-mono" /></label><Button disabled={busy || !selected.enabled || !tool} onClick={() => void run(async () => { const preview = await client.previewCall(selected.id, tool, args, selected.revision); if (preview.blocked.length) setPending({ preview, label: '运行工具' }); else { const output = await client.execute(preview); setResult(output.result); await refresh() } })}><Play />运行工具</Button>{result !== undefined && <pre className="personal-code" aria-label="工具执行结果">{JSON.stringify(result, null, 2)}</pre>}</div></details>}
        {Boolean(Array.isArray(selected.manifest.mcp) && selected.manifest.mcp.length) && <PackageMcpPanel pluginId={selected.id} revision={selected.revision} digest={selected.digest} installed={selected.installed} enabled={selected.enabled} disabled={busy || !ready} onChanged={() => void refresh()} />}
        <details className="personal-disclosure"><summary>配置与数据</summary><div className="personal-actions">{(['config', 'state', 'credentials'] as const).map(kind => <Button key={kind} variant="outline" disabled={busy} onClick={() => void run(async () => { const current = await client.readValue(selected.id, kind); setValue({ kind, text: JSON.stringify(current.value, null, 2) }) })}>{({config: '配置', state: '状态', credentials: '凭据引用'})[kind]}</Button>)}</div>{value && <div className="personal-form"><Textarea aria-label="插件配置 JSON" className="font-mono min-h-40" value={value.text} onChange={event => setValue({ ...value, text: event.target.value })} /><Button disabled={busy || selected.loaded} onClick={() => void run(async () => setPending({ preview: await client.previewValue(selected.id, value.kind, selected.revision, value.text), label: '保存配置' }))}>保存</Button></div>}<PluginDataPanel pluginId={selected.id} revision={selected.revision} installed={selected.installed} disabled={busy || !ready} onChanged={() => void refresh()} /></details>
      </>}
      <details className="personal-disclosure"><summary>文件与运行记录</summary><dl className="personal-properties"><dt>程序</dt><dd>{selected.source || '—'}</dd><dt>数据</dt><dd>{selected.data_dir}</dd></dl><Button variant="outline" disabled={busy} onClick={() => void run(async () => { const audit = await client.audit(selected.id); setResult(audit.items) })}>查看运行记录</Button>{result !== undefined && <pre className="personal-code">{JSON.stringify(result, null, 2)}</pre>}</details>
      <div className="personal-danger"><p>{selected.installed ? '卸载保留插件数据和配置。' : '清除会删除保留的数据，无法撤销。'}</p><Button variant="outline" disabled={busy || selected.loaded} onClick={() => action(selected, selected.installed ? 'uninstall' : 'purge', selected.installed ? '卸载插件' : '清除保留数据')}>{selected.installed ? '卸载插件' : '清除保留数据'}</Button></div>
      </section>}
    </div>
    <Dialog open={Boolean(source)} onOpenChange={open => { if (!open && !busy) setSource(undefined) }}><DialogContent><DialogHeader><DialogTitle>{source?.action === 'update' ? '更新插件' : '安装本地插件'}</DialogTitle><DialogDescription>填写插件 ZIP 或文件夹的完整路径，安装前会检查文件和依赖。</DialogDescription></DialogHeader><label className="personal-form">插件路径<Input aria-label="插件包路径" placeholder="例如 E:\\插件\\example.zip" value={source?.path || ''} onChange={event => setSource(current => current ? { ...current, path: event.target.value } : current)} /></label>{suggestedRoots.map(root => <button className="personal-path" key={root} onClick={() => setSource(current => current ? { ...current, path: root } : current)}><FolderOpen size={14} />{root}</button>)}<DialogFooter><Button disabled={busy || !source?.path.trim()} onClick={() => void run(async () => { if (!source) return; setPending({ preview: await client.previewSource(source.action, source.path, source.item?.revision || 0, source.item?.id), label: source.action === 'update' ? '更新插件' : '安装插件' }) })}><FileArchive />检查安装包</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(pending)} onOpenChange={open => { if (!open && !busy) setPending(undefined) }}><DialogContent className="max-w-xl max-h-[85vh] overflow-auto"><DialogHeader><DialogTitle>{pending?.label}</DialogTitle><DialogDescription>{pending?.preview.snapshot.manifest?.name || pending?.preview.snapshot.id} · {pending?.preview.snapshot.manifest?.version || selected?.version}</DialogDescription></DialogHeader>{pending?.preview.blocked.length ? <p className="personal-error" role="alert">暂时无法继续：{pending.preview.blocked.map(block => block.code).join('、')}</p> : <p className="text-sm text-muted-foreground">{pending?.preview.snapshot.action === 'uninstall' ? '卸载程序，保留数据、配置和状态。' : pending?.preview.snapshot.action === 'purge' ? '将永久清除该插件保留的数据。' : '确认后执行本次操作。'}</p>}<div>{pending?.preview.snapshot.delta&&<p className="text-sm text-muted-foreground">增量更新：复用 {pending.preview.snapshot.delta.reused_files} 项、更新 {pending.preview.snapshot.delta.changed_files} 项、移除 {pending.preview.snapshot.delta.removed_files} 项。{pending.preview.snapshot.delta.note}</p>}</div><details className="personal-disclosure"><summary>查看文件、依赖和参数</summary><pre className="personal-code">{JSON.stringify(pending?.preview.snapshot, null, 2)}</pre></details><DialogFooter><Button variant="outline" disabled={busy} onClick={() => setPending(undefined)}>取消</Button><Button disabled={busy || !pending?.preview.token || Boolean(pending?.preview.blocked.length)} onClick={commit}>确认{pending?.label}</Button></DialogFooter></DialogContent></Dialog>
  </div>
}
