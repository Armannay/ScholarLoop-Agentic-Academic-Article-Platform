import { useCallback, useEffect, useState } from 'react'
import { fetchHealth } from '../api/client'
import { Badge } from '../components'
import type { HealthResponse } from '../api/client'

export function Home(): React.JSX.Element {
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  const tick = useCallback((): void => {
    fetchHealth()
      .then((h) => {
        setHealth(h)
        setError(null)
      })
      .catch((cause) => {
        setError(cause instanceof Error ? cause.message : 'health check failed')
      })
  }, [])

  useEffect(() => {
    const timer = setInterval(tick, 5000)
    return () => clearInterval(timer)
  }, [tick])

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Console home</h2>
      </div>
      <p className="subtitle">
        Projects own a Brief; every run is one attempt at the article, followed live over
        Server-Sent Events and answered at its checkpoints.
      </p>
      {error && <p className="mono bad">{error}</p>}
      {health ? (
        <>
          <dl className="kv">
            <dt>Status</dt>
            <dd>
              <Badge variant={health.status === 'ok' ? 'ok' : 'warn'}>{health.status}</Badge>
            </dd>
            <dt>Environment</dt>
            <dd>{health.env}</dd>
            <dt>App version</dt>
            <dd>{health.version}</dd>
            <dt>Contracts</dt>
            <dd>v{health.contract_version}</dd>
            <dt>Policies</dt>
            <dd>{health.policies_loaded ? 'loaded' : 'failed to load'}</dd>
            <dt>Rubric</dt>
            <dd>{health.rubric_loaded ? 'loaded' : 'failed to load'}</dd>
            <dt>Account store</dt>
            <dd>
              {health.providers.account_store_ready === false ? 'sealed' : 'ready'}
            </dd>
            <dt>Providers</dt>
            <dd>
              {health.providers.mock ? 'mock' : 'none'}
              {health.providers.live_adapters_implemented.length > 0
                ? ` + ${health.providers.live_adapters_implemented.join(', ')}`
                : ''}
            </dd>
          </dl>
          <p className="subtitle">
            Point the client at a live backend with <code>VITE_API_BASE_URL</code>, or keep the
            dev proxy aimed at the Prism mock on port 4010.
          </p>
        </>
      ) : (
        <p className="dim">loading…</p>
      )}
      <div className="btn-row" style={{ marginTop: 12 }}>
        <a className="btn btn-primary" href="#/projects">
          Go to projects
        </a>
        <a className="btn" href="#/projects/new">
          New project
        </a>
      </div>
    </section>
  )
}
