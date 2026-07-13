const BASE = '/api'

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    if (res.status === 401) {
      // Force re-auth
      window.dispatchEvent(new Event('auth:logout'))
    }
    const text = await res.text()
    throw new Error(`${res.status}: ${text}`)
  }
  return res.json()
}

// Types
export interface CalendarEvent {
  summary: string | null
  calendar: string
  account: string
  start: string
  end: string
  all_day: boolean
  location: string | null
  description: string | null
}

export interface CalendarDashboard {
  connected_accounts: number
  events_this_week: number
  by_day: Record<string, CalendarEvent[]>
  next_event: CalendarEvent | null
  error?: string
}

export interface RemindersDashboard {
  total_incomplete: number
  overdue: number
  by_list: Record<string, number>
  error?: string
}

export interface FinanceDashboard {
  total_income: number
  total_expense: number
  net_savings: number
  transaction_count: number
  top_categories: Array<{ name: string; value: number }>
  previous?: { total_income: number; total_expense: number; net_savings: number }
  error?: string
}

export interface ReauthNeeded {
  provider: string
  account_email: string
  flagged_at: string | null
  reason: string | null
  reauth_url: string
}

export interface DashboardSummary {
  google_calendar?: CalendarDashboard | { error: string } | { status: string }
  apple_reminders?: RemindersDashboard | { error: string } | { status: string }
  finance?: FinanceDashboard | { error: string } | { status: string }
  reauth_needed?: ReauthNeeded[]
  [key: string]: unknown
}

export interface IntegrationStatus {
  name: string
  display_name: string
  configured: boolean
  schedule: string | null
  last_sync_at: string | null
  last_sync_status: string
  last_error: string | null
  last_sync_duration_ms: number | null
  consecutive_failures: number
  next_sync_at: string | null
}

export interface SystemInfo {
  uptime_seconds: number
  started_at: string
  python_version: string
  platform: string
  disk: {
    total_gb: number
    used_gb: number
    free_gb: number
    percent_used: number
  } | { error: string }
  database: {
    size_mb: number
    tables: Record<string, number>
  } | { error: string }
}

export interface EmbeddingQueue {
  pending: number
  processing: number
  done: number
  errored: number
  sources: Record<string, number>
}

export interface TokenInfo {
  provider: string
  account: string
  scopes: string
  expires_at: string | null
  expired: boolean
  has_refresh_token: boolean
}

export interface ClientInfo {
  id: number
  user: string
  label: string
  is_active: boolean
  created_at: string | null
  last_seen_at: string | null
  client_version: string | null
  token_preview: string
}

export interface ClientCreateResponse {
  id: number
  user: string
  label: string
  token: string
  message: string
}

export interface SyncHistoryEntry {
  started_at: string
  status: string
  duration_ms: number | null
  error: string | null
  trigger: string
}

export interface IntegrationDetail extends IntegrationStatus {
  history: SyncHistoryEntry[]
}

export interface LogEntry {
  id: number
  user: string
  level: string
  logger: string
  message: string
  logged_at: string | null
  client_version: string | null
}

export interface DataStats {
  embeddings: {
    model: string
    dimensions: number
    total_embeddings: number
    by_source: Record<string, number>
    queue_pending: number
    queue_errors: number
    last_embedded: string | null
  }
  tables: Record<string, number>
  db_size_mb: number
}

export interface AuthCheck {
  authenticated: boolean
  auth_required: boolean
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),

  // Auth
  checkAuth: () => request<AuthCheck>('GET', '/auth/check'),
  login: (token: string) => request<{ status: string }>('POST', '/auth/login', { token }),

  // Dashboard
  getDashboard: () => request<DashboardSummary>('GET', '/dashboard/summary'),

  // Integrations
  getIntegrations: () => request<{ integrations: IntegrationStatus[] }>('GET', '/integrations/'),
  triggerSync: (name: string) => request<{ status: string; message: string }>('POST', `/integrations/${name}/sync`),
  getIntegrationDetail: (name: string) => request<IntegrationDetail>('GET', `/integrations/${name}/detail`),

  // OAuth tokens
  getTokens: () => request<{ tokens: TokenInfo[] }>('GET', '/auth/tokens'),

  // Client tokens
  getClients: () => request<{ clients: ClientInfo[] }>('GET', '/auth/clients'),
  createClient: (user: string, label: string) =>
    request<ClientCreateResponse>('POST', '/auth/clients', { user, label }),
  deactivateClient: (id: number) =>
    request<{ status: string; id: number }>('DELETE', `/auth/clients/${id}`),

  // System
  getSystemInfo: () => request<SystemInfo>('GET', '/system/info'),

  // Logs
  getLogs: (params: { user?: string; level?: string; search?: string; before?: string; limit?: number }) => {
    const sp = new URLSearchParams()
    if (params.user) sp.set('user', params.user)
    if (params.level) sp.set('level', params.level)
    if (params.search) sp.set('search', params.search)
    if (params.before) sp.set('before', params.before)
    if (params.limit) sp.set('limit', String(params.limit))
    return request<{ logs: LogEntry[] }>('GET', `/logs/?${sp}`)
  },
  tailLogs: (afterId: number, params?: { user?: string; level?: string }) => {
    const sp = new URLSearchParams({ after_id: String(afterId) })
    if (params?.user) sp.set('user', params.user)
    if (params?.level) sp.set('level', params.level)
    return request<{ logs: LogEntry[] }>('GET', `/logs/tail?${sp}`)
  },
  getLogUsers: () => request<{ users: string[] }>('GET', '/logs/users'),

  // Data management
  getDataStats: () => request<DataStats>('GET', '/data/stats'),
  reindexEmbeddings: (source?: string) =>
    request<{ status: string; message: string }>('POST', `/data/reindex-embeddings${source ? `?source=${source}` : ''}`),
  purgeIntegration: (integration: string, before?: string) => {
    const sp = new URLSearchParams()
    if (before) sp.set('before', before)
    const qs = sp.toString()
    return request<{ status: string; integration: string; deleted: number }>('DELETE', `/data/purge/${integration}${qs ? `?${qs}` : ''}`)
  },
}
