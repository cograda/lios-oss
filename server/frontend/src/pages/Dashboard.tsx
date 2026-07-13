import { Calendar, MapPin, CheckSquare, Wallet } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { StatCard } from '@/components/ui/stat-card'
import { Badge } from '@/components/ui/badge'
import { useDashboard, useIntegrations, useClients, useSystemInfo } from '@/hooks/use-api'
import { SystemStatusBar } from '@/components/dashboard/system-status-bar'
import { ReauthBanner } from '@/components/dashboard/reauth-banner'
import { IntegrationGrid } from '@/components/dashboard/integration-grid'
import { ClientStatusCard } from '@/components/dashboard/client-status-card'
import { EmbeddingQueueCard } from '@/components/dashboard/embedding-queue-card'
import type {
  CalendarEvent,
  CalendarDashboard,
  RemindersDashboard,
  FinanceDashboard,
  EmbeddingQueue,
} from '@/lib/api'

// ─── Helpers ───

function formatTime(iso: string, allDay: boolean): string {
  if (allDay) return 'All day'
  return new Date(iso).toLocaleTimeString('en-IE', { hour: '2-digit', minute: '2-digit' })
}

function formatDayLabel(dateStr: string): string {
  const d = new Date(dateStr + 'T00:00:00')
  const today = new Date()
  const tomorrow = new Date()
  tomorrow.setDate(today.getDate() + 1)
  if (d.toDateString() === today.toDateString()) return 'Today'
  if (d.toDateString() === tomorrow.toDateString()) return 'Tomorrow'
  return d.toLocaleDateString('en-IE', { weekday: 'long', month: 'short', day: 'numeric' })
}

function formatCurrency(n: number): string {
  return new Intl.NumberFormat('en-IE', { style: 'currency', currency: 'EUR' }).format(n)
}

function isOk<T extends object>(d: T | { error: string } | { status: string } | undefined): d is T {
  if (!d || typeof d !== 'object') return false
  return !('error' in d) && !('status' in d)
}

// ─── Event Row ───

function EventRow({ event }: { event: CalendarEvent }) {
  return (
    <div className="flex items-start gap-3 py-2">
      <div className="mt-0.5 text-xs font-mono text-muted-foreground w-14 shrink-0">
        {formatTime(event.start, event.all_day)}
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium truncate">
          {event.summary || <span className="text-muted-foreground italic">Busy</span>}
        </p>
        <div className="flex items-center gap-3 mt-0.5">
          <span className="text-xs text-muted-foreground truncate">{event.calendar}</span>
          {event.location && (
            <span className="flex items-center gap-1 text-xs text-muted-foreground truncate">
              <MapPin className="h-3 w-3 shrink-0" />
              {event.location}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

// ─── Calendar Panel ───

function CalendarPanel({ data }: { data: CalendarDashboard }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Calendar className="h-4 w-4" />
          Calendar
          <Badge variant="outline" className="ml-auto font-mono text-xs">
            {data.events_this_week} this week
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {data.by_day && Object.entries(data.by_day).map(([day, events]) => (
          <div key={day}>
            <div className="flex items-center gap-2 mb-1">
              <Badge variant="outline">{formatDayLabel(day)}</Badge>
              <span className="text-xs text-muted-foreground">{events.length} events</span>
            </div>
            <div className="divide-y divide-border">
              {events.map((event, i) => (
                <EventRow key={`${event.start}-${i}`} event={event} />
              ))}
            </div>
          </div>
        ))}
        {(!data.by_day || Object.keys(data.by_day).length === 0) && (
          <p className="text-sm text-muted-foreground py-4 text-center">No upcoming events</p>
        )}
      </CardContent>
    </Card>
  )
}

// ─── Reminders Panel ───

function RemindersPanel({ data }: { data: RemindersDashboard }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CheckSquare className="h-4 w-4" />
          Reminders
          {data.overdue > 0 && (
            <Badge variant="destructive" className="ml-auto text-xs">{data.overdue} overdue</Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-2">
          <StatCard label="Open" value={data.total_incomplete} />
          <StatCard
            label="Overdue"
            value={data.overdue}
            trend={data.overdue > 0 ? { value: 'needs attention', positive: false } : undefined}
          />
        </div>
        {Object.keys(data.by_list).length > 0 && (
          <div className="space-y-1 pt-1">
            {Object.entries(data.by_list).map(([name, count]) => (
              <div key={name} className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground truncate">{name}</span>
                <span className="font-mono text-xs">{count}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

// ─── Finance Panel ───

function FinancePanel({ data }: { data: FinanceDashboard }) {
  const prev = data.previous

  function trend(current: number, previous: number | undefined): { value: string; positive: boolean } | undefined {
    if (previous === undefined || previous === 0) return undefined
    const pct = ((current - previous) / Math.abs(previous)) * 100
    const sign = pct >= 0 ? '+' : ''
    return { value: `${sign}${pct.toFixed(0)}% vs last`, positive: pct >= 0 }
  }

  const savingsPositive = data.net_savings >= 0

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Wallet className="h-4 w-4" />
          Finance
          <Badge variant="outline" className="ml-auto font-mono text-xs">
            {data.transaction_count} txns
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-3 gap-2">
          <StatCard
            label="Income"
            value={formatCurrency(data.total_income)}
            trend={trend(data.total_income, prev?.total_income)}
          />
          <StatCard
            label="Expense"
            value={formatCurrency(data.total_expense)}
            trend={trend(data.total_expense, prev?.total_expense)}
          />
          <StatCard
            label="Net"
            value={formatCurrency(data.net_savings)}
            trend={{ value: savingsPositive ? 'saving' : 'overspend', positive: savingsPositive }}
          />
        </div>
        {data.top_categories.length > 0 && (
          <div className="space-y-1 pt-1">
            <p className="text-xs uppercase tracking-wide text-muted-foreground">Top spending</p>
            {data.top_categories.map((cat) => (
              <div key={cat.name} className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground truncate">{cat.name}</span>
                <span className="font-mono text-xs">{formatCurrency(cat.value)}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

// ─── Error Panel ───

function PanelError({ icon: Icon, title, message }: { icon: React.ElementType; title: string; message: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><Icon className="h-4 w-4" /> {title}</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">{message}</p>
      </CardContent>
    </Card>
  )
}

// ─── Main Dashboard ───

export default function Dashboard() {
  const { data, error, isLoading } = useDashboard()
  const { data: intData } = useIntegrations()
  const { data: clientData } = useClients()
  const { data: systemInfo } = useSystemInfo()

  const integrations = intData?.integrations ?? []
  const clients = clientData?.clients ?? []

  const cal = data?.google_calendar
  const rem = data?.apple_reminders
  const fin = data?.finance
  const embeddingQueue = data?.embedding_queue as EmbeddingQueue | { error: string } | undefined

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">Family overview</p>
      </div>

      {error && (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
          {error.message}
        </div>
      )}

      {/* Re-auth banner — rendered above the status bar so a dead OAuth token
          is the very first thing visible. Self-hides when nothing is flagged. */}
      <ReauthBanner items={data?.reauth_needed ?? []} />

      {/* System status bar */}
      <SystemStatusBar integrations={integrations} systemInfo={systemInfo} />

      {isLoading && !data && (
        <p className="text-muted-foreground">Loading...</p>
      )}

      {/* Main layout: content left (2/3) + sidebar right (1/3) */}
      <div className="grid gap-6 xl:grid-cols-3">
        {/* Left column — integration grid + data panels */}
        <div className="xl:col-span-2 space-y-6">
          {/* Integration grid */}
          <div>
            <h2 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground mb-3">Integrations</h2>
            <IntegrationGrid integrations={integrations} />
          </div>

          {/* Calendar + Finance row */}
          {data && (
            <div className="grid gap-4 lg:grid-cols-2">
              {isOk<CalendarDashboard>(cal) ? (
                <CalendarPanel data={cal} />
              ) : cal && 'error' in cal ? (
                <PanelError icon={Calendar} title="Calendar" message={cal.error} />
              ) : (
                <PanelError icon={Calendar} title="Calendar" message="Not configured" />
              )}

              {isOk<FinanceDashboard>(fin) ? (
                <FinancePanel data={fin} />
              ) : fin && 'error' in fin ? (
                <PanelError icon={Wallet} title="Finance" message={fin.error} />
              ) : null}
            </div>
          )}
        </div>

        {/* Right column — clients, reminders, embeddings */}
        <div className="space-y-4">
          <ClientStatusCard clients={clients} />

          {isOk<RemindersDashboard>(rem) ? (
            <RemindersPanel data={rem} />
          ) : rem && 'error' in rem ? (
            <PanelError icon={CheckSquare} title="Reminders" message={rem.error} />
          ) : null}

          <EmbeddingQueueCard data={embeddingQueue} />
        </div>
      </div>
    </div>
  )
}
