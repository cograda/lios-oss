import { useEffect, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  api,
  openSystemEventStream,
  type DashboardSummary,
  type IntegrationStatus,
  type IntegrationDetail,
  type IntegrationConfigResponse,
  type IntegrationToolsResponse,
  type ClientInfo,
  type TokenInfo,
  type SystemInfo,
  type LogEntry,
  type DataStats,
  type SystemAlerts,
  type ToolStats,
} from '@/lib/api'

// ─── Query Keys ───

export const queryKeys = {
  dashboard: ['dashboard'] as const,
  integrations: ['integrations'] as const,
  integrationDetail: (name: string) => ['integrations', name] as const,
  integrationConfig: (name: string) => ['integrations', name, 'config'] as const,
  integrationTools: (name: string) => ['integrations', name, 'tools'] as const,
  clients: ['clients'] as const,
  tokens: ['tokens'] as const,
  system: ['system'] as const,
  logs: (filters: Record<string, string | undefined>) => ['logs', filters] as const,
  logUsers: ['log-users'] as const,
  dataStats: ['data-stats'] as const,
  systemAlerts: ['system-alerts'] as const,
  toolStats: (hours: number) => ['tool-stats', hours] as const,
}

// ─── Queries ───

export function useDashboard() {
  return useQuery<DashboardSummary>({
    queryKey: queryKeys.dashboard,
    queryFn: () => api.getDashboard(),
    refetchInterval: 30_000,
  })
}

export function useIntegrations() {
  return useQuery<{ integrations: IntegrationStatus[] }>({
    queryKey: queryKeys.integrations,
    queryFn: () => api.getIntegrations(),
    refetchInterval: 30_000,
  })
}

export function useIntegrationDetail(name: string) {
  return useQuery<IntegrationDetail>({
    queryKey: queryKeys.integrationDetail(name),
    queryFn: () => api.getIntegrationDetail(name),
    refetchInterval: 30_000,
  })
}

export function useIntegrationConfig(name: string) {
  return useQuery<IntegrationConfigResponse>({
    queryKey: queryKeys.integrationConfig(name),
    queryFn: () => api.getIntegrationConfig(name),
  })
}

export function useIntegrationTools(name: string) {
  return useQuery<IntegrationToolsResponse>({
    queryKey: queryKeys.integrationTools(name),
    queryFn: () => api.getIntegrationTools(name),
    refetchInterval: 60_000,
  })
}

export function useClients(live = false) {
  return useQuery<{ clients: ClientInfo[] }>({
    queryKey: queryKeys.clients,
    queryFn: () => api.getClients(),
    // While the SSE stream (`useSyncStream`) is connected, a daemon
    // connect/disconnect/heartbeat invalidates this query directly, so the
    // 60s poll is only a safety net against a missed/coalesced event —
    // widened rather than disabled outright, since "definitely stale after
    // N minutes regardless" is cheaper to reason about than "never poll".
    refetchInterval: live ? 5 * 60_000 : 60_000,
  })
}

export function useTokens() {
  return useQuery<{ tokens: TokenInfo[] }>({
    queryKey: queryKeys.tokens,
    queryFn: () => api.getTokens(),
  })
}

export function useSystemInfo() {
  return useQuery<SystemInfo>({
    queryKey: queryKeys.system,
    queryFn: () => api.getSystemInfo(),
    refetchInterval: 60_000,
  })
}

export function useLogs(filters: { user?: string; level?: string; search?: string }) {
  return useQuery<{ logs: LogEntry[] }>({
    queryKey: queryKeys.logs(filters),
    queryFn: () => api.getLogs({ ...filters, limit: 200 }),
    refetchInterval: 5_000,
  })
}

export function useLogUsers() {
  return useQuery<{ users: string[] }>({
    queryKey: queryKeys.logUsers,
    queryFn: () => api.getLogUsers(),
  })
}

export function useDataStats() {
  return useQuery<DataStats>({
    queryKey: queryKeys.dataStats,
    queryFn: () => api.getDataStats(),
    refetchInterval: 60_000,
  })
}

export function useSystemAlerts(live = false) {
  return useQuery<SystemAlerts>({
    queryKey: queryKeys.systemAlerts,
    queryFn: () => api.getSystemAlerts(),
    // Same reasoning as `useClients` above — SSE-driven invalidation is
    // primary while connected, this interval is only the safety net.
    refetchInterval: live ? 5 * 60_000 : 30_000,
  })
}

export function useToolStats(hours = 24) {
  return useQuery<ToolStats>({
    queryKey: queryKeys.toolStats(hours),
    queryFn: () => api.getToolStats(hours),
    refetchInterval: 60_000,
  })
}

// ─── Live sync-state stream (issue #141) ───

const RECONNECT_BASE_MS = 1_000
const RECONNECT_MAX_MS = 30_000

/** Subscribes to `GET /api/system/events` for the lifetime of the calling
 * component and invalidates the queries that poll would otherwise refresh
 * on a timer. Mount once (e.g. at the top of `Dashboard`) — every `useQuery`
 * elsewhere shares the same react-query cache, so one invalidate updates
 * every consumer.
 *
 * Reconnects with exponential backoff (capped at 30s) on error, since a
 * bare `EventSource` retries on a fixed browser-chosen interval with no
 * backoff of its own — fine for a blip, punishing for a server that's
 * actually down. `useClients`/`useSystemAlerts`'s own `refetchInterval`
 * is the fallback while `connected` is false, so a dead stream degrades to
 * the pre-SSE polling behaviour rather than to a stale UI.
 */
export function useSyncStream() {
  const qc = useQueryClient()
  const [connected, setConnected] = useState(false)
  const reconnectDelay = useRef(RECONNECT_BASE_MS)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    let cancelled = false
    let source: EventSource | null = null

    function invalidate() {
      qc.invalidateQueries({ queryKey: queryKeys.clients })
      qc.invalidateQueries({ queryKey: queryKeys.systemAlerts })
    }

    function connect() {
      if (cancelled) return
      source = openSystemEventStream()

      source.addEventListener('hello', () => {
        setConnected(true)
        reconnectDelay.current = RECONNECT_BASE_MS
      })
      // Every named event this stream emits (daemon_connection,
      // daemon_heartbeat, and anything added later) is a "go refetch"
      // nudge — none of them carry a payload the UI reads directly, so
      // one listener covers all of them via `onmessage`. `EventSource`'s
      // `onmessage` only fires for the unnamed `message` event, so this
      // uses the generic listener form to also catch the named ones the
      // server sends (`api/v1.py`/`routes/system.py` set `event: <type>`).
      for (const type of ['message', 'daemon_connection', 'daemon_heartbeat']) {
        source.addEventListener(type, invalidate)
      }
      source.onerror = () => {
        setConnected(false)
        source?.close()
        if (cancelled) return
        timerRef.current = setTimeout(connect, reconnectDelay.current)
        reconnectDelay.current = Math.min(reconnectDelay.current * 2, RECONNECT_MAX_MS)
      }
    }

    connect()

    return () => {
      cancelled = true
      if (timerRef.current) clearTimeout(timerRef.current)
      source?.close()
      setConnected(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return { connected }
}

// ─── Mutations ───

export function useReindexEmbeddings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (source?: string) => api.reindexEmbeddings(source),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.dataStats })
      qc.invalidateQueries({ queryKey: queryKeys.dashboard })
    },
  })
}

export function usePurgeIntegration() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ integration, before }: { integration: string; before?: string }) =>
      api.purgeIntegration(integration, before),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.dataStats })
      qc.invalidateQueries({ queryKey: queryKeys.dashboard })
      qc.invalidateQueries({ queryKey: queryKeys.integrations })
    },
  })
}

export function useTriggerSync() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) => api.triggerSync(name),
    onSuccess: (_data, name) => {
      qc.invalidateQueries({ queryKey: queryKeys.integrations })
      qc.invalidateQueries({ queryKey: queryKeys.integrationDetail(name) })
      qc.invalidateQueries({ queryKey: queryKeys.dashboard })
    },
  })
}

export function usePutIntegrationConfig(name: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (values: Record<string, unknown>) => api.putIntegrationConfig(name, values),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.integrationConfig(name) })
      qc.invalidateQueries({ queryKey: queryKeys.integrationDetail(name) })
      qc.invalidateQueries({ queryKey: queryKeys.integrations })
    },
  })
}

export function useSetIntegrationEnabled(name: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (enabled: boolean) => api.setIntegrationEnabled(name, enabled),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.integrationDetail(name) })
      qc.invalidateQueries({ queryKey: queryKeys.integrations })
    },
  })
}

export function useCreateClient() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ user, label }: { user: string; label: string }) =>
      api.createClient(user, label),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.clients })
    },
  })
}

export function useDeactivateClient() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.deactivateClient(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.clients })
    },
  })
}
