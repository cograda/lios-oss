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

// Generic dashboard panel envelope (V4 chunk 5.1) — any integration's
// dashboard_data() may emit `{panels: [...]}` alongside its own bespoke
// keys; the frontend renders only this envelope now (see
// components/dashboard/panel-renderer.tsx). Replaces the old
// CalendarDashboard/RemindersDashboard/FinanceDashboard interfaces, which
// were typed against calendar/reminders/finance's bespoke shapes only.
export interface DashboardPanelTrend {
  value: string
  positive?: boolean
}

export interface DashboardStatPanel {
  kind: 'stat'
  title: string
  data: { value: string | number; trend?: DashboardPanelTrend }
}

export interface DashboardListPanel {
  kind: 'list'
  title: string
  data: { items: Array<{ label: string; value: string | number; sublabel?: string }> }
}

export interface DashboardTablePanel {
  kind: 'table'
  title: string
  data: { columns: string[]; rows: Array<Array<string | number>> }
}

export type DashboardPanel = DashboardStatPanel | DashboardListPanel | DashboardTablePanel

export interface IntegrationDashboardData {
  panels?: DashboardPanel[]
  error?: string
  status?: string
  [key: string]: unknown
}

export interface ReauthNeeded {
  provider: string
  account_email: string
  flagged_at: string | null
  reason: string | null
  reauth_url: string
}

export interface DashboardSummary {
  google_calendar?: IntegrationDashboardData
  apple_reminders?: IntegrationDashboardData
  finance?: IntegrationDashboardData
  reauth_needed?: ReauthNeeded[]
  [key: string]: unknown
}

export interface IntegrationStatus {
  name: string
  display_name: string
  configured: boolean
  // V4 chunk 5.1 — orthogonal to `configured`: a disabled integration can
  // be fully configured, and a configured one can be disabled.
  enabled: boolean
  schedule: string | null
  last_sync_at: string | null
  last_sync_status: string
  last_error: string | null
  last_sync_duration_ms: number | null
  consecutive_failures: number
  next_sync_at: string | null
  // V4 chunk 1.3 — sourced from the integration's manifest.
  version: string | null
  type: string | null
  icon: string | null
  description: string | null
}

export interface OAuthRequirement {
  provider: string
  scopes: string[]
}

export interface BackgroundTaskSpec {
  name: string
  kind: 'startup' | 'cron'
  cron: string | null
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
    percent_used: number | null
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
  // The owning User's name — google/login now requires ?user=, so any
  // reconnect/reauth link built from this list needs this to avoid a 422.
  user: string
  scopes: string
  expires_at: string | null
  expired: boolean
  has_refresh_token: boolean
}

export interface ClientTaskHealth {
  alive_seconds_ago?: number | null
  restarts?: number | null
  last_error?: string | null
  finished?: boolean | null
}

export interface ClientInfo {
  id: number
  user: string
  user_id: number
  label: string
  // 'full' is a person's whole authority; 'readonly' is a device that may
  // look but never write (the Hall Panel). Enforced server-side.
  scope: 'full' | 'readonly'
  is_active: boolean
  created_at: string | null
  last_seen_at: string | null
  client_version: string | null
  token_preview: string
  task_health: Record<string, ClientTaskHealth> | null
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
  // Flows panel (V4 chunk 5.1) — straight off the manifest.
  reads_from: string[]
  writes_to: string[]
  embedding_sources: string[]
  depends_on: string[]
  provides: string[]
  oauth: OAuthRequirement | null
  freshness_threshold_minutes: number | null
  background_tasks: BackgroundTaskSpec[]
}

export interface ConfigField {
  value: unknown
  type: 'str' | 'int' | 'bool' | 'list_str' | 'dict_str_str'
  required: boolean
  secret: boolean
  description: string
  configured: boolean | null
}

export interface IntegrationConfigResponse {
  integration: string
  config: Record<string, ConfigField>
}

export interface ToolAnnotations {
  readOnlyHint?: boolean
  destructiveHint?: boolean
  idempotentHint?: boolean
  openWorldHint?: boolean
  [key: string]: unknown
}

export interface IntegrationToolInfo {
  name: string
  annotations: ToolAnnotations
  calls: number
}

export interface IntegrationToolsResponse {
  integration: string
  window_hours: number
  tools: IntegrationToolInfo[]
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

// Who is signed in (2026-09-06 — a person, via their per-user bearer; the
// server holds the session and the cookie carries only a random id).
export interface AuthUser {
  id: number
  name: string
  display_name: string
  is_admin: boolean
}

export interface AuthCheck {
  authenticated: boolean
  user: AuthUser | null
}

// System alerts (integration health + tool-call health)
export interface IntegrationAlert {
  integration: string
  status: string
  issues: string[]
  consecutive_failures: number
  last_error?: string
  last_sync_at?: string
}

export interface ToolAlert {
  tool: string
  issue: string
}

export interface SystemAlerts {
  status: 'all_ok' | 'degraded'
  alerts: IntegrationAlert[]
  data_freshness: Array<{
    integration: string
    latest: string | null
    age: string | null
    threshold: string
  }>
  reauth_needed: ReauthNeeded[]
  tool_alerts: ToolAlert[]
}

export interface ToolStat {
  name: string
  calls: number
  errors: number
  error_rate: number | null
  p50_duration_ms: number | null
  p95_duration_ms: number | null
}

export interface ToolStats {
  window_hours: number
  tools: ToolStat[]
}

// Preferences (per-user; GET /api/preferences/schema + /{user_id})
export type PreferenceType = 'str' | 'int' | 'bool' | 'list_str'

export interface PreferenceSchemaEntry {
  key: string
  type: PreferenceType
  default: unknown
  description: string
  group: string
}

export interface PreferencesSchemaResponse {
  preferences: PreferenceSchemaEntry[]
}

export interface UserPreferencesResponse {
  user_id: number
  preferences: Record<string, unknown>
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),

  // Auth
  checkAuth: () => request<AuthCheck>('GET', '/auth/check'),
  login: (token: string) => request<{ status: string; user: AuthUser }>('POST', '/auth/login', { token }),
  logout: () => request<{ status: string }>('POST', '/auth/logout'),

  // Dashboard
  getDashboard: () => request<DashboardSummary>('GET', '/dashboard/summary'),

  // Integrations
  getIntegrations: () => request<{ integrations: IntegrationStatus[] }>('GET', '/integrations/'),
  triggerSync: (name: string) => request<{ status: string; message: string }>('POST', `/integrations/${name}/sync`),
  getIntegrationDetail: (name: string) => request<IntegrationDetail>('GET', `/integrations/${name}/detail`),
  getIntegrationConfig: (name: string) =>
    request<IntegrationConfigResponse>('GET', `/integrations/${name}/config`),
  putIntegrationConfig: (name: string, values: Record<string, unknown>) =>
    request<{ status: string; updated: string[] }>('PUT', `/integrations/${name}/config`, values),
  getIntegrationTools: (name: string) =>
    request<IntegrationToolsResponse>('GET', `/integrations/${name}/tools`),
  setIntegrationEnabled: (name: string, enabled: boolean) =>
    request<{ status: string; enabled: boolean }>('PUT', `/integrations/${name}/enabled`, { enabled }),

  // OAuth tokens
  getTokens: () => request<{ tokens: TokenInfo[] }>('GET', '/auth/tokens'),
  // google/login is session-exempt (the re-auth link is followed on the
  // tailnet hostname, where the comar.lab cookie is not sent) and so requires
  // a signed, short-lived `start` in the URL. This session-gated route mints
  // it; navigate to the URL it returns rather than building one by hand.
  getGoogleLoginUrl: (account: string, user: string) =>
    request<{ url: string }>(
      'GET',
      `/auth/google/login-url?account=${encodeURIComponent(account)}&user=${encodeURIComponent(user)}`,
    ),

  // Client tokens
  getClients: () => request<{ clients: ClientInfo[] }>('GET', '/auth/clients'),
  createClient: (user: string, label: string) =>
    request<ClientCreateResponse>('POST', '/auth/clients', { user, label }),
  deactivateClient: (id: number) =>
    request<{ status: string; id: number }>('DELETE', `/auth/clients/${id}`),

  // System
  getSystemInfo: () => request<SystemInfo>('GET', '/system/info'),
  getSystemAlerts: () => request<SystemAlerts>('GET', '/system/alerts'),
  getToolStats: (hours?: number) =>
    request<ToolStats>('GET', `/system/tool-stats${hours ? `?hours=${hours}` : ''}`),

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

  // Preferences
  getPreferencesSchema: () => request<PreferencesSchemaResponse>('GET', '/preferences/schema'),
  getUserPreferences: (userId: number) =>
    request<UserPreferencesResponse>('GET', `/preferences/${userId}`),
  putUserPreferences: (userId: number, values: Record<string, unknown>) =>
    request<{ user_id: number; updated: string[] }>('PUT', `/preferences/${userId}`, values),
}

// SSE — dashboard sync-state stream (issue #141). Session-cookie
// authenticated (same-origin `EventSource` sends the `lios_session` cookie
// automatically; no bearer ever reaches the browser), so this does NOT go
// through `request()`/fetch — `EventSource` has its own transport. The path
// literal is kept in this file (route-reachability's one frontend seam,
// `test_route_reachability.py`) rather than inlined at the call site.
export const SYSTEM_EVENTS_PATH = '/system/events'

export function openSystemEventStream(): EventSource {
  return new EventSource(`${BASE}${SYSTEM_EVENTS_PATH}`)
}
