import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { createModelDocumentsClient, documentConversionKind, parseDocumentIndices, parseDocumentSheets, parsePdfPages, ModelDocumentError } from '@/lib/modelDocuments'
import type { ConversionPreview, ConversionResult, DocumentSource } from '@/lib/modelDocuments'

type Props={source:DocumentSource;connected:boolean;onReference?:(text:string)=>void}
/** Approved local extraction can enter the draft; sending remains a separate user action. */
export function ModelDocumentConversion({source,connected,onReference}:Props){
 const client=useRef<ReturnType<typeof createModelDocumentsClient>|null>(null)
 if(!client.current)client.current=createModelDocumentsClient()
 const api=client.current
 const [indices,setIndices]=useState(''),[sheets,setSheets]=useState(''),[hidden,setHidden]=useState(false),[limit,setLimit]=useState(6000)
 const [preview,setPreview]=useState<ConversionPreview>(),[result,setResult]=useState<ConversionResult>()
 const [busy,setBusy]=useState(false),[ack,setAck]=useState(false),[attempted,setAttempted]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[tick,setTick]=useState(0)
 const owner=useRef(0),lock=useRef(false),abort=useRef<AbortController|null>(null),current=useRef<ConversionPreview|undefined>(undefined)
 current.current=preview
 useEffect(()=>{
  owner.current++;api.invalidate();setPreview(undefined);setResult(undefined);setAck(false);setAttempted(false);setBusy(false);setError('');setNotice('');setIndices('');setSheets('');setHidden(false);setLimit(100000)
  return()=>{owner.current++;abort.current?.abort();const old=current.current;if(old&&!api.approvalProblem(old))void api.cancel(old).catch(()=>undefined);api.invalidate()}
 },[source.fid,source.session_id,source.sha256,source.filename,source.mime,source.size,connected,api])
 useEffect(()=>{if(!preview)return;const timer=window.setInterval(()=>setTick(v=>v+1),1000);return()=>window.clearInterval(timer)},[preview])
 const reset=()=>{const old=preview;if(old&&!api.approvalProblem(old))void api.cancel(old).catch(()=>undefined);owner.current++;abort.current?.abort();api.invalidate();setPreview(undefined);setResult(undefined);setAck(false);setAttempted(false);setError('');setNotice('选区已变化，旧转换批准失效；未发送模型。')}
 const run=async(action:(signal:AbortSignal,publish:(change:()=>void)=>void)=>Promise<void>)=>{
  if(lock.current||busy||!connected)return
  lock.current=true;setBusy(true);setError('');const generation=owner.current,controller=new AbortController();abort.current=controller
  const publish=(change:()=>void)=>{if(generation===owner.current&&!controller.signal.aborted)change()}
  try{await action(controller.signal,publish)}catch(cause){publish(()=>setError(cause instanceof ModelDocumentError&&/^pdf_[a-z0-9_]+$/.test(cause.code)?`${cause.message}（PDF诊断：${cause.code}）`:cause instanceof Error?cause.message:'转换结果未确认；未发送模型，不自动重试'))}finally{lock.current=false;if(generation===owner.current)setBusy(false)}
 }
 const kind=documentConversionKind(source.filename),pdf=kind==='pdf'
 const problem=preview?api.approvalProblem(preview):''
 void tick
 const prepare=()=>void run(async(signal,publish)=>{
  const selection={paragraphs:kind==='docx'?parseDocumentIndices(indices,50000):[],slides:kind==='pptx'?parseDocumentIndices(indices,200):[],pages:pdf?parsePdfPages(indices):[],sheets:kind==='xlsx'?parseDocumentSheets(sheets):[],include_hidden:['xlsx','pptx'].includes(kind)&&hidden,max_characters:limit}
  const value=await api.preview(source,selection,signal)
  publish(()=>{setPreview(value);setResult(undefined);setAck(false);setAttempted(false);setNotice('只完成原件与结构/选区预检，尚未提取文字，更未发送模型。')})
 })
 const convert=()=>void run(async(signal,publish)=>{
  if(!preview||!ack||attempted||api.approvalProblem(preview))return
  const pending=preview;setAttempted(true);setAck(false);setPreview(undefined)
  const value=await api.run(pending,true,signal)
  publish(()=>{setResult(value);setNotice('本地文字提取完成；请核对全部定位、公式/缓存与损失说明。尚未发送给模型。')})
 })
 const cancel=()=>void run(async(signal,publish)=>{if(!preview)return;const pending=preview;setPreview(undefined);setAck(false);setAttempted(true);await api.cancel(pending,signal);publish(()=>setNotice('本次转换预览已撤销；未提取、未删除原件、未发送模型。'))})
 return <section aria-label="模型文档转换" className="customer-settings customer-subsection">
  <header><h3>提取文字</h3><p>选择需要的内容，提取后可加入草稿。</p></header>
  {!connected&&<p role="status">连接本地服务后可提取。</p>}
  {error&&<p role="alert" className="customer-error">{error}</p>}
  <fieldset disabled={busy||!connected} className="space-y-4">
   {(kind==='docx'||kind==='pptx')&&<label className="customer-field"><span>{kind==='docx'?'段落编号':'幻灯片编号'}</span><Input aria-label="文档转换编号选区" placeholder="留空为全部可用内容，或填写 1,2,5" value={indices} maxLength={5000} onChange={e=>{reset();setIndices(e.target.value)}}/></label>}
   {pdf&&<label className="customer-field"><span>页码范围</span><Input aria-label="PDF页码范围" placeholder="例如 1,3-5,8" value={indices} maxLength={2000} onChange={e=>{reset();setIndices(e.target.value)}}/><small>请明确填写页码。扫描件和加密文件暂不支持文字提取。</small></label>}
   {kind==='xlsx'&&<label className="customer-field"><span>工作表与单元格范围</span><textarea aria-label="文档转换工作表范围" className="min-h-20 w-full rounded-xl border border-border bg-input p-3" placeholder={'留空为全部可见表，或填写：\nSheet1|A1:C20'} value={sheets} maxLength={20000} onChange={e=>{reset();setSheets(e.target.value)}}/></label>}
   {['xlsx','pptx'].includes(kind)&&<label className="customer-check"><input type="checkbox" checked={hidden} onChange={e=>{reset();setHidden(e.target.checked)}}/>包含隐藏工作表或幻灯片</label>}
   <label className="customer-field"><span>最多提取字数</span><Input aria-label="文档转换字符上限" type="number" min={1} max={100000} value={limit} onChange={e=>{reset();setLimit(Number(e.target.value))}}/><small>超过上限会停止，不会截断后当作完整内容。</small></label>
   <Button size="sm" variant="outline" onClick={prepare} disabled={busy||!connected||pdf&&!indices.trim()||!Number.isInteger(limit)||limit<1||limit>100000}>检查所选范围</Button>
  </fieldset>
  {preview&&<div className="customer-subsection"><h4 className="text-sm font-semibold">确认提取范围</h4><p>{preview.snapshot.source.filename} · 最多 {preview.limits.characters.toLocaleString()} 字</p><details className="personal-disclosure"><summary>原件与范围详情</summary><p className="break-all">SHA256 {preview.snapshot.source.sha256}</p><pre>{JSON.stringify(preview.snapshot.selection,null,2)}</pre></details><ul className="space-y-1 text-sm">{preview.snapshot.loss_notes.map((note,index)=><li key={index}>{note}</li>)}</ul>{problem&&<p role="alert">{problem}</p>}<label className="customer-check"><input type="checkbox" checked={ack} disabled={busy||Boolean(problem)} onChange={e=>setAck(e.target.checked)}/>同意提取所选内容，不发送给模型</label><div className="customer-actions"><Button size="sm" variant="outline" disabled={busy} onClick={cancel}>取消</Button><Button size="sm" disabled={busy||!connected||!ack||attempted||Boolean(problem)} onClick={convert}>提取文字</Button></div></div>}
  {busy&&<p role="status">正在处理…</p>}
  {result&&<div className="space-y-4"><h4 className="text-sm font-semibold">提取结果</h4><p>{result.characters.toLocaleString()} 字 · {result.parts.length} 个片段{result.skipped_hidden?` · 已跳过 ${result.skipped_hidden} 个隐藏项`:''}</p>{result.loss_notes.length>0&&<details className="personal-disclosure"><summary>内容说明</summary><ul>{result.loss_notes.map((note,index)=><li key={index}>{note}</li>)}</ul></details>}{!result.parts.length&&<p>选区没有可提取的文字，可调整范围后重试。</p>}{result.parts.map((part,index)=><article key={`${index}:${part.sha256}`} className="space-y-2"><p className="text-xs">{Object.entries(part.locator).map(([key,value])=>`${({paragraph:'段落',slide:'幻灯片',page:'页码',sheet:'工作表',cell:'单元格'} as Record<string,string>)[key]||key} ${value}`).join(' · ')}</p><pre className="max-h-56 overflow-auto whitespace-pre-wrap break-all rounded-xl bg-secondary p-3 text-sm">{part.text}</pre>{part.formula!==null&&<details className="personal-disclosure"><summary>公式与缓存值</summary><p>{part.formula}</p><p>缓存：{part.cached_value??'未提供'}，可能尚未更新。</p></details>}</article>)}<Button size="sm" disabled={!onReference||!result.parts.length} onClick={()=>onReference?.(`[资料摘录：${source.filename} · SHA256 ${source.sha256}]\n${result.parts.map(part=>`位置：${JSON.stringify(part.locator)}\n${part.text}`).join('\n\n')}\n说明：${result.loss_notes.join('；')}`)}>加入本轮草稿</Button><p>加入后可检查、编辑，再点击发送。</p></div>}
 </section>
}
