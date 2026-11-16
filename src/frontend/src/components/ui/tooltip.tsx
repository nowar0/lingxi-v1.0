import { useSurface } from '@/context/SurfaceContext'
import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import type { ComponentProps, ReactElement, ReactNode } from 'react'

export const TooltipProvider = TooltipPrimitive.Provider
export function Tooltip({ children, content, side = 'top' }: { children: ReactElement; content: ReactNode; side?: ComponentProps<typeof TooltipPrimitive.Content>['side'] }) {
  const surface=useSurface()
  if (content == null || typeof content === 'boolean' || (typeof content === 'string' && !content.trim())) return <>{children}</>
  return <TooltipPrimitive.Root delayDuration={450}><TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger><TooltipPrimitive.Portal><TooltipPrimitive.Content data-ui-surface={surface['data-ui-surface']} style={surface.style} side={side} sideOffset={6} className="z-[70] rounded-lg border border-border/70 bg-popover/95 px-2.5 py-1.5 text-xs text-popover-foreground shadow-md backdrop-blur-xl animate-in fade-in duration-150">{content}</TooltipPrimitive.Content></TooltipPrimitive.Portal></TooltipPrimitive.Root>
}
