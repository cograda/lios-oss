import { useState, useEffect, useRef } from 'react'
import { Search, ArrowDown } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { useLogs, useLogUsers } from '@/hooks/use-api'
import { cn } from '@/lib/utils'

const LEVELS = ['ERROR', 'WARNING', 'INFO', 'DEBUG'] as const

function levelColor(level: string): string {
  switch (level.toUpperCase()) {
    case 'ERROR': return 'text-destructive'
    case 'WARNING': return 'text-amber-400'
    case 'DEBUG': return 'text-muted-foreground/60'
    default: return 'text-foreground'
  }
}

function levelBadgeVariant(level: string): 'destructive' | 'warning' | 'default' | 'outline' {
  switch (level.toUpperCase()) {
    case 'ERROR': return 'destructive'
    case 'WARNING': return 'warning'
    case 'DEBUG': return 'outline'
    default: return 'default'
  }
}

function formatTime(iso: string | null): string {
  if (!iso) return '--'
  return new Date(iso).toLocaleTimeString('en-IE', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function formatDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  const today = new Date()
  if (d.toDateString() === today.toDateString()) return ''
  return d.toLocaleDateString('en-IE', { month: 'short', day: 'numeric' }) + ' '
}

export default function Logs() {
  const [level, setLevel] = useState<string>('')
  const [user, setUser] = useState<string>('')
  const [search, setSearch] = useState<string>('')
  const [searchInput, setSearchInput] = useState('')
  const [tail, setTail] = useState(true)
  const bottomRef = useRef<HTMLDivElement>(null)

  const { data: usersData } = useLogUsers()
  const { data, isLoading } = useLogs({
    user: user || undefined,
    level: level || undefined,
    search: search || undefined,
  })

  const logs = data?.logs ?? []
  const users = usersData?.users ?? []

  // Auto-scroll to bottom in tail mode
  useEffect(() => {
    if (tail && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [logs.length, tail])

  function handleSearch(e: React.FormEvent) {
    e.preventDefault()
    setSearch(searchInput)
  }

  // Reversed for display: newest at bottom (like a terminal)
  const displayLogs = [...logs].reverse()

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Logs</h1>
          <p className="text-sm text-muted-foreground mt-1">Client log stream</p>
        </div>
        <Button
          variant={tail ? 'default' : 'ghost'}
          size="sm"
          onClick={() => setTail(!tail)}
        >
          <ArrowDown className="h-3.5 w-3.5 mr-1.5" />
          {tail ? 'Tailing' : 'Tail'}
        </Button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        {/* Level filter */}
        <div className="flex gap-1">
          <Button
            variant={level === '' ? 'default' : 'ghost'}
            size="sm"
            onClick={() => setLevel('')}
            className="text-xs h-7"
          >
            All
          </Button>
          {LEVELS.map((l) => (
            <Button
              key={l}
              variant={level === l ? 'default' : 'ghost'}
              size="sm"
              onClick={() => setLevel(level === l ? '' : l)}
              className="text-xs h-7"
            >
              {l}
            </Button>
          ))}
        </div>

        {/* User filter */}
        {users.length > 0 && (
          <select
            value={user}
            onChange={(e) => setUser(e.target.value)}
            className="h-7 rounded-md border border-border bg-card px-2 text-xs text-foreground"
          >
            <option value="">All users</option>
            {users.map((u) => (
              <option key={u} value={u}>{u}</option>
            ))}
          </select>
        )}

        {/* Search */}
        <form onSubmit={handleSearch} className="flex gap-1 ml-auto">
          <Input
            placeholder="Search logs..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            className="h-7 w-48 text-xs"
          />
          <Button type="submit" variant="ghost" size="sm" className="h-7">
            <Search className="h-3 w-3" />
          </Button>
        </form>
      </div>

      {/* Log output */}
      <Card>
        <CardHeader className="py-2 px-4">
          <CardTitle className="text-xs font-normal text-muted-foreground flex items-center justify-between">
            <span>{logs.length} entries{search ? ` matching "${search}"` : ''}</span>
            {tail && (
              <Badge variant="outline" className="text-xs animate-pulse">live</Badge>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <div className="max-h-[calc(100vh-280px)] overflow-auto font-mono text-xs">
            {isLoading && logs.length === 0 && (
              <p className="text-muted-foreground text-center py-8">Loading...</p>
            )}
            {!isLoading && logs.length === 0 && (
              <p className="text-muted-foreground text-center py-8">No log entries found</p>
            )}
            <table className="w-full">
              <tbody>
                {displayLogs.map((entry) => (
                  <tr
                    key={entry.id}
                    className={cn(
                      'border-b border-border/30 hover:bg-muted/30',
                      entry.level === 'ERROR' && 'bg-destructive/5',
                    )}
                  >
                    <td className="px-3 py-1 text-muted-foreground whitespace-nowrap align-top w-24">
                      {formatDate(entry.logged_at)}{formatTime(entry.logged_at)}
                    </td>
                    <td className="px-2 py-1 align-top w-16">
                      <Badge variant={levelBadgeVariant(entry.level)} className="text-[10px] px-1 py-0">
                        {entry.level}
                      </Badge>
                    </td>
                    <td className="px-2 py-1 text-muted-foreground/70 whitespace-nowrap align-top w-20 truncate max-w-[80px]">
                      {entry.user}
                    </td>
                    <td className="px-2 py-1 text-muted-foreground/50 whitespace-nowrap align-top w-32 truncate max-w-[130px]">
                      {entry.logger}
                    </td>
                    <td className={cn('px-2 py-1 break-all', levelColor(entry.level))}>
                      {entry.message}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div ref={bottomRef} />
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
