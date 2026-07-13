import { CheckCircle, AlertTriangle, Clock, Server } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import type { IntegrationStatus, SystemInfo } from '@/lib/api'

function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const mins = Math.floor((seconds % 3600) / 60)
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${mins}m`
  return `${mins}m`
}

interface SystemStatusBarProps {
  integrations: IntegrationStatus[]
  systemInfo?: SystemInfo
}

export function SystemStatusBar({ integrations, systemInfo }: SystemStatusBarProps) {
  const failing = integrations.filter((i) => i.consecutive_failures >= 3)
  const configured = integrations.filter((i) => i.configured)
  const healthy = configured.filter((i) => i.last_sync_status === 'ok')
  const allGreen = failing.length === 0 && configured.length > 0

  return (
    <div className="flex items-center gap-4 rounded-lg border border-border bg-card px-4 py-3">
      <div className="flex items-center gap-2">
        {allGreen ? (
          <CheckCircle className="h-4 w-4 text-success" />
        ) : (
          <AlertTriangle className="h-4 w-4 text-warning" />
        )}
        <span className="text-sm font-medium">
          {allGreen ? 'All systems operational' : `${failing.length} integration${failing.length > 1 ? 's' : ''} degraded`}
        </span>
      </div>

      <div className="ml-auto flex items-center gap-4 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <Badge variant={allGreen ? 'success' : 'warning'} className="text-xs">
            {healthy.length}/{configured.length}
          </Badge>
          healthy
        </span>

        {systemInfo && (
          <>
            <span className="flex items-center gap-1">
              <Clock className="h-3 w-3" />
              {formatUptime(systemInfo.uptime_seconds)} uptime
            </span>
            <span className="flex items-center gap-1">
              <Server className="h-3 w-3" />
              {'size_mb' in systemInfo.database
                ? `${systemInfo.database.size_mb} MB`
                : 'DB unavailable'}
            </span>
          </>
        )}
      </div>
    </div>
  )
}
