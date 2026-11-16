import type { HTMLAttributes } from 'react'
import { cn } from '@/lib/utils'

export function Badge({ className, variant = 'default', ...props }: HTMLAttributes<HTMLSpanElement> & { variant?: 'default' | 'secondary' | 'outline' | 'success' | 'warning' | 'danger' }) {
  const styles = {
    default: 'bg-primary/10 text-primary border-primary/10',
    secondary: 'bg-secondary text-muted-foreground border-border/60',
    outline: 'bg-transparent text-muted-foreground border-border',
    success: 'bg-emerald-500/10 text-emerald-600 border-emerald-500/15 dark:text-emerald-400',
    warning: 'bg-amber-500/10 text-amber-600 border-amber-500/15 dark:text-amber-400',
    danger: 'bg-red-500/10 text-red-600 border-red-500/15 dark:text-red-400',
  }
  return <span className={cn('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4', styles[variant], className)} {...props} />
}
