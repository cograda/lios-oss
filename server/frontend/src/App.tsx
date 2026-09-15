import { useEffect, useState } from 'react'
import { Routes, Route, useLocation, Link } from 'react-router'
import { Home, Settings, RefreshCw, ScrollText, LogOut } from 'lucide-react'
import { SidebarNav } from '@/components/layout/sidebar-nav'
import { ErrorBoundary } from '@/components/error-boundary'
import { api, type AuthUser } from '@/lib/api'
import { AuthProvider } from '@/lib/auth'
import Login from '@/pages/Login'
import Dashboard from '@/pages/Dashboard'
import Integrations from '@/pages/Integrations'
import IntegrationDetail from '@/pages/IntegrationDetail'
import Logs from '@/pages/Logs'
import SettingsPage from '@/pages/Settings'

// Adapter: SidebarNav passes `href` but react-router Link expects `to`
function RouterLink({ href, ...props }: { href: string } & React.AnchorHTMLAttributes<HTMLAnchorElement>) {
  return <Link to={href} {...props} />
}

const navItems = [
  { href: '/', icon: Home, label: 'Dashboard' },
  { href: '/integrations', icon: RefreshCw, label: 'Integrations' },
  { href: '/logs', icon: ScrollText, label: 'Logs' },
  { href: '/settings', icon: Settings, label: 'Settings' },
]

type AuthState = { kind: 'loading' } | { kind: 'login' } | { kind: 'ok'; user: AuthUser }

export default function App() {
  const { pathname } = useLocation()
  const [auth, setAuth] = useState<AuthState>({ kind: 'loading' })

  useEffect(() => {
    api.checkAuth()
      .then((r) => setAuth(r.authenticated && r.user ? { kind: 'ok', user: r.user } : { kind: 'login' }))
      .catch(() => setAuth({ kind: 'login' }))

    function handleLogout() { setAuth({ kind: 'login' }) }
    window.addEventListener('auth:logout', handleLogout)
    return () => window.removeEventListener('auth:logout', handleLogout)
  }, [])

  if (auth.kind === 'loading') return null
  if (auth.kind === 'login') return <Login onLogin={(user) => setAuth({ kind: 'ok', user })} />

  const user = auth.user

  async function signOut() {
    try { await api.logout() } finally { setAuth({ kind: 'login' }) }
  }

  return (
    <AuthProvider value={{ user, isAdmin: user.is_admin, signOut }}>
      <div className="flex min-h-screen">
        <SidebarNav
          items={navItems}
          activePath={pathname}
          LinkComponent={RouterLink}
          header={
            <div className="space-y-2">
              <span className="text-xs font-bold uppercase tracking-widest text-muted-foreground">lios</span>
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium truncate" title={user.name}>
                  {user.display_name}
                  {user.is_admin && <span className="ml-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">admin</span>}
                </span>
                <button
                  type="button"
                  onClick={signOut}
                  className="text-muted-foreground hover:text-foreground"
                  title="Sign out"
                  aria-label="Sign out"
                >
                  <LogOut className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          }
        />
        <main className="flex-1 p-6 overflow-auto">
          <ErrorBoundary>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/integrations" element={<Integrations />} />
              <Route path="/integrations/:name" element={<IntegrationDetail />} />
              <Route path="/logs" element={<Logs />} />
              <Route path="/settings" element={<SettingsPage />} />
            </Routes>
          </ErrorBoundary>
        </main>
      </div>
    </AuthProvider>
  )
}
