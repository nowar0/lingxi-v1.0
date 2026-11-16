/** Declarative package UI only: no plugin HTML, JavaScript, iframe or execute RPC. */
import { approvalProblem, containsCredentialField, parsePluginPreview, pluginErrorMessage } from './pluginPackages'
import type { PluginPreview } from './pluginPackages'

export type PackageUiFieldType = 'text' | 'textarea' | 'number' | 'boolean' | 'select' | 'readonly'
export type PackageUiValue = string | number | boolean | null
export type PackageUiField = { id: string; type: PackageUiFieldType; label: string; source?: string; required?: boolean; default?: PackageUiValue; options?: string[]; min?: number; max?: number; max_length?: number }
export type PackageUiAction = { id: string; label: string; kind: 'tool' | 'save_config' | 'save_state'; tool?: string; arguments?: Record<string, string> }
export type PackageUiSchema = { schema_version: 1; title: string; description?: string; fields: PackageUiField[]; actions: PackageUiAction[] }
export type PackageUiBinding = { id: string; revision: number; digest: string }
export type PackageUiSession = { session_id: string; nonce: string; expires_at: number; next_sequence: number; binding: PackageUiBinding; schema: PackageUiSchema; approval_required: true }
export type PackageUiFetch = (path: string, init?: RequestInit) => Promise<Response>
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const asciiId = (value: unknown): value is string => typeof value === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(value) && !['__proto__','prototype','constructor'].includes(value)
const packageId = (value: string) => /^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$/.test(value)
const sensitive = (text: string) => /api[_-]?key|password|secret|access[_-]?token|credential/i.test(text)
function containsLikelyKey(value: unknown): boolean {
  if (typeof value === 'string') return /\bsk-[A-Za-z0-9_-]{12,}|\b(?:bearer|api[_-]?key|access[_-]?token)\s*[:= ]\s*\S{8,}/i.test(value)
  return Array.isArray(value) ? value.some(containsLikelyKey) : object(value) && Object.values(value).some(containsLikelyKey)
}
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const plainText = (value: unknown, max: number): value is string => typeof value === 'string' && value.length <= max && !value.includes('\0')
const primitive = (value: unknown): value is PackageUiValue => value === null || typeof value === 'string' || typeof value === 'boolean' || finite(value)
const canonical = (value: unknown): string => Array.isArray(value) ? `[${value.map(canonical).join(',')}]` : object(value) ? `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}` : JSON.stringify(value) ?? 'null'
function invalid(message = '插件声明式界面格式不受支持；未显示或执行插件内容'): never { throw new Error(message) }
function keys(value: Record<string, unknown>, allowed: string[]) { if (Object.keys(value).some(key => !allowed.includes(key))) invalid() }
function noSecrets(value: unknown) { if (containsCredentialField(value) || containsLikelyKey(value)) invalid('插件界面不能读取、显示或提交密钥字段及疑似真实凭据') }
function fieldValue(field: PackageUiField, value: unknown, require = false): PackageUiValue {
  if (!primitive(value)) invalid('字段值必须是有限数字、文字、布尔或空值')
  if (require && field.required && (value === null || typeof value === 'string' && !value.trim())) invalid(`请填写「${field.label}」`)
  if (typeof value === 'string' && value.includes('\0')) invalid('字段文字包含非法空字符')
  if (value === null) return value
  if (field.type === 'number') { if (!finite(value) || field.min !== undefined && value < field.min || field.max !== undefined && value > field.max) invalid(`「${field.label}」数值超出声明范围`) }
  else if (field.type === 'boolean') { if (typeof value !== 'boolean') invalid(`「${field.label}」必须为布尔值`) }
  else if (field.type !== 'readonly') {
    if (typeof value !== 'string' || value.length > (field.max_length ?? 4000)) invalid(`「${field.label}」文字长度超出限制`)
    if (field.type === 'select' && !field.options?.includes(value)) invalid(`「${field.label}」不是声明选项`)
  } else if (typeof value === 'string' && value.length > (field.max_length ?? 4000)) invalid('只读内容过长')
  noSecrets(value)
  return value
}
export function parsePackageUiSchema(value: unknown): PackageUiSchema {
  if (!object(value)) invalid()
  if (new TextEncoder().encode(JSON.stringify(value)).length > 32768) invalid('插件界面声明超过32 KiB')
  keys(value, ['schema_version','title','description','fields','actions'])
  if (value.schema_version !== 1 || !plainText(value.title,200) || !value.title.trim() || value.description !== undefined && !plainText(value.description,2000) || !Array.isArray(value.fields) || value.fields.length > 50 || !Array.isArray(value.actions) || value.actions.length > 30) invalid()
  const fieldIds = new Set<string>(); const sources = new Set<string>(); const fields: PackageUiField[] = []
  for (const raw of value.fields) {
    if (!object(raw)) invalid()
    keys(raw,['id','type','label','source','required','default','options','min','max','max_length'])
    if (!asciiId(raw.id) || sensitive(raw.id) || fieldIds.has(raw.id) || !['text','textarea','number','boolean','select','readonly'].includes(String(raw.type)) || !plainText(raw.label,200) || !raw.label.trim()) invalid()
    if (raw.source !== undefined && (typeof raw.source !== 'string' || !/^(config|state)\.[a-z][a-z0-9_-]{0,63}$/.test(raw.source) || sensitive(raw.source) || ['__proto__','prototype','constructor'].includes(raw.source.split('.')[1]))) invalid()
    if (typeof raw.source === 'string') { if(sources.has(raw.source)) invalid(); sources.add(raw.source) }
    if (raw.required !== undefined && typeof raw.required !== 'boolean') invalid()
    if (raw.min !== undefined && !finite(raw.min) || raw.max !== undefined && !finite(raw.max) || finite(raw.min) && finite(raw.max) && raw.min > raw.max) invalid()
    if (raw.max_length !== undefined && (!['text','textarea','select','readonly'].includes(String(raw.type)) || !Number.isSafeInteger(raw.max_length) || Number(raw.max_length) < 1 || Number(raw.max_length) > 16000)) invalid()
    if (raw.type !== 'number' && (raw.min !== undefined || raw.max !== undefined)) invalid()
    if (raw.type === 'select') {
      if (!Array.isArray(raw.options) || !raw.options.length || raw.options.length > 100 || !raw.options.every(option => plainText(option,300) && option.length > 0) || new Set(raw.options).size !== raw.options.length) invalid()
    } else if (raw.options !== undefined) invalid()
    const field = raw as PackageUiField
    if ('default' in raw) fieldValue(field,raw.default,true)
    noSecrets({label:field.label,default:field.default})
    fieldIds.add(field.id); fields.push(field)
  }
  const actionIds = new Set<string>(); const actions: PackageUiAction[] = []
  for (const raw of value.actions) {
    if (!object(raw)) invalid()
    keys(raw,['id','label','kind','tool','arguments'])
    if (!asciiId(raw.id) || actionIds.has(raw.id) || !plainText(raw.label,200) || !raw.label.trim() || !['tool','save_config','save_state'].includes(String(raw.kind))) invalid()
    if (raw.kind === 'tool' ? typeof raw.tool !== 'string' || !/^[a-z][a-z0-9_.-]{0,100}$/.test(raw.tool) : raw.tool !== undefined) invalid()
    if (raw.arguments !== undefined && (!object(raw.arguments) || Object.keys(raw.arguments).length > 100 || Object.entries(raw.arguments).some(([key,field]) => !plainText(key,100) || !key || sensitive(key) || ['__proto__','prototype','constructor'].includes(key) || typeof field !== 'string' || !fieldIds.has(field)))) invalid()
    if (raw.kind !== 'tool' && raw.arguments !== undefined) invalid()
    noSecrets(raw.label)
    actionIds.add(raw.id); actions.push(raw as PackageUiAction)
  }
  noSecrets({ title:value.title, description:value.description })
  return {schema_version:1,title:value.title, ...(value.description !== undefined ? {description:value.description as string} : {}),fields,actions}
}
export function packageUiDefaults(schema: PackageUiSchema): Record<string, PackageUiValue> {
  return Object.fromEntries(schema.fields.map(field => [field.id, field.default ?? (field.type === 'boolean' ? false : field.type === 'number' ? null : '')]))
}
export function packageUiEditableValues(schema: PackageUiSchema, values: Record<string, unknown>): Record<string, PackageUiValue> {
  if (!object(values) || Object.keys(values).some(key => !schema.fields.some(field => field.id === key))) invalid('存在未声明字段；不会提交')
  const result: Record<string, PackageUiValue> = {}
  for (const field of schema.fields) {
    if (field.type === 'readonly') continue
    result[field.id] = fieldValue(field,Object.prototype.hasOwnProperty.call(values,field.id) ? values[field.id] : field.default ?? (field.type === 'boolean' ? false : null),true)
  }
  noSecrets(result)
  return result
}
export function parsePackageUiSession(value: unknown, binding: PackageUiBinding): PackageUiSession {
  if (!object(value)) invalid()
  keys(value,['session_id','nonce','expires_at','next_sequence','binding','schema','approval_required','allowed_methods','note'])
  if (!plainText(value.session_id,200) || value.session_id.length < 16 || !plainText(value.nonce,200) || value.nonce.length < 16 || !finite(value.expires_at) || value.expires_at <= Date.now()/1000 || value.next_sequence !== 1 || value.approval_required !== true || !object(value.binding) || canonical(value.binding) !== canonical(binding)) invalid('插件界面会话与当前包版本不一致或已过期')
  if (canonical(value.allowed_methods)!==canonical(['schema','read_values','preview_action']) || value.note!==undefined && !plainText(value.note,2000)) invalid('界面声明包含未知 RPC 能力')
  return {...value,schema:parsePackageUiSchema(value.schema)} as PackageUiSession
}
export function packageUiApprovalProblem(preview: PluginPreview, session: PackageUiSession, action: PackageUiAction): string {
  if (session.expires_at <= Date.now()/1000) return '界面会话已过期，请重新打开后生成预览'
  const problem = approvalProblem(preview)
  if (problem) return problem
  const expected = action.kind === 'tool' ? 'call' : action.kind === 'save_config' ? 'config' : 'state'
  if (preview.snapshot.id !== session.binding.id || preview.snapshot.expected_revision !== session.binding.revision || preview.snapshot.digest !== session.binding.digest || preview.snapshot.action !== expected || expected === 'call' && preview.snapshot.tool !== action.tool) return '预览与界面会话或动作不一致，不能批准'
  return ''
}
export function createPackageUiClient({ apiBase='/v1/plugin-packages', fetcher=(path,init)=>fetch(path,init) }: {apiBase?:string;fetcher?:PackageUiFetch}={}) {
  if (!/^\/v1\/plugin-packages\/?$/.test(apiBase)) invalid('插件 UI 只允许宿主同源 API')
  const base=apiBase.replace(/\/$/,'')
  let session: PackageUiSession | undefined; let next=1; let pending:number|null=null; let generation=0
  let readOnlyValues: Record<string,PackageUiValue> = {}
  let valuesLoaded=false
  const invalidate=()=>{session=undefined;next=1;readOnlyValues={};valuesLoaded=false;pending=null;generation++}
  async function request(path:string,body:unknown):Promise<unknown> {
    const encoded=JSON.stringify(body)
    if(new TextEncoder().encode(encoded).length>65536) invalid('UI RPC 正文超过64 KiB；不会发送')
    const response=await fetcher(base+path,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:encoded,signal:AbortSignal.timeout(15000)})
    let payload:unknown
    try { payload=await response.json() } catch { invalid('插件界面服务未返回有效 JSON；结果未知，重新打开后再操作') }
    if(!response.ok) throw new Error(pluginErrorMessage(payload,`插件界面请求失败（HTTP ${response.status}）`))
    return payload
  }
  async function rpc(method:'schema'|'read_values'|'preview_action',params:Record<string,unknown>={}) {
    if(!session || session.expires_at<=Date.now()/1000) {invalidate();invalid('界面会话已失效，请重新打开')}
    if(pending!==null) invalid('已有界面请求正在处理，不会并行或重试')
    const live=session; const sequence=next++; const requestId=globalThis.crypto?.randomUUID?.() ?? `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`; const epoch=generation
    pending=epoch
    try {
      const payload=await request(`/${encodeURIComponent(live.binding.id)}/ui/rpc`,{session_id:live.session_id,nonce:live.nonce,sequence,request_id:requestId,method,params})
      if(epoch!==generation || session!==live) invalid('已丢弃旧界面响应，请重新打开')
      if(!object(payload) || payload.session_id!==live.session_id || payload.request_id!==requestId || payload.sequence!==sequence || payload.next_sequence!==next || canonical(payload.binding)!==canonical(live.binding)) invalid('RPC 响应会话、顺序或包版本不一致')
      const common=['session_id','request_id','sequence','next_sequence','binding']
      if(method==='schema') { keys(payload,[...common,'schema']); if(canonical(payload.schema)!==canonical(live.schema))invalid('同一界面会话的声明已改变，请重新打开'); return payload.schema }
      if(method==='read_values') { keys(payload,[...common,'values']); return {values:payload.values} }
      keys(payload,[...common,'preview','execution','execute_path'])
      if(payload.execution!=='separate-explicit-user-confirmation-required' || !['/v1/plugin-packages/calls/execute','/v1/plugin-packages/commit'].includes(String(payload.execute_path))) invalid('RPC 不得执行或扩大宿主能力')
      return payload.preview
    } catch(error) {if(epoch===generation)invalidate();throw error} finally {if(pending===epoch)pending=null}
  }
  return {
    invalidate,
    getSession:()=>session,
    async open(binding:PackageUiBinding) {
      if(!packageId(binding.id) || !Number.isSafeInteger(binding.revision) || binding.revision<0 || !/^[a-f0-9]{64}$/.test(binding.digest)) invalid('插件包版本无效')
      if(pending!==null) invalid('已有界面请求正在处理')
      invalidate();const epoch=generation;pending=epoch
      try { const value=parsePackageUiSession(await request(`/${encodeURIComponent(binding.id)}/ui/open`,{expected_revision:binding.revision}),binding);if(epoch!==generation) invalid('旧会话响应已丢弃');session=value;next=value.next_sequence;return value }
      catch(error){if(epoch===generation)invalidate();throw error} finally {if(pending===epoch)pending=null}
    },
    async schema() {return parsePackageUiSchema(await rpc('schema'))},
    async readValues() {
      const live=session;if(!live) invalid('请先打开界面')
      const result=await rpc('read_values')
      if(!object(result)) {invalidate();invalid('读取字段值格式无效')}
      if(!object(result.values)) {invalidate();invalid('读取字段值不是声明式 values')}
      const values=result.values
      try {
        if(Object.keys(values).some(id=>!live.schema.fields.some(field=>field.id===id)) || live.schema.fields.some(field=>!(field.id in values))) invalid('服务返回未声明或缺失字段')
        noSecrets(values)
        const parsed=Object.fromEntries(live.schema.fields.map(field=>[field.id,fieldValue(field,values[field.id])]))
        readOnlyValues=Object.fromEntries(live.schema.fields.filter(field=>field.type==='readonly').map(field=>[field.id,parsed[field.id]]))
        valuesLoaded=true
        return parsed
      } catch(error) {invalidate();throw error}
    },
    async previewAction(actionId:string,values:Record<string,unknown>) {
      const live=session;if(!live) invalid('请先打开界面')
      const action=live.schema.actions.find(action=>action.id===actionId);if(!action) invalid('动作未声明')
      if(Object.values(action.arguments??{}).some(id=>live.schema.fields.find(field=>field.id===id)?.type==='readonly')&&!valuesLoaded) invalid('先读取真实只读字段，再生成动作预览')
      const safe=packageUiEditableValues(live.schema,values)
      let preview:PluginPreview
      try {preview=parsePluginPreview(await rpc('preview_action',{action_id:actionId,values:safe}));noSecrets(preview.snapshot.args);noSecrets(preview.snapshot.value)}
      catch(error){invalidate();throw error}
      const problem=packageUiApprovalProblem(preview,live,action);if(problem) {invalidate();invalid(problem)}
      if(action.kind==='tool') {
        const expected=Object.fromEntries(Object.entries(action.arguments??{}).map(([key,id])=>[key,live.schema.fields.find(field=>field.id===id)?.type==='readonly'?readOnlyValues[id]:safe[id]]))
        if(canonical(preview.snapshot.args)!==canonical(expected)) {invalidate();invalid('工具预览参数与填写值不一致')}
      } else {
        const kind=action.kind==='save_config'?'config':'state';const expected=live.schema.fields.filter(field=>field.type!=='readonly'&&field.source?.startsWith(kind+'.')).map(field=>[field.source!.split('.')[1],safe[field.id]])
        if(!object(preview.snapshot.value) || expected.some(([key,value])=>canonical((preview.snapshot.value as Record<string,unknown>)[String(key)])!==canonical(value))) {invalidate();invalid('保存预览与声明字段不一致')}
      }
      return {preview,action,session:live}
    },
  }
}
