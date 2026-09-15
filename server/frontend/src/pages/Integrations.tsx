import { useState } from 'react'
import { Link } from 'react-router'
import { RefreshCw, Clock, AlertCircle, Timer, ChevronDown, ChevronUp } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { StatusDot } from '@/components/ui/status-dot'
import { useIntegrations, useTriggerSync } from '@/hooks/use-api'
import { useAuth } from '@/lib/auth'
import type { IntegrationStatus } from '@/lib/api'

function statusVariant(status: string): 'success' | 'error' | 'warning' | 'default' {
  if (status === 'ok') return 'success'
  if (status === 'error') return 'error'
  if (status === 'never') return 'default'
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

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const secs = ms / 1000
  if (secs < 60) return `${secs.toFixed(1)}s`
  const mins = Math.floor(secs / 60)
  const remainSecs = Math.floor(secs % 60)
  return `${mins}m ${remainSecs}s`
}

function nextSyncIn(iso: string | null): string | null {
  if (!iso) return null
  const diff = new Date(iso).getTime() - Date.now()
  if (diff < 0) return 'now'
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return '<1m'
  if (mins < 60) return `${mins}m`
  return `${Math.floor(mins / 60)}h ${mins % 60}m`
}

export default function Integrations() {
  const { isAdmin } = useAuth()
  const { data, error, isLoading } = useIntegrations()
  const syncMutation = useTriggerSync()
  const [syncing, setSyncing] = useState<Set<string>>(new Set())
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const integrations = data?.integrations ?? []

  async function handleSync(name: string) {
    setSyncing((s) => new Set(s).add(name))
    try {
      await syncMutation.mutateAsync(name)
    } finally {
      setSyncing((s) => { const n = new Set(s); n.delete(name); return n })
    }
  }

  function toggleExpanded(name: string) {
    setExpanded((s) => {
      const n = new Set(s)
      if (n.has(name)) n.delete(name)
      else n.add(name)
      return n
    })
  }

  // Group by manifest `type` (V4 chunk 5.1) — untyped/legacy integrations
  // (no manifest yet) fall into "other" rather than disappearing.
  const groups = new Map<string, IntegrationStatus[]>()
  for (const int of integrations) {
    const key = int.type ?? 'other'
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(int)
  }
  const sortedGroups = [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]))

  function renderIntegrationCard(int: IntegrationStatus) {
    const nextSync = nextSyncIn(int.next_sync_at)
    return (
      <Card key={int.name} className={!int.enabled ? 'opacity-50' : ''}>
        <CardHeader className="flex-row items-center justify-between">
          <div className="flex items-center gap-3">
            <CardTitle className="text-sm normal-case tracking-normal">
              <Link to={`/integrations/${int.name}`} className="hover:underline">{int.display_name}</Link>
            </CardTitle>
            {!int.enabled && <Badge variant="default">Disabled</Badge>}
            {int.configured ? (
              <Badge variant="success">Connected</Badge>
            ) : (
              <Badge variant="default">Not configured</Badge>
            )}
            {int.consecutive_failures >= 3 && (
              <Badge variant="destructive">Failing ({int.consecutive_failures}x)</Badge>
            )}
            {int.type && <Badge variant="default">{int.type}</Badge>}
            {int.version && (
              <Badge variant="default" className="font-mono">v{int.version}</Badge>
            )}
          </div>
          {isAdmin && int.configured && int.enabled && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => handleSync(int.name)}
              disabled={syncing.has(int.name)}
            >
              <RefreshCw className={`h-4 w-4 mr-1.5 ${syncing.has(int.name) ? 'animate-spin' : ''}`} />
              Sync
            </Button>
          )}
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-4 text-sm">
            <StatusDot variant={statusVariant(int.last_sync_status)} label={int.last_sync_status} />
            {int.last_sync_at && (
              <span className="flex items-center gap-1 text-muted-foreground">
                <Clock className="h-3 w-3" />
                {timeAgo(int.last_sync_at)}
              </span>
            )}
            {int.last_sync_duration_ms != null && (
              <span className="flex items-center gap-1 text-muted-foreground">
                <Timer className="h-3 w-3" />
                {formatDuration(int.last_sync_duration_ms)}
              </span>
            )}
            {int.schedule && (
              <span className="text-xs text-muted-foreground font-mono">{int.schedule}</span>
            )}
            {nextSync && (
              <span className="text-xs text-muted-foreground ml-auto">
                next: {nextSync}
              </span>
            )}
          </div>
          {int.last_error && (
            <button
              onClick={() => toggleExpanded(int.name)}
              className="mt-2 flex items-start gap-2 text-xs text-destructive hover:text-destructive/80 w-full text-left"
            >
              <AlertCircle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
              <span className={expanded.has(int.name) ? '' : 'truncate'}>{int.last_error}</span>
              {expanded.has(int.name) ? (
                <ChevronUp className="h-3.5 w-3.5 mt-0.5 shrink-0 ml-auto" />
              ) : (
                <ChevronDown className="h-3.5 w-3.5 mt-0.5 shrink-0 ml-auto" />
              )}
            </button>
          )}
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Integrations</h1>
        <p className="text-sm text-muted-foreground mt-1">Connected services and sync status</p>
      </div>

      {error && (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
          {error.message}
        </div>
      )}

      <div className="space-y-6">
        {sortedGroups.map(([type, group]) => (
          <div key={type} className="space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">{type}</h2>
            <div className="space-y-3">
              {group.map(renderIntegrationCard)}
            </div>
          </div>
        ))}

        {isLoading && integrations.length === 0 && (
          <p className="text-muted-foreground text-center py-8">Loading...</p>
        )}
      </div>
    </div>
  )
}
