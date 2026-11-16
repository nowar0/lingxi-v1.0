import { createPortal } from 'react-dom'
import { useEffect, useRef, useState, type CSSProperties, type RefObject } from 'react'
import { Bookmark } from 'lucide-react'
import { useMessageTokens } from '@/context/SurfaceContext'
import type { Message } from '@/types'

const title = (text:string) => text.replace(/!?\[([^\]]*)\]\([^)]*\)/g,'$1').replace(/[#*_`>|]/g,'').replace(/\s+/g,' ').trim() || '提问'
function readMarks(key:string):string[]{try{const value=JSON.parse(localStorage.getItem(key)||'[]');return Array.isArray(value)?value.filter(v=>typeof v==='string'&&v.length<100).slice(0,500):[]}catch{return[]}}

export function WorkMessageNavigator({messages,sessionId,scrollRef,enabled}:{messages:Message[];sessionId?:number;scrollRef:RefObject<HTMLDivElement|null>;enabled:boolean}){
  const items=messages.filter(m=>m.role==='user'&&m.content.trim()),key=`lingxi-work-message-marks-v1:${sessionId}`
  const [marks,setMarks]=useState<string[]>(()=>readMarks(key)),[active,setActive]=useState(''),[preview,setPreview]=useState<string>(),[position,setPosition]=useState<CSSProperties>({})
  const root=useRef<HTMLElement>(null),card=useRef<HTMLDivElement>(null),timer=useRef<ReturnType<typeof setTimeout>|undefined>(undefined),tokens=useMessageTokens()
  const cancel=()=>{if(timer.current)clearTimeout(timer.current);timer.current=undefined}
  const dismiss=()=>{cancel();timer.current=setTimeout(()=>setPreview(undefined),160)}
  useEffect(()=>{setMarks(readMarks(key));setPreview(undefined)},[key])
  useEffect(()=>{if(!enabled)setPreview(undefined)},[enabled])
  useEffect(()=>()=>cancel(),[])
  useEffect(()=>{
    const scroll=scrollRef.current;if(!enabled||!scroll)return
    const rows=new Map([...scroll.querySelectorAll<HTMLElement>('[data-message-id]')].map(row=>[row.dataset.messageId,row]))
    const update=()=>{const top=scroll.getBoundingClientRect().top+48;let next=String(items[0]?.id||'');for(const item of items){const row=rows.get(String(item.id));if(row&&row.getBoundingClientRect().top<=top)next=String(item.id)}setActive(next)}
    update();scroll.addEventListener('scroll',update,{passive:true});const observer=new ResizeObserver(update);observer.observe(scroll);if(scroll.firstElementChild)observer.observe(scroll.firstElementChild)
    return()=>{scroll.removeEventListener('scroll',update);observer.disconnect()}
  },[enabled,messages,scrollRef])
  useEffect(()=>{if(!preview)return;const close=(event:PointerEvent)=>{if(!root.current?.contains(event.target as Node)&&!card.current?.contains(event.target as Node))setPreview(undefined)};const escape=(event:KeyboardEvent)=>{if(event.key==='Escape')setPreview(undefined)};document.addEventListener('pointerdown',close);document.addEventListener('keydown',escape);return()=>{document.removeEventListener('pointerdown',close);document.removeEventListener('keydown',escape)}},[preview])
  const show=(item:Message,button:HTMLButtonElement)=>{cancel();const b=button.getBoundingClientRect(),width=Math.min(380,innerWidth-40);setPosition({left:Math.max(12,Math.min(innerWidth-width-12,b.right+10)),top:Math.max(12,Math.min(innerHeight-76,b.top-18)),width});setPreview(String(item.id))}
  const jump=(id:string)=>{const scroll=scrollRef.current,row=scroll&&[...scroll.querySelectorAll<HTMLElement>('[data-message-id]')].find(e=>e.dataset.messageId===id);if(!scroll||!row)return;scroll.scrollTo({top:scroll.scrollTop+row.getBoundingClientRect().top-scroll.getBoundingClientRect().top-18,behavior:'instant'});setActive(id)}
  const toggle=(id:string)=>{const next=marks.includes(id)?marks.filter(v=>v!==id):[...marks,id].slice(-500);setMarks(next);try{localStorage.setItem(key,JSON.stringify(next))}catch{/* Navigation marks are optional local preferences. */}}
  const item=items.find(v=>String(v.id)===preview)
  if(!enabled||items.length<2)return null
  return <nav ref={root} className="work-message-navigator" style={tokens} data-ui-surface="message" aria-label="对话快速定位" onPointerEnter={cancel} onPointerLeave={dismiss}>
    <div className="work-message-nav-list">{items.map((message,index)=>{const id=String(message.id),marked=marks.includes(id);return <button key={id} type="button" className={`work-message-nav-item${marked?' is-marked':''}`} aria-current={active===id?'location':undefined} aria-label={`定位第 ${index+1} 条提问：${title(message.content).slice(0,80)}`} tabIndex={active===id||!active&&index===0?0:-1} onPointerEnter={event=>show(message,event.currentTarget)} onFocus={event=>show(message,event.currentTarget)} onBlur={event=>{if(!card.current?.contains(event.relatedTarget as Node))dismiss()}} onClick={()=>jump(id)} onKeyDown={event=>{if(!['ArrowUp','ArrowDown','Home','End'].includes(event.key))return;event.preventDefault();const buttons=root.current?.querySelectorAll<HTMLButtonElement>('.work-message-nav-item');const i=event.key==='Home'?0:event.key==='End'?items.length-1:Math.max(0,Math.min(items.length-1,index+(event.key==='ArrowDown'?1:-1)));buttons?.[i]?.focus()}}><span style={{width:`${marked?22:Math.min(20,7+Math.log2(message.content.length+1)*1.3)}px`}}/></button>})}</div>
    {item&&createPortal(<div ref={card} className="work-message-nav-preview" style={{...tokens,...position}} data-ui-surface="message" role="group" aria-label="对话定位预览" onPointerEnter={cancel} onPointerLeave={dismiss}><button type="button" className="work-message-nav-title" onClick={()=>jump(String(item.id))}>{title(item.content)}</button><button type="button" className="work-message-nav-bookmark" aria-label={marks.includes(String(item.id))?'取消此提问书签':'为此提问添加书签'} aria-pressed={marks.includes(String(item.id))} onClick={()=>toggle(String(item.id))}><Bookmark fill={marks.includes(String(item.id))?'currentColor':'none'}/></button></div>,document.body)}
  </nav>
}
