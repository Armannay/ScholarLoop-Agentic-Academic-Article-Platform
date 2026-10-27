import { AuthProvider, useAuth } from './api/auth'
import { ErrorBoundary } from './components/ErrorBoundary'
import { ToastList, ToastProvider } from './components/Toast'
import { Layout } from './layout'
import { Article } from './pages/article'
import { Home } from './pages/home'
import { Login } from './pages/login'
import { NewProject } from './pages/new-project'
import { Project } from './pages/project'
import { Projects } from './pages/projects'
import { Run } from './pages/run'
import { navigate, Router, type Route } from './router'

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

/**
 * The route table.
 *
 * Every authenticated screen is reachable by URL (`#/projects`, `#/projects/<id>`,
 * `#/runs/<id>`, `#/runs/<id>/article`), which is what makes back/forward, a copied link
 * and the Playwright smoke test all work the same way. An unauthenticated visitor sees
 * the login page for any route, so a stale link never renders an empty shell.
 */
function routesFor(authenticated: boolean): Route[] {
  const guarded = (element: React.JSX.Element): React.JSX.Element =>
    authenticated ? element : <Login />

  return [
    { path: '/', element: authenticated ? <Home /> : <Login /> },
    { path: '/login', element: <Login /> },
    {
      path: '/projects',
      element: guarded(
        <Projects
          onOpenProject={(projectId) => navigate(`/projects/${projectId}`)}
          onNewProject={() => navigate('/projects/new')}
        />,
      ),
    },
    {
      path: '/projects/new',
      element: guarded(
        <NewProject
          onCreated={(project) => navigate(`/projects/${project.id}`)}
          onCancel={() => navigate('/projects')}
          onRunStarted={(run) => navigate(`/runs/${run.id}`)}
        />,
      ),
    },
    {
      path: '/projects/:projectId',
      element: (params) =>
        guarded(
          <Project
            projectId={params.projectId ?? ''}
            onOpenRun={(runId) => navigate(`/runs/${runId}`)}
            onBack={() => navigate('/projects')}
            onNewProject={() => navigate('/projects/new')}
          />,
        ),
    },
    {
      path: '/runs/:runId',
      element: (params) =>
        guarded(
          <Run
            runId={params.runId ?? ''}
            onOpenArticle={(runId) => navigate(`/runs/${runId}/article`)}
            onBack={() => navigate('/projects')}
          />,
        ),
    },
    {
      path: '/runs/:runId/article',
      element: (params) =>
        guarded(
          <Article runId={params.runId ?? ''} onBack={() => navigate(`/runs/${params.runId ?? ''}`)} />,
        ),
    },
  ]
}

function ShellRoutes(): React.JSX.Element {
  const { user } = useAuth()
  return (
    <Layout>
      <Router routes={routesFor(Boolean(user))} />
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
