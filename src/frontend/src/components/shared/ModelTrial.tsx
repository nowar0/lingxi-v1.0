import {useState} from 'react'
import {api,json} from '@/api/client'
import {Input} from '@/components/ui/input'
import {Button} from '@/components/ui/button'
import {Dialog,DialogContent,DialogHeader,DialogTitle,DialogDescription,DialogFooter} from '@/components/ui/dialog'
import type {ModelModule} from '@/types'
export function ModelTrial({module,onClose}:{module:ModelModule;onClose:()=>void}){
 const [prompt,setPrompt]=useState('请用一句话回复：连接正常。'),[busy,setBusy]=useState(false),[result,setResult]=useState(''),[error,setError]=useState('')
 return <Dialog open onOpenChange={open=>{if(!open&&!busy)onClose()}}><DialogContent inert={busy}><DialogHeader><DialogTitle>试用文字模型</DialogTitle><DialogDescription>{module.model} · 发送一次真实请求，会产生模型用量。</DialogDescription></DialogHeader><Input aria-label="试用消息" maxLength={1000} value={prompt} onChange={event=>setPrompt(event.target.value)}/>{result&&<div className="model-trial-result" role="status"><strong>文字调用成功</strong><p>{result}</p><small>图片和工具能力需在对应功能中试用。</small></div>}{error&&<p role="alert">{error}</p>}<DialogFooter><Button variant="outline" disabled={busy} onClick={onClose}>关闭</Button><Button disabled={busy||!prompt.trim()} onClick={()=>{setBusy(true);setError('');void api<{text:string}>(`/v6/modules/${module.id}/trial`,json('POST',{prompt})).then(value=>setResult(value.text)).catch(reason=>setError(reason.message)).finally(()=>setBusy(false))}}>{busy?'正在调用…':'发送试用'}</Button></DialogFooter></DialogContent></Dialog>
}
