import { useEffect, useState } from 'react'
import { api } from '@/api/client'
import type { Usage } from '@/types'
import { useApp } from '@/context/AppContext'

export function MemberUsage({jobId,nodeId,revision}:{jobId:string;nodeId:string;revision?:number}){
  const {connection}=useApp()
  const [usage,setUsage]=useState<Usage>(),[error,setError]=useState(''),[retry,setRetry]=useState(0)
  useEffect(()=>{const c=new AbortController();let reading=false;setUsage(undefined);setError('');const read=async()=>{if(connection!=='connected'||reading)return;reading=true;try{const value=await api<Usage>(`/v6/jobs/${encodeURIComponent(jobId)}/members/${encodeURIComponent(nodeId)}/usage`,{signal:c.signal});if(!c.signal.aborted){setUsage(value);setError('')}}catch(e){if(!c.signal.aborted)setError((e as Error).message)}finally{reading=false}};void read();const timer=window.setInterval(read,2500);return()=>{c.abort();window.clearInterval(timer)}},[jobId,nodeId,revision,retry,connection])
  return <div className="member-usage" aria-label="当前成员用量"><h4>当前成员用量</h4>{usage?.用量完整===false&&<small>部分用量未上报</small>}{error?<p role="alert">读取失败<button onClick={()=>setRetry(n=>n+1)}>重试</button></p>:!usage?<p>{connection==='connected'?'正在读取…':'未连接'}</p>:<dl><div><dt>累计</dt><dd>{((usage.总输入||0)+(usage.总输出||0)).toLocaleString()} tok</dd></div><div><dt>输入</dt><dd>{(usage.总输入||0).toLocaleString()}</dd></div><div><dt>输出</dt><dd>{(usage.总输出||0).toLocaleString()}</dd></div><div><dt>缓存读取</dt><dd>{(usage.总缓存||0).toLocaleString()}</dd></div></dl>}</div>
}
