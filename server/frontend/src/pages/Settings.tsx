import { useEffect, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Key, ExternalLink, CheckCircle, XCircle, Monitor, Plus, Copy, ShieldOff,
  Database, RefreshCw, Trash2, SlidersHorizontal,
} from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { Toggle } from '@/components/ui/toggle'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import { StatCard } from '@/components/ui/stat-card'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { api, type PreferenceSchemaEntry } from '@/lib/api'
import {
  useTokens, useClients, useCreateClient, useDeactivateClient,
  useDataStats, useReindexEmbeddings, usePurgeIntegration, useSystemInfo,
} from '@/hooks/use-api'

// `user` attributes the resulting OAuth token to that User row — required by
// google/login since it no longer defaults to "alex". Finn and Isla don't
// have their own comar User rows (only alex/sam do), so their household
// calendar accounts are attributed to alex, the household admin.
const ACCOUNTS = [
  { email: 'user@gmail.com', label: 'User', user: 'alex' },
  { email: 'sam@example.com', label: 'Sam', user: 'sam' },
  { email: 'finn@example.com', label: 'Finn', user: 'alex' },
  { email: 'isla@example.com', label: 'Isla', user: 'alex' },
]

// Integration display names for the purge UI
const INTEGRATION_LABELS: Record<string, string> = {
  google_calendar: 'Calendar',
  google_mail: 'Gmail',
  lastfm: 'Last.fm',
  whatsapp: 'WhatsApp',
  weather: 'Weather',
  finance: 'Finance',
  obsidian: 'Obsidian',
}

function relativeTime(iso: string | null): string {
  if (!iso) return 'Never'
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60_000)
  if (mins < 1) return 'Just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

// ─── Clients & Accounts Tab ───

function ClientsTab() {
  const { data: tokenData, error: tokenError } = useTokens()
  const { data: clientData, error: clientError } = useClients()
  const createMutation = useCreateClient()
  const deactivateMutation = useDeactivateClient()

  const tokens = tokenData?.tokens ?? []
  const clients = clientData?.clients ?? []
  const error = tokenError || clientError

  const [showCreate, setShowCreate] = useState(false)
  const [newUser, setNewUser] = useState('')
  const [newLabel, setNewLabel] = useState('')
  const [createdToken, setCreatedToken] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [showInactive, setShowInactive] = useState(false)

  const activeClients = clients.filter((c) => c.is_active)
  const inactiveClients = clients.filter((c) => !c.is_active)
  const visibleClients = showInactive ? [...activeClients, ...inactiveClients] : activeClients

  const connectedEmails = new Set(tokens.map((t) => t.account))

  function handleConnect(email: string, user: string) {
    window.location.href = `/api/auth/google/login?account=${encodeURIComponent(email)}&user=${encodeURIComponent(user)}`
  }

  async function handleCreateToken() {
    try {
      const res = await createMutation.mutateAsync({ user: newUser, label: newLabel })
      setCreatedToken(res.token)
    } catch {
      setShowCreate(false)
    }
  }

  async function handleDeactivate(id: number, label: string) {
    if (!window.confirm(`Deactivate token "${label}"? The client will no longer be able to connect.`)) return
    await deactivateMutation.mutateAsync(id)
  }

  function handleCopy() {
    if (createdToken) {
      navigator.clipboard.writeText(createdToken)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  function closeCreateDialog() {
    setShowCreate(false)
    setCreatedToken(null)
    setNewUser('')
    setNewLabel('')
    setCopied(false)
  }

  return (
    <div className="space-y-6">
      {error && (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
          {error.message}
        </div>
      )}

      {/* Connected Clients */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <CardTitle className="flex items-center gap-2">
            <Monitor className="h-4 w-4" />
            Connected Clients
          </CardTitle>
          <Button size="sm" onClick={() => setShowCreate(true)}>
            <Plus className="h-3.5 w-3.5 mr-1.5" />
            New Token
          </Button>
        </CardHeader>
        <CardContent>
          {clients.length === 0 ? (
            <p className="text-sm text-muted-foreground">No client tokens yet. Create one to connect a device.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>User</TableHead>
                  <TableHead>Label</TableHead>
                  <TableHead>Token</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Last Seen</TableHead>
                  <TableHead>Version</TableHead>
                  <TableHead>Tasks</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleClients.map((c) => {
                  const tasks = Object.entries(c.task_health ?? {})
                  const unhealthy = tasks.filter(
                    ([, t]) => (t.restarts ?? 0) > 3 || Boolean(t.last_error) || Boolean(t.finished)
                  )
                  const taskTooltip = tasks
                    .map(([name, t]) => {
                      const bits = [`${t.restarts ?? 0} restarts`]
                      if (t.alive_seconds_ago != null) bits.push(`alive ${t.alive_seconds_ago}s ago`)
                      if (t.finished) bits.push('finished')
                      if (t.last_error) bits.push(`error: ${t.last_error}`)
                      return `${name}: ${bits.join(', ')}`
                    })
                    .join('\n')
                  return (
                  <TableRow key={c.id} className={!c.is_active ? 'opacity-40' : ''}>
                    <TableCell className="font-medium">{c.user}</TableCell>
                    <TableCell>{c.label}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">{c.token_preview}</TableCell>
                    <TableCell>
                      <Badge variant={c.is_active ? 'success' : 'default'}>
                        {c.is_active ? 'Active' : 'Inactive'}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-muted-foreground">{relativeTime(c.last_seen_at)}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">{c.client_version || '—'}</TableCell>
                    <TableCell title={taskTooltip || undefined}>
                      {tasks.length === 0 ? (
                        <span className="text-xs text-muted-foreground">—</span>
                      ) : (
                        <Badge variant={unhealthy.length > 0 ? 'destructive' : 'success'}>
                          {unhealthy.length > 0 ? `${unhealthy.length}/${tasks.length} unhealthy` : `${tasks.length} ok`}
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell>
                      {c.is_active && (
                        <Button
                          variant="ghost"
                          size="sm"
                          title="Revoke this token — the device can no longer connect"
                          className="text-muted-foreground hover:text-destructive"
                          onClick={() => handleDeactivate(c.id, c.label)}
                        >
                          <ShieldOff className="h-3.5 w-3.5 mr-1" />
                          Revoke
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
          {inactiveClients.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              className="mt-3 text-muted-foreground"
              onClick={() => setShowInactive((v) => !v)}
            >
              {showInactive
                ? 'Hide revoked tokens'
                : `Show ${inactiveClients.length} revoked token${inactiveClients.length === 1 ? '' : 's'}`}
            </Button>
          )}
        </CardContent>
      </Card>

      {/* Create Token Dialog */}
      <Dialog open={showCreate} onOpenChange={closeCreateDialog}>
        <DialogContent>
          {!createdToken ? (
            <>
              <DialogHeader>
                <DialogTitle>Create Client Token</DialogTitle>
                <DialogDescription>
                  Generate a token for a new device. The user will enter this during <code className="text-xs">comar setup</code>.
                </DialogDescription>
              </DialogHeader>
              <div className="space-y-4 py-2">
                <div className="space-y-2">
                  <Label htmlFor="user">User</Label>
                  <Input id="user" placeholder="sam" value={newUser} onChange={(e) => setNewUser(e.target.value)} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="label">Device Label</Label>
                  <Input id="label" placeholder="sam-macbook" value={newLabel} onChange={(e) => setNewLabel(e.target.value)} />
                </div>
              </div>
              <DialogFooter>
                <Button variant="ghost" onClick={closeCreateDialog}>Cancel</Button>
                <Button onClick={handleCreateToken} disabled={createMutation.isPending || !newUser.trim() || !newLabel.trim()}>
                  {createMutation.isPending ? 'Creating...' : 'Create Token'}
                </Button>
              </DialogFooter>
            </>
          ) : (
            <>
              <DialogHeader>
                <DialogTitle>Token Created</DialogTitle>
                <DialogDescription>
                  Save this token now — it will not be shown again.
                </DialogDescription>
              </DialogHeader>
              <div className="space-y-3 py-2">
                <div className="relative">
                  <pre className="font-mono text-xs bg-muted p-3 rounded-md break-all pr-10 select-all">
                    {createdToken}
                  </pre>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="absolute top-1.5 right-1.5"
                    onClick={handleCopy}
                  >
                    <Copy className="h-3.5 w-3.5" />
                  </Button>
                </div>
                {copied && <p className="text-xs text-success">Copied to clipboard</p>}
                <p className="text-xs text-muted-foreground">
                  The user should run <code>comar setup</code> and paste this token when prompted.
                </p>
              </div>
              <DialogFooter>
                <Button onClick={closeCreateDialog}>Done</Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>

      {/* Google Accounts — per-integration Credentials panels (V4 chunk 5.1)
          moved this to each integration's detail page (calendar/mail/sheets),
          scoped to that integration's own manifest.oauth. This tab keeps only
          a raw connect-a-new-account action and the flat token list, since
          neither is tied to any one integration. */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Key className="h-4 w-4" />
            Connect a Google Account
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            Per-integration credential status (reauth state, scopes) now lives on each
            integration's own page — see Calendar, Gmail, or Sheets under Integrations.
            Use this to connect a new account.
          </p>
          <div className="flex flex-wrap gap-2">
            {ACCOUNTS.map((acc) => (
              <Button key={acc.email} variant="ghost" size="sm" onClick={() => handleConnect(acc.email, acc.user)}>
                <ExternalLink className="h-3.5 w-3.5 mr-1.5" />
                {connectedEmails.has(acc.email) ? `Reconnect ${acc.label}` : `Connect ${acc.label}`}
              </Button>
            ))}
          </div>
        </CardContent>
      </Card>

      {tokens.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Active Tokens</CardTitle>
          </CardHeader>
          <CardContent>
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
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-muted-foreground">{t.provider}</span>
                    {t.expires_at && (
                      <span className="text-xs text-muted-foreground">
                        Expires {new Date(t.expires_at).toLocaleString('en-IE', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

// ─── Data Tab ───

function DataTab() {
  const { data: stats, isLoading } = useDataStats()
  const { data: systemInfo } = useSystemInfo()
  const reindexMutation = useReindexEmbeddings()
  const purgeMutation = usePurgeIntegration()

  const [purgeTarget, setPurgeTarget] = useState<string | null>(null)
  const [purgeDate, setPurgeDate] = useState('')

  const emb = stats?.embeddings
  const tables = stats?.tables ?? {}

  function handleReindex(source?: string) {
    if (!window.confirm(
      source
        ? `Re-index all ${source} embeddings? Existing embeddings will be cleared and rebuilt on the next sync.`
        : 'Reset all errored embedding queue items to pending?'
    )) return
    reindexMutation.mutate(source)
  }

  function handlePurge() {
    if (!purgeTarget) return
    const label = INTEGRATION_LABELS[purgeTarget] ?? purgeTarget
    const msg = purgeDate
      ? `Purge all ${label} data before ${purgeDate}? This cannot be undone.`
      : `Purge ALL ${label} data? This cannot be undone.`
    if (!window.confirm(msg)) return
    purgeMutation.mutate(
      { integration: purgeTarget, before: purgeDate || undefined },
      { onSuccess: () => { setPurgeTarget(null); setPurgeDate('') } },
    )
  }

  // Group tables by integration for a cleaner display
  const integrationTables: Record<string, { table: string; rows: number }[]> = {}
  for (const [table, rows] of Object.entries(tables)) {
    // Map table names to integration groups
    let group = 'system'
    if (table.startsWith('calendar')) group = 'Calendar'
    else if (table.startsWith('mail')) group = 'Gmail'
    else if (table.startsWith('scrobble') || table.startsWith('artist_tag')) group = 'Last.fm'
    else if (table.startsWith('whatsapp')) group = 'WhatsApp'
    else if (table.startsWith('weather')) group = 'Weather'
    else if (table.startsWith('transaction') || table.startsWith('account') || table.startsWith('categori') || table.startsWith('import') || table.startsWith('monthly')) group = 'Finance'
    else if (table.startsWith('vault')) group = 'Obsidian'
    else if (table.startsWith('reminder')) group = 'Reminders'
    else if (table.startsWith('embedding')) group = 'Embeddings'
    else if (table.startsWith('health')) group = 'Health'
    else group = 'System'

    if (!integrationTables[group]) integrationTables[group] = []
    integrationTables[group].push({ table, rows })
  }

  // Sort groups by total row count
  const sortedGroups = Object.entries(integrationTables).sort(
    (a, b) => b[1].reduce((s, t) => s + t.rows, 0) - a[1].reduce((s, t) => s + t.rows, 0)
  )

  return (
    <div className="space-y-6">
      {/* Infrastructure overview */}
      <div className="grid gap-4 sm:grid-cols-4">
        <StatCard
          label="Database"
          value={stats ? `${stats.db_size_mb} MB` : '—'}
        />
        <StatCard
          label="Disk Used"
          value={systemInfo?.disk && !('error' in systemInfo.disk)
            ? `${systemInfo.disk.percent_used}%`
            : '—'}
          trend={systemInfo?.disk && !('error' in systemInfo.disk)
            ? { value: `${systemInfo.disk.used_gb} / ${systemInfo.disk.total_gb} GB` }
            : undefined}
        />
        <StatCard
          label="Total Embeddings"
          value={emb?.total_embeddings.toLocaleString() ?? '—'}
        />
        <StatCard
          label="Queue"
          value={emb ? `${emb.queue_pending} pending` : '—'}
          trend={emb && emb.queue_errors > 0
            ? { value: `${emb.queue_errors} errors`, positive: false }
            : undefined}
        />
      </div>

      {/* Embeddings by source */}
      {emb && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Database className="h-4 w-4" />
              Embedding Sources
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {Object.entries(emb.by_source)
                .sort((a, b) => b[1] - a[1])
                .map(([source, count]) => (
                  <div key={source} className="flex items-center justify-between py-1.5 border-b border-border last:border-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium capitalize">{source}</span>
                      <span className="text-xs text-muted-foreground font-mono">{count.toLocaleString()}</span>
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-xs"
                      onClick={() => handleReindex(source)}
                      disabled={reindexMutation.isPending}
                    >
                      <RefreshCw className="h-3 w-3 mr-1" />
                      Re-index
                    </Button>
                  </div>
                ))}
            </div>
            {emb.last_embedded && (
              <p className="text-xs text-muted-foreground mt-3">
                Last embedded: {relativeTime(emb.last_embedded)}
              </p>
            )}
            {emb.queue_errors > 0 && (
              <Button
                variant="ghost"
                size="sm"
                className="mt-2 text-xs"
                onClick={() => handleReindex()}
                disabled={reindexMutation.isPending}
              >
                <RefreshCw className="h-3 w-3 mr-1" />
                Retry {emb.queue_errors} errored items
              </Button>
            )}
            {reindexMutation.isSuccess && (
              <p className="text-xs text-success mt-2">{reindexMutation.data?.message}</p>
            )}
          </CardContent>
        </Card>
      )}

      {/* Table row counts */}
      <Card>
        <CardHeader>
          <CardTitle>Table Row Counts</CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading && <p className="text-sm text-muted-foreground">Loading...</p>}
          <div className="space-y-4">
            {sortedGroups.map(([group, groupTables]) => (
              <div key={group}>
                <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1">{group}</p>
                {groupTables
                  .sort((a, b) => b.rows - a.rows)
                  .map(({ table, rows }) => (
                    <div key={table} className="flex items-center justify-between text-sm py-0.5">
                      <span className="text-muted-foreground font-mono text-xs">{table}</span>
                      <span className="font-mono text-xs">{rows.toLocaleString()}</span>
                    </div>
                  ))}
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Data purge */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-destructive">
            <Trash2 className="h-4 w-4" />
            Purge Data
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Remove cached data for an integration. Related embeddings will also be deleted.
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1.5">
              <Label className="text-xs">Integration</Label>
              <select
                value={purgeTarget ?? ''}
                onChange={(e) => setPurgeTarget(e.target.value || null)}
                className="h-9 rounded-md border border-border bg-card px-3 text-sm text-foreground"
              >
                <option value="">Select...</option>
                {Object.entries(INTEGRATION_LABELS).map(([key, label]) => (
                  <option key={key} value={key}>{label}</option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Before date (optional)</Label>
              <Input
                type="date"
                value={purgeDate}
                onChange={(e) => setPurgeDate(e.target.value)}
                className="h-9 w-40"
              />
            </div>
            <Button
              variant="destructive"
              size="sm"
              onClick={handlePurge}
              disabled={!purgeTarget || purgeMutation.isPending}
            >
              <Trash2 className="h-3.5 w-3.5 mr-1.5" />
              {purgeMutation.isPending ? 'Purging...' : 'Purge'}
            </Button>
          </div>
          {purgeMutation.isSuccess && (
            <p className="text-xs text-success">
              Deleted {purgeMutation.data?.deleted.toLocaleString()} rows from {purgeMutation.data?.integration}.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// ─── Preferences Tab ───

function PreferencesTab() {
  const { data: clientData } = useClients()
  const { data: schemaData, isLoading: schemaLoading } = useQuery({
    queryKey: ['preferences-schema'],
    queryFn: () => api.getPreferencesSchema(),
  })
  const qc = useQueryClient()

  // Derive the user list from the clients list the page already fetches —
  // there's no dedicated /users endpoint, and every client token row is
  // already tied to a real User (id + name).
  const users = useMemo(() => {
    const seen = new Map<number, string>()
    for (const c of clientData?.clients ?? []) {
      if (!seen.has(c.user_id)) seen.set(c.user_id, c.user)
    }
    return Array.from(seen, ([id, name]) => ({ id, name }))
  }, [clientData])

  const [userId, setUserId] = useState<number | null>(null)
  useEffect(() => {
    if (userId === null && users.length > 0) setUserId(users[0].id)
  }, [users, userId])

  const { data: prefsData, isLoading: prefsLoading } = useQuery({
    queryKey: ['user-preferences', userId],
    queryFn: () => api.getUserPreferences(userId as number),
    enabled: userId !== null,
  })

  const [form, setForm] = useState<Record<string, unknown>>({})
  useEffect(() => {
    if (prefsData) setForm(prefsData.preferences)
  }, [prefsData])

  const saveMutation = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.putUserPreferences(userId as number, values),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['user-preferences', userId] })
    },
  })

  const schema = schemaData?.preferences ?? []
  const groups = useMemo(() => {
    const byGroup: Record<string, PreferenceSchemaEntry[]> = {}
    for (const entry of schema) {
      if (!byGroup[entry.group]) byGroup[entry.group] = []
      byGroup[entry.group].push(entry)
    }
    return byGroup
  }, [schema])

  function updateField(key: string, value: unknown) {
    setForm((f) => ({ ...f, [key]: value }))
  }

  function renderField(entry: PreferenceSchemaEntry) {
    const value = form[entry.key]
    switch (entry.type) {
      case 'bool':
        return <Toggle checked={Boolean(value)} onChange={(v) => updateField(entry.key, v)} />
      case 'int':
        return (
          <Input
            type="number"
            value={typeof value === 'number' ? value : ''}
            onChange={(e) => updateField(entry.key, e.target.value === '' ? 0 : Number(e.target.value))}
            className="w-32"
          />
        )
      case 'list_str': {
        const list = Array.isArray(value) ? (value as unknown[]).map(String) : []
        return (
          <Textarea
            value={list.join('\n')}
            onChange={(e) =>
              updateField(
                entry.key,
                e.target.value.split('\n').map((s) => s.trim()).filter(Boolean)
              )
            }
            className="font-mono text-xs"
            rows={Math.min(8, Math.max(2, list.length || 2))}
          />
        )
      }
      case 'str':
      default:
        return (
          <Input
            value={typeof value === 'string' ? value : ''}
            onChange={(e) => updateField(entry.key, e.target.value)}
          />
        )
    }
  }

  if (users.length === 0) {
    return (
      <Card>
        <CardContent className="pt-6">
          <p className="text-sm text-muted-foreground">
            No users found yet — create a client token first (Clients & Accounts tab).
          </p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <CardTitle className="flex items-center gap-2">
            <SlidersHorizontal className="h-4 w-4" />
            Preferences
          </CardTitle>
          <div className="w-40">
            <Select
              value={userId !== null ? String(userId) : ''}
              onChange={(e) => setUserId(Number(e.target.value))}
            >
              {users.map((u) => (
                <option key={u.id} value={u.id}>{u.name}</option>
              ))}
            </Select>
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          {(schemaLoading || prefsLoading) && (
            <p className="text-sm text-muted-foreground">Loading...</p>
          )}
          {Object.entries(groups).map(([group, entries]) => (
            <div key={group} className="space-y-3">
              <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{group}</p>
              <div className="space-y-4">
                {entries.map((entry) => (
                  <div key={entry.key} className="grid gap-1.5 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)] sm:items-start">
                    <div>
                      <Label className="text-xs">{entry.key}</Label>
                      <p className="text-xs text-muted-foreground">{entry.description}</p>
                    </div>
                    {renderField(entry)}
                  </div>
                ))}
              </div>
            </div>
          ))}

          <div className="flex items-center gap-3 pt-2">
            <Button
              size="sm"
              onClick={() => saveMutation.mutate(form)}
              disabled={saveMutation.isPending || userId === null}
            >
              {saveMutation.isPending ? 'Saving...' : 'Save Preferences'}
            </Button>
            {saveMutation.isSuccess && <p className="text-xs text-success">Saved.</p>}
            {saveMutation.isError && (
              <p className="text-xs text-destructive">{(saveMutation.error as Error).message}</p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

// ─── Main Settings Page ───

export default function Settings() {
  const [tab, setTab] = useState('clients')

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Settings</h1>
        <p className="text-sm text-muted-foreground mt-1">Manage accounts, clients, and data</p>
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="clients">
            <Monitor className="h-3.5 w-3.5 mr-1.5" />
            Clients & Accounts
          </TabsTrigger>
          <TabsTrigger value="data">
            <Database className="h-3.5 w-3.5 mr-1.5" />
            Data
          </TabsTrigger>
          <TabsTrigger value="preferences">
            <SlidersHorizontal className="h-3.5 w-3.5 mr-1.5" />
            Preferences
          </TabsTrigger>
        </TabsList>

        <TabsContent value="clients">
          <ClientsTab />
        </TabsContent>

        <TabsContent value="data">
          <DataTab />
        </TabsContent>

        <TabsContent value="preferences">
          <PreferencesTab />
        </TabsContent>
      </Tabs>
    </div>
  )
}
