import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import { Children, isValidElement, type ComponentProps } from 'react'
import { useSurface } from '@/context/SurfaceContext'
import { cn } from '@/lib/utils'

export const Dialog = DialogPrimitive.Root
export const DialogTrigger = DialogPrimitive.Trigger
export const DialogClose = DialogPrimitive.Close
export function DialogContent({ className, children, overlayClassName, dismissOnOutside = true, onCloseRequest, onPointerDownOutside, ...props }: ComponentProps<typeof DialogPrimitive.Content> & { overlayClassName?: string; dismissOnOutside?: boolean; onCloseRequest?: () => void }) {
  const surface=useSurface()
  const pieces=Children.toArray(children)
  const header=pieces.filter(child=>isValidElement(child)&&child.type===DialogHeader)
  const footer=pieces.filter(child=>isValidElement(child)&&child.type===DialogFooter)
  const body=pieces.filter(child=>!header.includes(child)&&!footer.includes(child))
  return <DialogPrimitive.Portal>
    <DialogPrimitive.Overlay data-ui-surface={surface['data-ui-surface']} className={cn('fixed inset-0 z-50 bg-slate-950/25 backdrop-blur-[3px] data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out data-[state=open]:fade-in', overlayClassName)} />
    <DialogPrimitive.Content data-ui-surface={surface['data-ui-surface']} style={surface.style} onPointerDownOutside={event => { onPointerDownOutside?.(event); if (!dismissOnOutside) event.preventDefault() }} className={cn(!surface.protectedUI && 'ui-dialog', 'fixed left-1/2 top-1/2 z-50 grid max-h-[88vh] w-[calc(100%-32px)] max-w-lg -translate-x-1/2 -translate-y-1/2 gap-4 overflow-auto rounded-[calc(var(--radius)*1.25)] border border-white/50 bg-card/95 p-6 shadow-2xl backdrop-blur-2xl outline-none data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out data-[state=open]:fade-in data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 dark:border-white/10', className)} {...props}>
      {surface.protectedUI ? children : <>{header}<div className="ui-dialog-body">{body}</div>{footer}</>}
      <DialogPrimitive.Close type="button" onClick={onCloseRequest} aria-label="关闭" className="absolute right-3 top-3 z-20 inline-flex size-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"><X className="size-4" aria-hidden="true" /></DialogPrimitive.Close>
    </DialogPrimitive.Content>
  </DialogPrimitive.Portal>
}
export const DialogHeader = ({ className, ...props }: ComponentProps<'div'>) => <div className={cn('flex flex-col space-y-1.5 text-left', className)} {...props} />
export const DialogFooter = ({ className, ...props }: ComponentProps<'div'>) => <div className={cn('flex flex-col-reverse gap-2 sm:flex-row sm:justify-end', className)} {...props} />
export const DialogTitle = ({ className, ...props }: ComponentProps<typeof DialogPrimitive.Title>) => <DialogPrimitive.Title className={cn('text-lg font-semibold tracking-tight', className)} {...props} />
export const DialogDescription = ({ className, ...props }: ComponentProps<typeof DialogPrimitive.Description>) => <DialogPrimitive.Description className={cn('text-sm leading-6 text-muted-foreground', className)} {...props} />
