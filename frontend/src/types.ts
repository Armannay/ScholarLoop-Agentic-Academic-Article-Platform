// Wire shapes for ScholarLoop REST and SSE endpoints (OpenAPI 3.1 + CONTRACTS v1).
// Plaintext API keys are write-only on AccountCreate/AccountPatch and never appear in views.

export type HealthStatus = 'ok' | 'degraded'

export interface ProviderStatus {
  mock: boolean
  live_adapters_implemented: string[]
  accounts_configured: number
  live_providers_enabled: boolean
  account_store_ready?: boolean | undefined
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

// --- Error shapes ----------------------------------------------------------

export interface ValidationFieldError {
  loc: string
  msg: string
  type: string
}

export interface ApiErrorBody {
  code: string
  message: string
  context?: Record<string, unknown> | undefined
}

// --- Auth ------------------------------------------------------------------

export interface RegisterRequest {
  email: string
  password: string
}

export interface LoginRequest {
  email: string
  password: string
}

export interface UserView {
  id: string
  email: string
  created_at: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
  user: UserView
}

// --- Accounts --------------------------------------------------------------

export const ACCOUNT_PROVIDERS = ['anthropic', 'openai_compatible', 'mock'] as const
export type AccountProvider = (typeof ACCOUNT_PROVIDERS)[number]

export const ACCOUNT_STATUSES = ['active', 'degraded', 'disabled'] as const
export type AccountStatus = (typeof ACCOUNT_STATUSES)[number]

export interface BudgetPatch {
  tokens?: number | null | undefined
  usd?: number | null | undefined
}

export interface AccountCreate {
  provider: AccountProvider
  label: string
  /** Write-only credential; encrypted at rest and never echoed back. */
  api_key: string
  base_url?: string | null | undefined
  budget?: BudgetPatch | undefined
}

export interface AccountPatch {
  /** Omit to keep the current key; provide a new string to rotate. */
  api_key?: string | undefined
  label?: string | undefined
  base_url?: string | null | undefined
  budget?: BudgetPatch | null | undefined
  status?: AccountStatus | undefined
}

export interface AccountView {
  id: string
  owner_id: string
  provider: AccountProvider
  label: string
  status: AccountStatus
  base_url: string | null
  key_fingerprint: string
  key_mask: string
  budget: {
    tokens: number | null
    usd: number | null
  }
  created_at: string
  updated_at: string
}

export interface DeleteResult {
  deleted: boolean
  account_id: string
  disabled_roles: string[]
  replaced_in: string[]
  removed_from: string[]
}

export interface AccountTestResult {
  ok: boolean
  account_id: string
  provider: AccountProvider
  model: string | null
  latency_ms: number
  error_code: string | null
  error_message: string | null
}

// --- Agent Bindings --------------------------------------------------------

export const EXECUTING_AGENT_ROLES = [
  'architect',
  'researcher',
  'writer',
  'reviewer',
  'validator',
] as const
export type ExecutingAgentRole = (typeof EXECUTING_AGENT_ROLES)[number]

export const PRIMARY_BINDING_ROLES = ['planner', ...EXECUTING_AGENT_ROLES] as const
export type PrimaryBindingRole = (typeof PRIMARY_BINDING_ROLES)[number]

export const ALL_BINDING_ROLES = [...PRIMARY_BINDING_ROLES, 'compactor'] as const
export type BindingRole = (typeof ALL_BINDING_ROLES)[number]

export const BINDING_STRATEGIES = ['dedicated', 'round_robin', 'failover'] as const
export type BindingStrategy = (typeof BINDING_STRATEGIES)[number]

export interface BindingPut {
  account_ids: string[]
  strategy: BindingStrategy
  model: string
  max_input_tokens: number
  max_output_tokens: number
  enabled: boolean
}

export interface BindingView extends BindingPut {
  role: BindingRole
  updated_at: string | null
}

// --- Step 10b Handoff: Run & SSE wire shapes -------------------------------

export type RunStateName =
  | 'INTAKE'
  | 'PLANNING'
  | 'EXECUTING'
  | 'VALIDATING'
  | 'DECIDING'
  | 'CHECKPOINT'
  | 'REPLANNING'
  | 'DONE'
  | 'STOPPED_EROSION'
  | 'STOPPED_BUDGET'
  | 'STOPPED_ERRORS'
  | 'STOPPED_USER'

export type StopOption =
  | 'finish'
  | 'continue'
  | 'rollback'
  | 'edit_goals'
  | 'add_budget'
  | 'continue_anyway'

export type OnComplete = 'ask' | 'finish' | 'continue_once'

export type LoopAction =
  | 'CONTINUE'
  | 'OFFER_FINISH'
  | 'ASK_USER'
  | 'STOP_EROSION'
  | 'STOP_BUDGET'
  | 'STOP_ERRORS'
  | 'STOP_USER'

export interface LoopDecisionView {
  schema_version?: 1 | undefined
  iter: number
  action: LoopAction
  reasons: string[]
  evidence: Record<string, unknown>
}

export interface VerdictView {
  schema_version?: 1 | undefined
  meets_minimum: boolean
  missing: string[]
  risks: string[]
  recommendation: 'finish' | 'continue'
}

export interface StopReportView {
  schema_version?: 1 | undefined
  run_id: string
  action: LoopAction
  reasons: string[]
  evidence: Record<string, unknown>
  q_series: number[]
  current_q: number | null
  best_q: number | null
  best_iter: number | null
  best_snapshot_id: string | null
  iterations: number
  tokens_spent: number
  verdict: VerdictView | null
  options: StopOption[]
  created_at: string
}

export interface RunEvent {
  seq: number
  ts: string
  type: string
  state: RunStateName
  iter: number
  detail: Record<string, unknown>
}

export interface UserDecisionRequest {
  option: StopOption
  extra_tokens?: number | null | undefined
  extra_iterations?: number | null | undefined
  new_goal?: string | null | undefined
  note?: string | null | undefined
  wait?: boolean | undefined
}

export interface RunView {
  id: string
  project_id: string
  owner_id: string | null
  kind: string
  state: RunStateName
  iter: number
  brief: Record<string, unknown> | null
  plan: Record<string, unknown> | null
  goal_override: string | null
  decisions: LoopDecisionView[]
  q_series: number[]
  best_q: number | null
  best_snapshot_id: string | null
  stop_report: StopReportView | null
  verdict: VerdictView | null
  checkpoint_kind: string | null
  on_complete: OnComplete
  user_stop_requested: boolean
  running: boolean
  tokens_spent: number
  last_error: string | null
  created_at: string
  updated_at: string
}
