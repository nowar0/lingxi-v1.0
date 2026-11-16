import type { ManagerFetch } from '@/lib/programManager'

const HEX=/^[a-f0-9]{64}$/
const BASE='/v11/program-manager'
const POST_PATHS=new Set(['/install/preview','/rollback/preview','/uninstall/preview','/commit','/uninstall/commit','/cancel','/retire','/recover/preview','/recover','/transactions/adopt'])
const REOPEN='管理器启动能力无效、已过期或已消费。请关闭页面，并从宿主重新打开；不会自动重试交换。'
export class ManagerSessionError extends Error { constructor(){super(REOPEN);this.name='ManagerSessionError'} }

/** Pure whitelist: the session header can never leave the fixed same-origin API. */
export function permittedManagerRequest(path:string,method='GET'):boolean {
  if(typeof path!=='string'||typeof method!=='string')return false
  if(method==='GET')return path===BASE||path===BASE+'/transactions'||new RegExp('^'+BASE+'/plans/[A-Za-z0-9_-]{32,100}$').test(path)
  return method==='POST'&&path.startsWith(BASE)&&POST_PATHS.has(path.slice(BASE.length))
}

type BootstrapLocation={hash:string;origin:string;pathname:string}
type StartOptions={location:BootstrapLocation;clearFragment:(pathname:string)=>void;fetcher?:ManagerFetch;onExpired?:()=>void;timeoutMs?:number}
/** One invocation per page, before rendering. The bootstrap fragment is erased first. */
export async function beginManagerSession({location,clearFragment,fetcher=(path,init)=>fetch(path,init),onExpired=()=>undefined,timeoutMs=15000}:StartOptions):Promise<ManagerFetch> {
  let bootstrap=location.hash
  clearFragment(location.pathname)
  if(!Number.isFinite(timeoutMs)||timeoutMs<1||timeoutMs>15000)throw new ManagerSessionError()
  let origin:URL
  try{origin=new URL(location.origin)}catch{throw new ManagerSessionError()}
  if(origin.protocol!=='http:'||origin.hostname!=='127.0.0.1'||!origin.port||origin.username||origin.password||origin.origin!==location.origin)throw new ManagerSessionError()
  const match=/^#bootstrap=([a-f0-9]{64})$/.exec(bootstrap)
  bootstrap=''
  if(!match)throw new ManagerSessionError()
  const controller=new AbortController()
  let reader:ReadableStreamDefaultReader<Uint8Array>|undefined
  let timer:ReturnType<typeof setTimeout>|undefined
  const stop=new Promise<never>((_,reject)=>{timer=setTimeout(()=>{controller.abort();void reader?.cancel().catch(()=>undefined);reject(new ManagerSessionError())},timeoutMs)})
  let token=''
  try {
    const work=(async()=>{
      const response=await fetcher('/manager/session',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'omit',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',signal:controller.signal,body:JSON.stringify({bootstrap:match[1]})})
      if(!response.ok||response.redirected||!/^application\/json(?:;|$)/i.test(response.headers.get('content-type')??''))throw new ManagerSessionError()
      const declared=response.headers.get('content-length')
      if(declared!==null&&(!/^\d+$/.test(declared)||Number(declared)>4096))throw new ManagerSessionError()
      if(!response.body)throw new ManagerSessionError()
      reader=response.body.getReader();let bytes=0;let raw='';const decoder=new TextDecoder('utf-8',{fatal:true})
      while(true){const next=await reader.read();if(next.done)break;bytes+=next.value.byteLength;if(bytes>4096)throw new ManagerSessionError();raw+=decoder.decode(next.value,{stream:true})}
      raw+=decoder.decode()
      if(!/^\s*\{\s*"session_token"\s*:\s*"[a-f0-9]{64}"\s*\}\s*$/.test(raw))throw new ManagerSessionError()
      const value:unknown=JSON.parse(raw)
      if(value===null||typeof value!=='object'||Array.isArray(value)||Object.keys(value).length!==1||!('session_token'in value)||typeof value.session_token!=='string'||!HEX.test(value.session_token))throw new ManagerSessionError()
      return value.session_token
    })()
    token=await Promise.race([work,stop])
    if(controller.signal.aborted)throw new ManagerSessionError()
  } catch {controller.abort();void reader?.cancel().catch(()=>undefined);throw new ManagerSessionError()}
  finally {if(timer!==undefined)clearTimeout(timer);match[1]=''}
  let expired=false
  return async(path,init={})=>{
    if(expired||!token||!permittedManagerRequest(path,init.method??'GET'))throw new ManagerSessionError()
    const headers=new Headers(init.headers)
    headers.set('X-Lingxi-Manager-Session',token)
    const response=await fetcher(path,{...init,headers,credentials:'omit',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer'})
    if(response.redirected||response.status===401||response.status===403){expired=true;token='';onExpired();void response.body?.cancel().catch(()=>undefined);throw new ManagerSessionError()}
    return response
  }
}
