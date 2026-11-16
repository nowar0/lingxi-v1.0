import {toast} from 'sonner'
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Download, FileText, LoaderCircle, RefreshCw } from 'lucide-react'
import { api } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { MessageContent } from './MessageContent'
import { useApp } from '@/context/AppContext'
import { ModelDocumentConversion } from './ModelDocumentConversion'
import { ImageEditor } from './ImageEditor'
import { supportsDocumentConversion } from '@/lib/modelDocuments'
import { artifactPath, artifactSize, textArtifact, type Artifact, type Excerpt } from '@/lib/artifacts'
import './artifacts.css'

const PdfOriginalPreview=lazy(()=>import('./PdfOriginalPreview'))

type Entry = {path: string; size: number; directory: boolean; encrypted: boolean; readable: boolean}
type Props = {id: string; sessionId: number; open: boolean; onOpenChange: (open:boolean)=>void; onReference?: (reference: string)=>void;onExtractedText?:(text:string)=>boolean; initialReference?: string}
const htmlPolicy = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'">`

export function safeHtmlPreview(text: string) {
  const template=document.createElement('template');template.innerHTML=text
  const allowed=new Set(['P','DIV','SPAN','SECTION','ARTICLE','HEADER','FOOTER','H1','H2','H3','H4','H5','H6','STRONG','EM','B','I','U','S','DEL','BR','HR','PRE','CODE','BLOCKQUOTE','UL','OL','LI','DL','DT','DD','TABLE','THEAD','TBODY','TFOOT','TR','TD','TH','CAPTION','IMG','A','STYLE'])
  const clean=(parent: ParentNode)=>{
    for(const child of Array.from(parent.children)) {
      if(!allowed.has(child.tagName)){child.remove();continue}
      for(const attr of Array.from(child.attributes)) {
        const key=attr.name.toLowerCase()
        if(['class','style','title','colspan','rowspan','alt'].includes(key))continue
        if(key==='src'&&child.tagName==='IMG'&&new RegExp('^data:image/(?:png|jpeg|webp);base64,[a-zA-Z0-9+/=]+$').test(attr.value))continue
        if(key==='href'&&/^#[a-zA-Z0-9_-]+$/.test(attr.value))continue
        child.removeAttribute(attr.name)
      }
      clean(child)
    }
  }
  clean(template.content)
  return htmlPolicy+template.innerHTML
}

export function ArtifactPreview({id,sessionId,open,onOpenChange,onReference,onExtractedText,initialReference}: Props) {
  const {connection,activeSessionId,activeWorkSessionId,drafts,setDraft}=useApp()
  const draftDestination=useRef({activeSessionId,activeWorkSessionId,drafts});draftDestination.current={activeSessionId,activeWorkSessionId,drafts}
  const addExtracted=(text:string)=>{
    if(onExtractedText){if(onExtractedText(text))onOpenChange(false);return}
    const current=draftDestination.current,mode=sessionId===current.activeWorkSessionId?'work':sessionId===current.activeSessionId?'companion':undefined
    if(!mode){toast.error('请返回原会话后加入草稿');return}
    const next=[current.drafts[mode].trimEnd(),text].filter(Boolean).join('\n\n')
    if(next.length>8000){toast.error('摘录与草稿合计超过8000字，请缩小提取范围');return}
    setDraft(mode,next);onOpenChange(false);toast.success('摘录已加入草稿')
  }
  const [artifact,setArtifact]=useState<Artifact>(),[excerpt,setExcerpt]=useState<Excerpt>(),[entries,setEntries]=useState<Entry[]>([])
  const [member,setMember]=useState(''),[start,setStart]=useState(1),[end,setEnd]=useState(200),[rendered,setRendered]=useState(false)
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[reload,setReload]=useState(0)
  const generation=useRef(0)
  useEffect(()=>{
    const request=++generation.current,owner=new AbortController()
    setArtifact(undefined);setExcerpt(undefined);setEntries([]);setMember('');setStart(1);setEnd(200);setRendered(false);setError('')
    if(!open)return
    const selected=initialReference?.split('#')[1] || ''
    const range=selected.match(/L([0-9]+)-L([0-9]+)$/)
    const first=range?Number(range[1]):1,last=range?Number(range[2]):200
    const memberPart=selected.startsWith('zip=')?selected.slice(4,selected.lastIndexOf(';')):''
    let initialMember=''
    try{initialMember=decodeURIComponent(memberPart)}catch{ /* Invalid fragments do not select a member. */ }
    setStart(first);setEnd(last);setMember(initialMember)
    const params=new URLSearchParams({start:String(first),end:String(last),...(initialMember?{member:initialMember}:{})})
    setBusy(true)
    void api<Artifact>(artifactPath(id,sessionId),{signal:owner.signal}).then(async item=>{
      if(request!==generation.current)return
      setArtifact(item)
      if(item.preview_kind==='zip') {
        const value=await api<{items:Entry[]}>(artifactPath(id,sessionId,'/archive'),{signal:owner.signal})
        if(request===generation.current)setEntries(value.items)
        if(initialMember){
          const value=await api<Excerpt>(artifactPath(id,sessionId,'/excerpt?'+params),{signal:owner.signal})
          if(request===generation.current)setExcerpt(value)
        }
      } else if(textArtifact(item.mime)||['pdf','download'].includes(item.preview_kind)) {
        const value=await api<Excerpt>(artifactPath(id,sessionId,'/excerpt?'+params),{signal:owner.signal})
        if(request===generation.current)setExcerpt(value)
      }
    }).catch(failure=>{if(!owner.signal.aborted&&request===generation.current)setError((failure as Error).message)})
      .finally(()=>{if(request===generation.current)setBusy(false)})
    return ()=>{generation.current++;owner.abort()}
  },[id,sessionId,open,reload,initialReference])
  const read=async (selected=member)=>{
    if(!artifact||busy||!Number.isInteger(start)||!Number.isInteger(end)||start<1||end<start||end-start>1999||end>100000)return
    const request=++generation.current;setBusy(true);setError('');setExcerpt(undefined)
    try {
      const params=new URLSearchParams({start:String(start),end:String(end),...(selected?{member:selected}:{})})
      const value=await api<Excerpt>(artifactPath(id,sessionId,'/excerpt?'+params))
      if(request===generation.current){setExcerpt(value);setMember(selected);setRendered(false)}
    }catch(failure){if(request===generation.current)setError((failure as Error).message)}
    finally{if(request===generation.current)setBusy(false)}
  }
  // Changing a range invalidates its old result immediately. A selection can
  // only use the exact range which successfully returned from the backend.
  const rangeValid=!!excerpt?.supported&&excerpt.line_start===start&&!!excerpt.reference?.endsWith(`L${start}-L${end}`)
  const source=artifact?.origin
  if(artifact?.preview_kind==='image')return <ImageEditor src={artifact.url} name={artifact.name} open={open} onOpenChange={onOpenChange}/>
  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent className="artifact-preview"><DialogHeader><DialogTitle>{artifact?.name||'附件预览'}</DialogTitle><DialogDescription>{artifact?`${artifactSize(artifact.size)} · 版本 ${artifact.version} · ${source?.kind==='workspace'?'来自项目文件':source?.kind==='upload'?'上传原件':source?.kind==='revision'?'原件的新版本':'已有附件'}`:'正在读取附件信息…'}</DialogDescription></DialogHeader>
    {artifact&&<div className="artifact-source"><span title={artifact.sha256}>SHA256 {artifact.sha256.slice(0,12)}…</span>{source?.path&&<span title={`${source.root}/${source.path}`}>{source.path}</span>}<a className="artifact-download" href={`${artifact.url}&download=true`} download={artifact.name}><Download/>下载原件</a></div>}
    {error&&<div className="artifact-error" role="alert">{error}<Button size="xs" variant="outline" onClick={()=>setReload(value=>value+1)}><RefreshCw/>重新读取</Button></div>}
    {artifact?.preview_kind==='image'&&<img className="artifact-image" src={artifact.url} alt={artifact.name}/>}
    {artifact?.preview_kind==='audio'&&<audio controls preload="metadata" src={artifact.url} aria-label={artifact.name}/>}
    {artifact?.preview_kind==='pdf'&&<Suspense fallback={<p className="artifact-loading">正在加载PDF预览…</p>}><PdfOriginalPreview id={artifact.id} url={artifact.url}/></Suspense>}
    {artifact?.preview_kind==='zip'&&<div className="artifact-archive" aria-label="ZIP目录">{entries.length===0&&!busy&&<p>归档目录为空。</p>}{entries.map((entry,index)=><button key={`${index}:${entry.path}`} disabled={!entry.readable||busy} aria-pressed={member===entry.path} onClick={()=>void read(entry.path)}><FileText/><span>{entry.path}</span><small>{entry.encrypted?'加密项':entry.directory?'目录':!entry.readable?'仅保留原件':artifactSize(entry.size)}</small></button>)}</div>}
    {artifact&&(textArtifact(artifact.mime)||member)&&<div className="artifact-range"><label>起始行<input type="number" aria-label="片段起始行" min={1} max={100000} value={start} onChange={event=>setStart(Number(event.target.value))}/></label><label>结束行<input type="number" aria-label="片段结束行" min={start} max={Math.min(100000,start+1999)} value={end} onChange={event=>setEnd(Number(event.target.value))}/></label><Button size="sm" variant="outline" disabled={busy||start<1||end<start||end-start>1999||end>100000} onClick={()=>void read()}>读取此范围</Button>{['text/markdown','text/html'].includes(artifact.mime)&&excerpt?.supported&&<Button size="sm" variant="ghost" aria-pressed={rendered} onClick={()=>setRendered(value=>!value)}>{rendered?'查看原文':'渲染片段'}</Button>}</div>}
    {busy&&<p className="artifact-loading" role="status"><LoaderCircle className="animate-spin"/>正在读取所选资料…</p>}
    {excerpt?.supported&&<><p className="artifact-read-status" role="status">{excerpt.source.member?`${excerpt.source.member} · `:''}已读取第 {excerpt.line_start}–{excerpt.line_end} 行{excerpt.empty_range?' · 此范围没有内容':''}{excerpt.truncated?' · 已达片段字数上限，后续未读取':excerpt.has_more?' · 后续内容未读取':''}</p>{rendered&&artifact?.mime==='text/html'?<iframe className="artifact-html" sandbox="" referrerPolicy="no-referrer" title="HTML安全片段预览" srcDoc={safeHtmlPreview(excerpt.text||'')}/>:rendered&&artifact?.mime==='text/markdown'?<div className="artifact-rendered"><MessageContent text={excerpt.text||''}/></div>:<pre className="artifact-text" tabIndex={0}>{excerpt.text||'此范围没有文字。'}</pre>}</>}
    {excerpt&&!excerpt.supported&&<p className="artifact-unread" role="status">{artifact&&supportsDocumentConversion(artifact.name)?'旧摘录接口未读取正文；本地转换请使用下方独立预览与确认，转换结果不会自动进入聊天。':`尚未读取正文。${excerpt.reason}`}</p>}
    {artifact&&open&&supportsDocumentConversion(artifact.name)&&<ModelDocumentConversion key={`${artifact.id}:${sessionId}:${artifact.sha256}`} connected={connection==='connected'} onReference={addExtracted} source={{fid:artifact.id,session_id:sessionId,filename:artifact.name,mime:artifact.mime,size:artifact.size,sha256:artifact.sha256}}/>}
    {onReference&&<div className="artifact-reference-actions"><p>仅所选片段会用于对话；文件内容不会自动成为个人记忆。</p><Button disabled={!rangeValid||busy||!excerpt?.text} onClick={()=>{if(excerpt?.reference){onReference(excerpt.reference);onOpenChange(false)}}}>将此范围用于本轮</Button></div>}
  </DialogContent></Dialog>
}
