'use client'

import { forwardRef, useEffect, useRef } from 'react'
import { X } from 'lucide-react'
import { cn } from '../../lib/utils'

interface DialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  children: React.ReactNode
}

function Dialog({ open, onOpenChange, children }: DialogProps) {
  const overlayRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onOpenChange(false)
    }
    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [open, onOpenChange])

  if (!open) return null

  return (
    <div
      ref={overlayRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={(e) => {
        if (e.target === overlayRef.current) onOpenChange(false)
      }}
    >
      {children}
    </div>
  )
}

const DialogContent = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, children, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        'relative w-full max-w-lg rounded-md border border-border bg-card p-4 shadow-lg',
        className
      )}
      {...props}
    >
      {children}
    </div>
  )
)
DialogContent.displayName = 'DialogContent'

interface DialogCloseProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  onClose: () => void
}

const DialogClose = forwardRef<HTMLButtonElement, DialogCloseProps>(
  ({ className, onClose, ...props }, ref) => (
    <button
      ref={ref}
      className={cn(
        'absolute right-4 top-4 rounded-md p-1 text-muted-foreground hover:text-foreground transition-colors',
        className
      )}
      onClick={onClose}
      {...props}
    >
      <X className="h-4 w-4" />
    </button>
  )
)
DialogClose.displayName = 'DialogClose'

const DialogHeader = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn('flex flex-col space-y-1.5 pb-4', className)} {...props} />
  )
)
DialogHeader.displayName = 'DialogHeader'

const DialogTitle = forwardRef<HTMLHeadingElement, React.HTMLAttributes<HTMLHeadingElement>>(
  ({ className, ...props }, ref) => (
    <h2
      ref={ref}
      className={cn('text-lg font-semibold leading-none tracking-tight', className)}
      {...props}
    />
  )
)
DialogTitle.displayName = 'DialogTitle'

const DialogDescription = forwardRef<HTMLParagraphElement, React.HTMLAttributes<HTMLParagraphElement>>(
  ({ className, ...props }, ref) => (
    <p ref={ref} className={cn('text-sm text-muted-foreground', className)} {...props} />
  )
)
DialogDescription.displayName = 'DialogDescription'

const DialogFooter = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn('flex justify-end gap-2 pt-4', className)}
      {...props}
    />
  )
)
DialogFooter.displayName = 'DialogFooter'

export { Dialog, DialogContent, DialogClose, DialogHeader, DialogTitle, DialogDescription, DialogFooter }
