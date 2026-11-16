import { useEffect, useRef, useState } from 'react'
import { ArrowUpRight, FileClock, LoaderCircle, RefreshCw, Search } from 'lucide-react'
import { api } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import './artifacts.css'
type Activity={id:string;origin:string;run_id:string;step_id:string;at:number;summary:string;tool:string;path:string;root:string;status:string;duration_ms:number|null;result:string;locatable:boolean;location_reason:string}
type Listing={items:Activity[];more:boolean;next_offset:number|null;snapshot:Record<string,number>}
const labels:Record<string,string>={complete:'已完成',running:'执行中',waiting_approval:'待审批',denied:'已拒绝',expired:'已过期',stopped:'已停止',unknown:'结果待核对',failed:'失败'}
export function FileActivity({sessionId,active,connected,onOpen}:{sessionId:number;active:boolean;connected:boolean;onOpen:(path:string)=>void}) {
  const [items,setItems]=useState<Activity[]>([]),[query,setQuery]=useState(''),[search,setSearch]=useState(''),[next,setNext]=useState<number|null>(null),[expanded,setExpanded]=useState(false)
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[reload,setReload]=useState(0)
  const snapshot=useRef<Record<string,number>>({})
  const generation=useRef(0),lock=useRef(false),extended=useRef(false),controller=useRef<AbortController|undefined>(undefined)
  useEffect(()=>{
    if(!active||!connected)return
    const owner=new AbortController();controller.current=owner;const request=++generation.current
    const load=async(initial=false)=>{
      if(lock.current||document.visibilityState==='hidden'&&!initial)return
      lock.current=true;if(initial)setBusy(true)
      try{const result=await api<Listing>(`/v9/workspace/activity?session_id=${sessionId}&query=${encodeURIComponent(search)}`,{signal:owner.signal})
        if(request===generation.current){setItems(result.items);setNext(result.next_offset);snapshot.current=result.snapshot;extended.current=false;setExpanded(false);setError('')}}
      catch(failure){if(!owner.signal.aborted&&request===generation.current)setError((failure as Error).message)}
      finally{if(request===generation.current){lock.current=false;setBusy(false)}}
    }
    void load(true)
    const timer=window.setInterval(()=>{if(!extended.current)void load()},3000)
    return()=>{generation.current++;owner.abort();window.clearInterval(timer);lock.current=false}
  },[sessionId,active,connected,search,reload])
  const more=async()=>{
    if(next===null||lock.current)return
    lock.current=true;setBusy(true);const request=generation.current
    try{const result=await api<Listing>(`/v9/workspace/activity?session_id=${sessionId}&query=${encodeURIComponent(search)}&offset=${next}&${new URLSearchParams(Object.fromEntries(Object.entries(snapshot.current).map(([key,value])=>[key,String(value)])))}`,{signal:controller.current?.signal})
      if(request===generation.current){setItems(old=>[...old,...result.items.filter(item=>!old.some(row=>row.id===item.id))]);setNext(result.next_offset);extended.current=true;setExpanded(true);setError('')}}
    catch(failure){if(request===generation.current)setError((failure as Error).message)}
    finally{if(request===generation.current){lock.current=false;setBusy(false)}}
  }
  return <section className="file-activity" aria-label="文件活动"><div className="workspace-panel-heading"><h2>文件活动</h2><Button size="icon" variant="ghost" aria-label="刷新文件活动" disabled={busy||!connected} onClick={()=>{setExpanded(false);setReload(value=>value+1)}}><RefreshCw/></Button></div><p className="artifact-library-note">模型文件工具的过程记录；当前留下的修改请查看 Git 差异。{expanded?'已暂停自动刷新，刷新可返回最新记录。':''}</p><form className="artifact-library-search" onSubmit={event=>{event.preventDefault();setExpanded(false);setSearch(query)}}><Search/><Input aria-label="搜索文件活动" value={query} maxLength={200} placeholder="按路径或文件工具搜索" onChange={event=>setQuery(event.target.value)}/><Button size="sm" variant="outline" type="submit" disabled={!connected}>搜索</Button></form>{error&&<div role="alert" className="artifact-error">{error}<Button size="xs" variant="outline" onClick={()=>setReload(value=>value+1)}>重试</Button></div>}{busy&&<p role="status" className="artifact-loading"><LoaderCircle className="animate-spin"/>正在读取活动…</p>}{!connected&&<p className="artifact-unread">连接后可核对最新活动。</p>}<div className="file-activity-items">{!busy&&!items.length&&<p className="artifact-library-empty">{search?'没有匹配的文件活动。':'还没有模型文件工具活动。手动浏览文件不会伪造成模型操作。'}</p>}{items.map(item=><article key={item.id}><div className="file-activity-heading"><FileClock/><strong>{item.summary}</strong><span data-status={item.status}>{labels[item.status]||item.status}</span></div><p className="file-activity-path" title={item.root?`${item.root}/${item.path}`:item.path}>{item.path||item.root||'未登记目标路径'}</p><div className="file-activity-meta"><span>{item.origin==='task'?'任务步骤':'工作对话'} · {item.run_id.slice(0,8)}{item.step_id?` / ${item.step_id}`:''}</span><time dateTime={item.at?new Date(item.at*1000).toISOString():undefined}>{item.at?new Date(item.at*1000).toLocaleString('zh-CN',{hour12:false}):'时间未记录'}</time>{item.duration_ms!==null&&<span>{(item.duration_ms/1000).toFixed(2)} 秒</span>}</div>{item.result&&<pre>{item.result}</pre>}<div className="file-activity-target">{item.locatable?<Button size="xs" variant="ghost" onClick={()=>onOpen(item.path)}>打开文件<ArrowUpRight/></Button>:item.location_reason&&<span>{item.location_reason}；可按原路径核对。</span>}</div></article>)}</div>{next!==null&&<Button size="sm" variant="outline" disabled={busy||!connected} onClick={()=>void more()}>继续加载活动</Button>}</section>
}
