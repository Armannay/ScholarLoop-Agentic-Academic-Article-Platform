// Shapes of the backend's responses. These mirror app/api/schemas.py.
// They are read-only views: nothing here can carry a credential.

export type HealthStatus = 'ok' | 'degraded'

export interface ProviderStatus {
  mock: boolean
  live_adapters_implemented: string[]
  accounts_configured: number
  live_providers_enabled: boolean
}

export interface HealthResponse {
  status: HealthStatus
  version: string
  contract_version: number
  env: string
  policies_loaded: boolean
  rubric_loaded: boolean
  providers: ProviderStatus
  config_errors: Record<string, string>
}

export interface RubricSummary {
  article_types: Record<string, string[]>
  gates: string[]
  criteria_weights: Record<string, number>
  weights_total: number
  abstract_word_range: { min: number; max: number }
}

export interface Policies {
  run_token_cap: number
  iteration_token_cap: number
  call_input_cap: number
  max_iterations: number
  max_parallel: number
  digest_max_tokens: number
  recent_window: number
  compact_trigger: number
  q_min: number
  plateau: { window: number; min_gain: number }
  regression: { delta: number; window: number }
  oscillation: { window: number }
  diminishing: { min_gain_per_10k: number; window: number }
  issue_churn: number
  error_streak: number
  model_tiers: Record<string, string>
}

export interface ConfigResponse {
  contract_version: number
  policies_path: string
  overrides_path: string | null
  project: string | null
  policies: Policies
  rubric: RubricSummary
}

export interface ContractListResponse {
  contract_version: number
  names: string[]
  schema_url_template: string
}
