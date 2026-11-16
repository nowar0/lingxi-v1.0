import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { stepDockSpring } from '@/lib/workDockMotion'

export function TaskSettingsDrawer({open,onOpenChange,children}:{open:boolean;onOpenChange:(open:boolean)=>void;children:ReactNode}){
 const [present,setPresent]=useState(open),[offset,setOffset]=useState(460)
 const motion=useRef({position:460,velocity:0})
 useEffect(()=>{
  if(open)setPresent(true)
  const reduced=document.documentElement.classList.contains('reduce-motion')||matchMedia('(prefers-reduced-motion: reduce)').matches
  if(reduced){motion.current={position:open?0:460,velocity:0};setOffset(motion.current.position);if(!open)setPresent(false);return}
  let frame=0,last=performance.now();const tick=(now:number)=>{const next=stepDockSpring(motion.current.position,motion.current.velocity,open?0:460,(now-last)/1000,460);last=now;motion.current=next;setOffset(next.position);if(next.settled){if(!open)setPresent(false)}else frame=requestAnimationFrame(tick)}
  frame=requestAnimationFrame(tick);return()=>cancelAnimationFrame(frame)
 },[open])
 return <Dialog open={present} onOpenChange={onOpenChange}><DialogContent aria-describedby={undefined} className="task-settings-drawer" style={{transform:`translateX(${offset}px)`}} onInteractOutside={event=>{const target=event.detail.originalEvent.target;if(target instanceof Element&&target.closest('.task-settings-drawer'))event.preventDefault()}}><DialogHeader><DialogTitle>任务设置</DialogTitle></DialogHeader>{children}</DialogContent></Dialog>
}
