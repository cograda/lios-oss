import { Calendar, CheckSquare, Wallet } from 'lucide-react'
import { useDashboard, useIntegrations, useClients, useSystemInfo } from '@/hooks/use-api'
import { SystemStatusBar } from '@/components/dashboard/system-status-bar'
import { ReauthBanner } from '@/components/dashboard/reauth-banner'
import { IntegrationGrid } from '@/components/dashboard/integration-grid'
import { ClientStatusCard } from '@/components/dashboard/client-status-card'
import { EmbeddingQueueCard } from '@/components/dashboard/embedding-queue-card'
import { SystemAlertsPanel } from '@/components/dashboard/system-alerts-panel'
import { IntegrationPanels } from '@/components/dashboard/panel-renderer'
import type { EmbeddingQueue } from '@/lib/api'

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

      {/* Active alerts + per-tool call health — full width, prominent */}
      <SystemAlertsPanel />

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

          {/* Calendar + Finance row — generic panel envelope (V4 chunk 5.1) */}
          {data && (
            <div className="grid gap-4 lg:grid-cols-2">
              <IntegrationPanels title="Calendar" icon={Calendar} data={cal} />
              <IntegrationPanels title="Finance" icon={Wallet} data={fin} />
            </div>
          )}
        </div>

        {/* Right column — clients, reminders, embeddings */}
        <div className="space-y-4">
          <ClientStatusCard clients={clients} />

          <IntegrationPanels title="Reminders" icon={CheckSquare} data={rem} />

          <EmbeddingQueueCard data={embeddingQueue} />
        </div>
      </div>
    </div>
  )
}
