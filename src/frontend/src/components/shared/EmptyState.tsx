import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

export function EmptyState({ icon: Icon, title, description, action, className }: { icon: LucideIcon; title: string; description: string; action?: ReactNode; className?: string }) {
  return <div className={cn('flex min-h-52 flex-col items-center justify-center px-6 py-10 text-center', className)}>
    <div className="mb-4 grid size-12 place-items-center rounded-2xl border border-primary/10 bg-primary/[0.07] text-primary"><Icon className="size-5" /></div>
    <h3 className="text-sm font-semibold">{title}</h3><p className="mt-1.5 max-w-sm text-xs leading-5 text-muted-foreground">{description}</p>{action && <div className="mt-4">{action}</div>}
  </div>
}
