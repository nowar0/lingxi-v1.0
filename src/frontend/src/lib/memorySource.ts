import { api } from '../api/client'

/** Current private-domain provenance only. No arbitrary message/session selector. */
export type MemorySourceMode = 'companion' | 'work'
export interface MemorySourceIdentity { entityId: string; expectedRevision: number; mode: MemorySourceMode; sessionId?: number }
export interface MemorySourceMessage { id: number; session_id: number; session_mode: MemorySourceMode; role: string; content: string; at: string | null; speaker: Record<string, unknown> | null }
export interface MemorySourcePage { entity_id: string; revision: number; version_id: string; source_status: 'verified'; read_only: true; total: number; offset: number; limit: number; items: MemorySourceMessage[] }
export type MemorySourceRequest = (path: string, options?: RequestInit) => Promise<unknown>
export const MEMORY_SOURCE_PAGE_SIZE = 10
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const integer = (value: unknown, min: number, max: number): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= min && value <= max
function invalid(message = '原消息返回格式无效；未显示或推测原文'): never { throw new Error(message) }
function keys(value: Record<string, unknown>, expected: string[]) { if (Object.keys(value).length !== expected.length || expected.some(key => !Object.prototype.hasOwnProperty.call(value, key))) invalid() }
function validate(identity: MemorySourceIdentity, offset: number, limit: number) {
  if (typeof identity.entityId !== 'string' || !identity.entityId.length || identity.entityId.length > 200 || !integer(identity.expectedRevision,1,2147483647) || !['companion','work'].includes(identity.mode) || identity.sessionId !== undefined && !integer(identity.sessionId,1,Number.MAX_SAFE_INTEGER) || identity.mode === 'work' && identity.sessionId === undefined || !integer(offset,0,200) || !integer(limit,1,20)) invalid('原消息请求缺少当前模式、会话或记忆修订')
}
function finiteJson(value: unknown, depth=0): boolean {
  if(depth>20)return false
  if(value===null||typeof value==='string'||typeof value==='boolean')return true
  if(typeof value==='number')return Number.isFinite(value)
  if(Array.isArray(value))return value.length<=100&&value.every(child=>finiteJson(child,depth+1))
  return object(value)&&Object.keys(value).length<=100&&Object.values(value).every(child=>finiteJson(child,depth+1))
}
export function parseMemorySourcePage(value: unknown, identity: MemorySourceIdentity, offset: number, limit: number): MemorySourcePage {
  validate(identity,offset,limit)
  if(!object(value))invalid()
  keys(value,['entity_id','revision','version_id','source_status','read_only','total','offset','limit','items'])
  if(value.entity_id!==identity.entityId||value.revision!==identity.expectedRevision||typeof value.version_id!=='string'||!value.version_id.startsWith(identity.entityId+':')||!/^\d+$/.test(value.version_id.slice(identity.entityId.length+1))||!integer(Number(value.version_id.slice(identity.entityId.length+1)),1,identity.expectedRevision)||value.source_status!=='verified'||value.read_only!==true||!integer(value.total,1,200)||value.offset!==offset||value.limit!==limit||!Array.isArray(value.items)||value.items.length!==Math.min(limit,Math.max(0,value.total-offset)))invalid('原消息与当前记忆修订、来源状态或分页不一致')
  const seen=new Set<number>();let previous=0
  for(const raw of value.items){
    if(!object(raw))invalid()
    keys(raw,['id','session_id','session_mode','role','content','at','speaker'])
    if(!integer(raw.id,1,Number.MAX_SAFE_INTEGER)||raw.id<=previous||seen.has(raw.id)||!integer(raw.session_id,1,Number.MAX_SAFE_INTEGER)||raw.session_mode!==identity.mode||identity.mode==='work'&&raw.session_id!==identity.sessionId||typeof raw.role!=='string'||!raw.role.length||raw.role.length>100||typeof raw.content!=='string'||raw.at!==null&&typeof raw.at!=='string'||raw.speaker!==null&&(!object(raw.speaker)||!finiteJson(raw.speaker)||JSON.stringify(raw.speaker).length>16384))invalid('原消息角色、会话或内容无效')
    seen.add(raw.id);previous=raw.id
  }
  if(new TextEncoder().encode(JSON.stringify(value)).length>512*1024)invalid('原消息页超过容量限制，请减小每页数量')
  return value as unknown as MemorySourcePage
}
export function createMemorySourceClient(request: MemorySourceRequest = api) {
  let generation=0, active:AbortController|undefined
  const cancel=()=>{generation++;active?.abort();active=undefined}
  return {
    cancel,
    async load(identity:MemorySourceIdentity,offset=0,limit=MEMORY_SOURCE_PAGE_SIZE):Promise<MemorySourcePage>{
      validate(identity,offset,limit);cancel();const owner=generation;const controller=new AbortController();active=controller
      const query=new URLSearchParams({expected_revision:String(identity.expectedRevision),mode:identity.mode,offset:String(offset),limit:String(limit)})
      if(identity.mode==='work')query.set('session_id',String(identity.sessionId))
      const timer=setTimeout(()=>controller.abort(),15000)
      try{
        const result=await request(`/v10/memory-sources/${encodeURIComponent(identity.entityId)}/messages?${query}`,{method:'GET',signal:controller.signal})
        if(owner!==generation||controller.signal.aborted)throw new DOMException('旧原消息响应已丢弃','AbortError')
        return parseMemorySourcePage(result,identity,offset,limit)
      }finally{clearTimeout(timer);if(owner===generation)active=undefined}
    },
  }
}
