import {ResetDefaults} from '@/components/settings/ResetDefaults'
import {settingsPayload,memorySettingFields} from '@/lib/settingsPayload'
import {confirmDiscard,useUnsavedChanges} from './UnsavedChanges'
import { useEffect, useState } from 'react'
import { useApp } from '@/context/AppContext'
import { toast } from 'sonner'
import { api, json } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { SettingRow } from './SettingRow'
import { memoryV2 } from '@/lib/memoryV2'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'

type Settings = { revision: number; shared_auto_categories: string[] } & Record<string, number | boolean | string[]>
const numbers = [
  ['keep_recent_turns', '近期原文最多保留（轮）', 1, 1000, '默认10轮，范围1–1000；一轮是一组用户输入及回复。受容量限制，与摘要后保留轮数不相加，不删除原聊天'],
  ['autoload_days', '近期记忆天数', 1, 36500, '自动加载近期确认的记忆；置顶记忆不受天数限制'],
  ['context_tokens', '全局后备容量', 2048, 2000000, '服务未提供容量且没有手动模型值时使用；实际请求优先使用模型容量'],
  ['reply_reserve_tokens', '回复预留 tokens', 256, 100000, ''],
  ['tool_reserve_tokens', '工具预留 tokens', 0, 100000, ''],
  ['memory_budget_tokens', '记忆预算 tokens', 256, 100000, '用于记忆及较早历史检索资料的预算'],
  ['rag_top_k', '历史聊天 RAG 最多片段', 1, 20, '检索近期窗口之外的历史聊天，不是项目资料'],
  ['rag_min_score', '本地向量检索最低相关度', 0, 1, '用于历史聊天 RAG，也用于记忆页的语义检索'],
] as const
const switches = [
  ['auto_memory_enabled', '自动提取记忆', '从已完成的对话提取记忆，可能调用模型；不是定期摘要开关'],
  ['semantic_enabled', '记忆语义检索', '为本页搜索与记忆查询工具补充本地向量匹配，不控制历史聊天 RAG 或项目资料检索'],
  ['rag_enabled', '历史聊天 RAG', '补充近期窗口之外的相关聊天原文；工作模式仅检索当前工作会话，不是项目资料检索'],
] as const

export function MemorySettings({inline=false}:{inline?:boolean}={}) {
  const {connection}=useApp()
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState<Settings>()
  const [error,setError]=useState('')
  const [saveError,setSaveError]=useState('')
  const [v2Active,setV2Active]=useState(false)
  const [authorityKnown,setAuthorityKnown]=useState(false)
  const [authorityError,setAuthorityError]=useState('')
  const [busy, setBusy] = useState(false)
  const [original,setOriginal]=useState('');useUnsavedChanges((open||inline)&&!!value&&JSON.stringify(value)!==original)
  const load = async () => {
    setBusy(true);setError('');setSaveError('');setAuthorityError('')
    try {
      const [settingsResult,migrationResult]=await Promise.allSettled([api<Settings>('/v4/memory/settings'),memoryV2.migrations(undefined,0,1)])
      if(settingsResult.status==='rejected')throw settingsResult.reason
      const fresh=settingsResult.value
      setValue(fresh);setOriginal(JSON.stringify(fresh));if(!inline)setOpen(true)
      if(migrationResult.status==='fulfilled'){setV2Active(migrationResult.value.authority==='v2');setAuthorityKnown(true)}
      else{setAuthorityKnown(false);setAuthorityError(`共享状态读取失败：${(migrationResult.reason as Error).message}`)}
    }
    catch (failure) {setError((failure as Error).message);if(!inline)toast.error('记忆设置读取失败', { description: (failure as Error).message }) }
    finally { setBusy(false) }
  }
  useEffect(()=>{if(inline&&connection==='connected')void load()},[inline,connection])
  const save = async () => {
    if (!value || busy || !valid) return
    setBusy(true);setSaveError('')
    try {
      // Keep legacy fields unchanged in the payload for backend compatibility.
      const saved=await api<Settings>('/v4/memory/settings', json('PUT', settingsPayload(value,memorySettingFields)))
      setValue(saved);setOriginal(JSON.stringify(saved));if(!inline)setOpen(false); toast.success('记忆与上下文设置已保存')
    } catch (failure) { setSaveError((failure as Error).message); toast.error('设置未保存，本地输入已保留', { description: (failure as Error).message }) }
    finally { setBusy(false) }
  }
  const invalidNumber=value&&numbers.some(([id,,min,max])=>!Number.isFinite(value[id])||Number(value[id])<min||Number(value[id])>max||(id!=='rag_min_score'&&!Number.isInteger(value[id])))
  const overBudget=value&&Number(value.reply_reserve_tokens)+Number(value.tool_reserve_tokens)+Number(value.memory_budget_tokens)>=Number(value.context_tokens)
  const blockedShare=(!authorityKnown||v2Active)&&Boolean(value?.shared_auto_categories.length)
  const valid=!!value&&!invalidNumber&&!overBudget&&!blockedShare
  const defaults:Record<string,number|boolean>={keep_recent_turns:10,autoload_days:7,context_tokens:32768,reply_reserve_tokens:4096,tool_reserve_tokens:4096,memory_budget_tokens:6000,rag_top_k:5,rag_min_score:.4,auto_memory_enabled:true,semantic_enabled:true,rag_enabled:true}
  const row=(field:typeof numbers[number])=>{const [id,title,min,max,description]=field;return value&&<SettingRow key={id} title={title} description={description}><Input className="w-32" aria-label={title} type="number" min={min} max={max} step={id==='rag_min_score'?.05:1} value={Number.isNaN(value[id])?'':String(value[id])} onChange={e=>setValue({...value,[id]:e.target.value===''?Number.NaN:Number(e.target.value)})}/><ResetDefaults title="此项" current={{[id]:value[id]}} defaults={{[id]:defaults[id]}} labels={{[id]:title}} disabled={busy} onApply={()=>setValue({...value,[id]:defaults[id]})}/></SettingRow>}
  const controls=value&&<div className="space-y-1">
          {switches.map(([id, title, description]) => <SettingRow key={id} title={title} description={description}><Switch aria-label={title} checked={Boolean(value[id])} onCheckedChange={(checked) => setValue({ ...value, [id]: checked })} /></SettingRow>)}
          {numbers.slice(0,2).map(row)}<details className="personal-disclosure"><summary>容量、预留与检索高级参数</summary>{numbers.slice(2).map(row)}</details><ResetDefaults title="上下文与检索组" current={value} defaults={defaults} labels={Object.fromEntries([...numbers,...switches].map(([id,title])=>[id,title]))} disabled={busy} onApply={()=>setValue({...value,...defaults})}/>
          <details className="personal-disclosure"><summary>旧版参数兼容</summary><p className="text-xs leading-6 text-muted-foreground">旧版参数 summarize_every_turns（摘要间隔）与 semantic_max_chunks（语义片段上限）仅保留兼容值，不再控制新聊天。新聊天使用近期完整问答与历史聊天 RAG，不按旧间隔自动摘要；历史片段数量由上方 RAG 设置控制。</p></details>
          {v2Active?value.shared_auto_categories.length>0?<div className="flex flex-wrap items-center gap-3 py-3 text-sm"><span>旧版自动共享与四层记忆不兼容。</span><Button type="button" size="sm" variant="outline" onClick={()=>setValue({...value,shared_auto_categories:[]})}>关闭旧版自动共享</Button></div>:<p className="py-3 text-xs text-muted-foreground">共享记忆请在记忆详情中逐条授权。</p>:authorityKnown?['communication_preference', 'writing_preference'].map((id) => <SettingRow key={id} title={id === 'communication_preference' ? '允许自动共享交流偏好' : '允许自动共享写作偏好'} description="只共享明确允许的类别；工作模式不读取私人陪伴记忆"><Switch aria-label={id === 'communication_preference' ? '允许自动共享交流偏好' : '允许自动共享写作偏好'} checked={value.shared_auto_categories.includes(id)} onCheckedChange={(checked) => setValue({ ...value, shared_auto_categories: checked ? [...value.shared_auto_categories, id] : value.shared_auto_categories.filter((item) => item !== id) })} /></SettingRow>):<p role="alert" className="py-3 text-xs text-destructive">{authorityError||'共享状态尚未读取，请刷新。'}</p>}
          {invalidNumber&&<p role="alert" className="text-sm text-destructive">请检查数值范围，除最低相关度外均需填写整数。</p>}
          {overBudget&&<p role="alert" className="text-sm text-destructive">回复、工具和记忆预留之和必须小于上下文容量。</p>}
          {saveError&&<p role="alert" className="text-sm text-destructive">保存失败：{saveError}</p>}
        </div>
  const saveButton=<Button disabled={busy || !valid || JSON.stringify(value)===original || (inline&&connection!=='connected')} onClick={() => void save()}>{busy ? '保存中…' : '保存上下文与检索设置'}</Button>
  if(inline)return <section className="customer-subsection memory-settings-inline"><header><h3>原文保留与记忆检索</h3><Button variant="ghost" size="sm" disabled={busy||connection!=='connected'} onClick={()=>void confirmDiscard().then(ok=>{if(ok)void load()})}>刷新</Button></header>{connection!=='connected'?<p>连接本地服务后可调整记忆与上下文。</p>:error?<p role="alert">记忆设置读取失败：{error}</p>:!value?<p role="status">正在读取记忆设置…</p>:controls}{value&&<div className="customer-save">{saveButton}</div>}</section>
  return <>
    <Button variant="outline" disabled={busy} onClick={() => void load()}>记忆与上下文设置</Button>
    <Dialog open={open} onOpenChange={v=>{if(v)setOpen(true);else if(!busy)void confirmDiscard().then(ok=>{if(ok)setOpen(false)})}}>
      <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
        <DialogHeader><DialogTitle>记忆与上下文设置</DialogTitle><DialogDescription>历史聊天 RAG 与项目资料检索是不同功能。回复、工具和记忆的预留容量之和必须小于上下文容量。</DialogDescription></DialogHeader>
        {controls}
        <DialogFooter>{saveButton}</DialogFooter>
      </DialogContent>
    </Dialog>
  </>
}
