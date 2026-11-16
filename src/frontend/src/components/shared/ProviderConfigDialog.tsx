import {confirmDiscard,useUnsavedChanges} from './UnsavedChanges'
import { Select } from '@/components/ui/select'
import { useEffect, useRef, useState } from 'react'
import { ChevronDown, LoaderCircle, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, json } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { capabilityNames, capabilitySourceNames, configurationModels, modalityNames, type CatalogMode, type CatalogModel, type Protocol, type ProviderPreset, type StoredKey } from '@/lib/modelCatalog'
import { effortLabels, effortLevels } from '@/lib/workComposerSettings'
import './provider-config.css'

export function ProviderConfigDialog({ initial, onClose, onSaved }: { initial?: StoredKey; onClose: () => void; onSaved: () => Promise<void> }) {
  const [stored, setStored] = useState(initial)
  const [presets, setPresets] = useState<ProviderPreset[]>([])
  const [provider, setProvider] = useState(initial?.provider_id || 'openai')
  const [name, setName] = useState(initial?.name || '')
  const [address, setAddress] = useState(initial?.base_url || 'https://api.openai.com/v1')
  const [protocol, setProtocol] = useState<Protocol>(initial?.protocol || 'openai')
  const [key, setKey] = useState('')
  const [mode, setMode] = useState<CatalogMode>(initial?.catalog_mode || 'service')
  const [models, setModels] = useState<CatalogModel[]>(initial?.models || [])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [detecting, setDetecting] = useState('')
  const editVersions = useRef<Record<number, number>>({})
  const focusedIds = useRef<Record<number, string>>({})
  const catalogEpoch = useRef(0), pendingDetections = useRef(0)
  const initialState=useRef<string|undefined>(undefined);const state=JSON.stringify([provider,name,address,protocol,key,mode,models]);if(initialState.current===undefined)initialState.current=state;useUnsavedChanges(state!==initialState.current);const close=()=>void confirmDiscard().then(ok=>{if(ok)onClose()})
  const preset = presets.find((item) => item.id === provider)
  useEffect(() => { let alive = true; api<{ items: ProviderPreset[] }>('/v6/provider-presets').then((data) => { if (alive) setPresets(data.items) }).catch((failure) => { if (alive) setError(failure.message) }); return () => { alive = false } }, [])
  const editModel = (index: number, patch: Partial<CatalogModel>) => { if (Object.keys(patch).some((key) => key !== 'display_name')) editVersions.current[index] = (editVersions.current[index] || 0) + 1; setMode('custom'); setModels((values) => values.map((value, position) => position === index ? { ...value, ...patch, capability_source: 'manual', capability_status: 'unverified' } : value)) }
  const toggle = (index: number, field: 'capabilities' | 'input_modalities' | 'output_modalities', value: string) => {
    const model = models[index]
    if (value === 'video' && field !== 'output_modalities') {
      const enabled = !model.capabilities.includes('video')
      editModel(index, { capabilities: enabled ? [...model.capabilities, 'video'] : model.capabilities.filter((item) => item !== 'video'), input_modalities: enabled ? [...new Set([...(model.input_modalities || []), 'video'])] : model.input_modalities?.filter((item) => item !== 'video') })
      return
    }
    const values = model[field] || []
    editModel(index, { [field]: values.includes(value) ? values.filter((item) => item !== value) : [...values, value] })
  }
  const detect = async (index: number) => {
    const identifier = models[index]?.id.trim()
    if (!identifier) return
    const version = editVersions.current[index] || 0, epoch = catalogEpoch.current
    pendingDetections.current++; setDetecting(identifier)
    try {
      const value = await api<CatalogModel>('/v6/model-metadata', json('POST', { id: identifier }))
      setModels((current) => current.map((model, i) => i === index && catalogEpoch.current === epoch && model.id.trim() === identifier && (editVersions.current[index] || 0) === version ? { ...value, display_name: model.display_name || value.display_name, capabilities: [...value.capabilities.filter((capability) => capability !== 'video'), ...(model.capabilities.includes('video') ? ['video'] : [])], input_modalities: model.capabilities.includes('video') ? [...new Set([...(value.input_modalities || []), 'video'])] : value.input_modalities, ...(model.capabilities.includes('video') ? { capability_source: 'manual', capability_status: 'unverified' as const } : {}) } : model))
    } catch (failure) { toast.error('文档识别未完成', { description: (failure as Error).message }) } finally { pendingDetections.current--; if (!pendingDetections.current) setDetecting('') }
  }
  const run = async (action: () => Promise<void>) => { if (busy) return; setBusy(true); setError(''); try { await action() } catch (failure) { setError((failure as Error).message) } finally { setBusy(false) } }
  const creationId=useRef(crypto.randomUUID().replaceAll('-',''))
  const save = () => run(async () => {
    const configuration={name:name.trim()||preset?.name||'自定义服务',base_url:address.trim(),provider_id:provider,protocol,catalog_mode:mode,models:configurationModels(models)}
    const saved=stored?await api<StoredKey>(`/v6/model-keys/${stored.id}`,json('PUT',{...configuration,expected_revision:stored.revision,...(key.trim()?{api_key:key}:{})})):await api<StoredKey>('/v6/model-key-configured',json('POST',{...configuration,api_key:key,creation_id:creationId.current}))
    setStored(saved);setKey('');initialState.current=state;try{await onSaved()}catch{toast.warning('已保存，列表刷新失败，请重新打开连接中心')}onClose()
  })
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) { close() } }}><DialogContent className="provider-config-dialog"><DialogHeader><DialogTitle>{initial ? `编辑 ${initial.name}` : '添加服务商与 Key'}</DialogTitle><DialogDescription>填写连接后读取模型，确认无误再保存。</DialogDescription></DialogHeader>
    <div className="provider-config-top"><label>服务商预设<Select aria-label="服务商预设" value={provider} disabled={busy} onChange={(event) => {
      const choice = presets.find((item) => item.id === event.target.value)
      if (!choice) return
      setProvider(choice.id); setAddress(choice.base_url); setProtocol(choice.protocol); if (!initial || !name) setName(choice.name); catalogEpoch.current++; setModels(choice.models); setMode(choice.id === 'custom' ? 'custom' : 'preset')
    }}>{presets.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</Select></label><label>配置名称<Input aria-label="服务商配置名称" value={name} placeholder={preset?.name || '自定义名称'} maxLength={100} onChange={(event) => setName(event.target.value)} /></label></div>
    <label className="provider-config-key">API 密钥<Input aria-label="服务商 API 密钥" type="password" autoComplete="off" value={key} placeholder={stored ? '已配置——输入新值可替换' : '输入 API Key'} onChange={(event) => setKey(event.target.value)} /></label>
    <details className="provider-config-custom"><summary><ChevronDown />自定义设置</summary><label>API 地址<Input aria-label="服务商 API 地址" value={address} maxLength={2000} placeholder={preset?.base_url || '填写 API 基础地址'} onChange={(event) => setAddress(event.target.value)} /></label><label>API 协议<Select aria-label="服务商 API 协议" value={protocol} onChange={(event) => setProtocol(event.target.value as Protocol)}><option value="openai">OpenAI 兼容</option><option value="anthropic">Anthropic Messages</option><option value="gemini">Gemini 官方兼容</option></Select></label>{preset?.documentation && <a className="provider-doc-link" href={preset.documentation} target="_blank" rel="noreferrer">查看官方模型与能力文档</a>}</details>
    <section className="provider-model-catalog"><div className="provider-model-heading"><div><h3>模型目录</h3><p>{mode === 'custom' ? '已自定义模型目录' : mode === 'preset' ? '使用官方资料预设 · 不代表 Key 的访问权限' : '使用服务返回的可用模型'}</p></div><div><Button size="xs" variant="ghost" disabled={busy || !preset?.models.length} onClick={() => { catalogEpoch.current++; setModels(preset?.models || []); setMode('preset') }}>恢复默认模型</Button><Button size="xs" variant="ghost" disabled={busy || (!stored && !key.trim()) || !address.trim()} onClick={() => void run(async () => {
      if(!stored){const fresh=await api<{models:CatalogModel[];connected:boolean;note?:string}>('/v6/model-key-preview',json('POST',{name:name.trim()||'临时连接',base_url:address.trim(),api_key:key,provider_id:provider,protocol}));catalogEpoch.current++; setModels(fresh.models||[]);setMode('custom');if(!fresh.connected)setError(fresh.note||'未能读取模型');return}
      if(address.trim()!==stored.base_url||protocol!==(stored.protocol||'openai')||key.trim())throw new Error('请先保存修改后的连接，再读取模型')
      const fresh=await api<StoredKey>(`/v6/model-keys/${stored.id}/refresh`,json('POST',{expected_revision:stored.revision}));setStored(fresh)
      const actual=(fresh as StoredKey&{discovered_models?:CatalogModel[]}).discovered_models||fresh.models
      catalogEpoch.current++; setModels(actual); setMode('service'); if (fresh.connected === false) setError(fresh.note)
    })}><RefreshCw />{stored ? '获取可用模型' : '保存 Key 并获取模型'}</Button></div></div>
      {!models.length && <p className="provider-empty-models">目录中暂无模型。可获取服务清单，或添加模型 ID 后按文档识别能力。</p>}
      {models.map((model, index) => <div className="provider-model-row" key={index}><div className="provider-model-fields"><label className="ui-form-field"><span>{`模型 ID ${index + 1}`}</span><Input aria-label={`模型 ID ${index + 1}`} placeholder="模型 ID" value={model.id} maxLength={200} onChange={(event) => editModel(index, { id: event.target.value, capabilities: [], input_modalities: [], output_modalities: [], reasoning: undefined })} onFocus={event => { focusedIds.current[index] = event.target.value.trim() }} onBlur={event => { if (focusedIds.current[index] !== event.target.value.trim()) void detect(index) }} /></label><label className="ui-form-field"><span>{`模型显示名称 ${index + 1}`}</span><Input aria-label={`模型显示名称 ${index + 1}`} placeholder="显示名称" value={model.display_name || ''} maxLength={100} onChange={(event) => editModel(index, { display_name: event.target.value })} /></label><Button aria-label={`删除模型 ${index + 1}`} size="icon" variant="ghost" disabled={busy} onClick={() => { setMode('custom'); catalogEpoch.current++; setModels((current) => current.filter((_, position) => position !== index)) }}><Trash2 /></Button></div>
        <div className="provider-model-capabilities"><Badge variant="outline">{capabilitySourceNames[model.capability_source || 'unknown'] || model.capability_source} · 未实测</Badge>{model.capabilities.map((cap) => <Badge variant="secondary" key={cap}>{capabilityNames[cap] || cap}</Badge>)}{model.input_modalities?.map((value) => <span key={`in-${value}`}>输入·{modalityNames[value] || value}</span>)}{model.output_modalities?.map((value) => <span key={`out-${value}`}>输出·{modalityNames[value] || value}</span>)}</div>
        <details className="provider-model-options"><summary><ChevronDown />模型能力与思考档位</summary><Button variant="ghost" size="xs" disabled={busy || !model.id.trim()} onClick={() => void detect(index)}>{detecting === model.id ? <LoaderCircle className="animate-spin" /> : <RefreshCw />}按文档识别</Button><div className="provider-model-checkboxes">{Object.entries(capabilityNames).map(([id, label]) => <label key={id}><input type="checkbox" checked={model.capabilities.includes(id)} onChange={() => toggle(index, 'capabilities', id)} />{label}</label>)}</div><fieldset><legend>输入模态（视频输入请使用上方显式声明）</legend>{Object.entries(modalityNames).filter(([id]) => id !== 'video').map(([id, label]) => <label key={id}><input type="checkbox" checked={model.input_modalities?.includes(id) || false} onChange={() => toggle(index, 'input_modalities', id)} />{label}</label>)}</fieldset><p>视频输入仅适用于支持的 Gemini 服务，需实际调用确认。</p><fieldset><legend>输出模态</legend>{Object.entries(modalityNames).map(([id, label]) => <label key={id}><input type="checkbox" checked={model.output_modalities?.includes(id) || false} onChange={() => toggle(index, 'output_modalities', id)} />{label}</label>)}</fieldset><fieldset><legend>可选思考档位（没有勾选时仅保留自动）</legend>{effortLevels.filter((value) => value !== 'auto').map((value) => <label key={value}><input type="checkbox" checked={model.reasoning?.levels.includes(value) || false} onChange={() => { const levels = model.reasoning?.levels || []; editModel(index, { reasoning: { levels: levels.includes(value) ? levels.filter((level) => level !== value) : [...levels, value], parameter: value === 'enabled' ? 'thinking' : model.reasoning?.parameter || 'effort', source: 'manual', note: '用户自定义' } }) }} />{effortLabels[effortLevels.indexOf(value)]}</label>)}</fieldset><p>{model.reasoning?.note || '未知能力不会伪装成已验证；可按服务文档自定义。'}</p></details>
      </div>)}
      <Button size="sm" variant="outline" disabled={busy} onClick={() => { setMode('custom'); setModels((current) => [...current, { id: '', display_name: '', capabilities: [], input_modalities: [], output_modalities: [] }]) }}><Plus />添加模型</Button>
    </section>{error && <p className="provider-config-error" role="alert">{error}</p>}
    <DialogFooter><Button variant="outline" disabled={busy} onClick={() => { close() }}>取消</Button><Button disabled={busy || Boolean(detecting) || !address.trim() || (!stored && !key.trim()) || models.some((model) => !model.id.trim())} onClick={() => void save()}>{busy ? <LoaderCircle className="animate-spin" /> : null}保存</Button></DialogFooter>
  </DialogContent></Dialog>
}
