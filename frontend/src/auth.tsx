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
  subscribeMockMode,
  subscribeUnauthorized,
} from './api'
import type { UserView } from './types'

export interface AuthContextValue {
  user: UserView | null
  loading: boolean
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
  const [user, setUser] = useState<UserView | null>(initialUser)
  const [loading, setLoading] = useState<boolean>(() => initialUser === null && getAccessToken() !== null)
  const [sessionNotice, setSessionNotice] = useState<string | null>(null)
  const [modeEpoch, setModeEpoch] = useState(0)

  useEffect(() => {
    return subscribeUnauthorized(() => {
      setAccessToken(null)
      setUser(null)
      setSessionNotice('Your session expired or is not valid for the active API target. Sign in again.')
    })
  }, [])

  useEffect(() => {
    return subscribeMockMode(() => {
      setModeEpoch((n) => n + 1)
    })
  }, [])

  useEffect(() => {
    if (initialUser !== null && modeEpoch === 0) return
    const token = getAccessToken()
    if (!token) {
      setUser(null)
      setLoading(false)
      return
    }
    const controller = new AbortController()
    setLoading(true)
    fetchCurrentUser(controller.signal)
      .then((me) => {
        if (controller.signal.aborted) return
        setUser(me)
        setSessionNotice(null)
      })
      .catch(() => {
        if (controller.signal.aborted) return
        setAccessToken(null)
        setUser(null)
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [initialUser, modeEpoch])

  const login = useCallback(async (email: string, password: string): Promise<UserView> => {
    const res = await loginUser({ email, password })
    setAccessToken(res.access_token)
    setUser(res.user)
    setSessionNotice(null)
    return res.user
  }, [])

  const register = useCallback(async (email: string, password: string): Promise<UserView> => {
    const res = await registerUser({ email, password })
    setAccessToken(res.access_token)
    setUser(res.user)
    setSessionNotice(null)
    return res.user
  }, [])

  const logout = useCallback(() => {
    setAccessToken(null)
    setUser(null)
    setSessionNotice(null)
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({ user, loading, sessionNotice, login, register, logout }),
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
