'use client'

import { forwardRef } from 'react'
import { cn } from '../../lib/utils'

interface SliderProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'onChange' | 'type'> {
  value: number
  onChange: (value: number) => void
  label?: string
  showValue?: boolean
}

const Slider = forwardRef<HTMLInputElement, SliderProps>(
  ({ className, value, onChange, label, showValue = true, min = 0, max = 100, step = 1, disabled, ...props }, ref) => (
    <div className={cn('flex flex-col gap-1', disabled && 'opacity-50', className)}>
      {(label || showValue) && (
        <div className="flex items-center justify-between">
          {label && <span className="text-xs font-medium text-foreground">{label}</span>}
          {showValue && <span className="text-xs font-mono text-muted-foreground">{value}</span>}
        </div>
      )}
      <input
        ref={ref}
        type="range"
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        className={cn(
          'h-2 w-full cursor-pointer appearance-none rounded-full bg-muted',
          '[&::-webkit-slider-thumb]:h-4 [&::-webkit-slider-thumb]:w-4 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-primary [&::-webkit-slider-thumb]:shadow-sm',
          '[&::-moz-range-thumb]:h-4 [&::-moz-range-thumb]:w-4 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-primary [&::-moz-range-thumb]:shadow-sm',
          disabled && 'cursor-not-allowed'
        )}
        {...props}
      />
    </div>
  )
)
Slider.displayName = 'Slider'

export { Slider }
export type { SliderProps }
