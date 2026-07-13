import { useState } from 'react'
import { useParams, Link } from 'react-router'
import { ArrowLeft, RefreshCw, Clock, Timer, AlertCircle, ChevronDown, ChevronUp } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { StatusDot } from '@/components/ui/status-dot'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import { useIntegrationDetail, useTriggerSync } from '@/hooks/use-api'
import type { SyncHistoryEntry } from '@/lib/api'

function statusVariant(status: string): 'success' | 'error' | 'warning' | 'default' {
  if (status === 'ok') return 'success'
  if (status === 'error' || status === 'timeout') return 'error'
  if (status === 'never') return 'default'
  return 'warning'
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleString('en-IE', {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function formatDuration(ms: number | null): string {
  if (ms == null) return '--'
  if (ms < 1000) return `${ms}ms`
  const secs = ms / 1000
  if (secs < 60) return `${secs.toFixed(1)}s`
  const mins = Math.floor(secs / 60)
  return `${mins}m ${Math.floor(secs % 60)}s`
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

function nextSyncIn(iso: string | null): string | null {
  if (!iso) return null
  const diff = new Date(iso).getTime() - Date.now()
  if (diff < 0) return 'now'
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return '<1m'
  if (mins < 60) return `${mins}m`
  return `${Math.floor(mins / 60)}h ${mins % 60}m`
}

// Compute stats from history
function computeStats(history: SyncHistoryEntry[]) {
  if (history.length === 0) return null
  const durations = history.filter((h) => h.status === 'ok' && h.duration_ms != null).map((h) => h.duration_ms!)
  const errors = history.filter((h) => h.status !== 'ok')
  const last24h = history.filter((h) => Date.now() - new Date(h.started_at).getTime() < 86400000)
  const successRate = last24h.length > 0
    ? Math.round((last24h.filter((h) => h.status === 'ok').length / last24h.length) * 100)
    : null

  return {
    avgDuration: durations.length > 0 ? Math.round(durations.reduce((a, b) => a + b, 0) / durations.length) : null,
    minDuration: durations.length > 0 ? Math.min(...durations) : null,
    maxDuration: durations.length > 0 ? Math.max(...durations) : null,
    totalSyncs: history.length,
    totalErrors: errors.length,
    successRate24h: successRate,
    syncs24h: last24h.length,
  }
}

export default function IntegrationDetail() {
  const { name } = useParams<{ name: string }>()
  const { data, error, isLoading } = useIntegrationDetail(name!)
  const syncMutation = useTriggerSync()
  const [syncing, setSyncing] = useState(false)
  const [expandedError, setExpandedError] = useState<number | null>(null)

  async function handleSync() {
    setSyncing(true)
    try {
      await syncMutation.mutateAsync(name!)
    } finally {
      setSyncing(false)
    }
  }

  if (isLoading) {
    return (
      <div className="space-y-6">
        <Link to="/integrations" className="text-sm text-muted-foreground hover:text-foreground flex items-center gap-1">
          <ArrowLeft className="h-3.5 w-3.5" /> Integrations
        </Link>
        <p className="text-muted-foreground text-center py-8">Loading...</p>
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="space-y-6">
        <Link to="/integrations" className="text-sm text-muted-foreground hover:text-foreground flex items-center gap-1">
          <ArrowLeft className="h-3.5 w-3.5" /> Integrations
        </Link>
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
          {error?.message || 'Integration not found'}
        </div>
      </div>
    )
  }

  const stats = computeStats(data.history)
  const nextSync = nextSyncIn(data.next_sync_at)

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <Link to="/integrations" className="text-sm text-muted-foreground hover:text-foreground flex items-center gap-1 mb-2">
            <ArrowLeft className="h-3.5 w-3.5" /> Integrations
          </Link>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold">{data.display_name}</h1>
            {data.configured ? (
              <Badge variant="success">Connected</Badge>
            ) : (
              <Badge variant="default">Not configured</Badge>
            )}
            {data.consecutive_failures >= 3 && (
              <Badge variant="destructive">Failing ({data.consecutive_failures}x)</Badge>
            )}
          </div>
        </div>
        {data.configured && (
          <Button onClick={handleSync} disabled={syncing}>
            <RefreshCw className={`h-4 w-4 mr-1.5 ${syncing ? 'animate-spin' : ''}`} />
            Sync Now
          </Button>
        )}
      </div>

      {/* Status row */}
      <div className="flex items-center gap-6 text-sm">
        <StatusDot variant={statusVariant(data.last_sync_status)} label={data.last_sync_status} />
        {data.last_sync_at && (
          <span className="flex items-center gap-1 text-muted-foreground">
            <Clock className="h-3 w-3" />
            Last sync {timeAgo(data.last_sync_at)}
          </span>
        )}
        {data.last_sync_duration_ms != null && (
          <span className="flex items-center gap-1 text-muted-foreground">
            <Timer className="h-3 w-3" />
            {formatDuration(data.last_sync_duration_ms)}
          </span>
        )}
        {data.schedule && (
          <span className="text-xs text-muted-foreground font-mono">{data.schedule}</span>
        )}
        {nextSync && (
          <span className="text-xs text-muted-foreground">next: {nextSync}</span>
        )}
      </div>

      {/* Error banner */}
      {data.last_error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive flex items-start gap-2">
          <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
          <span>{data.last_error}</span>
        </div>
      )}

      {/* Stats cards */}
      {stats && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Card>
            <CardContent className="p-4 text-center">
              <p className="text-2xl font-semibold font-mono">{stats.totalSyncs}</p>
              <p className="text-xs text-muted-foreground">Total syncs</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4 text-center">
              <p className="text-2xl font-semibold font-mono">
                {stats.successRate24h != null ? `${stats.successRate24h}%` : '--'}
              </p>
              <p className="text-xs text-muted-foreground">Success rate (24h)</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4 text-center">
              <p className="text-2xl font-semibold font-mono">{formatDuration(stats.avgDuration)}</p>
              <p className="text-xs text-muted-foreground">Avg duration</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4 text-center">
              <p className="text-2xl font-semibold font-mono">{stats.totalErrors}</p>
              <p className="text-xs text-muted-foreground">Total errors</p>
            </CardContent>
          </Card>
        </div>
      )}

      {/* Sync History */}
      <Card>
        <CardHeader>
          <CardTitle>Sync History</CardTitle>
        </CardHeader>
        <CardContent>
          {data.history.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-4">
              No sync history yet. History will appear after the next sync.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Time</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Duration</TableHead>
                  <TableHead>Trigger</TableHead>
                  <TableHead>Error</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.history.map((entry, i) => (
                  <TableRow key={i}>
                    <TableCell className="font-mono text-xs">{formatTime(entry.started_at)}</TableCell>
                    <TableCell>
                      <StatusDot variant={statusVariant(entry.status)} label={entry.status} size="sm" />
                    </TableCell>
                    <TableCell className="font-mono text-xs">{formatDuration(entry.duration_ms)}</TableCell>
                    <TableCell>
                      <Badge variant={entry.trigger === 'manual' ? 'outline' : 'default'} className="text-xs">
                        {entry.trigger}
                      </Badge>
                    </TableCell>
                    <TableCell className="max-w-xs">
                      {entry.error ? (
                        <button
                          onClick={() => setExpandedError(expandedError === i ? null : i)}
                          className="flex items-center gap-1 text-xs text-destructive hover:text-destructive/80 text-left"
                        >
                          <span className={expandedError === i ? '' : 'truncate max-w-[200px] inline-block'}>
                            {entry.error}
                          </span>
                          {expandedError === i ? (
                            <ChevronUp className="h-3 w-3 shrink-0" />
                          ) : (
                            <ChevronDown className="h-3 w-3 shrink-0" />
                          )}
                        </button>
                      ) : (
                        <span className="text-xs text-muted-foreground">--</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
