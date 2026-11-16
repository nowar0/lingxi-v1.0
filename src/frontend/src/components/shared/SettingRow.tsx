import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

export function SettingRow({ title, description, children, className }: { title: string; description?: string; children: ReactNode; className?: string }) {
  return <div className={cn('setting-row flex min-h-16 items-center justify-between gap-6 border-b border-border/60 py-4 last:border-0', className)}>
    <div className="min-w-0"><div className="text-[13px] font-medium">{title}</div>{description && <div className="mt-1 text-xs leading-5 text-muted-foreground">{description}</div>}</div>
    <div className="shrink-0">{children}</div>
  </div>
}
