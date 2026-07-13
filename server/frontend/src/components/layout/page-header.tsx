import { ArrowLeft } from 'lucide-react'

type LinkComponentType = React.ComponentType<{
  href: string
  className?: string
  children: React.ReactNode
}>

const DefaultLink: LinkComponentType = ({ href, className, children }) => (
  <a href={href} className={className}>{children}</a>
)

interface PageHeaderProps {
  title: string
  backTo?: string
  action?: React.ReactNode
  LinkComponent?: LinkComponentType
}

export function PageHeader({ title, backTo, action, LinkComponent = DefaultLink }: PageHeaderProps) {
  return (
    <header className="flex items-center justify-between mb-4">
      <div className="flex items-center gap-3">
        {backTo && (
          <LinkComponent
            href={backTo}
            className="p-1.5 -ml-1.5 rounded-lg hover:bg-muted transition-colors"
          >
            <ArrowLeft className="h-5 w-5" />
          </LinkComponent>
        )}
        <h1 className="text-sm font-semibold uppercase tracking-wider text-foreground">{title}</h1>
      </div>
      {action && <div>{action}</div>}
    </header>
  )
}
