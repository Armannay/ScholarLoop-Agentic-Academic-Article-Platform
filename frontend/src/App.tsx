import { useCallback, useEffect, useState } from 'react'
import { AccountsPage } from './AccountsPage'
import { AgentsPage } from './AgentsPage'
import { ArticleViewer } from './ArticleViewer'
import { AuthPage } from './AuthPage'
import {
  fetchAllBindings,
  fetchConfig,
  fetchContracts,
  fetchHealth,
  isMockMode,
  listAccounts,
  setMockMode,
  subscribeMockMode,
} from './api'
import { AuthProvider, useAuth } from './auth'
import { NewProjectPage } from './NewProjectPage'
import { ProjectPage } from './ProjectPage'
import { ProjectsPage } from './ProjectsPage'
import { RunPage } from './RunPage'
import { StatusConsole } from './StatusConsole'
import { StatusPill } from './StatusPill'
import {
  PRIMARY_BINDING_ROLES,
  type AccountView,
  type BindingRole,
  type BindingView,
  type ConfigResponse,
  type ContractListResponse,
  type HealthResponse,
  type ProjectView,
  type RunView,
} from './types'
import { useAsync } from './useApi'
import './styles.css'

const HEALTH_POLL_MS = 5000

export type PageId =
  | 'accounts'
  | 'agents'
  | 'status'
  | 'projects'
  | 'project'
  | 'new-project'
  | 'run'
  | 'article'

export interface Route {
  page: PageId
  projectId?: string
  runId?: string
}

const DEFAULT_ROUTE: Route = { page: 'projects' }

const EMPTY_BINDINGS: Readonly<Record<BindingRole, BindingView | null>> = {
  planner: null,
  architect: null,
  researcher: null,
  writer: null,
  reviewer: null,
  validator: null,
  compactor: null,
}

/**
 * Parse the hash route.
 *
 * `#projects`, `#projects/<id>`, `#projects/new`, `#runs/<runId>`, `#runs/<runId>/article`,
 * plus the pre-existing `#accounts`, `#agents`, `#status`. Anything unrecognised falls
 * back to the project list rather than rendering a blank page.
 */
export function parseRoute(hash: string): Route {
  const cleaned = hash.replace(/^#\/?/, '')
  const [head, ...rest] = cleaned.split('/')
  if (!head) return DEFAULT_ROUTE
  if (head === 'accounts' || head === 'agents' || head === 'status') {
    return { page: head }
  }
  if (head === 'projects') {
    const segment = rest.join('/')
    if (!segment) return { page: 'projects' }
    if (segment === 'new') return { page: 'new-project' }
    return { page: 'project', projectId: decodeURIComponent(segment) }
  }
  if (head === 'runs' && rest.length > 0) {
    const runId = decodeURIComponent(rest[0] ?? '')
    if (!runId) return DEFAULT_ROUTE
    return rest[1] === 'article' ? { page: 'article', runId } : { page: 'run', runId }
  }
  return DEFAULT_ROUTE
}

export function routeToHash(route: Route): string {
  switch (route.page) {
    case 'projects':
      return '#projects'
    case 'new-project':
      return '#projects/new'
    case 'project':
      return `#projects/${encodeURIComponent(route.projectId ?? '')}`
    case 'run':
      return `#runs/${encodeURIComponent(route.runId ?? '')}`
    case 'article':
      return `#runs/${encodeURIComponent(route.runId ?? '')}/article`
    default:
      return `#${route.page}`
  }
}

function readHashRoute(): Route {
  if (typeof window === 'undefined') return DEFAULT_ROUTE
  return parseRoute(window.location.hash)
}

export function App(): React.JSX.Element {
  return (
    <AuthProvider>
      <AppShell />
    </AuthProvider>
  )
}

export function AppShell({ initialPage }: { initialPage?: PageId | undefined }): React.JSX.Element {
  const { user, loading: authLoading, logout } = useAuth()
  const [route, setRoute] = useState<Route>(() =>
    initialPage ? { page: initialPage } : readHashRoute(),
  )
  const [mockActive, setMockActive] = useState<boolean>(() => isMockMode())
  const [lastProject, setLastProject] = useState<ProjectView | null>(null)

  const loadHealth = useCallback((signal?: AbortSignal) => fetchHealth(signal), [])
  const loadConfig = useCallback((signal?: AbortSignal) => fetchConfig(signal), [])
  const loadContracts = useCallback((signal?: AbortSignal) => fetchContracts(signal), [])

  const health = useAsync<HealthResponse>(loadHealth, HEALTH_POLL_MS)
  const config = useAsync<ConfigResponse>(loadConfig)
  const contracts = useAsync<ContractListResponse>(loadContracts)

  const [accounts, setAccounts] = useState<AccountView[]>([])
  const [bindings, setBindings] = useState<Record<BindingRole, BindingView | null>>({
    ...EMPTY_BINDINGS,
  })
  const [workspaceLoading, setWorkspaceLoading] = useState(false)
  const [workspaceError, setWorkspaceError] = useState<string | null>(null)
  const [refreshTick, setRefreshTick] = useState(0)

  const refreshWorkspace = useCallback(() => {
    setRefreshTick((n) => n + 1)
    health.reload()
  }, [health])

  useEffect(() => {
    return subscribeMockMode((enabled) => {
      setMockActive(enabled)
      health.reload()
      config.reload()
      contracts.reload()
      setRefreshTick((n) => n + 1)
    })
  }, [health, config, contracts])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const onHash = (): void => setRoute(readHashRoute())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const navigate = useCallback((next: Route): void => {
    setRoute(next)
    if (typeof window !== 'undefined') {
      const hash = routeToHash(next)
      if (window.location.hash !== hash) {
        // replaceState keeps the back button out of a scroll-fight, and the hash stays
        // copy-pasteable: every screen has a URL.
        window.history.replaceState(null, '', hash)
      }
      window.scrollTo({ top: 0 })
    }
  }, [])

  useEffect(() => {
    if (!user) {
      setAccounts([])
      setBindings({ ...EMPTY_BINDINGS })
      setWorkspaceError(null)
      return
    }
    const controller = new AbortController()
    setWorkspaceLoading(true)
    Promise.all([listAccounts(controller.signal), fetchAllBindings(undefined, controller.signal)])
      .then(([accList, bindingMap]) => {
        if (controller.signal.aborted) return
        setAccounts(accList)
        setBindings(bindingMap)
        setWorkspaceError(null)
      })
      .catch((cause) => {
        if (controller.signal.aborted) return
        setWorkspaceError(cause instanceof Error ? cause.message : 'Failed to load workspace data')
      })
      .finally(() => {
        if (!controller.signal.aborted) setWorkspaceLoading(false)
      })
    return () => controller.abort()
  }, [user, refreshTick])

  const configuredPrimaryCount = PRIMARY_BINDING_ROLES.filter(
    (role) => bindings[role] !== null && bindings[role]?.enabled,
  ).length

  const toggleMock = (): void => {
    setMockMode(!mockActive)
  }

  const handleProjectCreated = (project: ProjectView): void => {
    setLastProject(project)
    navigate({ page: 'project', projectId: project.id })
  }

  const handleRunStarted = (run: RunView): void => {
    navigate({ page: 'run', runId: run.id })
  }

  const onProjectsTab = route.page === 'projects' || route.page === 'project' || route.page === 'new-project'

  const renderMain = (): React.JSX.Element => {
    if (route.page === 'status') {
      return (
        <StatusConsole
          health={health.data}
          healthError={health.error}
          config={config.data}
          configError={config.error}
          contracts={contracts.data}
          contractsError={contracts.error}
        />
      )
    }
    if (authLoading) {
      return (
        <section className="panel">
          <p className="dim">Restoring session…</p>
        </section>
      )
    }
    if (!user) return <AuthPage mockMode={mockActive} />

    switch (route.page) {
      case 'accounts':
        return (
          <AccountsPage
            accounts={accounts}
            bindings={bindings}
            loading={workspaceLoading}
            error={workspaceError}
            onRefresh={refreshWorkspace}
          />
        )
      case 'agents':
        return (
          <AgentsPage
            accounts={accounts}
            bindings={bindings}
            policies={config.data?.policies ?? null}
            loading={workspaceLoading}
            onRefresh={refreshWorkspace}
            onOpenAccounts={() => navigate({ page: 'accounts' })}
          />
        )
      case 'new-project':
        return (
          <NewProjectPage
            onCreated={handleProjectCreated}
            onCancel={() => navigate({ page: 'projects' })}
            onRunStarted={handleRunStarted}
          />
        )
      case 'project':
        return route.projectId ? (
          <ProjectPage
            projectId={route.projectId}
            onOpenRun={(runId) => navigate({ page: 'run', runId })}
            onBack={() => navigate({ page: 'projects' })}
            onNewProject={() => navigate({ page: 'new-project' })}
          />
        ) : (
          <ProjectsPage
            onOpenProject={(projectId) => navigate({ page: 'project', projectId })}
            onNewProject={() => navigate({ page: 'new-project' })}
          />
        )
      case 'run':
        return route.runId ? (
          <RunPage
            runId={route.runId}
            onOpenArticle={(runId) => navigate({ page: 'article', runId })}
            onBack={() => navigate({ page: 'projects' })}
          />
        ) : (
          <ProjectsPage
            onOpenProject={(projectId) => navigate({ page: 'project', projectId })}
            onNewProject={() => navigate({ page: 'new-project' })}
          />
        )
      case 'article':
        return route.runId ? (
          <ArticleViewer
            runId={route.runId}
            brief={lastProject?.brief ?? null}
            onBack={() => {
              if (route.runId) navigate({ page: 'run', runId: route.runId })
            }}
          />
        ) : (
          <ProjectsPage
            onOpenProject={(projectId) => navigate({ page: 'project', projectId })}
            onNewProject={() => navigate({ page: 'new-project' })}
          />
        )
      default:
        return (
          <ProjectsPage
            onOpenProject={(projectId) => navigate({ page: 'project', projectId })}
            onNewProject={() => navigate({ page: 'new-project' })}
          />
        )
    }
  }

  return (
    <div className="app">
      <header className="masthead">
        <div>
          <h1>ScholarLoop</h1>
          <p className="tagline">
            Deterministic orchestrator · planning-only Planner · five executing agents
          </p>
        </div>

        <div className="masthead-status">
          <StatusPill
            tone={
              mockActive
                ? 'ok'
                : health.data?.status === 'ok'
                  ? 'ok'
                  : health.data
                    ? 'warn'
                    : health.error
                      ? 'bad'
                      : 'idle'
            }
            label={
              mockActive
                ? 'Mock API active'
                : health.error
                  ? 'API unreachable'
                  : health.data
                    ? `API ${health.data.status}`
                    : 'checking API…'
            }
          />
          {health.data && <span className="mono dim">contract v{health.data.contract_version}</span>}
          <button
            type="button"
            className={mockActive ? 'btn btn-sm btn-mock-on' : 'btn btn-sm'}
            onClick={toggleMock}
            title="Switch between live /api proxy and in-memory OpenAPI mock server"
          >
            {mockActive ? 'Mock API: On' : 'Mock API: Off'}
          </button>
          {user && (
            <div className="user-bar">
              <span className="mono small">{user.email}</span>
              <button type="button" className="btn btn-sm" onClick={logout}>
                Sign out
              </button>
            </div>
          )}
        </div>
      </header>

      <nav className="top-nav" aria-label="Primary navigation">
        <button
          type="button"
          className={onProjectsTab ? 'nav-tab nav-tab-active' : 'nav-tab'}
          onClick={() => navigate({ page: 'projects' })}
          aria-current={onProjectsTab ? 'page' : undefined}
        >
          Projects
        </button>
        <button
          type="button"
          className={route.page === 'accounts' ? 'nav-tab nav-tab-active' : 'nav-tab'}
          onClick={() => navigate({ page: 'accounts' })}
          aria-current={route.page === 'accounts' ? 'page' : undefined}
        >
          Accounts
          {user && <span className="nav-count mono">{accounts.length}</span>}
        </button>
        <button
          type="button"
          className={route.page === 'agents' ? 'nav-tab nav-tab-active' : 'nav-tab'}
          onClick={() => navigate({ page: 'agents' })}
          aria-current={route.page === 'agents' ? 'page' : undefined}
        >
          Agents
          {user && (
            <span className="nav-count mono">
              {configuredPrimaryCount}/{PRIMARY_BINDING_ROLES.length}
            </span>
          )}
        </button>
        <button
          type="button"
          className={route.page === 'status' ? 'nav-tab nav-tab-active' : 'nav-tab'}
          onClick={() => navigate({ page: 'status' })}
          aria-current={route.page === 'status' ? 'page' : undefined}
        >
          Status &amp; Schemas
        </button>
      </nav>

      {!mockActive && health.error && (
        <section className="panel panel-bad banner-panel">
          <div className="panel-head-wrap">
            <div>
              <h2>Backend not reachable</h2>
              <p className="mono small">{health.error}</p>
              <p className="subtitle">
                Start the FastAPI backend with <code>make run</code> on port 8000, or enable the
                built-in OpenAPI Mock Server so UI work is never blocked.
              </p>
            </div>
            <button type="button" className="btn btn-primary" onClick={() => setMockMode(true)}>
              Enable Mock API
            </button>
          </div>
        </section>
      )}

      {!mockActive && health.data && health.data.providers.account_store_ready === false && (
        <section className="panel panel-warn banner-panel">
          <div className="panel-head-wrap">
            <div>
              <h2>Account store is sealed</h2>
              <p className="subtitle">
                <code>APP_MASTER_KEY</code> is not set on the backend, so encrypting/decrypting
                credentials and signing JWTs are disabled. Configure <code>APP_MASTER_KEY</code> in{' '}
                <code>.env</code> or switch to Mock API mode.
              </p>
            </div>
            <button type="button" className="btn" onClick={() => setMockMode(true)}>
              Use Mock API
            </button>
          </div>
        </section>
      )}

      {health.data && health.data.status === 'degraded' && (
        <section className="panel panel-warn banner-panel">
          <h2>Configuration degraded</h2>
          <ul className="mono">
            {Object.entries(health.data.config_errors).map(([file, message]) => (
              <li key={file}>
                <strong>{file}</strong>: {message}
              </li>
            ))}
          </ul>
        </section>
      )}

      <main className="main-area">{renderMain()}</main>

      <footer className="footer">
        <span>
          Config source: <code className="mono">{config.data?.policies_path ?? '—'}</code>
          {config.data?.overrides_path ? (
            <>
              {' '}
              + overrides <code className="mono">{config.data.overrides_path}</code>
              {config.data.project ? ` (project: ${config.data.project})` : ''}
            </>
          ) : null}
        </span>
        <span className="dim">
          HARD RULE 2: keys are write-only; only 4-char masks and 12-char ciphertext fingerprints
          are ever rendered. Sessions live in sessionStorage, never in localStorage.
        </span>
      </footer>
    </div>
  )
}
