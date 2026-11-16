import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { stepDockSpring } from '@/lib/workDockMotion'
import './memory-tools.css'

export function MemoryToolsDrawer({open,onOpenChange,children,title='记忆设置',className='',side='right'}:{open:boolean;onOpenChange:(open:boolean)=>void;children:ReactNode;title?:string;className?:string;side?:'left'|'right'}){
 const [present,setPresent]=useState(open),[offset,setOffset]=useState(540)
 const motion=useRef({position:540,velocity:0})
 const opener=useRef<HTMLElement|null>(null)
 useEffect(()=>{
  if(open){opener.current=document.activeElement instanceof HTMLElement?document.activeElement:null;setPresent(true)}
  const reduced=document.documentElement.classList.contains('reduce-motion')||matchMedia('(prefers-reduced-motion: reduce)').matches
  if(reduced){motion.current={position:open?0:540,velocity:0};setOffset(motion.current.position);if(!open)setPresent(false);return}
  let frame=0,last=performance.now()
  const tick=(now:number)=>{const next=stepDockSpring(motion.current.position,motion.current.velocity,open?0:540,(now-last)/1000,540);last=now;motion.current=next;setOffset(next.position);if(next.settled){if(!open)setPresent(false)}else frame=requestAnimationFrame(tick)}
  frame=requestAnimationFrame(tick);return()=>cancelAnimationFrame(frame)
 },[open])
 return <Dialog open={present} onOpenChange={onOpenChange}><DialogContent className={`memory-tools-drawer ${className}`} overlayClassName="memory-tools-overlay" aria-describedby={undefined} onInteractOutside={event=>{const target=event.detail.originalEvent.target;if(target instanceof Element&&target.closest('.memory-tools-drawer,[data-memory-dialog],.lingxi-select-menu'))event.preventDefault()}} onCloseAutoFocus={event=>{event.preventDefault();if(!document.querySelector('[role="dialog"]:not(.memory-tools-drawer)')&&opener.current?.isConnected)opener.current.focus()}} style={{transform:`translateX(${side==='left'?-offset:offset}px)`}}><DialogHeader><DialogTitle>{title}</DialogTitle></DialogHeader>{children}</DialogContent></Dialog>
}
