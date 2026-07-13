'use client'

import { Star } from 'lucide-react'
import { cn } from '../../lib/utils'

interface RatingProps {
  value: number | null | undefined
  onChange?: (value: number) => void
  max?: number
  label?: string
  readonly?: boolean
}

export function Rating({ value, onChange, max = 5, label, readonly = false }: RatingProps) {
  const current = value ?? 0

  return (
    <div className="space-y-1">
      {label && <span className="text-sm font-medium text-muted-foreground">{label}</span>}
      <div className="flex gap-1">
        {Array.from({ length: max }, (_, i) => {
          const starValue = i + 1
          const filled = starValue <= current
          return (
            <button
              key={i}
              type="button"
              disabled={readonly}
              onClick={() => {
                if (!readonly && onChange) {
                  onChange(starValue === current ? 0 : starValue)
                }
              }}
              className={cn(
                'p-0.5 transition-colors',
                readonly ? 'cursor-default' : 'cursor-pointer active:scale-110'
              )}
              aria-label={`${starValue} of ${max}`}
            >
              <Star
                className={cn(
                  'h-6 w-6',
                  filled ? 'fill-accent text-accent' : 'text-muted-foreground/30'
                )}
              />
            </button>
          )
        })}
      </div>
    </div>
  )
}
