/** Public-key trust only. No private-key generation, package execution or implicit trust. */
export type TrustKey = {key_id:string;public_key:string;display_name:string;fingerprint:string;status:'trusted'|'revoked'}
export type TrustSnapshot = {schema:'lingxi.plugin-trust.v1';revision:number;fingerprint:string;keys:TrustKey[];exists:boolean;requires_retrust?:boolean}
export type TrustListing = TrustSnapshot & {approval_required:true;private_keys:false;automatic_trust:false;backup_exported:false;notice:string}
export type TrustPreview = {snapshot:{action:'add'|'revoke'|'trust';expected_revision:number;key:TrustKey;trust_fingerprint:string;notice:string};token:string;fingerprint:string;expires_in:number;approval_required:true;expiresAt:number}
export type TrustFetch = (path:string,options?:RequestInit)=>Promise<Response>
const obj=(v:unknown):v is Record<string,unknown>=>v!==null&&typeof v==='object'&&!Array.isArray(v)
const sha=(v:unknown):v is string=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v)
const revision=(v:unknown):v is number=>typeof v==='number'&&Number.isSafeInteger(v)&&v>=0
const ident=(v:unknown):v is string=>typeof v==='string'&&/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(v)
const text=(v:unknown):v is string=>typeof v==='string'&&v.trim().length>0&&v.length<=200&&!/[\x00-\x1f\x7f]/.test(v)
function invalid(message='可信公钥响应合同无效；未自动信任或执行插件'):never{throw Error(message)}
export function validateTrustPublicKey(value:unknown):string {
 if(typeof value!=='string'||value.length!==44||!/^[A-Za-z0-9+/]{43}=$/.test(value))invalid('只接受32字节Ed25519公钥的规范base64；不能输入私钥')
 try{const raw=atob(value);if(raw.length!==32||btoa(raw)!==value)invalid('公钥编码无效')}catch{invalid('公钥编码无效；不能输入私钥')}
 return value
}
export async function trustPublicFingerprint(publicKey:string):Promise<string>{
 const raw=atob(validateTrustPublicKey(publicKey));const bytes=Uint8Array.from(raw,c=>c.charCodeAt(0))
 return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),v=>v.toString(16).padStart(2,'0')).join('')
}
function parseKey(value:unknown):TrustKey {
 if(!obj(value)||!ident(value.key_id)||!text(value.display_name)||!sha(value.fingerprint)||!['trusted','revoked'].includes(String(value.status)))invalid()
 if(Object.keys(value).some(k=>!['key_id','public_key','display_name','fingerprint','status'].includes(k)))invalid()
 validateTrustPublicKey(value.public_key);return value as TrustKey
}
export function parseTrustSnapshot(value:unknown):TrustSnapshot {
 if(!obj(value)||value.schema!=='lingxi.plugin-trust.v1'||!revision(value.revision)||!sha(value.fingerprint)||typeof value.exists!=='boolean'||!Array.isArray(value.keys)||value.keys.length>128)invalid()
 const ids=new Set(),publicKeys=new Set()
 for(const raw of value.keys){const key=parseKey(raw);if(ids.has(key.key_id)||publicKeys.has(key.public_key))invalid('发布者ID或同公钥别名重复，不能绕过撤销');ids.add(key.key_id);publicKeys.add(key.public_key)}
 if(value.requires_retrust!==undefined&&typeof value.requires_retrust!=='boolean')invalid()
 if(!value.exists&&(value.revision!==0||value.keys.length))invalid()
 return value as TrustSnapshot
}
export function parseTrustListing(value:unknown):TrustListing {
 parseTrustSnapshot(value)
 if(!obj(value)||value.approval_required!==true||value.private_keys!==false||value.automatic_trust!==false||value.backup_exported!==false||typeof value.notice!=='string'||value.notice.length>2000)invalid()
 return value as TrustListing
}
export function trustOperationProblem(preview:TrustPreview,now=Date.now()):string {
 if(!preview||preview.approval_required!==true||!Number.isFinite(preview.expiresAt)||preview.expiresAt<=now)return '预览已过期，请重新读取并生成预览；不自动重试'
 return ''
}
function errorMessage(value:unknown):string {
 if(obj(value)){if(typeof value.detail==='string')return value.detail;if(obj(value.detail)&&typeof value.detail.message==='string')return value.detail.message;if(typeof value.message==='string')return value.message}
 return '可信公钥操作未完成；结果未知时不会自动重试'
}
export function createPluginTrustClient({fetcher=(path,options)=>fetch(path,options),now=()=>Date.now(),monotonicNow=()=>performance.now()}: {fetcher?:TrustFetch;now?:()=>number;monotonicNow?:()=>number}={}){
 const used=new Set<string>();const issued=new Map<string,{fingerprint:string;snapshot:string;deadline:number}>();let generation=0
 const invalidate=()=>{generation++;issued.clear()}
 async function request(path:string,body?:unknown,signal?:AbortSignal):Promise<unknown>{
  const epoch=generation
  const requestSignal=signal?AbortSignal.any([signal,AbortSignal.timeout(15000)]):AbortSignal.timeout(15000)
  const response=await fetcher('/v11/plugin-trust'+path,{method:body===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',headers:body===undefined?undefined:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:requestSignal})
  let value:unknown;try{value=await response.json()}catch{invalid('服务未返回有效JSON，结果未知；不要重复提交')}
  if(epoch!==generation||requestSignal.aborted)invalid('旧可信发布者响应已丢弃或请求已取消')
  if(!response.ok)throw Error(errorMessage(value))
  return value
 }
 async function checkKeys(value:TrustSnapshot){for(const key of value.keys)if(await trustPublicFingerprint(key.public_key)!==key.fingerprint)invalid('服务端公钥与指纹不一致')}
 async function preview(action:'add'|'revoke'|'trust',current:TrustSnapshot,key:TrustKey|{key_id:string;public_key:string;display_name:string},signal?:AbortSignal):Promise<TrustPreview>{
  if(!revision(current.revision)||!sha(current.fingerprint)||!ident(key.key_id))invalid('当前信任库未核实，请先只读刷新')
  if(action==='add'){
   validateTrustPublicKey(key.public_key);if(!text(key.display_name))invalid('发布者名称必须是1～200字单行文字')
   if(current.keys.some(k=>k.key_id===key.key_id||k.public_key===key.public_key))invalid('发布者ID或相同公钥已登记，请使用原记录的独立重新信任流程')
  }else{
   const actual=current.keys.find(k=>k.key_id===key.key_id)
   if(!actual||actual.public_key!==key.public_key||actual.status!==(action==='revoke'?'trusted':'revoked'))invalid('发布者状态已变化，请刷新后重新预览')
  }
  const body=action==='add'?{key_id:key.key_id,public_key:key.public_key,display_name:key.display_name,expected_revision:current.revision}:{key_id:key.key_id,expected_revision:current.revision}
  const requestedAt=now();const started=monotonicNow();const epoch=generation
  const value=await request(`/${action==='trust'?'retrust':action}/preview`,body,signal)
  if(!obj(value)||!obj(value.snapshot)||value.approval_required!==true||typeof value.token!=='string'||!/^[A-Za-z0-9_-]{20,200}$/.test(value.token)||!sha(value.fingerprint)||typeof value.expires_in!=='number'||!Number.isFinite(value.expires_in)||value.expires_in<=0||value.expires_in>600)invalid()
  if(Object.keys(value).some(key=>!['snapshot','token','fingerprint','expires_in','approval_required'].includes(key))||Object.keys(value.snapshot).some(key=>!['action','expected_revision','key','trust_fingerprint','notice'].includes(key)))invalid()
  const snapshot=value.snapshot;const returned=parseKey(snapshot.key)
  if(snapshot.action!==action||snapshot.expected_revision!==current.revision||snapshot.trust_fingerprint!==current.fingerprint||typeof snapshot.notice!=='string'||snapshot.notice.length>2000||returned.key_id!==key.key_id||returned.public_key!==key.public_key||returned.display_name!==key.display_name||returned.status!==(action==='revoke'?'revoked':'trusted'))invalid('批准快照与当前发布者、公钥或动作不一致')
  if(await trustPublicFingerprint(returned.public_key)!==returned.fingerprint)invalid('批准公钥指纹不一致')
  if(epoch!==generation||signal?.aborted)invalid('旧可信发布者响应已丢弃')
  if(issued.size>=128)invalid('待确认公钥预览过多，请刷新后重新操作')
  issued.set(value.token,{fingerprint:value.fingerprint,snapshot:JSON.stringify(snapshot),deadline:started+value.expires_in*1000})
  return {...value,expiresAt:requestedAt+value.expires_in*1000} as TrustPreview
 }
 return {invalidate,
  async list(connection:string,signal?:AbortSignal){if(connection!=='connected')return undefined;const epoch=generation;const value=parseTrustListing(await request('',undefined,signal));await checkKeys(value);if(epoch!==generation||signal?.aborted)invalid('旧可信发布者响应已丢弃');return value},
  add:(current:TrustSnapshot,input:{key_id:string;public_key:string;display_name:string},signal?:AbortSignal)=>preview('add',current,input,signal),
  revoke:(current:TrustSnapshot,key:TrustKey,signal?:AbortSignal)=>preview('revoke',current,key,signal),
  retrust:(current:TrustSnapshot,key:TrustKey,signal?:AbortSignal)=>preview('trust',current,key,signal),
  async commit(value:TrustPreview,acknowledged:boolean,signal?:AbortSignal){
   if(acknowledged!==true)invalid('需单独核对完整公钥快照并勾选确认')
   const problem=trustOperationProblem(value,now());if(problem)invalid(problem)
   if(used.has(value.token))invalid('本次批准已使用或尝试，不会重复提交')
   const original=issued.get(value.token)
   if(!original||original.fingerprint!==value.fingerprint||original.snapshot!==JSON.stringify(value.snapshot))invalid('预览不是当前客户端生成的原始快照，请重新预览')
   if(monotonicNow()>=original.deadline)invalid('预览已过期，请重新读取并生成预览；不自动重试')
   if(!/^[A-Za-z0-9_-]{20,200}$/.test(value.token)||!sha(value.fingerprint))invalid('批准凭据无效')
   if(used.size>=4096)invalid('本页操作容量已满，请重新打开可信发布者分区')
   used.add(value.token);issued.delete(value.token)
   const epoch=generation
   const result=parseTrustSnapshot(await request('/commit',{token:value.token,fingerprint:value.fingerprint},signal));await checkKeys(result)
   if(epoch!==generation||signal?.aborted)invalid('旧可信发布者响应已丢弃')
   const changed=result.keys.find(key=>key.key_id===value.snapshot.key.key_id)
   if(result.revision!==value.snapshot.expected_revision+1||!changed||(['key_id','public_key','display_name','fingerprint','status'] as const).some(name=>changed[name]!==value.snapshot.key[name]))invalid('提交结果与批准的公钥快照不一致，结果未知，不自动重试')
   return result
  },
 }
}
