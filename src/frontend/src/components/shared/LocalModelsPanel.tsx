import { useCallback, useEffect, useRef, useState } from 'react'
import { Download, MoreHorizontal, Plus, RefreshCw } from 'lucide-react'
import { toast } from 'sonner'
import { api, json } from '@/api/client'
import { useApp } from '@/context/AppContext'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { SpeechPackagesPanel } from '@/components/settings/EvolutionSettings'
import { ModelSettings } from './ModelSettings'
import './local-models.css'

type Service = { id: string; name: string; kind: 'ollama' | 'lmstudio' | 'openai'; base_url: string; revision: number; has_key: boolean }
type Model = { id: string; size: number | null; loaded: boolean | null; memory: number | null }
type Job = { id: string; service_id: string; model: string; status: string; phase: string; error: string; completed_bytes: number; total_bytes: number }
type Form = { id?: string; name: string; kind: Service['kind']; base_url: string; revision: number; has_key?: boolean; key: string; clear_key: boolean }
type Module = { id: string; revision: number }
const root = '/v11/local-models'
const names = { ollama: 'Ollama', lmstudio: 'LM Studio', openai: '其他兼容服务' }
const moduleNames: Record<string, string> = { companion: '消息', work: '工作', orchestrator: '任务调度', vision: '图片理解' }
const ongoing = (job: Job) => ['queued', 'downloading'].includes(job.status)
const size = (n: number | null) => n === null ? '' : n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(1)} GiB` : `${(n / 1024 ** 2).toFixed(0)} MiB`
const initial: Form = { name: 'Ollama', kind: 'ollama', base_url: 'http://127.0.0.1:11434', revision: 0, key: '', clear_key: false }

export function LocalModelsPanel() {
  const { connection, refreshModels, profiles } = useApp()
  const [services, setServices] = useState<Service[]>([]), [selected, setSelected] = useState('')
  const [models, setModels] = useState<Model[]>([]), [jobs, setJobs] = useState<Job[]>([])
  const [error, setError] = useState(''), [busy, setBusy] = useState(''), [loading, setLoading] = useState(false)
  const [tested, setTested] = useState(''), [query, setQuery] = useState(''), [addSignal, setAddSignal] = useState(0)
  const [form, setForm] = useState<Form>(), [pull, setPull] = useState(false), [downloadName, setDownloadName] = useState('')
  const [assignment, setAssignment] = useState<Model>(), [module, setModule] = useState('work'), [moduleList, setModuleList] = useState<Module[]>([]), [vision, setVision] = useState(false)
  const [removing, setRemoving] = useState(false), [deletion, setDeletion] = useState<{ token: string; model: string; size: number | null }>()
  const [modelDetail, setModelDetail] = useState<Model>()
  const [configuration, setConfiguration] = useState<Model>()
  const [configName, setConfigName] = useState('')
  const [configCaps, setConfigCaps] = useState<Array<'text' | 'vision'>>(['text'])
  const alive = useRef(true), gate = useRef(false), sequence = useRef(0), serviceSequence = useRef(0), jobSequence = useRef(0)
  const reads = useRef(new Set<AbortController>())
  const [nextCursor, setNextCursor] = useState('')
  const historyExpanded = useRef(false)
  const service = services.find(s => s.id === selected)
  const linkedProfile = (model: Model) => profiles.filter(profile => profile.local_service_id === selected && profile.model === model.id).at(-1)
  const openConfiguration = (model: Model) => { const profile = linkedProfile(model); setConfigName(profile?.name || (service?.name || '本地') + ' · ' + model.id); setConfigCaps(profile?.capabilities?.filter((cap): cap is 'text' | 'vision' => cap === 'text' || cap === 'vision') || ['text']); setModelDetail(undefined); setConfiguration(model) }
  const disabled = !!busy || connection !== 'connected'
  useEffect(() => { alive.current = true; return () => { alive.current = false; sequence.current++; serviceSequence.current++; jobSequence.current++; reads.current.forEach(c => c.abort()); reads.current.clear() } }, [])
  const queryApi = useCallback(async <T,>(path: string) => {
    const controller = new AbortController(); reads.current.add(controller)
    try { return await api<T>(path, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]) }) }
    finally { reads.current.delete(controller) }
  }, [])

  const listServices = useCallback(async () => {
    const current = ++serviceSequence.current
    const value = await queryApi<{ items: Service[] }>(root + '/services')
    if (!alive.current || current !== serviceSequence.current) return
    setServices(value.items)
    setSelected(old => value.items.some(i => i.id === old) ? old : value.items[0]?.id || '')
  }, [queryApi])
  useEffect(() => { if (connection === 'connected') void listServices().catch(e => { if (alive.current) setError(e.message) }); else { setServices([]); setModels([]) } }, [connection, listServices])

  const readModels = useCallback(async (sid: string) => {
    const current = ++sequence.current
    setLoading(true); setTested(''); setModels([])
    try {
      const value = await queryApi<{ items: Model[] }>(`${root}/services/${sid}/models`)
      if (alive.current && current === sequence.current) { setModels(value.items); setTested(sid); setError('') }
    } catch (e) { if (alive.current && current === sequence.current) setError((e as Error).message) }
    finally { if (alive.current && current === sequence.current) setLoading(false) }
  }, [queryApi])
  useEffect(() => {
    historyExpanded.current = false
    setModels([]); setTested(''); setError(''); setModelDetail(undefined); setConfiguration(undefined)
    if (selected && connection === 'connected') void readModels(selected)
    return () => { sequence.current++ }
  }, [selected, service?.revision, connection, readModels])

  const readJobs = useCallback(async (cursor = '') => {
    const current = ++jobSequence.current
    const value = await queryApi<{ items: Job[]; next_cursor: string }>(root + '/downloads?limit=30' + (cursor ? '&before=' + encodeURIComponent(cursor) : ''))
    if (alive.current && current === jobSequence.current) {
      if (cursor) historyExpanded.current = true
      setJobs(old => cursor ? [...new Map([...old, ...value.items].map(j => [j.id, j])).values()] : historyExpanded.current ? [...new Map([...value.items, ...old.filter(j => !value.items.some(n => n.id === j.id))].map(j => [j.id, j])).values()] : value.items)
      if (cursor || !historyExpanded.current) setNextCursor(value.next_cursor)
    }
    return value.items
  }, [queryApi])
  const hasActive = jobs.some(ongoing)
  useEffect(() => {
    if (connection !== 'connected') return
    let live = true; let timer: number | undefined
    const poll = async () => {
      try {
        const next = await readJobs()
        if (live) timer = window.setTimeout(() => void poll(), next.some(ongoing) ? 1500 : 15000)
        if (live && hasActive && !next.some(ongoing) && selected) void readModels(selected)
      } catch (e) { if (live) { setError((e as Error).message); timer = window.setTimeout(() => void poll(), 5000) } }
    }
    void poll()
    return () => { live = false; window.clearTimeout(timer) }
  }, [connection, hasActive, readJobs, readModels, selected])

  const run = async (id: string, task: () => Promise<void>) => {
    if (gate.current) return
    gate.current = true; setBusy(id); setError('')
    try { await task() } catch (e) { if (alive.current) setError((e as Error).message) }
    finally { gate.current = false; if (alive.current) setBusy('') }
  }
  const modelAction = (model: Model, action: 'load' | 'unload') => void run(action, async () => {
    await api(`${root}/services/${selected}/${action}`, { ...json('POST', { model: model.id, expected_revision: service!.revision }), signal: AbortSignal.timeout(190000) })
    if (alive.current) await readModels(selected)
  })
  const openAssignment = (model: Model) => void run('assignment', async () => {
    const value = await api<{ items: Module[] }>('/v6/modules')
    if (alive.current) { setModuleList(value.items); setModule('work'); setVision(Boolean(linkedProfile(model)?.capabilities?.includes('vision'))); setAssignment(model) }
  })
  const shownJobs = jobs.filter(j => j.service_id === selected)
  const filtered = models.filter(m => m.id.toLowerCase().includes(query.toLowerCase()))

  return <div className="local-models-panel">
<header className="management-section-title"><h2>本地模型</h2><div className="local-model-service-bar"><Select aria-label="本地模型服务" disabled={disabled || !services.length} value={selected} onChange={e => setSelected(e.target.value)}>{!services.length && <option value="">尚未添加服务</option>}{services.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</Select>
        <DropdownMenu><DropdownMenuTrigger asChild><Button size="sm" variant="outline" disabled={disabled} aria-label="添加本地模型"><Plus />添加</Button></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem onSelect={() => setForm({ ...initial })}>添加本地服务</DropdownMenuItem><DropdownMenuItem onSelect={() => setAddSignal(value => value + 1)}>添加自定义模型</DropdownMenuItem></DropdownMenuContent></DropdownMenu>
        {service && <DropdownMenu><DropdownMenuTrigger asChild><Button size="icon" variant="ghost" disabled={disabled} aria-label="本地服务操作"><MoreHorizontal /></Button></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem onSelect={() => setForm({ ...service, key: '', clear_key: false })}>编辑连接</DropdownMenuItem><DropdownMenuItem onSelect={() => setRemoving(true)}>移除连接</DropdownMenuItem></DropdownMenuContent></DropdownMenu>}
      </div></header>
    {error && <p className="personal-error" role="alert">{error}</p>}
    <div className="local-model-toolbar"><Input aria-label="搜索本地模型" placeholder="搜索模型" value={query} onChange={e => setQuery(e.target.value)} /><Button size="sm" variant="ghost" disabled={disabled || loading || !service} onClick={() => { if (service) void readModels(selected); void readJobs().catch(e => { if(alive.current) setError(e.message) }) }}><RefreshCw className={loading ? 'animate-spin' : ''} />刷新</Button>{service?.kind === 'ollama' && <Button size="sm" disabled={disabled} onClick={() => { setDownloadName(''); setPull(true) }}><Download />下载模型</Button>}</div>
    {connection !== 'connected' && <p className="local-model-note">连接零汐本地服务后查看模型。</p>}
    {connection === 'connected' && <div className="local-model-list" aria-busy={loading}>
      {service && filtered.map(m => { const profile = linkedProfile(m); return <article className="local-model-row" key={selected + ':' + m.id}><div><h3>{m.id}</h3><p>{m.loaded === null ? service.name : m.loaded ? '已加载' : '未加载'}{profile ? ' · 已配置' : ''}</p></div><div className="local-model-row-actions">{service.kind==='ollama'&&<Button size="sm" disabled={disabled} onClick={()=>modelAction(m,m.loaded?'unload':'load')}>{m.loaded?'释放':'加载'}</Button>}<Button size="sm" variant="outline" onClick={() => setModelDetail(m)}>详情</Button></div></article> })}
      <ModelSettings section="profiles" source="local" embedded externalQuery={query} addSignal={addSignal} />
      <SpeechPackagesPanel query={query} />
    </div>}
    {!!shownJobs.length && <section className="local-model-downloads" aria-label="模型下载"><h3>下载</h3>{shownJobs.map(j => <div className="local-model-job" key={j.id}><div><strong>{j.model}</strong><span>{j.error || j.phase}</span></div>{ongoing(j) && <><progress aria-label={j.model + ' 下载进度'} {...(j.total_bytes ? { max: j.total_bytes, value: j.completed_bytes } : {})} /><div><small>{j.total_bytes ? `${size(j.completed_bytes)} / ${size(j.total_bytes)}` : '正在获取下载信息'}</small><Button variant="ghost" size="sm" disabled={disabled} onClick={() => void run('cancel', async () => { await api(`${root}/downloads/${j.id}/cancel`, json('POST')); await readJobs() })}>取消下载</Button></div></>}</div>)}{nextCursor && <Button size="sm" variant="ghost" disabled={disabled} onClick={()=>void run('history',async()=>{await readJobs(nextCursor)})}>更早的下载</Button>}</section>}
    <Dialog open={!!modelDetail} onOpenChange={open => { if (!open) setModelDetail(undefined) }}><DialogContent className="local-model-detail-dialog"><DialogHeader><DialogTitle>{modelDetail?.id}</DialogTitle><DialogDescription>{service?.name} · {tested === selected ? '已连接' : '未连接'}</DialogDescription></DialogHeader>{modelDetail && service && <div className="local-model-detail-body"><dl><div><dt>服务</dt><dd>{names[service.kind]}</dd></div><div><dt>地址</dt><dd>{service.base_url}</dd></div><div><dt>能力</dt><dd>{linkedProfile(modelDetail)?.capabilities?.map(cap => cap === 'vision' ? '图片理解' : cap === 'text' ? '文字' : cap).join(' · ') || '待配置'}</dd></div><div><dt>文件大小</dt><dd>{size(modelDetail.size) || '未提供'}</dd></div><div><dt>加载状态</dt><dd>{modelDetail.loaded === null ? '未提供' : modelDetail.loaded ? '已加载' : '未加载'}</dd></div><div><dt>显存</dt><dd>{size(modelDetail.memory) || '未提供'}</dd></div></dl><div className="local-model-detail-actions"><Button size="sm" variant="outline" disabled={disabled} onClick={() => openConfiguration(modelDetail)}>配置能力</Button><Button size="sm" disabled={disabled} onClick={() => { setModelDetail(undefined); openAssignment(modelDetail) }}>分配用途</Button></div>{service.kind === 'ollama' && <details className="personal-disclosure"><summary>更多管理</summary><Button size="sm" variant="outline" disabled={disabled} onClick={() => void run('delete-preview', async () => { const value = await api<NonNullable<typeof deletion>>(`${root}/services/${selected}/delete-preview`, json('POST', { model: modelDetail.id, expected_revision: service.revision })); if (alive.current) { setModelDetail(undefined); setDeletion(value) } })}>删除模型…</Button></details>}</div>}</DialogContent></Dialog>
    <Dialog open={!!configuration} onOpenChange={open => { if (!open && !busy) setConfiguration(undefined) }}><DialogContent className="local-model-dialog"><DialogHeader><DialogTitle>配置模型能力</DialogTitle><DialogDescription>{configuration?.id} · {service?.name}</DialogDescription></DialogHeader><label className="local-model-config-name">配置名称<Input aria-label="本地模型配置名称" maxLength={100} value={configName} disabled={disabled} onChange={e => setConfigName(e.target.value)} /></label><fieldset className="local-model-config-capabilities"><legend>支持的能力</legend>{([['text','文字'],['vision','图片理解']] as const).map(([cap,label]) => <label key={cap}><input type="checkbox" checked={configCaps.includes(cap)} disabled={disabled} onChange={e => setConfigCaps(old => e.target.checked ? [...old,cap] : old.filter(value => value !== cap))}/>{label}</label>)}</fieldset><p className="local-model-config-note">能力由你声明，实际调用仍取决于模型支持。</p>{error && configuration && <p className="personal-error" role="alert">{error}</p>}<DialogFooter><Button variant="ghost" disabled={!!busy} onClick={() => setConfiguration(undefined)}>取消</Button><Button disabled={disabled || !configName.trim() || !configCaps.length} onClick={() => void run('configure', async () => { const current=linkedProfile(configuration!); await api(root + '/services/' + selected + '/configure',json('POST',{model:configuration!.id,expected_revision:service!.revision,name:configName.trim(),capabilities:configCaps,profile_id:current?.id || null,profile_revision:current?.revision || 0})); await refreshModels(); if(alive.current){setConfiguration(undefined);toast.success('模型能力已保存')} })}>保存能力</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={!!form} onOpenChange={open => { if (!open && !busy) setForm(undefined) }}><DialogContent className="local-model-dialog"><DialogHeader><DialogTitle>{form?.id ? '编辑连接' : '添加本地服务'}</DialogTitle><DialogDescription>连接已在本机启动的模型服务。修改地址或凭据会同步已关联的模型。</DialogDescription></DialogHeader>{form && <form id="local-model-service-form" onSubmit={e => { e.preventDefault(); void run('save', async () => {
      const value = await api<Service>(root + '/services' + (form.id ? '/' + form.id : ''), json(form.id ? 'PUT' : 'POST', { name: form.name, kind: form.kind, base_url: form.base_url, expected_revision: form.revision, clear_key: form.clear_key, ...(form.key ? { api_key: form.key } : {}) }))
      if (alive.current) { setForm(undefined); await listServices(); await refreshModels(); setSelected(value.id); toast.success('连接已保存') }
    }) }}>
      <label>服务类型<Select aria-label="本地服务类型" disabled={!!busy} value={form.kind} onChange={e => { const kind = e.target.value as Service['kind']; setForm({ ...form, kind, name: names[kind], base_url: kind === 'ollama' ? 'http://127.0.0.1:11434' : 'http://127.0.0.1:1234' }) }}>{Object.entries(names).map(([id, n]) => <option key={id} value={id}>{n}</option>)}</Select></label>
      <label>名称<Input required maxLength={80} aria-label="本地服务名称" disabled={!!busy} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
      <label>地址<Input required aria-label="本地服务地址" disabled={!!busy} value={form.base_url} onChange={e => setForm({ ...form, base_url: e.target.value })} /></label>
      <label>访问凭据 <small>可选{form.has_key ? ' · 已保存，留空保留' : ''}</small><Input type="password" autoComplete="new-password" aria-label="本地服务凭据" disabled={!!busy || form.clear_key} value={form.key} onChange={e => setForm({ ...form, key: e.target.value })} /></label>
      {form.has_key && <label className="local-model-check"><input type="checkbox" checked={form.clear_key} disabled={!!busy} onChange={e => setForm({ ...form, clear_key: e.target.checked, key: '' })} />移除已保存的凭据</label>}
    </form>}{error && form && <p className="personal-error" role="alert">{error}</p>}<DialogFooter><Button variant="ghost" disabled={!!busy} onClick={() => setForm(undefined)}>取消</Button><Button type="submit" form="local-model-service-form" disabled={!!busy}>{busy ? '保存中…' : '保存连接'}</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={pull} onOpenChange={open => { if (!busy) setPull(open) }}><DialogContent className="local-model-dialog"><DialogHeader><DialogTitle>下载模型</DialogTitle><DialogDescription>输入 Ollama 模型名，下载到该服务的模型目录。</DialogDescription></DialogHeader><form id="local-model-download-form" onSubmit={e => { e.preventDefault(); void run('download', async () => { await api(`${root}/services/${selected}/download`, json('POST', { model: downloadName.trim(), expected_revision: service!.revision })); await readJobs(); if (alive.current) setPull(false) }) }}><label>模型名称<Input autoFocus required maxLength={200} aria-label="下载模型名称" placeholder="例如 qwen3:8b" value={downloadName} onChange={e => setDownloadName(e.target.value)} /></label></form>{error && <p className="personal-error" role="alert">{error}</p>}<DialogFooter><Button variant="ghost" disabled={!!busy} onClick={() => setPull(false)}>取消</Button><Button type="submit" form="local-model-download-form" disabled={!!busy || !downloadName.trim()}>开始下载</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={!!assignment} onOpenChange={open => { if (!busy && !open) setAssignment(undefined) }}><DialogContent className="local-model-dialog"><DialogHeader><DialogTitle>分配模型</DialogTitle><DialogDescription>{assignment?.id}</DialogDescription></DialogHeader><label>使用模块<Select aria-label="本地模型分配模块" value={module} disabled={!!busy} onChange={e => { setModule(e.target.value); setVision(false) }}>{Object.entries(moduleNames).map(([id, n]) => <option key={id} value={id}>{n}</option>)}</Select></label>{module === 'vision' && !linkedProfile(assignment!) && <label className="local-model-check"><input type="checkbox" checked={vision} onChange={e => setVision(e.target.checked)} />确认此模型支持图片输入</label>}{module === 'vision' && linkedProfile(assignment!) && !linkedProfile(assignment!)?.capabilities?.includes('vision') && <p className="local-model-note">请先在模型详情中配置图片理解能力。</p>}<p className="local-model-note">将替换此功能的默认模型。</p>{error && <p className="personal-error" role="alert">{error}</p>}<DialogFooter><Button variant="ghost" disabled={!!busy} onClick={() => setAssignment(undefined)}>取消</Button><Button disabled={!!busy || module === 'vision' && (linkedProfile(assignment!) ? !linkedProfile(assignment!)?.capabilities?.includes('vision') : !vision)} onClick={() => void run('assign', async () => {
      await api(`${root}/services/${selected}/assign`, json('POST', { model: assignment!.id, expected_revision: service!.revision, module, module_revision: moduleList.find(m => m.id === module)?.revision || 0, vision })); await refreshModels(); if (alive.current) { setAssignment(undefined); toast.success('已分配到' + moduleNames[module]) }
    })}>确认分配</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={removing} onOpenChange={open => { if (!busy) setRemoving(open) }}><DialogContent className="local-model-dialog"><DialogHeader><DialogTitle>移除此连接？</DialogTitle><DialogDescription>保留已下载模型和分配，之后可在模型库编辑连接。</DialogDescription></DialogHeader>{error && <p role="alert" className="personal-error">{error}</p>}<DialogFooter><Button variant="ghost" disabled={!!busy} onClick={() => setRemoving(false)}>取消</Button><Button disabled={!!busy} onClick={() => void run('remove', async () => { await api(`${root}/services/${selected}?expected_revision=${service!.revision}`, json('DELETE')); if (alive.current) { setRemoving(false); await listServices(); await refreshModels() } })}>移除连接</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={!!deletion} onOpenChange={open => { if (!busy && !open) setDeletion(undefined) }}><DialogContent className="local-model-dialog"><DialogHeader><DialogTitle>删除模型？</DialogTitle><DialogDescription>{deletion?.model}{deletion?.size !== null && deletion?.size !== undefined ? ` · ${size(deletion.size)}` : ''}。删除后需要重新下载。</DialogDescription></DialogHeader>{error && <p className="personal-error" role="alert">{error}</p>}<DialogFooter><Button variant="ghost" disabled={!!busy} onClick={() => setDeletion(undefined)}>取消</Button><Button disabled={!!busy} onClick={() => void run('delete', async () => { await api(`${root}/services/${selected}/delete`, json('POST', { token: deletion!.token })); if (alive.current) { setDeletion(undefined); await readModels(selected); toast.success('模型已删除') } })}>确认删除</Button></DialogFooter></DialogContent></Dialog>
  </div>
}
