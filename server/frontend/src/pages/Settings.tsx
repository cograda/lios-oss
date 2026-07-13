import { useState } from 'react'
import {
  Key, ExternalLink, CheckCircle, XCircle, Monitor, Plus, Copy, ShieldOff,
  Database, RefreshCw, Trash2,
} from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import { StatCard } from '@/components/ui/stat-card'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import {
  useTokens, useClients, useCreateClient, useDeactivateClient,
  useDataStats, useReindexEmbeddings, usePurgeIntegration, useSystemInfo,
} from '@/hooks/use-api'

const ACCOUNTS = [
  { email: 'user@gmail.com', label: 'User' },
  { email: 'sam@example.com', label: 'Sam' },
  { email: 'finn@example.com', label: 'Finn' },
  { email: 'isla@example.com', label: 'Isla' },
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

  const connectedEmails = new Set(tokens.map((t) => t.account))

  function handleConnect(email: string) {
    window.location.href = `/api/auth/google/login?account=${encodeURIComponent(email)}`
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
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {clients.map((c) => (
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
                    <TableCell>
                      {c.is_active && (
                        <Button variant="ghost" size="sm" onClick={() => handleDeactivate(c.id, c.label)}>
                          <ShieldOff className="h-3.5 w-3.5" />
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
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

      {/* Google Accounts */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Key className="h-4 w-4" />
            Google Accounts
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {ACCOUNTS.map((acc) => {
            const token = tokens.find((t) => t.account === acc.email)
            const connected = connectedEmails.has(acc.email)

            return (
              <div key={acc.email} className="flex items-center justify-between py-2 border-b border-border last:border-0">
                <div>
                  <p className="text-sm font-medium">{acc.label}</p>
                  <p className="text-xs text-muted-foreground font-mono">{acc.email}</p>
                  {token && (
                    <div className="flex items-center gap-2 mt-1">
                      {token.expired ? (
                        <Badge variant="destructive">Expired</Badge>
                      ) : (
                        <Badge variant="success">Active</Badge>
                      )}
                      {token.has_refresh_token && (
                        <span className="text-xs text-muted-foreground">Auto-refresh</span>
                      )}
                    </div>
                  )}
                </div>
                <Button
                  variant={connected ? 'ghost' : 'default'}
                  size="sm"
                  onClick={() => handleConnect(acc.email)}
                >
                  <ExternalLink className="h-3.5 w-3.5 mr-1.5" />
                  {connected ? 'Reconnect' : 'Connect'}
                </Button>
              </div>
            )
          })}
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
        </TabsList>

        <TabsContent value="clients">
          <ClientsTab />
        </TabsContent>

        <TabsContent value="data">
          <DataTab />
        </TabsContent>
      </Tabs>
    </div>
  )
}
