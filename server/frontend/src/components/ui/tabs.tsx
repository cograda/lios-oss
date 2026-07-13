'use client'

import { createContext, forwardRef, useContext } from 'react'
import { cn } from '../../lib/utils'

const TabsContext = createContext<{ value: string; onValueChange: (value: string) => void }>({
  value: '',
  onValueChange: () => {},
})

interface TabsProps extends React.HTMLAttributes<HTMLDivElement> {
  value: string
  onValueChange: (value: string) => void
}

const Tabs = forwardRef<HTMLDivElement, TabsProps>(
  ({ className, value, onValueChange, ...props }, ref) => (
    <TabsContext.Provider value={{ value, onValueChange }}>
      <div ref={ref} className={cn('w-full', className)} {...props} />
    </TabsContext.Provider>
  )
)
Tabs.displayName = 'Tabs'

const TabsList = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        'inline-flex items-center gap-1 rounded-lg bg-muted p-1',
        className
      )}
      {...props}
    />
  )
)
TabsList.displayName = 'TabsList'

interface TabsTriggerProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  value: string
}

const TabsTrigger = forwardRef<HTMLButtonElement, TabsTriggerProps>(
  ({ className, value, ...props }, ref) => {
    const ctx = useContext(TabsContext)
    const isActive = ctx.value === value
    return (
      <button
        ref={ref}
        className={cn(
          'inline-flex items-center justify-center rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
          isActive
            ? 'bg-card text-foreground shadow-sm'
            : 'text-muted-foreground hover:text-foreground',
          className
        )}
        onClick={() => ctx.onValueChange(value)}
        {...props}
      />
    )
  }
)
TabsTrigger.displayName = 'TabsTrigger'

interface TabsContentProps extends React.HTMLAttributes<HTMLDivElement> {
  value: string
}

const TabsContent = forwardRef<HTMLDivElement, TabsContentProps>(
  ({ className, value, ...props }, ref) => {
    const ctx = useContext(TabsContext)
    if (ctx.value !== value) return null
    return <div ref={ref} className={cn('mt-3', className)} {...props} />
  }
)
TabsContent.displayName = 'TabsContent'

export { Tabs, TabsList, TabsTrigger, TabsContent }
