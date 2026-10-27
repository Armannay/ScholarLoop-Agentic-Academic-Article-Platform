import { AuthProvider } from './api/auth'
import { ErrorBoundary } from './components/ErrorBoundary'
import { ToastProvider, ToastList } from './components/Toast'
import { Router } from './router'
import { Layout } from './layout'
import { Login } from './pages/login'
import { Home } from './pages/home'
import { useAuth } from './api/auth'

function ErrorFallback({ error, reset }: { error: Error; reset: () => void }): React.JSX.Element {
  return (
    <section className="panel panel-bad">
      <h2>Something went wrong</h2>
      <p className="mono bad">{error.message}</p>
      <button type="button" className="btn btn-primary" onClick={reset}>
        Try again
      </button>
    </section>
  )
}

function ShellRoutes(): React.JSX.Element {
  const { user } = useAuth()
  return (
    <Layout>
      <Router
        routes={[
          { path: '/', element: user ? <Home /> : <Login /> },
          { path: '/login', element: <Login /> },
        ]}
      />
    </Layout>
  )
}

export function App(): React.JSX.Element {
  return (
    <AuthProvider>
      <ErrorBoundary fallback={ErrorFallback}>
        <ToastProvider>
          <ToastList />
          <ShellRoutes />
        </ToastProvider>
      </ErrorBoundary>
    </AuthProvider>
  )
}
