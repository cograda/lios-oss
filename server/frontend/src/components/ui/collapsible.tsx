'use client'

import { useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { cn } from '../../lib/utils'

interface CollapsibleProps {
  title: string
  children: React.ReactNode
  defaultOpen?: boolean
}

export function Collapsible({ title, children, defaultOpen = false }: CollapsibleProps) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center justify-between p-4 text-sm font-medium hover:bg-muted/50 transition-colors"
      >
        {title}
        <ChevronDown
          className={cn(
            'h-4 w-4 text-muted-foreground transition-transform duration-200',
            open && 'rotate-180'
          )}
        />
      </button>
      {open && <div className="px-4 pb-4">{children}</div>}
    </div>
  )
}
