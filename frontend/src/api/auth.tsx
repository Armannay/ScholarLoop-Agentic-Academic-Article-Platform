// Auth session: bearer token kept in memory only.
// Nothing is written to localStorage or cookies, so closing the tab ends the
// session and the user must sign in again. Nothing here ever touches key material.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import {
  fetchCurrentUser,
  getAccessToken,
  loginUser,
  registerUser,
  setAccessToken,
  subscribeToken,
  subscribeUnauthorized,
} from './client'
import { ApiError } from './client'
import type { UserView } from './client'

export interface AuthContextValue {
  user: UserView | null
  /** True while /me is being resolved after a token appeared. */
  loading: boolean
  isAuthenticated: boolean
  sessionNotice: string | null
  login: (email: string, password: string) => Promise<UserView>
  register: (email: string, password: string) => Promise<UserView>
  logout: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({
  children,
  initialUser = null,
}: {
  children: ReactNode
  initialUser?: UserView | null | undefined
}): React.JSX.Element {
  const [token, setToken] = useState<string | null>(() => getAccessToken())
  const [user, setUser] = useState<UserView | null>(initialUser ?? null)
  const [loading, setLoading] = useState<boolean>(() => token !== null && user === null)
  const [sessionNotice, setSessionNotice] = useState<string | null>(null)

  useEffect(() => {
    return subscribeToken((next) => setToken(next))
  }, [])

  useEffect(() => {
    return subscribeUnauthorized(() => {
      setToken(null)
      setUser(null)
      setSessionNotice(
        'Your session expired or is not valid for the active API target. Sign in again.',
      )
    })
  }, [])

  // Resolve the caller behind the token once, on boot or when a new token appears.
  useEffect(() => {
    if (token === null || user !== null) return
    let cancelled = false
    setLoading(true)
    fetchCurrentUser()
      .then((me) => {
        if (cancelled) return
        setUser(me)
        setSessionNotice(null)
      })
      .catch((cause) => {
        if (cancelled) return
        if (cause instanceof ApiError && cause.status === 401) {
          setAccessToken(null)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [token])

  const login = useCallback(
    async (email: string, password: string): Promise<UserView> => {
      const res = await loginUser({ email, password })
      setAccessToken(res.access_token)
      setUser(res.user)
      setSessionNotice(null)
      setLoading(false)
      return res.user
    },
    [],
  )

  const register = useCallback(
    async (email: string, password: string): Promise<UserView> => {
      const res = await registerUser({ email, password })
      setAccessToken(res.access_token)
      setUser(res.user)
      setSessionNotice(null)
      setLoading(false)
      return res.user
    },
    [],
  )

  const logout = useCallback((): void => {
    setAccessToken(null)
    setUser(null)
    setSessionNotice(null)
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      loading,
      isAuthenticated: user !== null,
      sessionNotice,
      login,
      register,
      logout,
    }),
    [user, loading, sessionNotice, login, register, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth must be used inside <AuthProvider>')
  }
  return ctx
}
