import * as Dropdown from '@radix-ui/react-dropdown-menu'
import type { ComponentProps } from 'react'
import { useSurface } from '@/context/SurfaceContext'
import { cn } from '@/lib/utils'

export const DropdownMenu = Dropdown.Root
export const DropdownMenuTrigger = Dropdown.Trigger
export const DropdownMenuGroup = Dropdown.Group
export const DropdownMenuSub = Dropdown.Sub
export function DropdownMenuSubTrigger({ className, ...props }: ComponentProps<typeof Dropdown.SubTrigger>) { return <Dropdown.SubTrigger className={cn('flex cursor-default items-center gap-2 rounded-lg px-2.5 py-2 text-sm outline-none focus:bg-accent data-[state=open]:bg-accent [&_svg]:size-4', className)} {...props} /> }
export function DropdownMenuSubContent({ className, ...props }: ComponentProps<typeof Dropdown.SubContent>) { const surface=useSurface(); return <Dropdown.Portal><Dropdown.SubContent data-ui-surface={surface['data-ui-surface']} style={surface.style} className={cn('z-[80] min-w-44 rounded-xl border border-border bg-popover/95 p-1.5 shadow-lg backdrop-blur-xl outline-none',className)} {...props} /></Dropdown.Portal> }
export const DropdownMenuSeparator = ({ className, ...props }: ComponentProps<typeof Dropdown.Separator>) => <Dropdown.Separator className={cn('my-1 h-px bg-border', className)} {...props} />
export const DropdownMenuLabel = ({ className, ...props }: ComponentProps<typeof Dropdown.Label>) => <Dropdown.Label className={cn('px-2 py-1.5 text-xs font-medium text-muted-foreground', className)} {...props} />
export function DropdownMenuContent({ className, sideOffset = 6, ...props }: ComponentProps<typeof Dropdown.Content>) {
  const surface=useSurface()
  return <Dropdown.Portal><Dropdown.Content data-ui-surface={surface['data-ui-surface']} style={surface.style} sideOffset={sideOffset} className={cn('z-50 min-w-44 rounded-xl border border-white/60 bg-popover/95 p-1.5 shadow-xl backdrop-blur-2xl outline-none data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out data-[state=open]:fade-in data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 dark:border-white/10', className)} {...props} /></Dropdown.Portal>
}
export const DropdownMenuItem = ({ className, ...props }: ComponentProps<typeof Dropdown.Item>) => <Dropdown.Item className={cn('relative flex cursor-default select-none items-center gap-2 rounded-lg px-2.5 py-2 text-sm outline-none transition-colors focus:bg-accent focus:text-foreground data-[disabled]:pointer-events-none data-[disabled]:opacity-50 [&_svg]:size-4', className)} {...props} />
