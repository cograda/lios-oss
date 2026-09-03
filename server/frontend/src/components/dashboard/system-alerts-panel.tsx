import { AlertTriangle, ShieldCheck, Wrench } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import { useSystemAlerts, useToolStats } from '@/hooks/use-api'

function formatMs(ms: number | null): string {
  if (ms === null) return '--'
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`
  return `${ms}ms`
}

function formatPct(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`
}

// ─── Active alerts ───

function ActiveAlertsList() {
  const { data, isLoading } = useSystemAlerts()

  if (isLoading && !data) {
    return <p className="text-sm text-muted-foreground">Loading...</p>
  }
  if (!data) {
    return <p className="text-sm text-muted-foreground">Unavailable</p>
  }

  const entries: Array<{ severity: 'destructive' | 'warning'; source: string; message: string; detail?: string }> = []

  for (const a of data.alerts) {
    for (const issue of a.issues) {
      entries.push({
        severity: issue.includes('failing') || issue.includes('never synced') ? 'destructive' : 'warning',
        source: a.integration,
        message: issue,
        detail: a.last_error,
      })
    }
  }
  for (const t of data.tool_alerts) {
    entries.push({ severity: 'destructive', source: t.tool, message: t.issue })
  }
  for (const r of data.reauth_needed) {
    entries.push({
      severity: 'destructive',
      source: `${r.provider} (${r.account_email})`,
      message: 'needs re-authentication',
      detail: r.reason ?? undefined,
    })
  }

  if (entries.length === 0) {
    return (
      <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
        <ShieldCheck className="h-4 w-4 text-success" />
        All systems nominal
      </div>
    )
  }

  return (
    <ul className="space-y-2">
      {entries.map((e, i) => (
        <li key={i} className="flex items-start gap-2 text-sm">
          <AlertTriangle
            className={`h-4 w-4 shrink-0 mt-0.5 ${e.severity === 'destructive' ? 'text-destructive' : 'text-warning'}`}
          />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="font-mono text-xs text-muted-foreground">{e.source}</span>
              <Badge variant={e.severity} className="text-[10px]">{e.severity === 'destructive' ? 'error' : 'warning'}</Badge>
            </div>
            <p className="truncate">{e.message}</p>
            {e.detail && <p className="text-xs text-muted-foreground truncate">{e.detail}</p>}
          </div>
        </li>
      ))}
    </ul>
  )
}

// ─── Per-tool call stats table ───

function ToolStatsTable() {
  const { data, isLoading } = useToolStats(24)

  if (isLoading && !data) {
    return <p className="text-sm text-muted-foreground">Loading...</p>
  }
  if (!data || data.tools.length === 0) {
    return <p className="text-sm text-muted-foreground py-4 text-center">No tool calls recorded yet</p>
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Tool</TableHead>
          <TableHead className="text-right">Calls</TableHead>
          <TableHead className="text-right">Errors</TableHead>
          <TableHead className="text-right">Error rate</TableHead>
          <TableHead className="text-right">p50</TableHead>
          <TableHead className="text-right">p95</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.tools.map((t) => (
          <TableRow key={t.name}>
            <TableCell className="font-mono text-xs">{t.name}</TableCell>
            <TableCell className="text-right font-mono text-xs">{t.calls}</TableCell>
            <TableCell className="text-right font-mono text-xs">{t.errors}</TableCell>
            <TableCell className="text-right font-mono text-xs">
              {t.errors > 0 ? (
                <span className="text-destructive">{formatPct(t.error_rate)}</span>
              ) : (
                formatPct(t.error_rate)
              )}
            </TableCell>
            <TableCell className="text-right font-mono text-xs">{formatMs(t.p50_duration_ms)}</TableCell>
            <TableCell className="text-right font-mono text-xs">
              {t.p95_duration_ms !== null && t.p95_duration_ms > 5000 ? (
                <span className="text-warning">{formatMs(t.p95_duration_ms)}</span>
              ) : (
                formatMs(t.p95_duration_ms)
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

// ─── Panel ───

export function SystemAlertsPanel() {
  const { data } = useSystemAlerts()
  const alertCount =
    (data?.alerts.reduce((n, a) => n + a.issues.length, 0) ?? 0) +
    (data?.tool_alerts.length ?? 0) +
    (data?.reauth_needed.length ?? 0)

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <AlertTriangle className="h-4 w-4" />
          Alerts
          {data && (
            <Badge variant={alertCount > 0 ? 'destructive' : 'success'} className="ml-auto text-xs">
              {alertCount > 0 ? `${alertCount} active` : 'all ok'}
            </Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <ActiveAlertsList />

        <div className="pt-2 border-t border-border">
          <p className="flex items-center gap-2 text-xs uppercase tracking-wide text-muted-foreground mb-2">
            <Wrench className="h-3 w-3" />
            Tool calls (last 24h)
          </p>
          <ToolStatsTable />
        </div>
      </CardContent>
    </Card>
  )
}
