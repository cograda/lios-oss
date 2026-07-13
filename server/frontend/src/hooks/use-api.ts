import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type DashboardSummary,
  type IntegrationStatus,
  type IntegrationDetail,
  type ClientInfo,
  type TokenInfo,
  type SystemInfo,
  type LogEntry,
  type DataStats,
} from '@/lib/api'

// ─── Query Keys ───

export const queryKeys = {
  dashboard: ['dashboard'] as const,
  integrations: ['integrations'] as const,
  integrationDetail: (name: string) => ['integrations', name] as const,
  clients: ['clients'] as const,
  tokens: ['tokens'] as const,
  system: ['system'] as const,
  logs: (filters: Record<string, string | undefined>) => ['logs', filters] as const,
  logUsers: ['log-users'] as const,
  dataStats: ['data-stats'] as const,
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

export function useClients() {
  return useQuery<{ clients: ClientInfo[] }>({
    queryKey: queryKeys.clients,
    queryFn: () => api.getClients(),
    refetchInterval: 60_000,
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
