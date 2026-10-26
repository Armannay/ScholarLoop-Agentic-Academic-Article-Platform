import { useState, type ReactNode } from 'react'
import { SchemaViewer } from './SchemaViewer'
import { StatusPill, type Tone } from './StatusPill'
import type { ConfigResponse, ContractListResponse, HealthResponse, Policies } from './types'

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

const EROSION_KEYS = [
  'plateau',
  'regression',
  'oscillation',
  'diminishing',
  'issue_churn',
  'error_streak',
] as const

export interface StatusConsoleProps {
  health: HealthResponse | null
  healthError: string | null
  config: ConfigResponse | null
  configError: string | null
  contracts: ContractListResponse | null
  contractsError: string | null
}

export function StatusConsole({
  health,
  healthError,
  config,
  configError,
  contracts,
  contractsError,
}: StatusConsoleProps): React.JSX.Element {
  const [selected, setSelected] = useState<string | null>(null)
  const policies: Policies | null = config?.policies ?? null

  return (
    <section className="grid">
      <Panel title="Runtime" subtitle="Loaded configuration and provider availability">
        {healthError && <p className="mono bad">{healthError}</p>}
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
            <dt>Account store</dt>
            <dd>{health.providers.account_store_ready === false ? 'sealed (missing APP_MASTER_KEY)' : 'ready'}</dd>
            <dt>Providers</dt>
            <dd>
              {health.providers.mock ? 'mock' : 'none'}
              {health.providers.live_adapters_implemented.length > 0
                ? ` + ${health.providers.live_adapters_implemented.join(', ')}`
                : ''}
            </dd>
          </dl>
        )}
      </Panel>

      <Panel
        title="Budgets"
        subtitle="Nested caps: one call < one iteration < the whole run"
        tone={policies ? 'ok' : undefined}
      >
        {configError && <p className="mono bad">{configError}</p>}
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
          !configError && <p className="dim">loading…</p>
        )}
      </Panel>

      <Panel title="Erosion detectors" subtitle="How the loop controller decides to stop improving">
        {policies ? (
          <>
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
            <p className="subtitle">
              Model tiers:{' '}
              {Object.entries(policies.model_tiers)
                .map(([role, tier]) => `${role}→${tier}`)
                .join(' · ')}
            </p>
          </>
        ) : (
          <p className="dim">loading…</p>
        )}
      </Panel>

      <Panel
        title="Rubric"
        subtitle={
          config
            ? `${config.rubric.gates.length} hard gates · criteria weights sum to ${config.rubric.weights_total}`
            : undefined
        }
        tone={configError ? 'bad' : config ? 'ok' : undefined}
      >
        {configError && <p className="mono bad">{configError}</p>}
        {!configError && !config && <p className="dim">loading…</p>}
        {config && (
          <>
            <h3>Hard gates</h3>
            <ul className="list mono">
              {config.rubric.gates.map((gate) => (
                <li key={gate}>{gate}</li>
              ))}
            </ul>
            <h3>Soft criteria</h3>
            <table className="table">
              <tbody>
                {Object.entries(config.rubric.criteria_weights)
                  .sort(([, left], [, right]) => right - left)
                  .map(([name, weight]) => (
                    <tr key={name}>
                      <th scope="row">{name}</th>
                      <td className="mono">{weight}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </>
        )}
      </Panel>

      <Panel
        title="Article types"
        subtitle={
          config
            ? `Abstract range: ${config.rubric.abstract_word_range.min}–${config.rubric.abstract_word_range.max} words`
            : undefined
        }
      >
        {!config ? (
          <p className="dim">loading…</p>
        ) : (
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
        )}
      </Panel>

      <Panel
        title="Contracts v1"
        subtitle="Pydantic v2 models exported to JSON Schema — the wire format every step shares"
        tone={contracts ? 'ok' : undefined}
      >
        {contractsError && <p className="mono bad">{contractsError}</p>}
        {contracts && (
          <>
            <div className="chips">
              {contracts.names.map((name) => (
                <button
                  key={name}
                  type="button"
                  className={name === selected ? 'chip chip-active' : 'chip'}
                  onClick={() => setSelected(name === selected ? null : name)}
                >
                  {name}
                </button>
              ))}
            </div>
            {selected && <SchemaViewer name={selected} />}
          </>
        )}
      </Panel>
    </section>
  )
}

function Panel({
  title,
  subtitle,
  children,
  tone,
}: {
  title: string
  subtitle?: string | undefined
  children: ReactNode
  tone?: Tone | undefined
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

function formatValue(value: unknown): string {
  if (typeof value === 'object' && value !== null) {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, nested]) => `${key}=${String(nested)}`)
      .join(' ')
  }
  return String(value)
}
