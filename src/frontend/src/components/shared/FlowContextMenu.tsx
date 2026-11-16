import { createPortal } from 'react-dom'
import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'

/** Native pointer and keyboard activation; the menu never depends on a hidden trigger. */
export function FlowContextMenu({x,y,children,onClose}:{x:number;y:number;children:ReactNode;onClose:()=>void}){
 const ref=useRef<HTMLDivElement>(null),[position,setPosition]=useState({left:x,top:y})
 useLayoutEffect(()=>{
  const menu=ref.current;if(!menu)return
  const owner=document.activeElement instanceof HTMLElement?document.activeElement:null,r=menu.getBoundingClientRect()
  setPosition({left:Math.max(8,Math.min(x,innerWidth-r.width-8)),top:Math.max(8,Math.min(y,innerHeight-r.height-8))})
  menu.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus({preventScroll:true})
  const outside=(event:PointerEvent)=>{if(!menu.contains(event.target as Node))onClose()}
  const dismiss=()=>onClose()
  document.addEventListener('pointerdown',outside,true);window.addEventListener('blur',dismiss);window.addEventListener('resize',dismiss)
  return()=>{document.removeEventListener('pointerdown',outside,true);window.removeEventListener('blur',dismiss);window.removeEventListener('resize',dismiss);if(document.activeElement===document.body||menu.contains(document.activeElement))owner?.focus({preventScroll:true})}
 },[x,y,onClose])
 return createPortal(<div ref={ref} role="menu" aria-label="画布操作" className="flow-context-menu" style={position} onPointerDown={e=>e.stopPropagation()} onContextMenu={e=>{e.preventDefault();e.stopPropagation()}} onClick={e=>e.stopPropagation()} onKeyDown={e=>{
  const items=[...ref.current!.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')],index=items.indexOf(document.activeElement as HTMLButtonElement)
  if(e.key==='Escape'||e.key==='Tab'){e.preventDefault();e.stopPropagation();onClose()}
  else if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){e.preventDefault();e.stopPropagation();const next=e.key==='Home'?0:e.key==='End'?items.length-1:(index+(e.key==='ArrowDown'?1:-1)+items.length)%items.length;items[next]?.focus()}
 }}>{children}</div>,document.body)
}
