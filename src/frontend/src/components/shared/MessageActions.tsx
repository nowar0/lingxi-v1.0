import { protectMessageUI } from '@/context/SurfaceContext'
import { useEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from 'react'
import { Copy, Download, FolderOpen, Pencil, Quote, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import type { Message, Mode } from '@/types'
import { useApp } from '@/context/AppContext'
import { useMessageActions } from '@/context/MessageActionsContext'
import { actOnImage, messageImages } from '@/lib/messageActions'
import { PointerMenu, type PointerMenuAnchor, type PointerMenuItem } from './PointerMenu'
import { Button } from '@/components/ui/button'

function MessageActionsBody({message,mode,children,onChanged,onQuote,className}: {message:Message;mode:Mode;children:ReactNode;onChanged?:() => void;onQuote?:() => void;className?:string}) {
  const {activeSessionId,activeWorkSessionId,generating,sending,connection,mutateMessage,undoMessageDeletion,messages,workMessages,conversationVersions} = useApp()
  const {quote} = useMessageActions()
  const owner = useRef<HTMLDivElement>(null); const input = useRef<HTMLTextAreaElement>(null)
  const [anchor,setAnchor] = useState<PointerMenuAnchor>(); const [selected,setSelected] = useState('')
  const [copyImage,setCopyImage] = useState(false)
  const [imageIndex,setImageIndex] = useState(0); const [editing,setEditing] = useState(false)
  const [text,setText] = useState(message.content); const [busy,setBusy] = useState(false)
  const sid = mode === 'work' ? activeWorkSessionId : activeSessionId
  const images = messageImages(message.content); const image = images[imageIndex] || images[0]
  const waiting = generating[mode] || sending[mode]
  const unresolved = (mode === 'work' ? workMessages : messages).some((item) => String(item.id).startsWith('outbox-'))
  const canChange = connection === 'connected' && conversationVersions[mode]>=0 && !!sid && typeof message.id === 'number' && !waiting && !unresolved && !busy
  useEffect(() => {if (editing) {input.current?.focus();input.current?.setSelectionRange(text.length,text.length)}},[editing])
  const open = (event:MouseEvent<HTMLDivElement>|KeyboardEvent<HTMLDivElement>) => {
    if (editing || !owner.current) return
    event.preventDefault(); event.stopPropagation()
    const selection = window.getSelection(); setSelected(selection?.anchorNode && owner.current.contains(selection.anchorNode) ? selection.toString() : '')
    const target = event.target instanceof Element ? event.target.closest('img') : null
    setCopyImage(!!target || images.length===1 && message.content.trim()===`[${images[0].name}](${images[0].url})`)
    setImageIndex(Math.max(0,images.findIndex((item) => target?.getAttribute('src') === item.url)))
    const rect = owner.current.getBoundingClientRect()
    setAnchor({x:'clientX' in event ? event.clientX : rect.left+12,y:'clientY' in event ? event.clientY : rect.bottom,owner:owner.current})
  }
  const run = async (task:() => Promise<void>) => {if (busy) return;setBusy(true);try {await task()} catch(error) {toast.error('消息操作未完成',{description:(error as Error).message})} finally {setBusy(false)}}
  const items:PointerMenuItem[] = [
    {id:'copy',label:'复制',icon:<Copy />,action:() => void run(async() => {if (image && copyImage && !selected) {if (!await actOnImage('copy',image)) return} else await navigator.clipboard.writeText(selected || message.content);toast.success('已复制')})},
    {id:'quote',label:'引用',icon:<Quote />,disabled:!sid || message.role === 'system',action:() => {if (sid) {quote(mode,sid,message);onQuote?.()}}},
    ...(!images.length && message.role === 'user' ? [{id:'edit',label:'编辑',icon:<Pencil />,disabled:!canChange,action:() => {setText(message.content);setEditing(true)}}] : []),
    ...(image ? [
      {id:'save',label:'另存为',icon:<Download />,action:() => void run(async() => {if (await actOnImage('save',image)) toast.success('图片已保存')})},
      {id:'folder',label:'打开文件夹',icon:<FolderOpen />,disabled:!window.LingxiShell?.messageImages,action:() => void run(async() => {await actOnImage('reveal',image)})},
    ] : []),
    {id:'delete',label:'删除',icon:<Trash2 />,destructive:true,separator:true,disabled:!canChange,action:() => void run(async() => {const result=await mutateMessage(mode,message);onChanged?.();toast.success('消息已删除',{action:{label:'撤销',onClick:() => void undoMessageDeletion(mode,result.view.session_id,result.undo_id!,result.view.session_version).then(() => {onChanged?.();toast.success('已恢复消息')}).catch((error) => toast.error('未能撤销',{description:(error as Error).message}))}})})},
  ]
  return <div ref={owner} className={className} tabIndex={0} data-message-actions onContextMenu={open} onKeyDown={(event) => {if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) open(event)}}>
    {editing ? <form className="message-inline-edit" onSubmit={(event) => {event.preventDefault();void run(async() => {await mutateMessage(mode,message,text);setEditing(false);onChanged?.();toast.success('消息已修改')})}}><textarea ref={input} aria-label="编辑消息" maxLength={8000} rows={Math.max(2,Math.min(8,text.split('\n').length))} value={text} disabled={busy} onChange={(event) => setText(event.target.value)} onKeyDown={(event) => {if(event.key==='Escape' && !busy) {event.stopPropagation();setEditing(false)}}} /><div><Button type="button" size="xs" variant="ghost" disabled={busy} onClick={() => setEditing(false)}>取消</Button><Button type="submit" size="xs" disabled={!canChange || !text.trim() || text===message.content}>{busy?'保存中…':'保存'}</Button></div></form> : children}
    {anchor && <PointerMenu anchor={anchor} items={items} label="消息操作" onClose={() => setAnchor(undefined)} />}
  </div>
}

export const MessageActions = protectMessageUI(MessageActionsBody)
