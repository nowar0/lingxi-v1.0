import {ModelTrial} from './ModelTrial'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '@/components/ui/dropdown-menu'
import {GenerationParameters} from '@/components/settings/ManagementExtras'
import { Select } from '@/components/ui/select'
import { ModuleReasoningDialog } from './ReasoningSettings'
import { KeyModelPicker } from './KeyLibrary'
import { useState,useEffect } from 'react'
import { Bot, KeyRound, MoreHorizontal, LoaderCircle, Plus, Trash2, SlidersHorizontal } from 'lucide-react'
import { toast } from 'sonner'
import { api, json } from '@/api/client'
import { lingxi } from '@/api/lingxi'
import { useApp } from '@/context/AppContext'
import type { ModelCapability, ModelModule, ModelProfile } from '@/types'
import { capabilityNames, nativeVideoInputLabel, type Protocol } from '@/lib/modelCatalog'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { SettingRow } from './SettingRow'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ModelAdvancedDialog } from './ModelAdvancedDialog'
import './model-center.css'

const capabilities = capabilityNames
const names: Record<string, string> = { companion: '消息', work: '工作', orchestrator: '任务调度', vision: '视觉', image: '生图', stt: '语音识别', tts: '语音合成' }
const selectClass = 'h-9 max-w-52 rounded-lg border border-border bg-input px-2 text-xs'

function ProfileUsage({id}:{id:string}){const {modules}=useApp();const [error,setError]=useState(''),[retry,setRetry]=useState(0);const [value,setValue]=useState<{modules:string[];references:Array<{kind:string;id:string}>}>();useEffect(()=>{const c=new AbortController();void api<typeof value>('/v6/manage/model-usage/'+id,{signal:c.signal}).then(v=>{setValue(v);setError('')}).catch(e=>{if(!c.signal.aborted)setError(e.message)});return()=>c.abort()},[id,modules,retry]);return <small>{error&&<button title={error} onClick={()=>setRetry(v=>v+1)}>引用读取失败，重试</button>}{value?(value.modules.length||value.references.length?`功能 ${value.modules.length} · 会话与配置引用 ${value.references.length}`:'暂无引用'):error?'':'正在核对引用…'}</small>}
export function ModelSettings({ section = 'modules', source = 'cloud', embedded = false, externalQuery = '', addSignal = 0 }: { section?: 'modules' | 'profiles'; source?: 'cloud' | 'local'; embedded?: boolean; externalQuery?: string; addSignal?: number }) {
  const { modules, profiles, refreshModels, connection } = useApp()
  useEffect(()=>{if(connection==='connected')void refreshModels().catch(error=>toast.error('模型配置读取失败',{description:error.message}))},[connection,refreshModels])
  const [modelTab]=useState(section)
  const [open, setOpen] = useState(false)
  const [reasoningFor, setReasoningFor] = useState<ModelModule>()
  const [editing, setEditing] = useState<ModelProfile>(); const [details,setDetails]=useState<ModelProfile>(); const [parameters, setParameters] = useState<ModelModule>(); const [query,setQuery]=useState(''); const [filter,setFilter]=useState('all')
  const [trial,setTrial]=useState<ModelModule>()
  const [busy, setBusy] = useState('')
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [model, setModel] = useState('')
  const [key, setKey] = useState('')
  const [local, setLocal] = useState(false)
  const [caps, setCaps] = useState<ModelCapability[]>(['text'])
  const [protocol, setProtocol] = useState<Protocol>('openai')
  const [videoProfile, setVideoProfile] = useState<ModelProfile>()
  const [nativeVideo, setNativeVideo] = useState(false)
  const [advanced,setAdvanced]=useState<{kind:'module'|'profile';id:string}>()
  const [assigning, setAssigning] = useState<ModelModule>(); const [assignmentSource,setAssignmentSource]=useState('library')
  const [profileId, setProfileId] = useState('')
  const [voice, setVoice] = useState('')
  const [deleting, setDeleting] = useState<ModelProfile>()
  const [replacing, setReplacing] = useState<ModelProfile>()
  const ready = connection === 'connected'
  const search = embedded ? externalQuery : query
  const visibleProfiles = profiles.filter(profile => profile.local === (source === 'local') && (source !== 'local' || !profile.local_service_id) && `${profile.name} ${profile.model} ${profile.base_url}`.toLowerCase().includes(search.trim().toLowerCase()) && (embedded || filter === 'all' || profile.capabilities?.includes(filter as ModelCapability)))
  const openCreate = () => { setName(''); setUrl(''); setModel(''); setLocal(source === 'local'); setKey(''); setCaps(['text']); setProtocol('openai'); setOpen(true) }
  useEffect(() => { if (embedded && addSignal > 0) openCreate() }, [addSignal, embedded])
  const profileRows = visibleProfiles.map(profile => <article className={embedded ? 'model-center-profile local-model-row' : 'model-center-profile'} key={profile.id}><div><h3>{profile.name}</h3><p>{profile.model}{embedded ? ' · 自定义' : ''}</p></div><Button variant="outline" size="sm" onClick={() => setDetails(profile)}>详情</Button></article>)
  const run = async (id: string, task: () => Promise<void>) => {
    if (busy || !ready) return
    setBusy(id)
    try { await task() } catch (error) { toast.error('操作未完成', { description: (error as Error).message }) }
    finally { setBusy('') }
  }
  const create = () => run('create', async () => {
    await api('/v6/model-profiles', json('POST', { name, base_url: url.trim(), model: model.trim(), api_key: key, local, protocol, provider_id: protocol === 'gemini' ? 'gemini' : protocol === 'anthropic' ? 'anthropic' : 'custom', capabilities: caps }))
    setKey(''); setOpen(false); setName(''); setUrl(''); setModel(''); setCaps(['text']); setProtocol('openai')
    await refreshModels(); toast.success('模型配置已保存')
  })
  const saveVideo = () => run('video', async () => {
    if (!videoProfile) return
    await api(`/v6/model-profiles/${videoProfile.id}/video-input`, json('PUT', { expected_revision: videoProfile.revision, native_video_input: nativeVideo }))
    await refreshModels(); setVideoProfile(undefined); toast.success('原生视频输入声明已保存，尚未实测')
  })
  const assign = () => run('assign', async () => {
    if (!assigning) return
    await api(`/v6/model-assignments/${assigning.id}`, json('PUT', { profile_id: profileId, expected_revision: assigning.revision, enabled: true, voice }))
    await refreshModels(); setAssigning(undefined); toast.success('模块分配已保存')
  })
  const test = (item: ModelModule) => run(item.id, async () => {
    const result = await lingxi.testModule(item.id)
    toast[result.connected ? 'success' : 'error'](result.connected ? '服务连接可达' : '连接失败', { description: result.note || '连接可达不代表模型调用或能力已经验证。' })
  })
  const toggle = (item: ModelModule, enabled: boolean) => run(item.id, async () => {
    const payload = Object.fromEntries(['base_url', 'model', 'credential_ref', 'local', 'capabilities', 'voice', 'difficulty', 'timeout', 'temperature', 'top_p','stop','max_output_tokens','profile_id', 'reasoning_effort', 'protocol', 'provider_id'].map((field) => [field, (item as unknown as Record<string, unknown>)[field]]))
    await api(`/v6/modules/${item.id}`, json('PUT', { ...payload, enabled, expected_revision: item.revision }))
    await refreshModels()
  })
  return <div className="model-center-panel">{trial&&<ModelTrial module={trial} onClose={()=>setTrial(undefined)}/>}{advanced&&<GenerationParameters {...advanced} onClose={()=>setAdvanced(undefined)} onSaved={refreshModels}/>}<Tabs value={modelTab} className="model-settings space-y-4">

    <TabsContent value="modules" className="mt-0 space-y-4">    <Card className="settings-section"><div className="settings-section-heading"><div><h2>功能分配</h2><p>选择各项功能默认使用的模型。单独设置的会话保持原选择。</p></div></div><div className="p-5">{!modules.length && <p className="py-4 text-center text-xs text-muted-foreground">暂无模型模块，请检查本地服务连接。</p>}{modules.map((item) => <SettingRow key={item.id} className="model-module-row" title={names[item.id] || item.id} description={`${item.model || '未分配模型'} · 需要 ${capabilities[item.required_capability as keyof typeof capabilities] || '文字'}`}><div className="flex flex-wrap items-center justify-end gap-2"><Switch aria-label={`启用${names[item.id] || item.id}`} checked={item.enabled} disabled={!ready || Boolean(busy) || !item.model} onCheckedChange={(enabled) => void toggle(item, enabled)} /><Button size="sm" variant="outline" disabled={!ready || Boolean(busy)} onClick={() => { setAssigning(item); setAssignmentSource('library'); setProfileId(item.profile_id || ''); setVoice(item.voice || '') }}>分配</Button><DropdownMenu><DropdownMenuTrigger asChild><Button size="icon" variant="ghost" aria-label={`${names[item.id] || item.id}模型操作`} disabled={!ready || Boolean(busy)}><MoreHorizontal /></Button></DropdownMenuTrigger><DropdownMenuContent align="end">{['text', 'vision'].includes(item.required_capability || '') && <DropdownMenuItem aria-label={`${names[item.id] || item.id}思考强度`} disabled={!ready || !item.model || Boolean(busy)} onSelect={() => setReasoningFor(item)}><SlidersHorizontal />思考强度</DropdownMenuItem>}<DropdownMenuItem disabled={!ready || !item.model || Boolean(busy)} onSelect={() => void test(item)}>{busy === item.id ? <LoaderCircle className="animate-spin" /> : <Bot />}测试连接</DropdownMenuItem><DropdownMenuItem disabled={!item.model} onSelect={()=>setParameters(item)}>生成参数</DropdownMenuItem>{item.capabilities?.includes('text')&&<DropdownMenuItem disabled={!ready||!!busy} onSelect={()=>setTrial(item)}>试用文字模型</DropdownMenuItem>}</DropdownMenuContent></DropdownMenu></div></SettingRow>)}</div></Card></TabsContent>
<TabsContent value="profiles" className="mt-0">{embedded ? <div className="model-center-library local-model-custom-list">{profileRows}</div> : <section className="settings-section"><header className="settings-section-heading"><div><h2>{source==='local'?'自定义模型':'云端模型'}</h2></div><Button size="sm" disabled={!ready||!!busy} onClick={openCreate}><Plus/>{source==='local'?'添加自定义模型':'添加模型'}</Button></header>
<div className="personal-toolbar"><label className="personal-search"><KeyRound size={15}/><input aria-label="搜索模型配置" placeholder="搜索名称、模型或服务" value={query} onChange={e=>setQuery(e.target.value)}/></label><Select aria-label="筛选模型能力" value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">全部能力</option>{Object.entries(capabilities).map(([id,name])=><option key={id} value={id}>{name}</option>)}</Select></div>
<div className="model-center-library">{profileRows}{!visibleProfiles.length&&<div className="personal-empty"><p>{!ready?'连接本地服务后查看模型配置。':query.trim()||filter!=='all'?'没有匹配的模型，请调整搜索或筛选。':source==='local'?'暂无自定义模型。':'暂无云端模型配置，可从模型服务读取或添加配置。'}</p></div>}</div></section>}</TabsContent>

  </Tabs>
    <Dialog open={Boolean(details)} onOpenChange={value=>{if(!value)setDetails(undefined)}}><DialogContent className="model-profile-detail-dialog"><DialogHeader><DialogTitle>{details?.name}</DialogTitle><DialogDescription>模型详情</DialogDescription></DialogHeader>{details&&<div className="model-profile-detail"><dl><div><dt>模型</dt><dd>{details.model}</dd></div><div><dt>接入地址</dt><dd>{details.base_url}</dd></div><div><dt>能力</dt><dd>{details.capabilities?.map(capability=>capabilities[capability]).join(' · ')||'未声明'}</dd></div><div><dt>接入方式</dt><dd>{details.local?'本机免 Key API':'API 服务'}</dd></div></dl><ProfileUsage id={details.id}/></div>}<DialogFooter><Button variant="outline" onClick={()=>setDetails(undefined)}>关闭</Button><Button disabled={!ready||!!busy} onClick={()=>{if(details){setEditing(details);setDetails(undefined)}}}>编辑配置</Button></DialogFooter>{details&&<div className="model-profile-detail-actions"><Button size="sm" variant="ghost" onClick={()=>{setVideoProfile(details);setNativeVideo(details.capabilities?.includes('video')||false);setDetails(undefined)}}>视频输入声明</Button>{!details.local&&!details.key_id&&<Button size="sm" variant="ghost" onClick={()=>{setKey('');setReplacing(details);setDetails(undefined)}}>更换 Key</Button>}<Button size="sm" variant="ghost" onClick={()=>{setDeleting(details);setDetails(undefined)}}>删除配置…</Button></div>}</DialogContent></Dialog>
    <Dialog open={open} onOpenChange={(value) => { if (!busy) { setOpen(value); if (!value) setKey('') } }}><DialogContent><DialogHeader><DialogTitle>{source==='local'?'添加本地模型':'添加云端 API 模型'}</DialogTitle><DialogDescription>填写模型连接与能力；保存后可在详情中检查配置。</DialogDescription></DialogHeader><div className="space-y-3"><label className="ui-form-field"><span>配置名称</span><Input aria-label="配置名称" placeholder="配置名称" value={name} maxLength={100} onChange={(event) => setName(event.target.value)} /></label><label className="ui-form-field"><span>模型服务地址</span><Input aria-label="模型服务地址" placeholder={local?'本机模型服务地址':'提供方的 API 基础地址'} value={url} onChange={(event) => setUrl(event.target.value)} /></label><label className="flex items-center justify-between text-xs">API 协议<Select aria-label="模型配置 API 协议" className={selectClass} value={protocol} onChange={(event) => setProtocol(event.target.value as Protocol)}><option value="openai">OpenAI 兼容</option><option value="anthropic">Anthropic Messages</option><option value="gemini">Gemini（原生路由使用 generateContent）</option></Select></label><label className="ui-form-field"><span>模型名称</span><Input aria-label="模型名称" placeholder="模型名称" value={model} onChange={(event) => setModel(event.target.value)} /></label>{!local && <label className="ui-form-field"><span>API Key</span><Input aria-label="API Key" placeholder="API Key" type="password" autoComplete="off" value={key} onChange={(event) => setKey(event.target.value)} /></label>}<div className="flex flex-wrap gap-3 text-xs">{Object.entries(capabilities).map(([value, label]) => { const id = value as ModelCapability; return <label className="flex items-center gap-1.5" key={id}><input type="checkbox" checked={caps.includes(id)} onChange={(event) => setCaps((current) => event.target.checked ? [...current, id] : current.filter((capability) => capability !== id))} />{label}</label> })}</div><p className="text-xs text-muted-foreground">能力声明不会自动验证。原生视频输入仅用于支持视频理解的 Gemini 模型。</p></div><DialogFooter><Button disabled={Boolean(busy) || !name.trim() || !url.trim() || !model.trim() || !caps.length || (!local && !key.trim())} onClick={() => void create()}>保存配置</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(assigning)} onOpenChange={value=>{if(!value&&!busy)setAssigning(undefined)}}><DialogContent className="model-center-dialog"><DialogHeader><DialogTitle>选择{assigning?names[assigning.id]||assigning.id:''}模型</DialogTitle><DialogDescription>默认用于此功能，单独设置的会话保持原选择。</DialogDescription></DialogHeader><Tabs value={assignmentSource} onValueChange={setAssignmentSource}><TabsList><TabsTrigger value="library">模型库</TabsTrigger><TabsTrigger value="service">从服务选择</TabsTrigger></TabsList><TabsContent value="library" className="space-y-4"><label className="ui-form-field"><span>模型配置</span><Select aria-label="模块模型配置" value={profileId} onChange={e=>setProfileId(e.target.value)}><option value="">选择兼容的模型</option>{profiles.filter(p=>p.capabilities?.includes(assigning?.required_capability||'text')).map(p=><option key={p.id} value={p.id}>{p.name} · {p.model}</option>)}</Select></label>{assigning?.id==='tts'&&<label className="ui-form-field"><span>默认音色编号</span><Input aria-label="默认音色编号" value={voice} onChange={e=>setVoice(e.target.value)}/></label>}<Button disabled={!profileId||!!busy} onClick={()=>void assign()}>保存分配</Button></TabsContent><TabsContent value="service">{assigning&&<KeyModelPicker key={assigning.id} module={assigning} onAssigned={async()=>{await refreshModels();setAssigning(undefined)}}/>}</TabsContent></Tabs></DialogContent></Dialog>
    <Dialog open={Boolean(videoProfile)} onOpenChange={(value) => { if (!value && !busy) setVideoProfile(undefined) }}><DialogContent><DialogHeader><DialogTitle>「{videoProfile?.name}」视频输入声明</DialogTitle><DialogDescription>确认模型支持视频后开启，保存后可测试。</DialogDescription></DialogHeader><label className="flex items-start gap-2 text-xs leading-5"><input type="checkbox" checked={nativeVideo} disabled={Boolean(busy)} onChange={(event) => setNativeVideo(event.target.checked)} />{nativeVideoInputLabel}</label><p className="text-xs text-muted-foreground">仅适用于支持视频输入的 Gemini 模型。</p><details className="personal-disclosure"><summary>支持范围</summary><p className="text-xs text-muted-foreground">支持 MP4 / WebM，需可用 Key 和文字能力。当前协议：{videoProfile?.protocol || 'openai'}。此设置用于视频理解，不用于视频生成。</p></details><DialogFooter><Button variant="outline" disabled={Boolean(busy)} onClick={() => setVideoProfile(undefined)}>取消</Button><Button disabled={Boolean(busy)} onClick={() => void saveVideo()}>保存声明</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(deleting)} onOpenChange={(value) => { if (!value && !busy) setDeleting(undefined) }}><DialogContent><DialogHeader><DialogTitle>删除模型配置</DialogTitle><DialogDescription>删除「{deleting?.name}」及其独立凭据。仍被模块使用的配置会拒绝删除。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" onClick={() => setDeleting(undefined)}>取消</Button><Button disabled={Boolean(busy)} onClick={() => void run('delete', async () => { if (!deleting) return; await api(`/v6/model-profiles/${deleting.id}?expected_revision=${deleting.revision}`, json('DELETE')); await refreshModels(); setDeleting(undefined) })}>确认删除</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(replacing)} onOpenChange={(value) => { if (!value && !busy) { setReplacing(undefined); setKey('') } }}><DialogContent><DialogHeader><DialogTitle>更换「{replacing?.name}」的 Key</DialogTitle></DialogHeader><label className="ui-form-field"><span>替换 API Key</span><Input aria-label="替换 API Key" type="password" autoComplete="off" value={key} onChange={(event) => setKey(event.target.value)} /></label><DialogFooter><Button disabled={!key.trim() || Boolean(busy)} onClick={() => void run('key', async () => { if (!replacing) return; await api(`/v6/model-profiles/${replacing.id}/key`, json('PUT', { api_key: key })); setKey(''); setReplacing(undefined); toast.success('Key 已交给系统凭据管理器') })}>保存 Key</Button></DialogFooter></DialogContent></Dialog>
<ModuleReasoningDialog key={reasoningFor?.id || "closed"} module={reasoningFor} onClose={() => setReasoningFor(undefined)} onSaved={async (saved) => { setReasoningFor(saved); await refreshModels() }} />{(editing||parameters)&&<ModelAdvancedDialog key={editing?.id||parameters?.id} profile={editing} module={parameters} onClose={()=>{setEditing(undefined);setParameters(undefined)}} onSaved={refreshModels}/>}
  </div>
}
