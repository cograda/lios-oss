import { Link } from 'react-router'
import {
  Calendar, Mail, CheckSquare, Wallet, BookOpen, MessageCircle,
  Cloud, Music, Train, Heart, Activity,
} from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { StatusDot } from '@/components/ui/status-dot'
import { cn } from '@/lib/utils'
import type { IntegrationStatus } from '@/lib/api'

const ICONS: Record<string, React.ElementType> = {
  google_calendar: Calendar,
  google_mail: Mail,
  apple_reminders: CheckSquare,
  apple_health: Heart,
  finance: Wallet,
  obsidian: BookOpen,
  whatsapp: MessageCircle,
  weather: Cloud,
  lastfm: Music,
  irish_rail: Train,
  system: Activity,
}

function statusVariant(int: IntegrationStatus): 'success' | 'error' | 'warning' | 'default' {
  if (!int.configured) return 'default'
  if (int.consecutive_failures >= 3) return 'error'
  if (int.last_sync_status === 'ok') return 'success'
  if (int.last_sync_status === 'never') return 'default'
  return 'warning'
}

function timeAgo(iso: string | null): string {
  if (!iso) return 'never'
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

function formatDuration(ms: number | null): string | null {
  if (ms == null) return null
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

interface IntegrationGridProps {
  integrations: IntegrationStatus[]
}

export function IntegrationGrid({ integrations }: IntegrationGridProps) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {integrations.map((int) => {
        const Icon = ICONS[int.name] || Activity
        const variant = statusVariant(int)
        const duration = formatDuration(int.last_sync_duration_ms)

        return (
          <Link key={int.name} to={`/integrations/${int.name}`}>
            <Card className={cn(
              'transition-colors hover:border-muted-foreground/30 cursor-pointer',
              variant === 'error' && 'border-destructive/30',
            )}>
              <CardContent className="p-4">
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-2.5">
                    <Icon className="h-4 w-4 text-muted-foreground" />
                    <span className="text-sm font-medium">{int.display_name}</span>
                  </div>
                  <StatusDot variant={variant} size="sm" />
                </div>

                <div className="mt-3 flex items-center gap-3 text-xs text-muted-foreground">
                  <span>{timeAgo(int.last_sync_at)}</span>
                  {duration && <span>{duration}</span>}
                  {int.schedule && (
                    <span className="font-mono ml-auto">{int.schedule}</span>
                  )}
                </div>

                {int.consecutive_failures > 0 && (
                  <p className="mt-2 text-xs text-destructive truncate">
                    {int.consecutive_failures}x failed{int.last_error ? `: ${int.last_error}` : ''}
                  </p>
                )}
              </CardContent>
            </Card>
          </Link>
        )
      })}
    </div>
  )
}
