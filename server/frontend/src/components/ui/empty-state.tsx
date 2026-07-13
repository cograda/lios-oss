import type { LucideIcon } from 'lucide-react'
import { Button } from './button'

type LinkComponentType = React.ComponentType<{
  href: string
  className?: string
  children: React.ReactNode
}>

const DefaultLink: LinkComponentType = ({ href, className, children }) => (
  <a href={href} className={className}>{children}</a>
)

interface EmptyStateProps {
  icon: LucideIcon
  title: string
  description?: string
  actionLabel?: string
  actionHref?: string
  LinkComponent?: LinkComponentType
}

export function EmptyState({
  icon: Icon,
  title,
  description,
  actionLabel,
  actionHref,
  LinkComponent = DefaultLink,
}: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-12 px-6 text-center">
      <Icon className="h-12 w-12 text-muted-foreground/40 mb-4" />
      <h3 className="text-lg font-medium text-foreground mb-1">{title}</h3>
      {description && <p className="text-sm text-muted-foreground mb-4">{description}</p>}
      {actionLabel && actionHref && (
        <LinkComponent href={actionHref}>
          <Button size="sm" className="w-auto px-6">
            {actionLabel}
          </Button>
        </LinkComponent>
      )}
    </div>
  )
}
