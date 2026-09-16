import { createContext, useContext } from 'react'
import type { AuthUser } from '@/lib/api'

// Who is signed in (2026-09-06 — the dashboard signs in a PERSON with their
// per-user bearer; the server holds the session). `isAdmin` only hides
// controls — every admin-only route is enforced server-side by
// `require_admin`, so a hidden button is a courtesy, not the gate.
export interface AuthState {
  user: AuthUser
  isAdmin: boolean
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export const AuthProvider = AuthContext.Provider

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth() outside <AuthProvider>')
  return ctx
}
