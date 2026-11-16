import { recoveryStorage } from '@/lib/recoveryStorage'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { lingxi } from '@/api/lingxi'
import { useApp } from '@/context/AppContext'
import type { Mode } from '@/types'
import { fileToBase64 } from '@/lib/utils'
import { api, json } from '@/api/client'
import { artifactMime, artifactReference, textArtifact } from '@/lib/artifacts'

export type Attachment = { localId: string; id?: string; url?: string; mime: string; name: string; sessionId: number; size: number; status: 'queued' | 'uploading' | 'ready' | 'error'; reference?: string; comment?:string; error?: string; file?: File; controller?: AbortController }
// First upload can create a session and remount WorkSurface. Its transfer belongs
// to the new session, rather than the component which opened the file picker.
const attachments = new Map<string, Attachment[]>()
const storageKey = (key: string) => `lingxi-pending-attachments-v1:${key}`
function rows(key: string) {
  if (!attachments.has(key)) {
    try {
      const value: unknown = JSON.parse(recoveryStorage.getItem(storageKey(key)) || '[]')
      const safe = Array.isArray(value) ? value.slice(0,8).filter(item => item && typeof item.localId === 'string' && typeof item.sessionId === 'number' && typeof item.name === 'string' && typeof item.mime === 'string' && item.name.length <= 200 && key.endsWith(`:${item.sessionId}`)).map(item => {
        const ready = item.status === 'ready' && /^[a-f0-9]{32}$/.test(item.id) && item.url === `/v6/media/${item.id}?session_id=${item.sessionId}`
        return { ...item, file: undefined, controller: undefined, status: ready ? 'ready' : 'error', error: ready ? undefined : '上传已中断，请重新选择文件', reference: ready && typeof item.reference === 'string' && item.reference.startsWith(item.url + '#') && !/[\s<>]/.test(item.reference) ? item.reference : undefined } as Attachment
      }) : []
      attachments.set(key,safe)
    } catch { attachments.set(key,[]) }
  }
  return attachments.get(key)!
}
function persist() {
  for(const [key,items] of attachments) {
    try { recoveryStorage.setItem(storageKey(key),JSON.stringify(items.map(({file: _file,controller: _controller,...item}) => item))) } catch { /* The live queue remains usable if local storage is full. */ }
  }
}
const uploads = new Map<string, AbortController>()
const listeners = new Set<() => void>()
const publish = () => { persist(); listeners.forEach(listener => listener()) }
const mediaKey = (mode: Mode, sid?: number) => `${mode}:${sid || 'new'}`

export function useComposerMedia(mode: Mode, {passive=false}:{passive?:boolean}={}) {
  const { ensureSession, activeSessionId, activeWorkSessionId, modules, connection, drafts, setDraft } = useApp()
  const sid = mode === 'companion' ? activeSessionId : activeWorkSessionId
  const [items, setItems] = useState<Attachment[]>(() => [...rows(mediaKey(mode,sid))])
  const readyAttachment = items.find(item => item.status === 'ready')
  const [busy, setBusy] = useState(() => uploads.has(mediaKey(mode, sid)))
  const [recording, setRecording] = useState(false)
  const live = useRef(true), locked = useRef(false), sequence = useRef(0)
  const creating = useRef(false), previous = useRef(sid), recognizing = useRef<string | undefined>(undefined)
  const controller = useRef<AbortController | undefined>(undefined)
  const recorder = useRef<MediaRecorder | undefined>(undefined)
  const stream = useRef<MediaStream | undefined>(undefined)
  const timer = useRef<number | undefined>(undefined)
  const current = useRef({ id: sid, draft: drafts[mode] })
  current.current = { id: sid, draft: drafts[mode] }
  const fail = (error: unknown) => toast.error('媒体操作未完成', { description: (error as Error).message })
  const stopRecording = () => {
    const instance = recorder.current
    if (instance?.state === 'recording') { instance.onstop = null; instance.stop() }
    stream.current?.getTracks().forEach(track => track.stop())
    if (timer.current) window.clearTimeout(timer.current)
    recorder.current = undefined; stream.current = undefined; timer.current = undefined
  }
  const invalidate = () => {
    sequence.current++; controller.current?.abort(); controller.current = undefined
    stopRecording(); locked.current = false
  }
  useEffect(() => {
    live.current = true
    return () => {
      live.current = false
      queueMicrotask(() => {
        // A StrictMode probe remounts this same hook before the microtask.
        if (live.current) return
        // Only an initial upload is explicitly handed to a newly created session.
        // Recognition and recording always stop when their composer leaves.
        if (!creating.current) {
          controller.current?.abort()
          const key = mediaKey(mode, current.current.id)
          if(!passive){uploads.get(key)?.abort(); uploads.delete(key); publish()}
        }
        sequence.current++; stopRecording()
      })
    }
  }, [])
  useEffect(() => {
    const old = previous.current
    if (old !== sid && !(old === undefined && creating.current)) {
      invalidate()
      const oldKey = mediaKey(mode, old)
      if(!passive){uploads.get(oldKey)?.abort(); uploads.delete(oldKey)}
      setRecording(false)
    }
    previous.current = sid
    const update = () => {
      if (!live.current) return
      setItems([...rows(mediaKey(mode,sid))])
      if (!locked.current) setBusy(uploads.has(mediaKey(mode, sid)))
    }
    update(); listeners.add(update)
    return () => { listeners.delete(update) }
  }, [mode, sid])
  const valid = (request: number, target: number) => live.current && sequence.current === request && current.current.id === target && !controller.current?.signal.aborted
  const append = (request: number, target: number, text: string) => {
    if (!valid(request, target)) return
    const next = [current.current.draft.trimEnd(), text].filter(Boolean).join('\n\n')
    if (next.length > 8000) throw new Error('识别结果加草稿超过 8000 字，请缩短草稿后重试')
    setDraft(mode, next)
  }
  const upload = async (file: Blob, target: number, name: string, signal: AbortSignal) => {
    if (!file.size || file.size > 15 * 1024 * 1024) throw new Error('每个附件需为 15 MiB 以内的非空文件')
    const data = await fileToBase64(file)
    if (signal.aborted) throw new DOMException('已取消', 'AbortError')
    const mime = artifactMime(file,name)
    return { ...await api<{id:string;url:string;mime:string;size:number}>('/v9/artifacts/upload',{...json('POST',{session_id:target,mime,name:name.slice(0,200),data}),signal}), sessionId: target, name }
  }
  const chooseMany = async (files: File[] = []) => {
    if (!files.length || locked.current || recorder.current?.state === 'recording' || uploads.has(mediaKey(mode, sid)) || connection !== 'connected') return
    if (rows(mediaKey(mode,sid)).length + files.length > 8) { fail(new Error('每轮最多保留 8 个附件，请先移除已有附件')); return }
    if (files.reduce((sum,file)=>sum+file.size,0) + rows(mediaKey(mode,sid)).reduce((sum,item)=>sum+item.size,0) > 60*1024*1024) { fail(new Error('待发送附件合计需在 60 MiB 以内')); return }
    locked.current = true; setBusy(true); creating.current = !current.current.id
    const handoff = creating.current, owner = new AbortController()
    controller.current?.abort(); controller.current = owner
    let key: string | undefined
    const onAbort = () => { if(key)for(const item of rows(key))item.controller?.abort() }
    owner.signal.addEventListener('abort',onAbort)
    try {
      const target = await ensureSession(mode)
      key = mediaKey(mode, target)
      if (owner.signal.aborted || !handoff && (!live.current || current.current.id !== target)) return
      uploads.set(key, owner)
      const added: Attachment[] = files.map(file=>({localId:crypto.randomUUID(),name:file.name.slice(0,200),mime:artifactMime(file,file.name),sessionId:target,size:file.size,file,status:'queued'}))
      rows(key).push(...added);publish()
      for(const item of added) {
        if(owner.signal.aborted)break
        if(!rows(key).includes(item))continue
        item.controller=new AbortController();item.status='uploading';publish()
        try {
          const saved=await upload(item.file!,target,item.name,item.controller.signal)
          if(!owner.signal.aborted&&!item.controller.signal.aborted&&rows(key).includes(item)) {
            Object.assign(item,saved,{status:'ready',error:undefined,reference:textArtifact(saved.mime)?saved.url+'#L1-L200':undefined})
            item.file=undefined
          }
        } catch(error) { if(rows(key).includes(item)){item.status='error';item.error=(error as Error).name==='AbortError'?'上传已取消，可重试':(error as Error).message} }
        finally { item.controller=undefined;publish() }
      }
      if(owner.signal.aborted)for(const item of added)if(item.status!=='ready'){item.status='error';item.error='上传已取消，可重试'}
      return added.every(item=>item.status==='ready')
    } catch (error) { if (!owner.signal.aborted && live.current) fail(error) }
    finally {
      owner.signal.removeEventListener('abort',onAbort)
      if (key && uploads.get(key) === owner) { uploads.delete(key); publish() }
      creating.current = false
      if (controller.current === owner) { locked.current = false; if (live.current) setBusy(false) }
    }
  }
  const choose = async (file?: File) => file?chooseMany([file]):false
  const remove = (localId: string) => {
    const key=mediaKey(mode,sid),list=rows(key),item=list.find(row=>row.localId===localId)
    if(!item)return
    item.controller?.abort()
    if(item.localId===recognizing.current&&locked.current&&!uploads.has(key)){invalidate();setBusy(false)}
    attachments.set(key,list.filter(row=>row!==item));publish()
  }
  const retry = async (localId: string) => {
    const item=rows(mediaKey(mode,sid)).find(row=>row.localId===localId)
    if(!item?.file){fail(new Error('请通过附件入口重新选择原文件'));return}
    if(locked.current||uploads.has(mediaKey(mode,sid)))return
    const file=item.file;remove(localId);await chooseMany([file])
  }
  const insertExtracted = (localId:string,text:string) => {
    const item=rows(mediaKey(mode,current.current.id)).find(v=>v.localId===localId)
    if(!live.current||!item||item.sessionId!==current.current.id){toast.error('会话已切换，请返回原会话');return false}
    const next=[current.current.draft.trimEnd(),text].filter(Boolean).join('\n\n')
    if(next.length>8000){toast.error('摘录与草稿合计超过8000字，请缩小提取范围');return false}
    setDraft(mode,next);remove(localId);toast.success('摘录已加入草稿');return true
  }
  const selectReference = (localId: string,reference: string) => {
    const item=rows(mediaKey(mode,sid)).find(row=>row.localId===localId)
    if(!item?.url||!reference.startsWith(item.url+'#'))return
    item.reference=reference;publish()
  }
  const prepare = (text: string) => {
    const list=rows(mediaKey(mode,current.current.id))
    if(uploads.has(mediaKey(mode,current.current.id))||list.some(item=>item.status!=='ready'))throw new Error('请等待附件上传完成，并重试或移除失败项')
    const content=[text.trimEnd(),...list.map(item=>[artifactReference(item.name,item.reference||item.url!),item.comment?`图片批注：\n${item.comment}`:''].filter(Boolean).join('\n'))].filter(Boolean).join('\n\n')
    if(content.length>8000)throw new Error('正文、引用和附件链接合计超过 8000 字，请缩短后重试')
    return {content,localIds:list.map(item=>item.localId),sessionId:current.current.id}
  }
  const sent = (prepared: ReturnType<typeof prepare>) => {
    const key=mediaKey(mode,prepared.sessionId)
    attachments.set(key,rows(key).filter(item=>!prepared.localIds.includes(item.localId)));publish()
  }
  const drop = (event: import('react').DragEvent) => {
    if(!event.dataTransfer.types.includes('Files'))return
    event.preventDefault();void chooseMany(Array.from(event.dataTransfer.files))
  }
  const paste = (event: import('react').ClipboardEvent) => {
    const files=Array.from(event.clipboardData.files)
    if(files.length){event.preventDefault();void chooseMany(files)}
  }
  const analyze = async (localId?: string) => {
    const selectedAttachment = localId ? rows(mediaKey(mode,sid)).find(item=>item.localId===localId) : readyAttachment
    const attachment = selectedAttachment
    if (!attachment?.id || !attachment.url || locked.current || uploads.has(mediaKey(mode, sid)) || recorder.current?.state === 'recording') return
    if(!attachment.mime.startsWith('audio/')&&!attachment.mime.startsWith('image/'))return
    const moduleId = attachment.mime.startsWith('audio/') ? 'stt' : 'vision'
    if (!modules.find(item => item.id === moduleId)?.enabled) { fail(new Error('请先在模型与语音中启用对应的识别模块')); return }
    locked.current = true; setBusy(true)
    const request = ++sequence.current, owner = new AbortController(), selected = attachment
    recognizing.current=selected.localId
    controller.current?.abort(); controller.current = owner
    try {
      const result = await lingxi.invokeMedia(moduleId, selected.sessionId, { attachment_id: selected.id!,
        text: moduleId === 'vision' ? current.current.draft || '请描述这张图片' : '' }, owner.signal)
      if (!valid(request, selected.sessionId)) return
      if (!result.text?.trim()) throw new Error('未返回识别文字，请重试')
      append(request, selected.sessionId, `${result.text}\n[${selected.name}](${selected.url})`)
      const key=mediaKey(mode,selected.sessionId);attachments.set(key,rows(key).filter(item=>item.localId!==selected.localId));publish()
    } catch (error) { if (!owner.signal.aborted && valid(request, selected.sessionId)) fail(error) }
    finally { if (sequence.current === request) { locked.current = false; if (live.current) setBusy(false) } }
  }
  const record = async () => {
    if (recorder.current?.state === 'recording') { recorder.current.stop(); return }
    if (locked.current || uploads.has(mediaKey(mode, sid)) || connection !== 'connected') return
    if (!modules.find(item => item.id === 'stt')?.enabled) { fail(new Error('请先在连接中心 → 本地模型中启用语音输入模型')); return }
    locked.current = true; setBusy(true); creating.current = !current.current.id
    const request = ++sequence.current, owner = new AbortController()
    controller.current = owner
    try {
      const target = await ensureSession(mode)
      const audio = await navigator.mediaDevices.getUserMedia({ audio: true })
      creating.current = false
      if (!valid(request, target)) { audio.getTracks().forEach(track => track.stop()); return }
      stream.current = audio
      const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'].find(type => MediaRecorder.isTypeSupported(type))
      const instance = new MediaRecorder(audio, mime ? { mimeType: mime } : undefined)
      const chunks: Blob[] = []
      let size = 0, tooLarge = false
      recorder.current = instance
      instance.ondataavailable = event => {
        size += event.data.size
        if (size > 15 * 1024 * 1024) { tooLarge = true; if (instance.state === 'recording') instance.stop(); return }
        if (event.data.size) chunks.push(event.data)
      }
      instance.onerror = () => { audio.getTracks().forEach(track => track.stop()); if (valid(request, target)) { setRecording(false); setBusy(false); fail(new Error('录音失败，请检查麦克风权限')) } }
      instance.onstop = () => {
        audio.getTracks().forEach(track => track.stop()); if (timer.current) window.clearTimeout(timer.current)
        if (!valid(request, target)) return
        setRecording(false); locked.current = true; setBusy(true)
        void (async () => {
          if (tooLarge) throw new Error('录音超过 15 MiB，未上传，请缩短后重试')
          const saved = await upload(new Blob(chunks, { type: instance.mimeType.split(';')[0] }), target, '录音.webm', owner.signal)
          if (!valid(request, target)) return
          const result = await lingxi.invokeMedia('stt', target, { attachment_id: saved.id }, owner.signal)
          if (!valid(request, target)) return
          if (!result.text?.trim()) throw new Error('未识别到文字，请重新录音')
          append(request, target, result.text)
        })().catch(error => { if (!owner.signal.aborted && valid(request, target)) fail(error) }).finally(() => {
          if (sequence.current === request) { locked.current = false; if (live.current) setBusy(false) }
        })
      }
      instance.start(); setRecording(true); locked.current = false; setBusy(false)
      timer.current = window.setTimeout(() => { if (instance.state === 'recording') instance.stop() }, 120000)
    } catch (error) {
      stream.current?.getTracks().forEach(track => track.stop())
      if (!owner.signal.aborted && live.current) fail(error)
    } finally {
      creating.current = false
      if (sequence.current === request) { locked.current = false; if (live.current) setBusy(false) }
    }
  }
  const clear = () => {
    invalidate()
    const key = mediaKey(mode, sid)
    uploads.get(key)?.abort(); uploads.delete(key); attachments.set(key,[])
    setRecording(false); setBusy(false); publish()
  }
  const applyImage=async(file:File,comment:string,replaceId?:string)=>{
    const target=current.current.id,key=mediaKey(mode,target),list=rows(key),previous=replaceId?list.find(item=>item.localId===replaceId):undefined
    if(!target||connection!=='connected'||locked.current||uploads.has(key))throw new Error('请等待当前操作完成')
    if(replaceId&&!previous)throw new Error('此图片已移除，请重新打开')
    if(!previous&&list.length>=8)throw new Error('每轮最多保留 8 个附件')
    if(file.size+list.filter(item=>item!==previous).reduce((n,item)=>n+item.size,0)>60*1024*1024)throw new Error('待发送附件超过 60 MiB，请先移除部分附件')
    if(comment.length>4000)throw new Error('图片批注最多 4000 字')
    const owner=new AbortController(),request=++sequence.current;controller.current=owner;locked.current=true;setBusy(true);uploads.set(key,owner)
    try{const saved=await upload(file,target,file.name,owner.signal);if(!valid(request,target))throw new Error('对话已切换，原图和草稿保留');const currentRows=rows(key);if(previous&&!currentRows.includes(previous))throw new Error('原附件已移除，未替换');const edited:Attachment={...saved,localId:previous?.localId||crypto.randomUUID(),mime:saved.mime,size:saved.size,status:'ready',comment};attachments.set(key,previous?currentRows.map(item=>item===previous?edited:item):[...currentRows,edited]);publish()}
    finally{if(uploads.get(key)===owner)uploads.delete(key);if(sequence.current===request){locked.current=false;if(live.current)setBusy(false)}publish()}
  }
  return { attachment: readyAttachment, items, clear, remove, retry, selectReference,insertExtracted, prepare, sent, drop, paste, busy, recording, choose, chooseMany, analyze, record, applyImage }
}
