import { forwardRef } from 'react'
import { cn } from '../../lib/utils'

interface StatCardProps extends React.HTMLAttributes<HTMLDivElement> {
  label: string
  value: string | number
  trend?: { value: string; positive?: boolean }
}

const StatCard = forwardRef<HTMLDivElement, StatCardProps>(
  ({ className, label, value, trend, ...props }, ref) => (
    <div
      ref={ref}
      className={cn('rounded-md border border-border bg-card p-3 shadow-sm', className)}
      {...props}
    >
      <p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="mt-1 text-lg font-semibold tracking-tight font-mono">{value}</p>
      {trend && (
        <p
          className={cn(
            'mt-1 text-xs font-medium',
            trend.positive === true && 'text-success',
            trend.positive === false && 'text-destructive',
            trend.positive === undefined && 'text-muted-foreground'
          )}
        >
          {trend.value}
        </p>
      )}
    </div>
  )
)
StatCard.displayName = 'StatCard'

export { StatCard }
export type { StatCardProps }
