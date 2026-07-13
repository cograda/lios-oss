import { useEffect, useState } from 'react'
import { Routes, Route, useLocation, Link } from 'react-router'
import { Home, Settings, RefreshCw, ScrollText } from 'lucide-react'
import { SidebarNav } from '@/components/layout/sidebar-nav'
import { ErrorBoundary } from '@/components/error-boundary'
import { api } from '@/lib/api'
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

export default function App() {
  const { pathname } = useLocation()
  const [authState, setAuthState] = useState<'loading' | 'login' | 'ok'>('loading')

  useEffect(() => {
    api.checkAuth()
      .then((r) => setAuthState(r.authenticated ? 'ok' : r.auth_required ? 'login' : 'ok'))
      .catch(() => setAuthState('login'))

    function handleLogout() { setAuthState('login') }
    window.addEventListener('auth:logout', handleLogout)
    return () => window.removeEventListener('auth:logout', handleLogout)
  }, [])

  if (authState === 'loading') return null
  if (authState === 'login') return <Login onLogin={() => setAuthState('ok')} />

  return (
    <div className="flex min-h-screen">
      <SidebarNav
        items={navItems}
        activePath={pathname}
        LinkComponent={RouterLink}
        header={<span className="text-xs font-bold uppercase tracking-widest text-muted-foreground">Home Services</span>}
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
  )
}
