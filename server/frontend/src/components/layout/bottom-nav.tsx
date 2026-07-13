'use client'

import type { LucideIcon } from 'lucide-react'
import { cn } from '../../lib/utils'

type LinkComponentType = React.ComponentType<{
  href: string
  className?: string
  children: React.ReactNode
}>

const DefaultLink: LinkComponentType = ({ href, className, children }) => (
  <a href={href} className={className}>{children}</a>
)

export interface NavItem {
  href: string
  icon: LucideIcon
  label: string
}

interface BottomNavProps {
  items: NavItem[]
  activePath: string
  LinkComponent?: LinkComponentType
}

export function BottomNav({ items, activePath, LinkComponent = DefaultLink }: BottomNavProps) {
  return (
    <nav className="fixed bottom-0 left-0 right-0 z-40 border-t border-border bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/80">
      <div className="max-w-lg mx-auto flex justify-around">
        {items.map(({ href, icon: Icon, label }) => {
          const active = activePath.startsWith(href)
          return (
            <LinkComponent
              key={href}
              href={href}
              className={cn(
                'flex flex-col items-center gap-0.5 py-2 px-4 text-xs transition-colors',
                active ? 'text-accent' : 'text-muted-foreground'
              )}
            >
              <Icon className="h-5 w-5" />
              {label}
            </LinkComponent>
          )
        })}
      </div>
    </nav>
  )
}
