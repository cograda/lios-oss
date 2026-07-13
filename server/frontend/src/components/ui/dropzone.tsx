'use client'

import { forwardRef, useCallback, useState } from 'react'
import { Upload } from 'lucide-react'
import { cn } from '../../lib/utils'

interface DropzoneProps extends Omit<React.HTMLAttributes<HTMLDivElement>, 'onDrop'> {
  onDrop: (files: File[]) => void
  accept?: string
  multiple?: boolean
  label?: string
  description?: string
  disabled?: boolean
}

const Dropzone = forwardRef<HTMLDivElement, DropzoneProps>(
  ({ className, onDrop, accept, multiple = false, label = 'Drop files here', description, disabled, ...props }, ref) => {
    const [isDragging, setIsDragging] = useState(false)

    const handleDragOver = useCallback((e: React.DragEvent) => {
      e.preventDefault()
      if (!disabled) setIsDragging(true)
    }, [disabled])

    const handleDragLeave = useCallback(() => {
      setIsDragging(false)
    }, [])

    const handleDrop = useCallback(
      (e: React.DragEvent) => {
        e.preventDefault()
        setIsDragging(false)
        if (disabled) return
        const files = Array.from(e.dataTransfer.files)
        if (files.length > 0) onDrop(multiple ? files : [files[0]])
      },
      [disabled, multiple, onDrop]
    )

    const handleClick = useCallback(() => {
      if (disabled) return
      const input = document.createElement('input')
      input.type = 'file'
      if (accept) input.accept = accept
      input.multiple = multiple
      input.onchange = () => {
        const files = Array.from(input.files || [])
        if (files.length > 0) onDrop(files)
      }
      input.click()
    }, [accept, disabled, multiple, onDrop])

    return (
      <div
        ref={ref}
        role="button"
        tabIndex={disabled ? -1 : 0}
        className={cn(
          'flex flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed p-8 text-center transition-colors cursor-pointer',
          isDragging
            ? 'border-primary bg-primary/5'
            : 'border-border hover:border-muted-foreground',
          disabled && 'cursor-not-allowed opacity-50',
          className
        )}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        onClick={handleClick}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') handleClick() }}
        {...props}
      >
        <Upload className="h-8 w-8 text-muted-foreground" />
        <p className="text-sm font-medium text-foreground">{label}</p>
        {description && <p className="text-xs text-muted-foreground">{description}</p>}
      </div>
    )
  }
)
Dropzone.displayName = 'Dropzone'

export { Dropzone }
export type { DropzoneProps }
