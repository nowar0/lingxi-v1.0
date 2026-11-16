import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

export function PageHeader({ eyebrow, title, description, actions, className, headingLevel = 'h1' }: { headingLevel?: 'h1' | 'h2'; eyebrow?: string; title: string; description?: string; actions?: ReactNode; className?: string }) {
  const Heading = headingLevel
  return <div data-page-header className={cn('mb-5 flex flex-wrap items-end justify-between gap-4', className)}>
    <div className="min-w-0">
      {false && eyebrow}
      <Heading className={cn(headingLevel === "h2" ? "text-[18px]" : "text-[24px]", "font-semibold tracking-[-0.035em] text-foreground")}>{title}</Heading>
      {description && <p className="mt-1.5 max-w-2xl text-[13px] leading-5 text-muted-foreground">{description}</p>}
    </div>
    {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
  </div>
}
