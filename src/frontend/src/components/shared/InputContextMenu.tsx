import { useEffect, useState } from 'react'
import { ClipboardPaste, Copy, Scissors, TextSelect } from 'lucide-react'
import { toast } from 'sonner'
import { PointerMenu, type PointerMenuAnchor, type PointerMenuItem } from './PointerMenu'

type Field = HTMLInputElement|HTMLTextAreaElement
type Selection = {field:Field;start:number;end:number;value:string}
export function InputContextMenu() {
  const [state,setState] = useState<{anchor:PointerMenuAnchor;selection:Selection}>()
  useEffect(() => {
    const open = (event:Event) => {
      const field = event.target
      if (!(field instanceof HTMLTextAreaElement || field instanceof HTMLInputElement && ['text','search','email','url','tel'].includes(field.type)) || field.disabled) return
      const keyboard = event instanceof KeyboardEvent
      if (keyboard && event.key!=='ContextMenu' && !(event.shiftKey && event.key==='F10')) return
      event.preventDefault()
      const rect=field.getBoundingClientRect()
      setState({anchor:{x:event instanceof MouseEvent ? event.clientX : rect.left+12,y:event instanceof MouseEvent ? event.clientY : rect.bottom,owner:field},selection:{field,start:field.selectionStart??field.value.length,end:field.selectionEnd??field.value.length,value:field.value}})
    }
    document.addEventListener('contextmenu',open);document.addEventListener('keydown',open)
    return () => {document.removeEventListener('contextmenu',open);document.removeEventListener('keydown',open)}
  },[])
  if (!state) return null
  const {field,start,end,value} = state.selection
  const current = () => {if (!field.isConnected || field.value!==value) throw new Error('输入内容已变化，请重新打开菜单')}
  const replace = (insert:string) => {
    current();const next=value.slice(0,start)+insert+value.slice(end)
    if (field.maxLength>0 && next.length>field.maxLength) throw new Error(`内容超过 ${field.maxLength} 字符`)
    const prototype=field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
    Object.getOwnPropertyDescriptor(prototype,'value')!.set!.call(field,next)
    field.dispatchEvent(new Event('input',{bubbles:true}))
    requestAnimationFrame(() => {if(field.isConnected) {field.focus({preventScroll:true});try {field.setSelectionRange(start+insert.length,start+insert.length)} catch { /* Some text input types expose no selection API. */ }}})
  }
  const run = (task:() => Promise<void>) => {void task().catch((error) => toast.error('剪贴板操作未完成',{description:(error as Error).message}))}
  const items:PointerMenuItem[] = [
    {id:'copy',label:'复制',icon:<Copy />,shortcut:'Ctrl+C',disabled:start===end,action:() => run(async() => {current();await navigator.clipboard.writeText(value.slice(start,end))})},
    {id:'cut',label:'剪切',icon:<Scissors />,shortcut:'Ctrl+X',disabled:field.readOnly || start===end,action:() => run(async() => {current();await navigator.clipboard.writeText(value.slice(start,end));replace('')})},
    {id:'paste',label:'粘贴',icon:<ClipboardPaste />,shortcut:'Ctrl+V',disabled:field.readOnly,action:() => run(async() => {const text=await navigator.clipboard.readText();replace(text)})},
    {id:'all',label:'全选',icon:<TextSelect />,shortcut:'Ctrl+A',separator:true,disabled:!value.length,action:() => {field.focus({preventScroll:true});field.select()}},
  ]
  return <PointerMenu anchor={state.anchor} items={items} label="输入框操作" onClose={() => setState(undefined)} />
}
