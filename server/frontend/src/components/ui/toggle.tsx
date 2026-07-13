'use client'

import { forwardRef } from 'react'
import { cn } from '../../lib/utils'

interface ToggleProps extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'onChange'> {
  checked: boolean
  onChange: (checked: boolean) => void
  label?: string
}

const Toggle = forwardRef<HTMLButtonElement, ToggleProps>(
  ({ className, checked, onChange, label, disabled, ...props }, ref) => (
    <label className={cn('inline-flex items-center gap-2', disabled && 'opacity-50', className)}>
      <button
        ref={ref}
        role="switch"
        type="button"
        aria-checked={checked}
        disabled={disabled}
        className={cn(
          'relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors',
          checked ? 'bg-primary' : 'bg-input',
          disabled && 'cursor-not-allowed'
        )}
        onClick={() => onChange(!checked)}
        {...props}
      >
        <span
          className={cn(
            'pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow-sm transition-transform',
            checked ? 'translate-x-4' : 'translate-x-0'
          )}
        />
      </button>
      {label && <span className="text-xs text-foreground">{label}</span>}
    </label>
  )
)
Toggle.displayName = 'Toggle'

export { Toggle }
export type { ToggleProps }
