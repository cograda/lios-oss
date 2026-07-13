'use client'

import { useCallback, useState } from 'react'
import { X } from 'lucide-react'
import { cva } from 'class-variance-authority'
import { cn } from '../../lib/utils'

const toastVariants = cva(
  'pointer-events-auto flex items-center gap-3 rounded-lg border px-4 py-3 shadow-lg transition-all',
  {
    variants: {
      variant: {
        default: 'border-border bg-card text-card-foreground',
        success: 'border-success/30 bg-success/10 text-success',
        error: 'border-destructive/30 bg-destructive/10 text-destructive',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  }
)

interface Toast {
  id: string
  message: string
  variant?: 'default' | 'success' | 'error'
}

interface ToastProps extends Toast {
  onDismiss: (id: string) => void
}

function ToastItem({ id, message, variant, onDismiss }: ToastProps) {
  return (
    <div className={cn(toastVariants({ variant }))}>
      <span className="flex-1 text-sm">{message}</span>
      <button
        className="shrink-0 rounded-md p-0.5 hover:bg-muted transition-colors"
        onClick={() => onDismiss(id)}
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  )
}

function ToastContainer({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: string) => void }) {
  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 pointer-events-none">
      {toasts.map((toast) => (
        <ToastItem key={toast.id} {...toast} onDismiss={onDismiss} />
      ))}
    </div>
  )
}

let toastCounter = 0

function useToast(autoDismissMs = 4000) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const toast = useCallback(
    (message: string, variant: Toast['variant'] = 'default') => {
      const id = `toast-${++toastCounter}`
      setToasts((prev) => [...prev, { id, message, variant }])
      if (autoDismissMs > 0) {
        setTimeout(() => dismiss(id), autoDismissMs)
      }
      return id
    },
    [autoDismissMs, dismiss]
  )

  return { toasts, toast, dismiss, ToastContainer }
}

export { ToastContainer, useToast, toastVariants }
export type { Toast }
