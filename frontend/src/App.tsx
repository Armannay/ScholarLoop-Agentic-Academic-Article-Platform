import { useCallback, useState } from 'react'
import { fetchConfig, fetchContracts, fetchHealth } from './api'
import { useAsync } from './useApi'
import type { ConfigResponse, ContractListResponse, HealthResponse, Policies } from './types'
import { AccountsPanel } from './AccountsPanel'
import { SchemaViewer } from './SchemaViewer'
import { StatusPill } from './StatusPill'
import './styles.css'

const HEALTH_POLL_MS = 5000
const BUDGET_KEYS = [
  'run_token_cap',
  'iteration_token_cap',
  'call_input_cap',
  'max_iterations',
  'max_parallel',
  'digest_max_tokens',
  'recent_window',
  'compact_trigger',
  'q_min',
] as const

const EROSION_KEYS = ['plateau', 'regression', 'oscillation', 'diminishing', 'issue_churn', 'error_streak'] as const

export function App(): React.JSX.Element {
  const loadHealth = useCallback((signal?: AbortSignal) => fetchHealth(signal), [])
  const loadConfig = useCallback((signal?: AbortSignal) => fetchConfig(signal), [])
  const loadContracts = useCallback((signal?: AbortSignal) => fetchContracts(signal), [])

  const health = useAsync<HealthResponse>(loadHealth, HEALTH_POLL_MS)
  const config = useAsync<ConfigResponse>(loadConfig)
  const contracts = useAsync<ContractListResponse>(loadContracts)
  const [selected, setSelected] = useState<string | null>(null)

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
            tone={health.data?.status === 'ok' ? 'ok' : health.data ? 'warn' : health.error ? 'bad' : 'idle'}
            label={
              health.error
                ? 'API unreachable'
                : health.data
                  ? `API ${health.data.status}`
                  : 'checking API…'
            }
          />
          {health.data && <span className="mono dim">contract v{health.data.contract_version}</span>}
        </div>
      </header>

      {health.error && (
        <section className="panel panel-bad">
          <h2>Backend not reachable</h2>
          <p className="mono">{health.error}</p>
          <p>
            The UI calls <code>/api</code> through the dev-server proxy. Start the API with{' '}
            <code>make run</code> (port 8000) and reload.
          </p>
        </section>
      )}

      {health.data && health.data.status === 'degraded' && (
        <section className="panel panel-warn">
          <h2>Configuration degraded</h2>
          <p>
            The service is up but a config file failed to load, so the loop would not obey the
            numbers below. Fix the file and restart.
          </p>
          <ul className="mono">
            {Object.entries(health.data.config_errors).map(([file, message]) => (
              <li key={file}>
                <strong>{file}</strong>: {message}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="grid">
        <RuntimePanel health={health.data} error={health.error} />
        <AccountsPanel />
        <BudgetPanel policies={config.data?.policies ?? null} error={config.error} />
        <ErosionPanel policies={config.data?.policies ?? null} />
        <RubricPanel config={config.data} error={config.error} />
        <ArticleTypesPanel config={config.data} />
        <ContractsPanel
          contracts={contracts.data}
          error={contracts.error}
          selected={selected}
          onSelect={setSelected}
        />
      </section>

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
          No key material is ever rendered here: the API exposes account fingerprints only.
        </span>
      </footer>
    </div>
  )
}

function Panel({
  title,
  subtitle,
  children,
  tone,
}: {
  title: string
  subtitle?: string
  children: React.ReactNode
  tone?: 'ok' | 'warn' | 'bad' | 'idle' | undefined
}): React.JSX.Element {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>{title}</h2>
        {tone && <StatusPill tone={tone} label="" />}
      </div>
      {subtitle && <p className="subtitle">{subtitle}</p>}
      {children}
    </section>
  )
}

function RuntimePanel({
  health,
  error,
}: {
  health: HealthResponse | null
  error: string | null
}): React.JSX.Element {
  return (
    <Panel title="Runtime" subtitle="Loaded configuration and provider availability">
      {error && <p className="mono bad">{error}</p>}
      {health && (
        <dl className="kv">
          <dt>Status</dt>
          <dd>{health.status}</dd>
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
          <dt>Manual digest</dt>
          <dd>{health.rubric_loaded ? 'claude.md ≤ 2000 tokens' : '—'}</dd>
          <dt>Provider</dt>
          <dd>
            {health.providers.mock ? 'mock (deterministic)' : 'none'}
            {health.providers.live_adapters_implemented.length === 0
              ? ' — live adapters not implemented yet'
              : ` + ${health.providers.live_adapters_implemented.join(', ')}`}
          </dd>
        </dl>
      )}
    </Panel>
  )
}

function BudgetPanel({
  policies,
  error,
}: {
  policies: Policies | null
  error: string | null
}): React.JSX.Element {
  return (
    <Panel
      title="Budgets"
      subtitle="Nested caps: one call < one iteration < the whole run"
      tone={policies ? 'ok' : undefined}
    >
      {error && <p className="mono bad">{error}</p>}
      {policies ? (
        <table className="table">
          <tbody>
            {BUDGET_KEYS.map((key) => (
              <tr key={key}>
                <th scope="row">{key}</th>
                <td className="mono">{formatValue(policies[key])}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        !error && <p className="dim">loading…</p>
      )}
    </Panel>
  )
}

function ErosionPanel({ policies }: { policies: Policies | null }): React.JSX.Element {
  return (
    <Panel title="Erosion detectors" subtitle="How the loop controller decides to stop improving">
      {policies ? (
        <table className="table">
          <tbody>
            {EROSION_KEYS.map((key) => (
              <tr key={key}>
                <th scope="row">{key}</th>
                <td className="mono">{formatValue(policies[key])}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="dim">loading…</p>
      )}
      {policies && (
        <p className="subtitle">
          Model tiers: {Object.entries(policies.model_tiers).map(([role, tier]) => `${role}→${tier}`).join(' · ')}
        </p>
      )}
    </Panel>
  )
}

function RubricPanel({
  config,
  error,
}: {
  config: ConfigResponse | null
  error: string | null
}): React.JSX.Element {
  if (error) {
    return (
      <Panel title="Rubric" tone="bad">
        <p className="mono bad">{error}</p>
      </Panel>
    )
  }
  if (!config) {
    return (
      <Panel title="Rubric">
        <p className="dim">loading…</p>
      </Panel>
    )
  }
  const { rubric } = config
  return (
    <Panel
      title="Rubric"
      subtitle={`${rubric.gates.length} hard gates · criteria weights sum to ${rubric.weights_total}`}
      tone="ok"
    >
      <h3>Hard gates</h3>
      <ul className="list mono">
        {rubric.gates.map((gate) => (
          <li key={gate}>{gate}</li>
        ))}
      </ul>
      <h3>Soft criteria</h3>
      <table className="table">
        <tbody>
          {Object.entries(rubric.criteria_weights)
            .sort(([, left], [, right]) => right - left)
            .map(([name, weight]) => (
              <tr key={name}>
                <th scope="row">{name}</th>
                <td className="mono">{weight}</td>
              </tr>
            ))}
        </tbody>
      </table>
    </Panel>
  )
}

function ArticleTypesPanel({ config }: { config: ConfigResponse | null }): React.JSX.Element {
  if (!config) {
    return (
      <Panel title="Article types">
        <p className="dim">loading…</p>
      </Panel>
    )
  }
  return (
    <Panel
      title="Article types"
      subtitle={`Abstract range: ${config.rubric.abstract_word_range.min}–${config.rubric.abstract_word_range.max} words`}
    >
      <table className="table">
        <tbody>
          {Object.entries(config.rubric.article_types).map(([type, sections]) => (
            <tr key={type}>
              <th scope="row">{type}</th>
              <td className="mono">{sections.join(' → ')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  )
}

function ContractsPanel({
  contracts,
  error,
  selected,
  onSelect,
}: {
  contracts: ContractListResponse | null
  error: string | null
  selected: string | null
  onSelect: (name: string | null) => void
}): React.JSX.Element {
  return (
    <Panel
      title="Contracts v1"
      subtitle="Pydantic v2 models exported to JSON Schema — the wire format every step shares"
      tone={contracts ? 'ok' : undefined}
    >
      {error && <p className="mono bad">{error}</p>}
      {contracts && (
        <>
          <div className="chips">
            {contracts.names.map((name) => (
              <button
                key={name}
                type="button"
                className={name === selected ? 'chip chip-active' : 'chip'}
                onClick={() => onSelect(name === selected ? null : name)}
              >
                {name}
              </button>
            ))}
          </div>
          {selected && <SchemaViewer name={selected} />}
        </>
      )}
    </Panel>
  )
}

function formatValue(value: unknown): string {
  if (typeof value === 'object' && value !== null) {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, nested]) => `${key}=${String(nested)}`)
      .join(' ')
  }
  return String(value)
}
