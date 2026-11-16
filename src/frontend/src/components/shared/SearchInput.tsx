import { Search, X } from 'lucide-react'
import type { InputHTMLAttributes } from 'react'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

export function SearchInput({ className, value, onChange, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <div className={cn('relative', className)}><Search className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground/70" /><Input className="pl-8 pr-8" value={value} onChange={onChange} {...props} />{value && <button aria-label="清空搜索" className="absolute right-2.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted-foreground hover:text-foreground" onClick={() => onChange?.({ target: { value: '' } } as React.ChangeEvent<HTMLInputElement>)}><X className="size-3.5" /></button>}</div>
}
