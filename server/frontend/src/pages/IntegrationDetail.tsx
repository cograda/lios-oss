import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router'
import * as LucideIcons from 'lucide-react'
import {
  ArrowLeft, RefreshCw, Clock, Timer, AlertCircle, ChevronDown, ChevronUp,
  Blocks, ExternalLink, CheckCircle, XCircle, Save,
} from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { StatusDot } from '@/components/ui/status-dot'
import { Toggle } from '@/components/ui/toggle'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import {
  useIntegrationDetail, useTriggerSync, useIntegrationConfig, usePutIntegrationConfig,
  useIntegrationTools, useSetIntegrationEnabled, useTokens,
} from '@/hooks/use-api'
import type { SyncHistoryEntry, ConfigField, IntegrationDetail as IntegrationDetailData } from '@/lib/api'

// Icon names come from the manifest (`icon` field, chunk 1.3) — same
// dynamic lookup as components/dashboard/integration-grid.tsx.
function resolveIcon(name: string | null | undefined): React.ElementType {
  if (!name) return Blocks
  const icon = (LucideIcons as unknown as Record<string, React.ElementType>)[name]
  return icon ?? Blocks
}

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

// ─── Flows panel ───

function ChipList({ label, values }: { label: string; values: string[] }) {
  if (values.length === 0) return null
  return (
    <div>
      <p className="text-xs uppercase tracking-wide text-muted-foreground mb-1.5">{label}</p>
      <div className="flex flex-wrap gap-1.5">
        {values.map((v) => (
          <Badge key={v} variant="outline" className="font-mono normal-case">{v}</Badge>
        ))}
      </div>
    </div>
  )
}

function FlowsPanel({ data }: { data: IntegrationDetailData }) {
  const hasAny = [data.reads_from, data.writes_to, data.embedding_sources, data.depends_on, data.provides]
    .some((l) => l.length > 0)
  if (!hasAny) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle>Flows</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <ChipList label="Reads from" values={data.reads_from} />
        <ChipList label="Writes to" values={data.writes_to} />
        <ChipList label="Embedding sources" values={data.embedding_sources} />
        <ChipList label="Depends on (capabilities)" values={data.depends_on} />
        <ChipList label="Provides (capabilities)" values={data.provides} />
      </CardContent>
    </Card>
  )
}

// ─── Config panel ───

function ConfigPanel({ name }: { name: string }) {
  const { data, isLoading } = useIntegrationConfig(name)
  const putMutation = usePutIntegrationConfig(name)
  const [edits, setEdits] = useState<Record<string, string>>({})

  const fields = data?.config ?? {}
  const keys = Object.keys(fields)

  if (!isLoading && keys.length === 0) return null

  function fieldValue(key: string, field: ConfigField): string {
    if (key in edits) return edits[key]
    if (field.secret) return '' // write-only — never pre-fill a masked value into an editable input
    if (field.type === 'list_str' || field.type === 'dict_str_str') {
      return typeof field.value === 'string' ? field.value : JSON.stringify(field.value ?? '')
    }
    return field.value == null ? '' : String(field.value)
  }

  function coerce(raw: string, type: ConfigField['type']): unknown {
    if (type === 'bool') return raw === 'true'
    if (type === 'int') return Number(raw)
    if (type === 'list_str' || type === 'dict_str_str') {
      try { return JSON.parse(raw) } catch { return type === 'list_str' ? [] : {} }
    }
    return raw
  }

  function handleSave() {
    if (Object.keys(edits).length === 0) return
    const body: Record<string, unknown> = {}
    for (const [key, raw] of Object.entries(edits)) {
      const field = fields[key]
      if (!field) continue
      if (field.secret && raw === '') continue // don't overwrite a set secret with a blank edit
      body[key] = coerce(raw, field.type)
    }
    putMutation.mutate(body, { onSuccess: () => setEdits({}) })
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>Config</CardTitle>
        <Button size="sm" onClick={handleSave} disabled={Object.keys(edits).length === 0 || putMutation.isPending}>
          <Save className="h-3.5 w-3.5 mr-1.5" />
          {putMutation.isPending ? 'Saving...' : 'Save'}
        </Button>
      </CardHeader>
      <CardContent className="space-y-4">
        {isLoading && <p className="text-sm text-muted-foreground">Loading...</p>}
        {keys.map((key) => {
          const field = fields[key]
          return (
            <div key={key} className="space-y-1.5">
              <div className="flex items-center gap-2">
                <Label htmlFor={`cfg-${key}`} className="font-mono text-xs">{key}</Label>
                {field.required && <Badge variant="outline" className="text-[10px]">required</Badge>}
                {field.secret && <Badge variant="default" className="text-[10px]">secret</Badge>}
                {field.configured === false && <Badge variant="destructive" className="text-[10px]">not set</Badge>}
              </div>
              {field.description && (
                <p className="text-xs text-muted-foreground">{field.description}</p>
              )}
              {field.type === 'bool' ? (
                <Toggle
                  checked={fieldValue(key, field) === 'true' || field.value === true}
                  onChange={(checked) => setEdits((e) => ({ ...e, [key]: checked ? 'true' : 'false' }))}
                />
              ) : (
                <Input
                  id={`cfg-${key}`}
                  type={field.secret ? 'password' : 'text'}
                  placeholder={field.secret ? String(field.value ?? '(not set)') : undefined}
                  value={fieldValue(key, field)}
                  onChange={(e) => setEdits((prev) => ({ ...prev, [key]: e.target.value }))}
                />
              )}
            </div>
          )
        })}
        {putMutation.isSuccess && (
          <p className="text-xs text-success">Saved.</p>
        )}
      </CardContent>
    </Card>
  )
}

// ─── Credentials panel ───

function CredentialsPanel({ data }: { data: IntegrationDetailData }) {
  const { data: tokenData } = useTokens()
  if (!data.oauth) return null

  const tokens = (tokenData?.tokens ?? []).filter((t) => t.provider === data.oauth!.provider)

  return (
    <Card>
      <CardHeader>
        <CardTitle>Credentials</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">
          Requires {data.oauth.provider} OAuth — scopes: {data.oauth.scopes.join(', ')}
        </p>
        {tokens.length === 0 ? (
          <p className="text-sm text-muted-foreground">No connected accounts yet.</p>
        ) : (
          <div className="space-y-2">
            {tokens.map((t) => (
              <div key={`${t.provider}-${t.account}`} className="flex items-center justify-between text-sm py-1.5 border-b border-border last:border-0">
                <div className="flex items-center gap-2">
                  {t.expired ? (
                    <XCircle className="h-4 w-4 text-destructive" />
                  ) : (
                    <CheckCircle className="h-4 w-4 text-success" />
                  )}
                  <span className="font-mono text-xs">{t.account}</span>
                  {t.expired && <Badge variant="destructive" className="text-[10px]">reauth needed</Badge>}
                </div>
                <a href={`/api/auth/google/login?account=${encodeURIComponent(t.account)}&user=${encodeURIComponent(t.user)}`}>
                  <Button variant="ghost" size="sm">
                    <ExternalLink className="h-3.5 w-3.5 mr-1.5" />
                    {t.expired ? 'Reconnect' : 'Reconnect'}
                  </Button>
                </a>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

// ─── Tools panel ───

function ToolsPanel({ name }: { name: string }) {
  const { data, isLoading } = useIntegrationTools(name)
  const tools = data?.tools ?? []

  if (!isLoading && tools.length === 0) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle>Tools {data && `(${data.window_hours}h window)`}</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Annotations</TableHead>
                <TableHead>Calls</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {tools.map((tool) => (
                <TableRow key={tool.name}>
                  <TableCell className="font-mono text-xs">{tool.name}</TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {tool.annotations.readOnlyHint && <Badge variant="outline" className="text-[10px]">read-only</Badge>}
                      {tool.annotations.destructiveHint && <Badge variant="destructive" className="text-[10px]">destructive</Badge>}
                      {tool.annotations.idempotentHint && <Badge variant="outline" className="text-[10px]">idempotent</Badge>}
                      {tool.annotations.openWorldHint && <Badge variant="outline" className="text-[10px]">open-world</Badge>}
                    </div>
                  </TableCell>
                  <TableCell className="font-mono text-xs">{tool.calls}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}

// ─── Enable/disable switch ───

function EnabledSwitch({ data }: { data: IntegrationDetailData }) {
  const setEnabled = useSetIntegrationEnabled(data.name)
  const [optimistic, setOptimistic] = useState(data.enabled)

  useEffect(() => setOptimistic(data.enabled), [data.enabled])

  return (
    <Toggle
      checked={optimistic}
      disabled={setEnabled.isPending}
      label={optimistic ? 'Enabled' : 'Disabled'}
      onChange={(checked) => {
        setOptimistic(checked)
        setEnabled.mutate(checked, { onError: () => setOptimistic(!checked) })
      }}
    />
  )
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
  const Icon = resolveIcon(data.icon)

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <Link to="/integrations" className="text-sm text-muted-foreground hover:text-foreground flex items-center gap-1 mb-2">
            <ArrowLeft className="h-3.5 w-3.5" /> Integrations
          </Link>
          <div className="flex items-center gap-3 flex-wrap">
            <Icon className="h-6 w-6 text-muted-foreground" />
            <h1 className="text-2xl font-semibold">{data.display_name}</h1>
            {data.type && <Badge variant="default">{data.type}</Badge>}
            {data.version && <Badge variant="default" className="font-mono">v{data.version}</Badge>}
            {!data.enabled && <Badge variant="default">Disabled</Badge>}
            {data.configured ? (
              <Badge variant="success">Connected</Badge>
            ) : (
              <Badge variant="default">Not configured</Badge>
            )}
            {data.consecutive_failures >= 3 && (
              <Badge variant="destructive">Failing ({data.consecutive_failures}x)</Badge>
            )}
          </div>
          {data.description && (
            <p className="text-sm text-muted-foreground mt-1">{data.description}</p>
          )}
        </div>
        <div className="flex items-center gap-3">
          <EnabledSwitch data={data} />
          {data.configured && data.enabled && (
            <Button onClick={handleSync} disabled={syncing}>
              <RefreshCw className={`h-4 w-4 mr-1.5 ${syncing ? 'animate-spin' : ''}`} />
              Sync Now
            </Button>
          )}
        </div>
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

      {/* Flows + Config + Credentials — the manifest-driven hub (V4 chunk 5.1) */}
      <div className="grid gap-4 lg:grid-cols-2">
        <FlowsPanel data={data} />
        <CredentialsPanel data={data} />
      </div>
      <ConfigPanel name={data.name} />
      <ToolsPanel name={data.name} />

      {/* Runtime: schedule, freshness threshold, background tasks */}
      {(data.freshness_threshold_minutes != null || data.background_tasks.length > 0) && (
        <Card>
          <CardHeader>
            <CardTitle>Runtime</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {data.freshness_threshold_minutes != null && (
              <p className="text-sm text-muted-foreground">
                Freshness threshold: <span className="font-mono text-foreground">{data.freshness_threshold_minutes}min</span>
              </p>
            )}
            {data.background_tasks.length > 0 && (
              <div className="space-y-1.5">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">Background tasks</p>
                {data.background_tasks.map((t) => (
                  <div key={t.name} className="flex items-center justify-between text-sm py-1 border-b border-border last:border-0">
                    <span className="font-mono text-xs">{t.name}</span>
                    <div className="flex items-center gap-2">
                      <Badge variant="outline" className="text-[10px]">{t.kind}</Badge>
                      {t.cron && <span className="text-xs text-muted-foreground font-mono">{t.cron}</span>}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
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
