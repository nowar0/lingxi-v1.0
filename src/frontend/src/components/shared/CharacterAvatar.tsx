import { cn } from '@/lib/utils'

export function CharacterAvatar({ src, name, className }: { src?: string; name: string; className?: string }) {
  return <div className={cn('relative grid size-9 shrink-0 place-items-center overflow-hidden rounded-xl border border-white/60 bg-gradient-to-br from-sky-100 to-indigo-100 shadow-sm dark:border-white/10 dark:from-sky-950 dark:to-indigo-950', className)}>{src ? <img src={src} alt={name} className="size-full object-cover" /> : <span className="text-sm font-semibold text-primary">{name.slice(0, 1)}</span>}</div>
}
