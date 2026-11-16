import * as React from 'react'
import { cn } from '@/lib/utils'

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(({ className, type, ...props }, ref) => <input type={type} className={cn('flex h-9 w-full rounded-[calc(var(--radius)*0.68)] border border-border/80 bg-input/75 px-3 text-sm text-foreground shadow-[inset_0_1px_0_rgba(255,255,255,.35)] outline-none transition-[border-color,box-shadow,background-color] placeholder:text-muted-foreground/70 focus:border-foreground/40 focus:bg-card focus-visible:outline-1 focus-visible:outline-foreground/60 focus-visible:outline-offset-2 disabled:opacity-50', className)} ref={ref} {...props} />)
Input.displayName = 'Input'

export const Textarea = React.forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(({ className, ...props }, ref) => <textarea className={cn('flex min-h-24 w-full resize-none rounded-[calc(var(--radius)*0.75)] border border-border/80 bg-input/75 px-3 py-2.5 text-sm leading-6 outline-none transition-[border-color,box-shadow] placeholder:text-muted-foreground/70 focus:border-foreground/40 focus-visible:outline-1 focus-visible:outline-foreground/60 focus-visible:outline-offset-2 disabled:opacity-50', className)} ref={ref} {...props} />)
Textarea.displayName = 'Textarea'
