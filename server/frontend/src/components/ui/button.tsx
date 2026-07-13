import { forwardRef } from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '../../lib/utils'

const buttonVariants = cva(
  'inline-flex items-center justify-center rounded-md font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        default: 'bg-primary text-primary-foreground shadow-sm hover:bg-primary/90',
        secondary: 'bg-card text-card-foreground border border-border shadow-sm hover:bg-muted',
        ghost: 'text-muted-foreground hover:text-foreground hover:bg-muted',
        destructive: 'bg-destructive text-destructive-foreground shadow-sm hover:bg-destructive/90',
        link: 'text-muted-foreground underline-offset-4 hover:underline hover:text-foreground',
      },
      size: {
        default: 'px-3 py-1.5 text-sm',
        sm: 'px-3 py-1.5 text-sm rounded-md',
        lg: 'py-2.5 text-sm font-semibold',
        icon: 'h-9 w-9 rounded-md',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  }
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => {
    return (
      <button
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    )
  }
)
Button.displayName = 'Button'

export { Button, buttonVariants }
