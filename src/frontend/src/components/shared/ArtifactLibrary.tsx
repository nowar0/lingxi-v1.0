import { useEffect, useRef, useState } from 'react'
import { ArrowDownToLine, ArrowLeft, FileText, History, LoaderCircle, RefreshCw, Search } from 'lucide-react'
import { toast } from 'sonner'
import { api, json } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ArtifactPreview } from './ArtifactPreview'
import { artifactPath, artifactReference, artifactSize, type Artifact, type Excerpt } from '@/lib/artifacts'
import { useApp } from '@/context/AppContext'
import './artifacts.css'

type Listing={items:Artifact[];more:boolean;next_offset:number|null}
const originLabel=(item:Artifact)=>({upload:'上传',workspace:'项目文件',revision:'新版本',restore:'恢复版本'}[item.origin.kind]||'已有附件')
export function ArtifactLibrary({sessionId,active,connected}:{sessionId:number;active:boolean;connected:boolean}) {
  const {activeWorkSessionId,drafts,setDraft}=useApp()
  const composer=useRef({sid:activeWorkSessionId,draft:drafts.work});composer.current={sid:activeWorkSessionId,draft:drafts.work}
  const [items,setItems]=useState<Artifact[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState(''),[query,setQuery]=useState(''),[search,setSearch]=useState(''),[next,setNext]=useState<number|null>(null),[reload,setReload]=useState(0)
  const [preview,setPreview]=useState<Artifact>(),[history,setHistory]=useState<Artifact>(),[versions,setVersions]=useState<Artifact[]>([]),[versionNext,setVersionNext]=useState<number|null>(null)
  const [selected,setSelected]=useState<string>(),[comparison,setComparison]=useState<{source:Artifact;latest:Artifact;left:Excerpt;right:Excerpt}>(),[restoring,setRestoring]=useState(false)
  const generation=useRef(0),historyGeneration=useRef(0),live=useRef(true),operation=useRef(false)
  useEffect(()=>{live.current=true;return()=>{live.current=false;generation.current++;historyGeneration.current++}},[])
  useEffect(()=>{
    if(!active||!connected)return
    const request=++generation.current,owner=new AbortController();setBusy(true);setError('')
    void api<Listing>(`/v9/artifacts?session_id=${sessionId}&query=${encodeURIComponent(search)}`,{signal:owner.signal}).then(value=>{
      if(request===generation.current){setItems(value.items);setNext(value.next_offset)}
    }).catch(failure=>{if(!owner.signal.aborted&&request===generation.current)setError((failure as Error).message)})
      .finally(()=>{if(request===generation.current)setBusy(false)})
    return()=>{generation.current++;owner.abort()}
  },[sessionId,active,connected,search,reload])
  const more=async()=>{
    if(next===null||busy)return
    const request=generation.current;setBusy(true);setError('')
    try{const value=await api<Listing>(`/v9/artifacts?session_id=${sessionId}&offset=${next}&query=${encodeURIComponent(search)}`)
      if(live.current&&request===generation.current){setItems(old=>[...old,...value.items.filter(item=>!old.some(row=>row.id===item.id))]);setNext(value.next_offset)}}
    catch(failure){if(live.current&&request===generation.current)setError((failure as Error).message)}
    finally{if(live.current&&request===generation.current)setBusy(false)}
  }
  const openHistory=async(item:Artifact,offset=0)=>{
    if(operation.current)return
    const request=++historyGeneration.current;setHistory(item);setSelected(undefined);setComparison(undefined);if(!offset)setVersions([])
    setBusy(true);setError('')
    try{const value=await api<Listing>(artifactPath(item.id,sessionId,`/versions?offset=${offset}`))
      if(live.current&&request===historyGeneration.current){setVersions(old=>offset?[...old,...value.items]:value.items);setVersionNext(value.next_offset)}}
    catch(failure){if(live.current&&request===historyGeneration.current)setError((failure as Error).message)}
    finally{if(live.current&&request===historyGeneration.current)setBusy(false)}
  }
  const compare=async()=>{
    const item=versions.find(row=>row.id===selected)
    if(!item||operation.current)return
    const request=++historyGeneration.current;setBusy(true);setError('');setComparison(undefined)
    try{
      const source=await api<Artifact>(artifactPath(item.id,sessionId))
      const latest=await api<Artifact>(artifactPath(source.latest_id,sessionId))
      const [left,right]=await Promise.all([api<Excerpt>(artifactPath(source.id,sessionId,'/excerpt')),api<Excerpt>(artifactPath(latest.id,sessionId,'/excerpt'))])
      if(live.current&&request===historyGeneration.current)setComparison({source,latest,left,right})
    }catch(failure){if(live.current&&request===historyGeneration.current)setError((failure as Error).message)}
    finally{if(live.current&&request===historyGeneration.current)setBusy(false)}
  }
  const restore=async()=>{
    if(!comparison||operation.current||restoring)return
    operation.current=true;setRestoring(true);setError('')
    try{
      const item=await api<Artifact>(`/v9/artifacts/${comparison.source.id}/restore`,json('POST',{session_id:sessionId,expected_revision:comparison.latest.family_revision}))
      if(live.current){setComparison(undefined);setHistory(undefined);setReload(value=>value+1);toast.success(`已另存为版本 ${item.version}`)}
    }catch(failure){if(live.current)setError((failure as Error).message)}
    finally{operation.current=false;if(live.current)setRestoring(false)}
  }
  const reference=(value:string)=>{
    if(composer.current.sid!==sessionId){toast.error('会话已切换，请返回原会话引用');return}
    const content=[composer.current.draft.trimEnd(),artifactReference(preview?.name||'附件',value)].filter(Boolean).join('\n\n')
    if(content.length>8000){toast.error('加入引用后草稿超过8000字，请先缩短');return}
    setDraft('work',content);toast.success('已加入当前工作草稿')
  }
  return <section className="artifact-library" aria-label="产物与附件"><div className="workspace-panel-heading"><h2>产物与附件</h2><Button size="icon" variant="ghost" aria-label="刷新产物列表" disabled={busy||!connected} onClick={()=>setReload(value=>value+1)}><RefreshCw/></Button></div><p className="artifact-library-note">当前会话的原件与版本。引用进入工作草稿，恢复会另存新版本。</p><form className="artifact-library-search" onSubmit={event=>{event.preventDefault();setSearch(query)}}><Search/><Input aria-label="搜索产物名称" placeholder="按文件名搜索" value={query} maxLength={200} onChange={event=>setQuery(event.target.value)}/><Button size="sm" variant="outline" type="submit" disabled={!connected||busy}>搜索</Button></form>
    {!connected&&<p className="artifact-unread">连接本地服务后可核对原件。</p>}{error&&<div className="artifact-error" role="alert">{error}</div>}{busy&&<p role="status" className="artifact-loading"><LoaderCircle className="animate-spin"/>正在读取产物…</p>}
    <div className="artifact-library-items">{!busy&&!items.length&&<p className="artifact-library-empty">{search?'没有匹配名称的产物。':'还没有附件或产物。上传文件、生成媒体或预览项目原件后会出现在这里。'}</p>}{items.map(item=><article key={item.id}><FileText/><div><strong title={item.name}>{item.name||'未命名附件'}</strong><small>{artifactSize(item.size)} · {originLabel(item)} · 版本 {item.version}</small><small title={item.sha256}>SHA256 {item.sha256.slice(0,12)}… · 打开时校验原件</small></div><div className="artifact-library-actions"><Button size="xs" variant="ghost" onClick={()=>setPreview(item)}>预览</Button><Button size="icon" variant="ghost" title="查看版本" aria-label={`查看版本：${item.name}`} disabled={busy} onClick={()=>void openHistory(item)}><History/></Button><a aria-label={`下载原件：${item.name}`} href={`${item.url}&download=true`} download={item.name}><ArrowDownToLine/></a></div></article>)}</div>{next!==null&&<Button size="sm" variant="outline" disabled={busy||!connected} onClick={()=>void more()}>继续加载</Button>}
    {preview&&<ArtifactPreview id={preview.id} sessionId={sessionId} open onOpenChange={open=>{if(!open)setPreview(undefined)}} onReference={reference}/>}
    <Dialog open={!!history} onOpenChange={open=>{if(!open&&!restoring){historyGeneration.current++;setBusy(false);setHistory(undefined);setComparison(undefined)}}}><DialogContent className="artifact-history"><DialogHeader><DialogTitle>{history?.name} · 版本</DialogTitle><DialogDescription>每个版本保留独立原件；另存新版本不会改写项目磁盘文件。</DialogDescription></DialogHeader>{error&&<div className="artifact-error" role="alert">{error}</div>}{comparison?<><Button size="sm" variant="ghost" className="justify-self-start" disabled={restoring} onClick={()=>setComparison(undefined)}><ArrowLeft/>返回版本列表</Button><div className="artifact-compare"><div><h3>选定版本 {comparison.source.version}</h3><small>{comparison.source.sha256.slice(0,12)}…</small><pre>{comparison.left.supported?comparison.left.text:comparison.left.reason}</pre></div><div><h3>当前版本 {comparison.latest.version}</h3><small>{comparison.latest.sha256.slice(0,12)}…</small><pre>{comparison.right.supported?comparison.right.text:comparison.right.reason}</pre></div></div><p className="artifact-library-note">文字仅比较前200行、最多8000字；其余内容未载入。请预览或下载原件核对完整版本。</p><Button disabled={restoring||comparison.source.id===comparison.latest.id||!connected} onClick={()=>void restore()}>{restoring?'正在另存…':`将版本 ${comparison.source.version} 另存为新版本`}</Button></>:<><div className="artifact-version-list">{versions.map(item=><label key={item.id}><input type="radio" name="artifact-version" checked={selected===item.id} onChange={()=>setSelected(item.id)}/><span>版本 {item.version}{item.id===item.latest_id?' · 当前':''}<small>{originLabel(item)} · {artifactSize(item.size)} · {item.sha256.slice(0,12)}…</small></span><Button size="xs" variant="ghost" onClick={()=>setPreview(item)}>预览原件</Button></label>)}</div>{versionNext!==null&&history&&<Button size="sm" variant="outline" disabled={busy} onClick={()=>void openHistory(history,versionNext)}>继续加载版本</Button>}<Button disabled={!selected||busy||!connected} onClick={()=>void compare()}>与当前版本比较</Button></>}</DialogContent></Dialog>
  </section>
}
