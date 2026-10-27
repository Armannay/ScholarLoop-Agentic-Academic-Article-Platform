import { useCallback, type ReactNode } from 'react'
import { useAuth } from './api/auth'
import { useHash, navigate } from './router'
import { Badge } from './components'

export interface LayoutProps {
  children: ReactNode
}

export function Layout({ children }: LayoutProps): React.JSX.Element {
  const { user, loading: authLoading, logout, isAuthenticated } = useAuth()
  const hash = useHash()

  const active = (path: string): boolean => hash === '' || hash === '/' ? path === '/' : hash === path

  const signOut = useCallback((): void => {
    logout()
    navigate('/')
  }, [logout])

  return (
    <div className="shell">
      <header className="masthead">
        <div>
          <h1>ScholarLoop</h1>
          <p className="tagline">Console shell · typed OpenAPI client · in-memory session</p>
        </div>
        <div className="masthead-status">
          {authLoading ? (
            <Badge variant="idle">Restoring session…</Badge>
          ) : isAuthenticated ? (
            <>
              <Badge variant="ok">Signed in as {user?.email}</Badge>
              <button type="button" className="btn btn-sm" onClick={signOut}>
                Sign out
              </button>
            </>
          ) : (
            <Badge variant="warn">Not signed in</Badge>
          )}
        </div>
      </header>

      <nav className="top-nav" aria-label="Primary navigation">
        <button
          type="button"
          className={active('/') ? 'nav-tab nav-tab-active' : 'nav-tab'}
          onClick={() => navigate('/')}
        >
          Home
        </button>
        {!isAuthenticated && (
          <button
            type="button"
            className={active('/login') ? 'nav-tab nav-tab-active' : 'nav-tab'}
            onClick={() => navigate('/login')}
          >
            Sign in
          </button>
        )}
      </nav>

      <main className="shell-main">{children}</main>
    </div>
  )
}

export default Layout
