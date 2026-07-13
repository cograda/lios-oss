import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { api } from '@/lib/api'

interface LoginProps {
  onLogin: () => void
}

export default function Login({ onLogin }: LoginProps) {
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setLoading(true)
    setError('')
    try {
      await api.login(token)
      onLogin()
    } catch {
      setError('Invalid token')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <form onSubmit={handleSubmit} className="w-full max-w-sm space-y-4 p-6">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold">Home Services</h1>
          <p className="text-sm text-muted-foreground">Enter your access token</p>
        </div>
        <Input
          type="password"
          placeholder="Token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          autoFocus
        />
        {error && <p className="text-sm text-destructive">{error}</p>}
        <Button type="submit" disabled={loading || !token} className="w-full">
          {loading ? 'Checking...' : 'Sign in'}
        </Button>
      </form>
    </div>
  )
}
