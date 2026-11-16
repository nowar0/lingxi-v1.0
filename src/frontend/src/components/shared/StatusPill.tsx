import { cn } from '@/lib/utils'

export function StatusPill({ label, tone = 'neutral', pulse = false, className }: { label: string; tone?: 'neutral' | 'success' | 'warning' | 'danger' | 'primary'; pulse?: boolean; className?: string }) {
  const colors = { neutral: 'bg-slate-400', success: 'bg-emerald-500', warning: 'bg-amber-500', danger: 'bg-red-500', primary: 'bg-primary' }
  return <span className={cn('inline-flex h-7 items-center gap-1.5 rounded-full border border-border/65 bg-card/60 px-2.5 text-[11px] text-muted-foreground', className)}><span className={cn('size-1.5 rounded-full', colors[tone], pulse && 'animate-pulse')} />{label}</span>
}
