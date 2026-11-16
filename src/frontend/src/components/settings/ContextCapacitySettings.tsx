import {useEffect,useState} from 'react'
import {api,json} from '@/api/client'
import {useApp} from '@/context/AppContext'
import {useUnsavedChanges} from '@/components/shared/UnsavedChanges'
import {Button} from '@/components/ui/button'
import {Input} from '@/components/ui/input'
import {Select} from '@/components/ui/select'
import {ResetDefaults} from './ResetDefaults'
import {toast} from 'sonner'
type Capacity={id:string;route:string;model:string;tokens:number;manual_tokens:number|null;service_tokens:number|null;source:string;notice:string;revision:number}
export function ContextCapacitySettings(){
 const {connection}=useApp(),[items,setItems]=useState<Capacity[]>([]),[route,setRoute]=useState(''),[draft,setDraft]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false)
 const current=items.find(item=>item.route===route),original=current?.manual_tokens?.toString()||'',dirty=!!current&&draft!==original
 useUnsavedChanges(dirty)
 const load=()=>api<{items:Capacity[]}>('/v6/context-capacities').then(value=>{setItems(value.items);setRoute(previous=>value.items.some(i=>i.route===previous)?previous:value.items[0]?.route||'');setError('')}).catch(e=>setError((e as Error).message))
 useEffect(()=>{if(connection==='connected')void load()},[connection])
 useEffect(()=>setDraft(current?.manual_tokens?.toString()||''),[route,current?.revision])
 return <section className="customer-subsection"><header><h3>模型上下文容量</h3></header><p className="text-xs leading-6 text-muted-foreground">容量随当前会话实际选用的服务和模型生效；消息与工作可使用不同模型。手动值适用于同一服务地址、协议和模型。服务未提供容量时沿用下方全局后备值。</p>{error&&<p role="alert">{error}<Button variant="ghost" onClick={()=>void load()}>重试</Button></p>}{items.length?<><label className="customer-field"><span>查看模型</span><Select aria-label="查看上下文容量模型" value={route} disabled={dirty||busy} onChange={e=>setRoute(e.target.value)}>{items.map(item=><option key={item.route} value={item.route}>{item.route}</option>)}</Select></label>{current&&<><p className="text-sm leading-6">当前预算容量：<strong>{current.tokens.toLocaleString()} tokens</strong> · {current.source==='service'?'服务元数据':current.source==='manual'?'手动设置':'全局后备值（尚未确认）'}<br/><span className="text-xs text-muted-foreground">{current.notice}{current.service_tokens?` · 服务声明 ${current.service_tokens.toLocaleString()} tokens`:''}。按较小容量预算；小容量模型会按比例缩减回复、工具和记忆预留，为输入留出至少约四分之一容量。计数为保守估算。</span></p><label className="customer-field"><span>手动容量（留空跟随服务，未知时使用全局后备）</span><Input aria-label="模型手动上下文容量" type="number" min={2048} max={2000000} step={1} value={draft} disabled={busy} onChange={e=>setDraft(e.target.value)}/></label><div className="customer-save"><Button disabled={busy||!dirty||connection!=='connected'||(draft!==''&&(!Number.isInteger(Number(draft))||Number(draft)<2048||Number(draft)>2000000))} onClick={async()=>{setBusy(true);setError('');try{await api('/v6/context-capacities',json('PUT',{route,tokens:draft===''?null:Number(draft),expected_revision:current.revision}));await load();toast.success('模型容量已保存')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>保存模型容量</Button><ResetDefaults title="此模型容量" current={{tokens:draft||null}} defaults={{tokens:null}} labels={{tokens:'手动容量'}} disabled={busy} onApply={()=>setDraft('')}/></div></>}</>:!error&&<p className="text-xs text-muted-foreground">配置文字模型后，可在这里查看和设置其容量。</p>}</section>
}
