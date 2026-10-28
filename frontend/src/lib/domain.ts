// Domain types and deterministic rules for the Step 10b screens.
//
// Wire types are *generated* from `docs/openapi/openapi.yaml`
// (`src/api/openapi.gen.d.ts`, refreshed by `npm run api:gen`); this module only
// re-exports the ones the console renders and adds the rules a browser must apply
// before a request is sent — the same rules deterministic intake applies on the server.

import type { components } from '../api/openapi.gen'

type Schemas = components['schemas']

/**
 * Contract schemas are exported from Pydantic as JSON Schema, so the generated types
 * carry the schema's `$defs` bookkeeping. The wire payload never includes it.
 */
type Wire<T> = Omit<T, '$defs'>

export type Brief = Wire<Schemas['Brief']>
export type Plan = Wire<Schemas['Plan']>
export type Task = Wire<Schemas['Task']>
export type SectionState = Wire<Schemas['SectionState']>
export type CitationRef = Wire<Schemas['CitationRef']>
export type SourceCard = Wire<Schemas['SourceCard']>
export type ChangeEntry = Wire<Schemas['ChangeEntry']>
export type TokenUsage = Wire<Schemas['TokenUsage']>
export type QualityReport = Wire<Schemas['QualityReport']>
export type QualityIssue = Wire<Schemas['QualityIssue']>

export type QualityPoint = Schemas['QualityPoint']
export type ArticleSection = Schemas['ArticleSection']

/** The latest quality report plus the Q history, as `/quality` sends it. */
export interface QualityView {
  run_id: string
  latest: QualityReport | null
  series: QualityPoint[]
  best_q?: number | null | undefined
}

/** The assembled article: markdown, sections with bodies, and the source cards. */
export interface ArticleView {
  run_id: string
  markdown: string
  body_words: number
  words_by_section: Record<string, number>
  sections: ArticleSection[]
  sources: SourceCard[]
}
export type VersionInfo = Schemas['VersionInfo']
export type SectionHistoryView = Schemas['SectionHistoryView']
export type SectionDiffView = Schemas['SectionDiffView']
export type TextDiff = Schemas['TextDiff']
export type SnapshotSummary = Schemas['SnapshotSummary']
export type SnapshotList = Schemas['SnapshotList']
export type RestoreRequest = Schemas['RestoreRequest']
export type RestoreView = Schemas['RestoreView']
export type RunUsageView = Schemas['RunUsageView']
export type UsageBreakdown = Schemas['UsageBreakdown']
export interface LedgerView {
  run_id: string
  entries: ChangeEntry[]
}
export type RunEvent = Schemas['RunEvent']
export type RunEventList = Schemas['RunEventList']
export interface LoopDecision {
  schema_version?: 1 | undefined
  iter: number
  action: LoopActionName
  reasons: string[]
  evidence: Record<string, unknown>
}

export interface Verdict {
  schema_version?: 1 | undefined
  meets_minimum: boolean
  missing: string[]
  risks: string[]
  recommendation: 'finish' | 'continue'
}

export interface StopReport {
  schema_version?: 1 | undefined
  run_id: string
  action: LoopActionName
  reasons: string[]
  evidence: Record<string, unknown>
  q_series: number[]
  current_q: number | null
  best_q: number | null
  best_iter: number | null
  best_snapshot_id: string | null
  iterations: number
  tokens_spent: number
  verdict: Verdict | null
  options: StopOption[]
  created_at: string
}

/**
 * A run as the screens read it.
 *
 * Structurally the generated `RunView`, but with the contract schemas replaced by the
 * `Wire<>` versions above so the mock fixtures can build a run without carrying the
 * JSON-Schema `$defs` bookkeeping.
 */
export interface RunView {
  id: string
  project_id: string
  owner_id?: string | null | undefined
  kind: 'brief' | 'quick_edit' | string
  state: RunStateName
  iter: number
  brief: Brief | Record<string, unknown> | null
  plan: Plan | Record<string, unknown> | null
  goal_override: string | null
  decisions: LoopDecision[]
  q_series: number[]
  best_q: number | null
  best_snapshot_id: string | null
  stop_report: StopReport | null
  verdict: Verdict | null
  checkpoint_kind: CheckpointKind | string | null
  on_complete: 'ask' | 'finish' | 'continue_once'
  user_stop_requested: boolean
  running: boolean
  tokens_spent: number
  last_error: string | null
  created_at: string
  updated_at: string
}
export type StopOption = Schemas['StopOption']
export type RunStateName = Schemas['RunStateName']
export type CheckpointKind = Schemas['CheckpointKind']
export type RunSummary = Schemas['RunSummary']
export type RunList = Schemas['RunList']
export type ProjectView = Schemas['ProjectView']
export type ProjectList = Schemas['ProjectList']
export type AgentRole = Schemas['AgentRole']
export type SectionStatus = Schemas['SectionStatus']
export type ArticleType = Schemas['ArticleType']
export type CitationStyle = Schemas['CitationStyle']
export type LoopActionName = Schemas['LoopActionName']

/** The five executing roles, in the order the spec and the plan board use. */
export const AGENT_ROLE_ORDER = [
  'architect',
  'researcher',
  'writer',
  'reviewer',
  'validator',
] as const satisfies readonly AgentRole[]

/** The eight hard gates, in `config/rubric.yaml` order (CONTRACTS v1). */
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

export const ARTICLE_TYPES = ['research', 'review', 'position', 'case_study'] as const
export const CITATION_STYLES = ['APA7', 'IEEE', 'Harvard', 'Vancouver'] as const

/** Brief bounds, identical to `app/contracts/brief.py`. */
export const MIN_TARGET_WORDS = 300
export const MAX_TARGET_WORDS = 30_000
export const MAX_CONSTRAINT_WORDS = 40

/** States in which a run still does work without a user decision. */
export const RUNNING_STATES: readonly RunStateName[] = [
  'INTAKE',
  'PLANNING',
  'EXECUTING',
  'VALIDATING',
  'DECIDING',
  'REPLANNING',
]

/** States a run does not leave on its own (`DONE` cannot be revived). */
export const TERMINAL_STATES: readonly RunStateName[] = [
  'DONE',
  'STOPPED_EROSION',
  'STOPPED_BUDGET',
  'STOPPED_ERRORS',
  'STOPPED_USER',
]

const WORD_RE = /\S+/g
const BCP47_RE = /^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/

/** Count whitespace-separated words: the same definition every budget uses. */
export function countWords(text: string): number {
  if (!text) return 0
  const matches = text.match(WORD_RE)
  return matches ? matches.length : 0
}

/** The total-words gate tolerance (±15%), so the form can show the real range. */
export function wordToleranceRange(targetWords: number): { low: number; high: number } {
  return { low: Math.floor(targetWords * 0.85), high: Math.floor(targetWords * 1.15) }
}

/** True for the languages the article viewer renders right-to-left. */
export function isRtlLanguage(language?: string | null): boolean {
  if (!language) return false
  const primary = language.trim().toLowerCase().split(/[-_]/)[0]
  return primary === 'fa' || primary === 'ar'
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

export interface BriefValidationResult {
  valid: boolean
  fieldErrors: Record<string, string>
  missingFields: string[]
  problems: string[]
}

/**
 * Validate a Brief draft the way deterministic intake does.
 *
 * Client-side validation is a courtesy, never a substitute: it blocks the obvious
 * mistakes before a round trip and names each field, and the server's
 * `409 needs_user_input` / `422 validation_error` still wins when the two disagree.
 */
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
    fieldErrors.topic =
      'Topic is too vague — provide at least 3 words describing the research subject.'
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
    typeof draft.target_words === 'number' ? String(draft.target_words) : draft.target_words.trim()
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

  const include = draft.must_include.map((item) => item.trim()).filter(Boolean)
  const avoid = draft.must_avoid.map((item) => item.trim()).filter(Boolean)

  for (const item of include) {
    if (countWords(item) > MAX_CONSTRAINT_WORDS) {
      problems.push('invalid_must_include')
      fieldErrors.must_include = `Each must_include instruction must be at most ${MAX_CONSTRAINT_WORDS} words.`
      break
    }
  }
  for (const item of avoid) {
    if (countWords(item) > MAX_CONSTRAINT_WORDS) {
      problems.push('invalid_must_avoid')
      fieldErrors.must_avoid = `Each must_avoid instruction must be at most ${MAX_CONSTRAINT_WORDS} words.`
      break
    }
  }

  const avoidSet = new Set(avoid.map((item) => item.toLowerCase()))
  const overlap = include.filter((item) => avoidSet.has(item.toLowerCase()))
  if (overlap.length > 0) {
    problems.push('contradictory_constraints')
    const message = `must_include and must_avoid overlap (${overlap.join(', ')}), making the brief unsatisfiable.`
    fieldErrors.must_include = message
    fieldErrors.must_avoid = message
  }

  return {
    valid: Object.keys(fieldErrors).length === 0 && missingFields.length === 0,
    fieldErrors,
    missingFields,
    problems,
  }
}

/** Task ids with a `task_completed` event: the plan board's progress source. */
export function completedTaskIds(events: readonly RunEvent[]): Set<string> {
  const done = new Set<string>()
  for (const event of events) {
    if (event.type === 'task_completed' && typeof event.detail?.task_id === 'string') {
      done.add(event.detail.task_id)
    }
  }
  return done
}
