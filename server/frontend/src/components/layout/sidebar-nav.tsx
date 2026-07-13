'use client'

import { forwardRef } from 'react'
import type { LucideIcon } from 'lucide-react'
import { cn } from '../../lib/utils'

type LinkComponentType = React.ComponentType<{
  href: string
  className?: string
  children: React.ReactNode
}>

interface NavItem {
  href: string
  icon?: LucideIcon
  label: string
}

interface SidebarNavProps extends React.HTMLAttributes<HTMLElement> {
  items: NavItem[]
  activePath: string
  header?: React.ReactNode
  LinkComponent?: LinkComponentType
}

const SidebarNav = forwardRef<HTMLElement, SidebarNavProps>(
  ({ className, items, activePath, header, LinkComponent = 'a' as unknown as LinkComponentType, ...props }, ref) => (
    <nav
      ref={ref}
      className={cn('flex h-screen w-60 shrink-0 flex-col border-r border-border bg-card', className)}
      {...props}
    >
      {header && <div className="p-4 border-b border-border">{header}</div>}
      <div className="flex flex-col gap-0.5 p-2">
        {items.map((item) => {
          const isActive = activePath === item.href || activePath.startsWith(item.href + '/')
          const Icon = item.icon
          return (
            <LinkComponent
              key={item.href}
              href={item.href}
              className={cn(
                'flex items-center gap-3 rounded-lg px-3 py-1.5 text-xs uppercase tracking-wide font-medium transition-colors',
                isActive
                  ? 'bg-muted text-foreground'
                  : 'text-muted-foreground hover:bg-muted/50 hover:text-foreground'
              )}
            >
              {Icon && <Icon className="h-4 w-4 shrink-0" />}
              {item.label}
            </LinkComponent>
          )
        })}
      </div>
    </nav>
  )
)
SidebarNav.displayName = 'SidebarNav'

export { SidebarNav }
export type { SidebarNavProps, NavItem }
