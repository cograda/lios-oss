import { forwardRef } from 'react'
import { cn } from '../../lib/utils'

export interface LabelProps extends React.LabelHTMLAttributes<HTMLLabelElement> {}

const Label = forwardRef<HTMLLabelElement, LabelProps>(
  ({ className, ...props }, ref) => {
    return (
      <label
        className={cn('text-xs font-medium uppercase tracking-wide leading-none', className)}
        ref={ref}
        {...props}
      />
    )
  }
)
Label.displayName = 'Label'

export { Label }
