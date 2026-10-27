// Wire shapes for ScholarLoop REST and SSE endpoints (OpenAPI 3.1 + CONTRACTS v1).
// Backed by the generated OpenAPI schema in `./generated/api-schema.d.ts`.
// Plaintext API keys are write-only on AccountCreate/AccountPatch and never appear in views.

import type { components, operations, paths } from './generated/api-schema'

export type { components, operations, paths }
export type OpenApiSchemas = components['schemas']

/**
 * Contract schemas are exported from Pydantic as JSON Schema, so the generated types
 * carry the schema's `$defs` bookkeeping. The wire payload never includes it, so it is
 * dropped here once instead of at every use site.
 */
type Wire<T> = Omit<T, '$defs'>

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

export type ContractListResponse = OpenApiSchemas['ContractListResponse']

// --- Error shapes ----------------------------------------------------------

export type ValidationFieldError = OpenApiSchemas['ValidationFieldError']
export type ErrorCode = OpenApiSchemas['ErrorCode']

export interface ApiErrorBody {
  code: string
  message: string
  context?: Record<string, unknown> | undefined
}

// --- Auth ------------------------------------------------------------------

export type RegisterRequest = OpenApiSchemas['RegisterRequest']
export type LoginRequest = OpenApiSchemas['LoginRequest']
export type UserView = OpenApiSchemas['UserView']
export type TokenResponse = OpenApiSchemas['TokenResponse']

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

export type DeleteResult = OpenApiSchemas['DeleteResult']

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

// --- CONTRACTS v1 & Step 10b Domain Types ----------------------------------

export const ARTICLE_TYPES = ['research', 'review', 'position', 'case_study'] as const
export type ArticleType = OpenApiSchemas['ArticleType']

export const CITATION_STYLES = ['APA7', 'IEEE', 'Harvard', 'Vancouver'] as const
export type CitationStyle = OpenApiSchemas['CitationStyle']

export type AgentRole = OpenApiSchemas['AgentRole']

/** The five executing roles, in the order the plan board and the spec use. */
export const AGENT_ROLE_ORDER = [
  'architect',
  'researcher',
  'writer',
  'reviewer',
  'validator',
] as const satisfies readonly AgentRole[]
export type SectionStatus = OpenApiSchemas['SectionStatus']
export type VerificationStatus = OpenApiSchemas['VerificationStatus']
export type IssueSeverity = OpenApiSchemas['IssueSeverity']
export type ChangeStatus = OpenApiSchemas['ChangeStatus']
export type Recommendation = OpenApiSchemas['Recommendation']

export const GATE_NAMES = [
  'required_sections_present',
  'abstract_within_range',
  'total_words_within_tolerance',
  'citations_resolve_both_ways',
  'references_verified_or_flagged',
  'no_placeholders',
  'citation_style_matches',
  'referenced_figures_and_tables_exist',
] as const
export type GateName = (typeof GATE_NAMES)[number]

export const MIN_TARGET_WORDS = 300
export const MAX_TARGET_WORDS = 30_000
export const MAX_CONSTRAINT_WORDS = 40

export interface Brief {
  schema_version?: 1 | undefined
  topic: string
  article_type: ArticleType
  audience: string
  language: string
  target_words: number
  citation_style: CitationStyle
  must_include?: string[] | undefined
  must_avoid?: string[] | undefined
  provided_data?: string[] | undefined
}

export interface Task {
  schema_version?: 1 | undefined
  id: string
  title: string
  role: AgentRole
  section_ids?: string[] | undefined
  source_ids?: string[] | undefined
  instructions: string
  acceptance: string[]
  token_budget: number
  depends_on?: string[] | undefined
}

export interface Plan {
  schema_version?: 1 | undefined
  version: number
  goal: string
  tasks: Task[]
}

export type TokenUsage = Wire<OpenApiSchemas['TokenUsage']>
export type ChangeEntry = Wire<OpenApiSchemas['ChangeEntry']>
export type CitationRef = Wire<OpenApiSchemas['CitationRef']>
export type SectionState = Wire<OpenApiSchemas['SectionState']>
export type SourceCard = Wire<OpenApiSchemas['SourceCard']>
export type QualityIssue = Wire<OpenApiSchemas['QualityIssue']>
export type QualityReport = Wire<OpenApiSchemas['QualityReport']>
export type QualityPoint = OpenApiSchemas['QualityPoint']

/** The latest report plus the Q history, as `GET /runs/{id}/quality` sends it. */
export interface QualityView {
  run_id: string
  latest: QualityReport | null
  series: QualityPoint[]
  best_q?: number | null | undefined
}

export type RunStateName = OpenApiSchemas['RunStateName']
export type CheckpointKind = OpenApiSchemas['CheckpointKind']
export type StopOption = OpenApiSchemas['StopOption']
export type OnComplete = OpenApiSchemas['OnComplete']
export type LoopAction = OpenApiSchemas['LoopActionName']

export const RUNNING_STATES: readonly RunStateName[] = [
  'INTAKE',
  'PLANNING',
  'EXECUTING',
  'VALIDATING',
  'DECIDING',
  'REPLANNING',
]

export const TERMINAL_STATES: readonly RunStateName[] = [
  'DONE',
  'STOPPED_EROSION',
  'STOPPED_BUDGET',
  'STOPPED_ERRORS',
  'STOPPED_USER',
]

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
  recommendation: Recommendation
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

export type RunEventList = OpenApiSchemas['RunEventList']

export interface UserDecisionRequest {
  option: StopOption
  extra_tokens?: number | null | undefined
  extra_iterations?: number | null | undefined
  new_goal?: string | null | undefined
  note?: string | null | undefined
  wait?: boolean | undefined
}

export interface QuickEditRequest {
  section_id: string
  instructions: string
  role?: AgentRole | undefined
  acceptance?: string[] | undefined
  token_budget?: number | undefined
}

export interface RunCreateRequest {
  brief?: Partial<Brief> | Record<string, unknown> | undefined
  quick_edit?: QuickEditRequest | undefined
  on_complete?: OnComplete | undefined
  max_wall_seconds?: number | undefined
  wait?: boolean | undefined
}

export interface RunView {
  id: string
  project_id: string
  owner_id?: string | null | undefined
  kind: string
  state: RunStateName
  iter: number
  brief: Brief | Record<string, unknown> | null
  plan: Plan | Record<string, unknown> | null
  goal_override: string | null
  decisions: LoopDecisionView[]
  q_series: number[]
  best_q: number | null
  best_snapshot_id: string | null
  stop_report: StopReportView | null
  verdict: VerdictView | null
  checkpoint_kind: CheckpointKind | string | null
  on_complete: OnComplete
  user_stop_requested: boolean
  running: boolean
  tokens_spent: number
  last_error: string | null
  created_at: string
  updated_at: string
}

export type RunSummary = OpenApiSchemas['RunSummary']
export type RunList = OpenApiSchemas['RunList']
export type DigestView = OpenApiSchemas['DigestView']
export interface LedgerView {
  run_id: string
  entries: ChangeEntry[]
}
export type UsageBreakdown = OpenApiSchemas['UsageBreakdown']
export type RunUsageView = OpenApiSchemas['RunUsageView']

// --- Article, History, Diff & Snapshots ------------------------------------

export type ArticleSection = OpenApiSchemas['ArticleSection']

/** The assembled article: markdown, sections with bodies, and the source cards. */
export interface ArticleView {
  run_id: string
  markdown: string
  body_words: number
  words_by_section: Record<string, number>
  sections: ArticleSection[]
  sources: SourceCard[]
}
export type VersionInfo = OpenApiSchemas['VersionInfo']
export type SectionHistoryView = OpenApiSchemas['SectionHistoryView']
export type DiffStats = OpenApiSchemas['DiffStats']
export type TextDiff = OpenApiSchemas['TextDiff']
export type SectionDiffView = OpenApiSchemas['SectionDiffView']
export type SnapshotSummary = OpenApiSchemas['SnapshotSummary']
export type SnapshotList = OpenApiSchemas['SnapshotList']
export type RestoreRequest = { snapshot_id: string; dry_run?: boolean | undefined }
export type RestoreView = OpenApiSchemas['RestoreView']

// --- Projects --------------------------------------------------------------

export interface ProjectCreate {
  id?: string | undefined
  name: string
  description?: string | null | undefined
  brief?: Brief | Record<string, unknown> | undefined
  policy_overrides?: Record<string, unknown> | undefined
  status?: 'active' | 'archived' | undefined
}

export interface ProjectPatch {
  name?: string | undefined
  description?: string | null | undefined
  brief?: Brief | Record<string, unknown> | undefined
  policy_overrides?: Record<string, unknown> | null | undefined
  status?: 'active' | 'archived' | undefined
}

export interface ProjectView {
  id: string
  owner_id?: string | undefined
  name: string
  description?: string | null | undefined
  brief?: Brief | undefined
  policy_overrides?: Record<string, unknown> | undefined
  status?: 'active' | 'archived' | undefined
  run_count: number
  latest_run_id?: string | null | undefined
  latest_state?: RunStateName | string | null | undefined
  latest_q?: number | null | undefined
  best_q?: number | null | undefined
  created_at: string
  updated_at: string
}

export interface ProjectList {
  projects: ProjectView[]
  total: number
  next_cursor?: string | null | undefined
  has_more?: boolean | undefined
}

// --- Deterministic helpers (Brief validation, word count, RTL detection) ---

const WORD_RE = /\S+/g
const BCP47_RE = /^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/

export function countWords(text: string): number {
  if (!text) return 0
  const matches = text.match(WORD_RE)
  return matches ? matches.length : 0
}

export function wordToleranceRange(targetWords: number): { low: number; high: number } {
  return {
    low: Math.floor(targetWords * 0.85),
    high: Math.floor(targetWords * 1.15),
  }
}

export function isRtlLanguage(language?: string | null): boolean {
  if (!language) return false
  const primary = language.trim().toLowerCase().split(/[-_]/)[0]
  return primary === 'fa' || primary === 'ar'
}

export interface BriefValidationResult {
  valid: boolean
  fieldErrors: Record<string, string>
  missingFields: string[]
  problems: string[]
}

export interface BriefDraftInput {
  topic: string
  article_type: string
  audience: string
  language: string
  target_words: string | number
  citation_style: string
  must_include: string[]
  must_avoid: string[]
  provided_data: string[]
}

export function validateBriefDraft(draft: BriefDraftInput): BriefValidationResult {
  const fieldErrors: Record<string, string> = {}
  const missingFields: string[] = []
  const problems: string[] = []

  const topic = draft.topic.trim()
  if (!topic) {
    missingFields.push('topic')
    fieldErrors.topic = 'Topic is required (a one-sentence statement of the article subject).'
  } else if (countWords(topic) < 3) {
    missingFields.push('topic')
    problems.push('topic_too_vague')
    fieldErrors.topic = 'Topic is too vague — provide at least 3 words describing the research subject.'
  }

  if (!draft.article_type || !ARTICLE_TYPES.includes(draft.article_type as ArticleType)) {
    missingFields.push('article_type')
    fieldErrors.article_type = `Article type must be one of: ${ARTICLE_TYPES.join(', ')}.`
  }

  const audience = draft.audience.trim()
  if (!audience) {
    missingFields.push('audience')
    fieldErrors.audience = 'Target audience is required (e.g., "CS faculty and graduate reviewers").'
  }

  const language = draft.language.trim()
  if (!language) {
    missingFields.push('language')
    fieldErrors.language = 'Language is required (BCP-47 tag, e.g., "en", "fa", "ar").'
  } else if (language.length < 2 || language.length > 35 || !BCP47_RE.test(language)) {
    missingFields.push('language')
    problems.push('language_not_bcp47')
    fieldErrors.language = 'Language must be a valid BCP-47 tag (e.g., "en", "en-GB", "fa", "ar").'
  }

  const rawWords =
    typeof draft.target_words === 'number'
      ? String(draft.target_words)
      : draft.target_words.trim()
  if (!rawWords) {
    missingFields.push('target_words')
    fieldErrors.target_words = `Target word count is required (${MIN_TARGET_WORDS.toLocaleString()}–${MAX_TARGET_WORDS.toLocaleString()}).`
  } else {
    const parsedWords = Number(rawWords)
    if (
      !Number.isInteger(parsedWords) ||
      parsedWords < MIN_TARGET_WORDS ||
      parsedWords > MAX_TARGET_WORDS
    ) {
      problems.push('invalid_target_words')
      fieldErrors.target_words = `Target words must be an integer between ${MIN_TARGET_WORDS.toLocaleString()} and ${MAX_TARGET_WORDS.toLocaleString()}.`
    }
  }

  if (
    !draft.citation_style ||
    !CITATION_STYLES.includes(draft.citation_style as CitationStyle)
  ) {
    missingFields.push('citation_style')
    fieldErrors.citation_style = `Citation style must be one of: ${CITATION_STYLES.join(', ')}.`
  }

  const incClean = draft.must_include.map((s) => s.trim()).filter(Boolean)
  const avoidClean = draft.must_avoid.map((s) => s.trim()).filter(Boolean)

  for (const item of incClean) {
    if (countWords(item) > MAX_CONSTRAINT_WORDS) {
      problems.push('invalid_must_include')
      fieldErrors.must_include = `Each must_include instruction must be at most ${MAX_CONSTRAINT_WORDS} words.`
      break
    }
  }

  for (const item of avoidClean) {
    if (countWords(item) > MAX_CONSTRAINT_WORDS) {
      problems.push('invalid_must_avoid')
      fieldErrors.must_avoid = `Each must_avoid instruction must be at most ${MAX_CONSTRAINT_WORDS} words.`
      break
    }
  }

  const avoidSet = new Set(avoidClean.map((s) => s.toLowerCase()))
  const overlap = incClean.filter((s) => avoidSet.has(s.toLowerCase()))
  if (overlap.length > 0) {
    problems.push('contradictory_constraints')
    const msg = `must_include and must_avoid overlap (${overlap.join(', ')}), making the brief unsatisfiable.`
    fieldErrors.must_include = msg
    fieldErrors.must_avoid = msg
  }

  return {
    valid: Object.keys(fieldErrors).length === 0 && missingFields.length === 0,
    fieldErrors,
    missingFields,
    problems,
  }
}
