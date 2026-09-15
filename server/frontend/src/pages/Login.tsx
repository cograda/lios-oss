import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { api, type AuthUser } from '@/lib/api'

interface LoginProps {
  onLogin: (user: AuthUser) => void
}

// The dashboard signs in a PERSON (2026-09-06): the token asked for here is
// the per-user lios bearer, the same one the daemon and MCP use. The server
// resolves it once and hands back a session cookie — the bearer itself is
// never stored in the browser beyond this form.
export default function Login({ onLogin }: LoginProps) {
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setLoading(true)
    setError('')
    try {
      const res = await api.login(token)
      setToken('')
      onLogin(res.user)
    } catch (err) {
      const message = err instanceof Error ? err.message : ''
      setError(message.startsWith('429') ? 'Too many attempts — wait a moment' : 'That token was not recognised')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <form onSubmit={handleSubmit} className="w-full max-w-sm space-y-4 p-6">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold">lios</h1>
          <p className="text-sm text-muted-foreground">Sign in with your lios token</p>
        </div>
        <Input
          type="password"
          placeholder="Your lios token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          autoComplete="current-password"
          autoFocus
        />
        <p className="text-xs text-muted-foreground leading-relaxed">
          The install script put it in <code className="font-mono">~/.config/lios/config.toml</code> under{' '}
          <code className="font-mono">[server] token</code>. No token yet? Ask the admin for one.
        </p>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <Button type="submit" disabled={loading || !token} className="w-full">
          {loading ? 'Checking...' : 'Sign in'}
        </Button>
      </form>
    </div>
  )
}
