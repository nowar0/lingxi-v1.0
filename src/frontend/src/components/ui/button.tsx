import * as React from 'react'
import { useSurface } from '@/context/SurfaceContext'
import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const buttonVariants = cva('inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-[calc(var(--radius)*0.72)] text-sm font-medium transition-[background-color,border-color,color,box-shadow,transform] duration-150 outline-none focus-visible:ring-2 focus-visible:ring-ring/30 disabled:pointer-events-none disabled:opacity-45 active:scale-[0.98] [&_svg]:size-4 [&_svg]:shrink-0', {
  variants: {
    variant: {
      default: 'bg-primary text-primary-foreground shadow-[0_5px_14px_color-mix(in_srgb,var(--primary)_22%,transparent)] hover:bg-primary/90',
      secondary: 'bg-secondary/85 text-secondary-foreground border border-border/70 hover:bg-secondary',
      outline: 'border border-border bg-card/65 shadow-sm hover:bg-accent',
      ghost: 'text-muted-foreground hover:bg-accent hover:text-foreground',
      soft: 'bg-primary/10 text-primary hover:bg-primary/15',
      destructive: 'bg-red-500 text-white hover:bg-red-600',
    },
    size: { default: 'h-9 px-4', sm: 'h-8 px-3 text-xs', lg: 'h-11 px-5', icon: 'size-9 p-0', xs: 'h-7 px-2.5 text-xs' },
  },
  defaultVariants: { variant: 'default', size: 'default' },
})

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> { asChild?: boolean }
export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, size, asChild = false, ...props }, ref) => {
  const surface=useSurface()
  const Comp = asChild ? Slot : 'button'
  return <Comp data-ui-surface={surface['data-ui-surface']} data-ui-button={variant || 'default'} data-ui-size={size || 'default'} className={cn(buttonVariants({ variant, size }), className)} ref={ref} {...props} />
})
Button.displayName = 'Button'
export { buttonVariants }
