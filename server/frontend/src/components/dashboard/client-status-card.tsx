import { useState } from 'react'
import { ChevronDown, Monitor } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { StatusDot } from '@/components/ui/status-dot'
import { cn } from '@/lib/utils'
import type { ClientInfo, ClientTaskHealth } from '@/lib/api'

// F11b: mirrors system_alerts axis 5's thresholds (app/integrations/system/tools.py)
// so the dashboard flags the exact same conditions the ntfy push would.
const DAEMON_SILENT_MINUTES = 20
const TASK_RESTART_THRESHOLD = 3

function isOnline(lastSeen: string | null): boolean {
  if (!lastSeen) return false
  return Date.now() - new Date(lastSeen).getTime() < 2 * 60_000 // 2 minutes
}

function isSilent(lastSeen: string | null): boolean {
  if (!lastSeen) return true
  return Date.now() - new Date(lastSeen).getTime() > DAEMON_SILENT_MINUTES * 60_000
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

function secondsAgo(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  if (n < 60) return `${n}s ago`
  if (n < 3600) return `${Math.floor(n / 60)}m ago`
  return `${Math.floor(n / 3600)}h ago`
}

function isTaskUnhealthy(task: ClientTaskHealth): boolean {
  return (
    (task.restarts ?? 0) > TASK_RESTART_THRESHOLD ||
    Boolean(task.last_error) ||
    Boolean(task.finished)
  )
}

interface ClientStatusCardProps {
  clients: ClientInfo[]
}

export function ClientStatusCard({ clients }: ClientStatusCardProps) {
  const active = clients.filter((c) => c.is_active)
  const [expanded, setExpanded] = useState<Set<number>>(new Set())

  function toggle(id: number) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

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
          // Only daemons report a version — a phone bearer (Health Auto
          // Export) has task_health/client_version null and never shows
          // the daemon-only silent/unhealthy flags or the expand toggle.
          const isDaemon = Boolean(c.client_version)
          const tasks = Object.entries(c.task_health ?? {})
          const silent = isDaemon && isSilent(c.last_seen_at)
          const unhealthyTasks = tasks.filter(([, t]) => isTaskUnhealthy(t))
          const hasIssue = silent || unhealthyTasks.length > 0
          const isExpanded = expanded.has(c.id)

          return (
            <div key={c.id} className="rounded-md">
              <div
                className={cn(
                  'flex items-center justify-between text-sm',
                  isDaemon && tasks.length > 0 && 'cursor-pointer'
                )}
                onClick={() => isDaemon && tasks.length > 0 && toggle(c.id)}
              >
                <div className="flex items-center gap-2">
                  <StatusDot
                    variant={hasIssue ? 'error' : online ? 'success' : 'default'}
                    size="sm"
                  />
                  <span className="font-medium">{c.user}</span>
                  <span className="text-xs text-muted-foreground">{c.label}</span>
                  {silent && (
                    <Badge variant="outline" className="text-xs text-destructive border-destructive/40">
                      silent
                    </Badge>
                  )}
                  {unhealthyTasks.length > 0 && (
                    <Badge variant="outline" className="text-xs text-destructive border-destructive/40">
                      {unhealthyTasks.length} task{unhealthyTasks.length > 1 ? 's' : ''} unhealthy
                    </Badge>
                  )}
                </div>
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  {c.client_version && (
                    <span className="font-mono">{c.client_version}</span>
                  )}
                  <span>{relativeTime(c.last_seen_at)}</span>
                  {isDaemon && tasks.length > 0 && (
                    <ChevronDown
                      className={cn(
                        'h-3.5 w-3.5 transition-transform duration-200',
                        isExpanded && 'rotate-180'
                      )}
                    />
                  )}
                </div>
              </div>
              {isDaemon && isExpanded && tasks.length > 0 && (
                <div className="mt-1.5 ml-4 space-y-1 border-l border-border pl-3">
                  {tasks.map(([name, task]) => {
                    const bad = isTaskUnhealthy(task)
                    return (
                      <div
                        key={name}
                        className={cn(
                          'flex items-center justify-between text-xs',
                          bad ? 'text-destructive' : 'text-muted-foreground'
                        )}
                      >
                        <span className="font-mono">{name}</span>
                        <span className="flex items-center gap-2">
                          <span title="last alive">{secondsAgo(task.alive_seconds_ago)}</span>
                          <span title="restart count">
                            {task.restarts ?? 0} restart{(task.restarts ?? 0) === 1 ? '' : 's'}
                          </span>
                          {task.finished && <span>finished</span>}
                          {task.last_error && (
                            <span className="max-w-[16rem] truncate" title={task.last_error}>
                              {task.last_error}
                            </span>
                          )}
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}
