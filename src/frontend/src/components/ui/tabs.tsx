import * as TabsPrimitive from '@radix-ui/react-tabs'
import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

export const Tabs = TabsPrimitive.Root
export const TabsList = ({ className, ...props }: ComponentProps<typeof TabsPrimitive.List>) => <TabsPrimitive.List className={cn('inline-flex h-9 items-center justify-center rounded-[calc(var(--radius)*0.7)] bg-secondary/70 p-1 text-muted-foreground', className)} {...props} />
export const TabsTrigger = ({ className, ...props }: ComponentProps<typeof TabsPrimitive.Trigger>) => <TabsPrimitive.Trigger className={cn('inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-[calc(var(--radius)*0.5)] px-3 py-1.5 text-xs font-medium transition-[background-color,color,box-shadow] focus-visible:outline-none disabled:opacity-50 data-[state=active]:bg-card data-[state=active]:text-foreground data-[state=active]:shadow-sm [&_svg]:size-3.5', className)} {...props} />
export const TabsContent = ({ className, ...props }: ComponentProps<typeof TabsPrimitive.Content>) => <TabsPrimitive.Content className={cn('mt-3 outline-none', className)} {...props} />
