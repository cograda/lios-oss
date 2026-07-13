import { Monitor } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { StatusDot } from '@/components/ui/status-dot'
import type { ClientInfo } from '@/lib/api'

function isOnline(lastSeen: string | null): boolean {
  if (!lastSeen) return false
  return Date.now() - new Date(lastSeen).getTime() < 2 * 60_000 // 2 minutes
}

function relativeTime(iso: string | null): string {
  if (!iso) return 'Never'
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60_000)
  if (mins < 1) return 'Just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

interface ClientStatusCardProps {
  clients: ClientInfo[]
}

export function ClientStatusCard({ clients }: ClientStatusCardProps) {
  const active = clients.filter((c) => c.is_active)

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Monitor className="h-4 w-4" />
          Clients
          <Badge variant="outline" className="ml-auto text-xs font-mono">
            {active.length} active
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2.5">
        {active.length === 0 && (
          <p className="text-sm text-muted-foreground">No active clients</p>
        )}
        {active.map((c) => {
          const online = isOnline(c.last_seen_at)
          return (
            <div key={c.id} className="flex items-center justify-between text-sm">
              <div className="flex items-center gap-2">
                <StatusDot variant={online ? 'success' : 'default'} size="sm" />
                <span className="font-medium">{c.user}</span>
                <span className="text-xs text-muted-foreground">{c.label}</span>
              </div>
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                {c.client_version && (
                  <span className="font-mono">{c.client_version}</span>
                )}
                <span>{relativeTime(c.last_seen_at)}</span>
              </div>
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}
