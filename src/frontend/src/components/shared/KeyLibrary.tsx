import './key-library-catalog.css'
import {useApp} from '@/context/AppContext'
import { Select } from '@/components/ui/select'
import { ProviderConfigDialog } from './ProviderConfigDialog'
import { matchesCatalogModel, capabilityNames, capabilitySourceNames, modalityNames, nativeVideoInputLabel, type StoredKey } from '@/lib/modelCatalog'
import { useEffect, useState } from 'react'
import { KeyRound, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, json } from '@/api/client'
import type { ModelModule } from '@/types'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'

const names = capabilityNames
const selectClass = 'h-9 w-full rounded-lg border border-border bg-input px-3 text-xs'
const loadKeys = () => api<{ items: StoredKey[] }>('/v6/model-keys')

export function KeyLibrary({ ready }: { ready: boolean }) {
  const {refreshModels,modules}=useApp()
  const [items, setItems] = useState<StoredKey[]>([])
  const [focusedKey,setFocusedKey]=useState('')
  const focus=items.some(item=>item.id===focusedKey)?focusedKey:items[0]?.id
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<StoredKey>()
  const [selected, setSelected] = useState<StoredKey>()
  const [replacing, setReplacing] = useState<StoredKey>()
  const [deleting, setDeleting] = useState<StoredKey>()
  const [key, setKey] = useState('')
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const reload = async () => {setItems((await loadKeys()).items);await refreshModels()}
  useEffect(() => { if (ready) void reload().catch((error) => toast.error('Key 库读取失败', { description: error.message })) }, [ready])
  const run = async (task: () => Promise<void>) => {
    if (busy || !ready) return
    setBusy(true)
    try { await task();try{await reload()}catch{toast.warning('已完成，列表刷新失败，请重新打开连接中心')} }
    catch (error) { toast.error('Key 操作未完成', { description: (error as Error).message }) }
    finally { setBusy(false) }
  }
  const announce = (item: StoredKey) => toast[item.connected ? 'success' : 'info'](item.connected ? `已识别 ${item.models.length} 个模型` : 'Key 已保存，模型清单尚未确认', { description: item.note })
  return <><Card className="settings-section key-library-section"><div className="settings-section-heading"><div><h2>模型服务</h2><p>保存连接与 Key，识别可用模型。</p></div><div className="key-library-heading-actions">{items.length>0&&<label className="key-library-sidebar"><span>当前服务</span><Select aria-label="选择模型服务" value={focus||''} onChange={event=>setFocusedKey(event.target.value)}>{items.map(item=><option key={item.id} value={item.id}>{item.name} · {item.models.length} 个模型</option>)}</Select></label>}<Button size="sm" disabled={!ready || busy} onClick={() => { setKey(''); setAdding(true) }}><Plus />添加服务</Button></div></div><div className={`key-library-layout${items.length ? '' : ' is-empty'}`}><div className="key-library-detail">{!items.length && <p className="py-4 text-xs text-muted-foreground">添加模型服务，读取可用模型。</p>}{items.filter(item=>item.id===focus).map((item) => <div key={item.id} className="model-profile flex-wrap"><div className="service-icon"><KeyRound /></div><div className="min-w-0 flex-1"><div className="text-xs font-semibold">{item.name}</div><p className="mt-1 break-all text-xs text-muted-foreground">{item.base_url}</p><p className="mt-1 text-xs text-muted-foreground">{item.connected ? `${item.models.length} 个模型 · 能力待实际调用验证` : item.note || '尚未识别'}{item.discovered_at && ` · ${new Date(item.discovered_at).toLocaleString()}`}</p></div><div className="flex flex-wrap items-center gap-1"><Button size="xs" variant="outline" disabled={busy} onClick={() => void run(async () => { const value = await api<StoredKey>(`/v6/model-keys/${item.id}/refresh`, json('POST', { expected_revision: item.revision })); announce(value) })}><RefreshCw />识别</Button><Button size="xs" variant="ghost" disabled={busy} onClick={() => setEditing(item)}>编辑</Button><Button size="xs" variant="ghost" onClick={() => { setSelected(item); setQuery('') }}>模型清单</Button><Button size="xs" variant="ghost" disabled={busy} onClick={() => { setKey(''); setReplacing(item) }}>更换</Button>{item.owned && <Button size="icon" variant="ghost" className="size-7" aria-label={`删除 Key ${item.name}`} disabled={busy} onClick={() => setDeleting(item)}><Trash2 /></Button>}</div></div>)}</div></div></Card>
    {(adding || editing) && <ProviderConfigDialog key={editing?.id || 'new'} initial={editing} onClose={() => { setAdding(false); setEditing(undefined) }} onSaved={reload} />}
    <Dialog open={Boolean(selected)} onOpenChange={(next) => { if (!next) setSelected(undefined) }}><DialogContent dismissOnOutside={false} onCloseRequest={() => setSelected(undefined)} className="model-catalog-dialog max-h-[80vh] max-w-2xl overflow-y-auto"><DialogHeader><DialogTitle>{selected?.name} · 模型清单</DialogTitle><DialogDescription>{selected?.note}。选择模型后即可用于消息或工作。</DialogDescription></DialogHeader><label className="ui-form-field"><span>筛选已识别模型</span><Input aria-label="筛选已识别模型" placeholder="搜索模型名称" value={query} onChange={(event) => setQuery(event.target.value)} /></label>{selected?.models.filter((model) => matchesCatalogModel(model, query)).map((model) => <article className="model-catalog-row" key={model.id}><div className="model-catalog-name">{model.display_name || model.id}<small className="block mt-1 text-muted-foreground">{model.id}</small></div><div className="model-catalog-capabilities"><Badge variant="outline">{capabilitySourceNames[model.capability_source || 'unknown'] || model.capability_source} · 未实测</Badge>{model.capabilities.map((capability) => <Badge variant="secondary" key={capability}>{names[capability] || capability}</Badge>)}{model.input_modalities?.map((value) => <Badge variant="outline" key={`in-${value}`}>输入·{modalityNames[value] || value}</Badge>)}{model.output_modalities?.map((value) => <Badge variant="outline" key={`out-${value}`}>输出·{modalityNames[value] || value}</Badge>)}</div>{model.capabilities.includes('text')&&<div className="model-catalog-actions">{['companion','work'].map(mode=><Button className="model-catalog-assign" key={mode} variant="outline" size="xs" disabled={busy} onClick={()=>void run(async()=>{const module=modules.find(v=>v.id===mode);if(!module||!selected)return;await api(`/v6/model-keys/${selected.id}/assign`,json('POST',{model:model.id,module_id:mode,expected_revision:module.revision,allow_unverified:false,native_video_input:false,voice:''}));toast.success(mode==='work'?'已用于工作':'已用于消息')})}>{mode==='work'?'用于工作':'用于消息'}</Button>)}</div>}{!model.capabilities.length && <span className="text-xs text-muted-foreground">能力未知</span>}</article>)}{!selected?.models.length && <p className="text-xs text-muted-foreground">尚无已确认的模型清单，请先识别。</p>}</DialogContent></Dialog>
    <Dialog open={Boolean(replacing)} onOpenChange={(next) => { if (!next && !busy) { setReplacing(undefined); setKey('') } }}><DialogContent><DialogHeader><DialogTitle>更换「{replacing?.name}」的 Key</DialogTitle><DialogDescription>所有使用这个 Key 的模型与模态都会同步使用新凭据；更换后重新读取模型清单。</DialogDescription></DialogHeader><label className="ui-form-field"><span>Key 库替换凭据</span><Input aria-label="Key 库替换凭据" type="password" autoComplete="off" value={key} onChange={(event) => setKey(event.target.value)} /></label><DialogFooter><Button disabled={busy || !key.trim()} onClick={() => void run(async () => { if (!replacing) return; const result = await api<StoredKey>(`/v6/model-keys/${replacing.id}/key`, json('PUT', { expected_revision: replacing.revision, api_key: key })); setKey(''); setReplacing(undefined); announce(result) })}>更换并重新识别</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(deleting)} onOpenChange={(next) => { if (!next && !busy) setDeleting(undefined) }}><DialogContent><DialogHeader><DialogTitle>删除「{deleting?.name}」的 Key</DialogTitle><DialogDescription>仍被模型或模态使用时会拒绝删除，以免中断其他配置。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" disabled={busy} onClick={() => setDeleting(undefined)}>取消</Button><Button disabled={busy} onClick={() => void run(async () => { if (!deleting) return; await api(`/v6/model-keys/${deleting.id}?expected_revision=${deleting.revision}`, json('DELETE')); setDeleting(undefined) })}>确认删除 Key</Button></DialogFooter></DialogContent></Dialog>
  </>
}

export function KeyModelPicker({ module, onAssigned }: { module: ModelModule; onAssigned: () => Promise<void> }) {
  const [keys, setKeys] = useState<StoredKey[]>([])
  const [keyId, setKeyId] = useState('')
  const [model, setModel] = useState('')
  const [query, setQuery] = useState('')
  const [voice, setVoice] = useState(module.voice || '')
  const [allow, setAllow] = useState(false)
  const [nativeVideo, setNativeVideo] = useState(false)
  const [busy, setBusy] = useState(false)
  useEffect(() => { let disposed = false; void loadKeys().then((value) => { if (!disposed) setKeys(value.items) }).catch((error) => toast.error('Key 库读取失败', { description: error.message })); return () => { disposed = true } }, [])
  const selected = keys.find((item) => item.id === keyId)
  const chosen = selected?.models.find((item) => item.id === model)
  const needed = module.required_capability || 'text'
  const unknown = chosen && !chosen.capabilities.includes(needed)
  const run = async (task: () => Promise<void>) => { if (busy) return; setBusy(true); try { await task() } catch (error) { toast.error('模型选择未完成', { description: (error as Error).message }) } finally { setBusy(false) } }
  return <div className="space-y-3 rounded-xl border border-border p-3"><h3 className="text-xs font-medium">从 Key 选择模型</h3><Select aria-label="模态使用的 Key" className={selectClass} value={keyId} disabled={busy} onChange={(event) => { setKeyId(event.target.value); setModel(''); setAllow(false); setNativeVideo(false) }}><option value="">选择已保存的 Key</option>{keys.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.models.length} 个模型</option>)}</Select>{selected && <><p className="break-all text-xs text-muted-foreground">{selected.base_url} · {selected.connected === false ? selected.note : '能力建议不等于调用已验证'}</p><Button size="xs" variant="outline" disabled={busy} onClick={() => void run(async () => { const fresh = await api<StoredKey>(`/v6/model-keys/${selected.id}/refresh`, json('POST', { expected_revision: selected.revision })); setKeys((items) => items.map((item) => item.id === fresh.id ? fresh : item)); if (fresh.connected === false) toast.info(fresh.note) })}>重新识别模型</Button><Input aria-label="筛选模态模型" placeholder="搜索模型" value={query} onChange={(event) => setQuery(event.target.value)} /><Select size={6} aria-label="自动识别的模型" className={selectClass} disabled={busy} value={model} onChange={(event) => { setModel(event.target.value); setAllow(false); setNativeVideo(false) }}><option value="">选择模型</option>{selected.models.filter((item) => matchesCatalogModel(item, query) || item.id === model).slice().sort((a, b) => Number(b.capabilities.includes(needed)) - Number(a.capabilities.includes(needed))).map((item) => <option key={item.id} value={item.id}>{item.display_name || item.id} · {item.capabilities.includes(needed) ? '能力建议匹配' : '此项能力待确认'}</option>)}</Select>
    {chosen && <><p className="break-all font-mono text-xs">{chosen.id}</p><div className="flex flex-wrap gap-1 text-xs"><Badge variant="outline">{capabilitySourceNames[chosen.capability_source || 'unknown'] || chosen.capability_source} · 未实测</Badge>{chosen.input_modalities?.map((value) => <Badge variant="outline" key={value}>输入·{modalityNames[value] || value}</Badge>)}</div><label className="flex gap-2 text-xs leading-5"><input type="checkbox" checked={nativeVideo || (chosen.capability_source === 'manual' && chosen.capabilities.includes('video'))} disabled={busy || (chosen.capability_source === 'manual' && chosen.capabilities.includes('video'))} onChange={(event) => setNativeVideo(event.target.checked)} />{nativeVideoInputLabel}</label><p className="text-xs text-muted-foreground">仅 Gemini generateContent 路由映射支持原生 MP4/WebM，OpenAI/Anthropic 不支持原生视频发送；此项不是视频生成。服务清单不会自动勾选；自定义目录或已有配置中明确声明的视频输入会保留，取消请编辑目录及已有模型配置。</p></>}
    {unknown && <label className="flex gap-2 text-xs leading-5"><input type="checkbox" checked={allow} onChange={(event) => setAllow(event.target.checked)} />此项能力未识别，仍按{names[needed]}模态使用（实际调用前尚未验证）</label>}{module.id === 'tts' && <Input aria-label="Key 模型音色" placeholder="服务支持的音色编号" value={voice} onChange={(event) => setVoice(event.target.value)} />}<Button disabled={busy || !chosen || Boolean(unknown && !allow)} onClick={() => void run(async () => { await api(`/v6/model-keys/${keyId}/assign`, json('POST', { model, module_id: module.id, voice, expected_revision: module.revision, allow_unverified: allow, native_video_input: nativeVideo })); await onAssigned(); toast.success('Key 与模型已分配到此模态') })}>使用这个 Key 与模型</Button></>}{!keys.length && <p className="text-xs text-muted-foreground">请先在 Key 库添加 Key，或使用下方已有配置。</p>}</div>
}
