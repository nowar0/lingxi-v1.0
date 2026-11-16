import * as ProgressPrimitive from '@radix-ui/react-progress'
import { cn } from '@/lib/utils'

export function Progress({ value = 0, className }: { value?: number; className?: string }) {
  return <ProgressPrimitive.Root value={value} className={cn('relative h-1.5 w-full overflow-hidden rounded-full bg-primary/10', className)}><ProgressPrimitive.Indicator className="h-full w-full rounded-full bg-primary transition-transform duration-300" style={{ transform: `translateX(-${100 - Math.min(100, Math.max(0, value))}%)` }} /></ProgressPrimitive.Root>
}
