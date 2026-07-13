import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '../../lib/utils'

const statusDotVariants = cva(
  'inline-block shrink-0 rounded-full',
  {
    variants: {
      variant: {
        default: 'bg-muted-foreground',
        success: 'bg-success',
        warning: 'bg-warning',
        error: 'bg-destructive',
      },
      size: {
        sm: 'h-2 w-2',
        default: 'h-2.5 w-2.5',
        lg: 'h-3 w-3',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  }
)

export interface StatusDotProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof statusDotVariants> {
  label?: string
}

function StatusDot({ className, variant, size, label, ...props }: StatusDotProps) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={cn(statusDotVariants({ variant, size, className }))} {...props} />
      {label && <span className="text-sm text-foreground">{label}</span>}
    </span>
  )
}

export { StatusDot, statusDotVariants }
