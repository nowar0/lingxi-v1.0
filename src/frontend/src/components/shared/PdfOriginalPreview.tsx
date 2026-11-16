import { useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, LoaderCircle, RefreshCw } from 'lucide-react'
import { getDocument, GlobalWorkerOptions, type PDFDocumentProxy, type RenderTask } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { Button } from '@/components/ui/button'
GlobalWorkerOptions.workerSrc=workerUrl
const positions=new Map<string,{page:number;zoom:string}>()

export default function PdfOriginalPreview({url,id}:{url:string;id:string}) {
  const saved=positions.get(id)
  const [document,setDocument]=useState<PDFDocumentProxy>(),[page,setPage]=useState(saved?.page||1),[zoom,setZoom]=useState(saved?.zoom||'fit')
  const [busy,setBusy]=useState(true),[error,setError]=useState(''),[reload,setReload]=useState(0),[width,setWidth]=useState(700)
  const surface=useRef<HTMLDivElement>(null),renderGeneration=useRef(0)
  useEffect(()=>{
    const element=surface.current;if(!element)return
    const observer=new ResizeObserver(entries=>{const value=entries[0]?.contentRect.width;if(value)setWidth(Math.max(100,value-24))})
    observer.observe(element);return()=>observer.disconnect()
  },[])
  useEffect(()=>{
    let alive=true;setBusy(true);setError('');setDocument(undefined)
    const resources=new URL('pdfjs/',new URL(import.meta.env.BASE_URL,window.location.href)).href
    const loading=getDocument({url,cMapUrl:resources+'cmaps/',cMapPacked:true,standardFontDataUrl:resources+'standard_fonts/',wasmUrl:resources+'wasm/',iccUrl:resources+'iccs/',enableXfa:false,maxImageSize:16_000_000,canvasMaxAreaInBytes:64_000_000,stopAtErrors:true})
    loading.promise.then(pdf=>{
      if(!alive)return
      if(pdf.numPages>10000){setError('PDF超过10000页，未继续载入。请下载原件使用对应应用查看。');setBusy(false);void loading.destroy();return}
      setDocument(pdf);setPage(value=>Math.min(pdf.numPages,Math.max(1,value)))
    }).catch(failure=>{if(alive){setError(failure.name==='PasswordException'?'这是加密PDF，请下载原件在对应应用输入密码。':`PDF未能显示：${failure.message}。原件仍可下载。`);setBusy(false)}})
    return()=>{alive=false;renderGeneration.current++;void loading.destroy().catch(()=>{})}
  },[url,reload])
  useEffect(()=>{
    if(!document||!surface.current)return
    const request=++renderGeneration.current;let rendering:RenderTask|undefined
    setBusy(true);setError('')
    const container=surface.current
    void document.getPage(page).then(async pdfPage=>{
      if(request!==renderGeneration.current)return
      const normal=pdfPage.getViewport({scale:1})
      const scale=zoom==='fit'?Math.min(3,width/normal.width):Number(zoom)/100
      const viewport=pdfPage.getViewport({scale}),ratio=Math.min(2,window.devicePixelRatio||1)
      if(!Number.isFinite(viewport.width*viewport.height)||viewport.width*viewport.height*ratio*ratio>16_000_000)throw new Error('此页在当前缩放下过大，请减小缩放或下载原件查看')
      const canvas=window.document.createElement('canvas'),context=canvas.getContext('2d')
      if(!context)throw new Error('当前窗口不能创建PDF画布')
      canvas.width=Math.ceil(viewport.width*ratio);canvas.height=Math.ceil(viewport.height*ratio)
      canvas.style.width=`${viewport.width}px`;canvas.style.height=`${viewport.height}px`
      canvas.setAttribute('role','img');canvas.setAttribute('aria-label',`PDF 第 ${page} 页`)
      rendering=pdfPage.render({canvas,canvasContext:context,viewport,transform:ratio!==1?[ratio,0,0,ratio,0,0]:undefined})
      await rendering.promise
      if(request!==renderGeneration.current)return
      container.replaceChildren(canvas);container.dataset.page=String(page)
      positions.delete(id);positions.set(id,{page,zoom});if(positions.size>50)positions.delete(positions.keys().next().value!)
    }).catch(failure=>{if(request===renderGeneration.current&&failure.name!=='RenderingCancelledException'){container.replaceChildren();delete container.dataset.page;setError(`本页未显示：${failure.message}`)}})
      .finally(()=>{if(request===renderGeneration.current)setBusy(false)})
    return()=>{renderGeneration.current++;rendering?.cancel()}
  },[document,page,zoom,width,id])
  return <div className="pdf-original-preview"><div className="pdf-original-controls"><Button size="icon" variant="ghost" aria-label="PDF上一页" disabled={!document||page<=1||busy} onClick={()=>setPage(value=>value-1)}><ChevronLeft/></Button><label>页码<input aria-label="PDF页码" type="number" min={1} max={document?.numPages||1} value={page} disabled={!document||busy} onChange={event=>{const value=Number(event.target.value);if(Number.isInteger(value)&&value>=1&&value<=document!.numPages)setPage(value)}}/></label><span>/ {document?.numPages||'…'}</span><Button size="icon" variant="ghost" aria-label="PDF下一页" disabled={!document||page>=document.numPages||busy} onClick={()=>setPage(value=>value+1)}><ChevronRight/></Button><label>缩放<select aria-label="PDF缩放" value={zoom} disabled={!document||busy} onChange={event=>setZoom(event.target.value)}><option value="fit">适应宽度</option>{[25,50,75,100,125,150,200].map(value=><option key={value} value={String(value)}>{value}%</option>)}</select></label></div>{busy&&<p className="artifact-loading" role="status"><LoaderCircle className="animate-spin"/>正在显示第 {page} 页…</p>}{error&&<div className="artifact-error" role="alert">{error}<Button size="xs" variant="outline" onClick={()=>setReload(value=>value+1)}><RefreshCw/>重试显示</Button></div>}<div ref={surface} className="pdf-original-canvas" aria-busy={busy}/><p className="artifact-library-note">仅显示原件页面；没有将PDF正文交给对话模型。复杂图像页可能受预览资源限制，请下载核对。</p></div>
}
