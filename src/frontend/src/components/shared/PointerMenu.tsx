import { protectMessageUI } from '@/context/SurfaceContext'
import { useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import './message-actions.css'

export type PointerMenuItem = {id: string; label: string; icon: ReactNode; shortcut?: string; disabled?: boolean; destructive?: boolean; separator?: boolean; action: () => void}
export type PointerMenuAnchor = {x: number; y: number; owner: HTMLElement}
function PointerMenuBody({anchor,items,onClose,label}: {anchor:PointerMenuAnchor;items:PointerMenuItem[];onClose:() => void;label:string}) {
  useEffect(() => {
    const dismiss = (event: Event) => { if (!(event.target instanceof Element && event.target.closest('[data-pointer-menu]'))) onClose() }
    window.addEventListener('resize',dismiss); window.addEventListener('scroll',dismiss,true)
    return () => {window.removeEventListener('resize',dismiss);window.removeEventListener('scroll',dismiss,true)}
  },[onClose])
  if (typeof document === 'undefined') return null
  return createPortal(<DropdownMenu open modal={false} onOpenChange={(open) => {if (!open) onClose()}}><DropdownMenuTrigger asChild><button aria-hidden="true" tabIndex={-1} className="pointer-menu-anchor" style={{left:anchor.x,top:anchor.y}} /></DropdownMenuTrigger><DropdownMenuContent data-pointer-menu aria-label={label} className="message-action-menu" align="start" sideOffset={0} collisionPadding={8} onContextMenu={(event) => event.preventDefault()} onCloseAutoFocus={(event) => {event.preventDefault(); if (anchor.owner.isConnected) anchor.owner.focus({preventScroll:true})}}>{items.map((item) => <div key={item.id}>{item.separator && <DropdownMenuSeparator />}<DropdownMenuItem disabled={item.disabled} className={item.destructive ? 'is-destructive' : undefined} onSelect={() => {onClose();item.action()}}>{item.icon}<span>{item.label}</span>{item.shortcut && <kbd>{item.shortcut}</kbd>}</DropdownMenuItem></div>)}</DropdownMenuContent></DropdownMenu>,document.body)
}

const ProtectedPointerMenu=protectMessageUI(PointerMenuBody)
export function PointerMenu(props:Parameters<typeof PointerMenuBody>[0]){return props.anchor.owner.closest('[data-message-ui]')?<ProtectedPointerMenu {...props}/>:<PointerMenuBody {...props}/>}
