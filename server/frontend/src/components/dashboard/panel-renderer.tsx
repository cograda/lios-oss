import type { LucideIcon } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { StatCard } from '@/components/ui/stat-card'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import type { DashboardPanel, IntegrationDashboardData } from '@/lib/api'

/**
 * Generic renderer for the manifest-driven dashboard envelope (V4 chunk
 * 5.1) — `{panels: [{kind, title, data}]}`. Any integration's
 * `dashboard_data()` that emits this shape renders here, replacing the
 * bespoke CalendarPanel/RemindersPanel/FinancePanel components that used
 * to be typed against each integration's ad hoc fields.
 */

function isErrorOrUnconfigured(d: IntegrationDashboardData | undefined): boolean {
  if (!d || typeof d !== 'object') return true
  return 'error' in d || 'status' in d
}

function StatPanelCard({ panel }: { panel: Extract<DashboardPanel, { kind: 'stat' }> }) {
  return (
    <StatCard
      label={panel.title}
      value={panel.data.value}
      trend={panel.data.trend}
    />
  )
}

function ListPanelCard({ panel }: { panel: Extract<DashboardPanel, { kind: 'list' }> }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{panel.title}</CardTitle>
      </CardHeader>
      <CardContent>
        {panel.data.items.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-2">Nothing to show</p>
        ) : (
          <div className="space-y-1">
            {panel.data.items.map((item, i) => (
              <div key={`${item.label}-${i}`} className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground truncate">
                  {item.label}
                  {item.sublabel && <span className="ml-1 text-xs">({item.sublabel})</span>}
                </span>
                <span className="font-mono text-xs">{item.value}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function TablePanelCard({ panel }: { panel: Extract<DashboardPanel, { kind: 'table' }> }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{panel.title}</CardTitle>
      </CardHeader>
      <CardContent>
        {panel.data.rows.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-4">Nothing to show</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                {panel.data.columns.map((col) => (
                  <TableHead key={col}>{col}</TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {panel.data.rows.map((row, i) => (
                <TableRow key={i}>
                  {row.map((cell, j) => (
                    <TableCell key={j} className="text-sm">{cell}</TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}

export function DashboardPanelCard({ panel }: { panel: DashboardPanel }) {
  if (panel.kind === 'stat') return <StatPanelCard panel={panel} />
  if (panel.kind === 'list') return <ListPanelCard panel={panel} />
  return <TablePanelCard panel={panel} />
}

/** Renders every panel an integration's dashboard_data() emits, grouped
 * under one header card so stat panels sit in a row above list/table ones. */
export function IntegrationPanels({
  title,
  icon: Icon,
  data,
  fallbackMessage = 'Not configured',
}: {
  title: string
  icon?: LucideIcon
  data: IntegrationDashboardData | undefined
  fallbackMessage?: string
}) {
  if (isErrorOrUnconfigured(data)) {
    const message = data && typeof data === 'object' && typeof data.error === 'string' ? data.error : fallbackMessage
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {Icon && <Icon className="h-4 w-4" />}
            {title}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">{message}</p>
        </CardContent>
      </Card>
    )
  }

  const panels = data!.panels ?? []
  if (panels.length === 0) return null

  const stats = panels.filter((p): p is Extract<DashboardPanel, { kind: 'stat' }> => p.kind === 'stat')
  const rest = panels.filter((p) => p.kind !== 'stat')

  return (
    <div className="space-y-3">
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        {Icon && <Icon className="h-4 w-4" />}
        {title}
      </h3>
      {stats.length > 0 && (
        <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${stats.length}, minmax(0, 1fr))` }}>
          {stats.map((p, i) => <DashboardPanelCard key={i} panel={p} />)}
        </div>
      )}
      {rest.map((p, i) => <DashboardPanelCard key={i} panel={p} />)}
    </div>
  )
}
