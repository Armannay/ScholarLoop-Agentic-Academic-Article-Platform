// Stateful OpenAPI-conformant mock server for ScholarLoop REST and SSE routes.
// Runs identically in browser memory, Node 22 tests, or Vite dev middleware.
// HARD RULE 2: plaintext keys are validated for shape, masked, and immediately discarded.

import {
  ACCOUNT_PROVIDERS,
  ACCOUNT_STATUSES,
  ALL_BINDING_ROLES,
  BINDING_STRATEGIES,
  ARTICLE_TYPES,
  CITATION_STYLES,
  RUNNING_STATES,
  isRtlLanguage,
  type AccountProvider,
  type AccountStatus,
  type AccountTestResult,
  type AccountView,
  type BindingRole,
  type BindingStrategy,
  type BindingView,
  type Brief,
  type CheckpointKind,
  type ConfigResponse,
  type DeleteResult,
  type HealthResponse,
  type LoopAction,
  type ProjectList,
  type ProjectView,
  type RunEvent,
  type RunList,
  type RunStateName,
  type RunSummary,
  type RunView,
  type StopOption,
  type UserView,
} from './types'
import {
  ENGLISH_BRIEF,
  PERSIAN_BRIEF,
  buildBundle,
  buildDiff,
  buildQuality,
  mockHash,
  mockId,
  type RunBundle,
} from './fixtures'

const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/
const ANTHROPIC_KEY_RE = /^sk-ant-api[0-9A-Za-z_-]{8,}$/
const ANTHROPIC_OAUTH_RE = /^sk-ant-oat/i

const CONTRACT_NAMES = [
  'Account', 'AccountPublic', 'AgentBinding', 'AgentResult', 'Brief', 'ChangeEntry',
  'CitationRef', 'LLMMessage', 'LLMRequest', 'LLMResponse', 'LoopDecision', 'MonthlyBudget',
  'Patch', 'PatchOp', 'Plan', 'QualityIssue', 'QualityReport', 'SectionState', 'SourceCard',
  'SystemBlock', 'Task', 'TokenUsage', 'Verdict',
]

interface StoredUser {
  view: UserView
  credentialTag: string
}

interface StoredRun {
  view: RunView
  events: RunEvent[]
  bundle: RunBundle
}

interface StoredProject {
  view: ProjectView
  runIds: string[]
}

/** Owner id of the seeded demo projects: visible to every signed-in user. */
const DEMO_OWNER = 'demo'

/** Read the mock scenario selector (header first, `?scenario=` second). */
function scenarioFrom(headers: Headers, query?: URLSearchParams): string {
  const header = headers.get('x-scholarloop-scenario')
  if (header) return header
  return query?.get('scenario') ?? 'completion'
}

/**
 * Validate a raw Brief payload the way deterministic intake does.
 *
 * Missing or blank required fields are a `409 needs_user_input` naming each one
 * (the console's "fill in the blanks"); malformed values are a `422 validation_error`
 * with per-field `context.errors[]`. Nothing is defaulted.
 */
function checkBrief(
  raw: Record<string, unknown>,
): { ok: true; brief: Brief } | { ok: false; res: Response } {
  const text = (key: string): string => (typeof raw[key] === 'string' ? (raw[key] as string).trim() : '')
  const required = ['topic', 'article_type', 'audience', 'language', 'target_words', 'citation_style']
  const missing = required.filter((key) => {
    const value = raw[key]
    return value === undefined || value === null || (typeof value === 'string' && !value.trim())
  })
  if (missing.length > 0) {
    return {
      ok: false,
      res: errRes(409, 'needs_user_input', `intake needs user input: ${missing.join(', ')}`, {
        missing_fields: missing,
        problems: [],
        kind: 'brief',
      }),
    }
  }
  const problems: Array<{ loc: string; msg: string; type: string }> = []
  const topic = text('topic')
  if (topic.split(/\s+/).filter(Boolean).length < 3) {
    problems.push({ loc: 'body.brief.topic', msg: 'topic needs at least 3 words', type: 'value_error' })
  }
  const articleType = text('article_type')
  if (!ARTICLE_TYPES.includes(articleType as (typeof ARTICLE_TYPES)[number])) {
    problems.push({
      loc: 'body.brief.article_type',
      msg: `article_type must be one of ${ARTICLE_TYPES.join(' | ')}`,
      type: 'enum',
    })
  }
  const citationStyle = text('citation_style')
  if (!CITATION_STYLES.includes(citationStyle as (typeof CITATION_STYLES)[number])) {
    problems.push({
      loc: 'body.brief.citation_style',
      msg: `citation_style must be one of ${CITATION_STYLES.join(' | ')}`,
      type: 'enum',
    })
  }
  const targetWords = Number(raw.target_words)
  if (!Number.isInteger(targetWords) || targetWords < 300 || targetWords > 30_000) {
    problems.push({
      loc: 'body.brief.target_words',
      msg: 'target_words must be an integer in 300..30,000',
      type: 'value_error',
    })
  }
  const language = text('language')
  if (!/^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/.test(language)) {
    problems.push({
      loc: 'body.brief.language',
      msg: 'language must be a BCP-47 tag',
      type: 'value_error',
    })
  }
  const mustInclude = Array.isArray(raw.must_include) ? raw.must_include.filter((v) => typeof v === 'string') : []
  const mustAvoid = Array.isArray(raw.must_avoid) ? raw.must_avoid.filter((v) => typeof v === 'string') : []
  const avoidSet = new Set(mustAvoid.map((item) => item.trim().toLowerCase()))
  const overlap = mustInclude.filter((item) => avoidSet.has(item.trim().toLowerCase()))
  if (overlap.length > 0) {
    problems.push({
      loc: 'body.brief.must_include',
      msg: 'must_include and must_avoid overlap, so the brief is unsatisfiable',
      type: 'value_error',
    })
  }
  if (problems.length > 0) {
    return {
      ok: false,
      res: errRes(422, 'validation_error', problems[0]?.msg ?? 'invalid brief', { errors: problems }),
    }
  }
  const providedData = Array.isArray(raw.provided_data)
    ? raw.provided_data.filter((v): v is string => typeof v === 'string')
    : []
  return {
    ok: true,
    brief: {
      schema_version: 1,
      topic,
      article_type: articleType as Brief['article_type'],
      audience: text('audience'),
      language,
      target_words: targetWords,
      citation_style: citationStyle as Brief['citation_style'],
      must_include: mustInclude.map((item) => item.trim()),
      must_avoid: mustAvoid.map((item) => item.trim()),
      provided_data: providedData.map((item) => item.trim()),
    },
  }
}

function maskSecret(secret: string): string {
  return secret.length <= 4 ? '*'.repeat(Math.max(secret.length, 4)) : `****${secret.slice(-4)}`
}

function simpleTag(input: string): string {
  let h1 = 0xdeadbeef ^ input.length
  let h2 = 0x41c6ce57 ^ input.length
  for (let i = 0; i < input.length; i += 1) {
    const ch = input.charCodeAt(i)
    h1 = Math.imul(h1 ^ ch, 2654435761)
    h2 = Math.imul(h2 ^ ch, 1597334677)
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909)
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909)
  return (h2 >>> 0).toString(16).padStart(8, '0') + (h1 >>> 0).toString(16).padStart(8, '0')
}

function jsonRes(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', 'x-contract-version': '1' },
  })
}

function errRes(status: number, code: string, message: string, context: Record<string, unknown> = {}): Response {
  return jsonRes(status, { error: { code, message, context } })
}

function valErr(loc: string, msg: string, type = 'value_error'): Response {
  const safe = loc.endsWith('api_key') ? 'invalid api_key' : msg
  return errRes(422, 'validation_error', `${loc}: ${safe}`, { errors: [{ loc, msg: safe, type }] })
}

function defaultModelFor(provider: AccountProvider): string {
  if (provider === 'anthropic') return 'claude-sonnet-4'
  if (provider === 'openai_compatible') return 'gpt-4.1-mini'
  return 'mock-default'
}

export class MockApiServer {
  private seq = 1
  private fpCounter = 1
  private readonly usersByEmail = new Map<string, StoredUser>()
  private readonly usersById = new Map<string, StoredUser>()
  private readonly tokens = new Map<string, string>()
  private readonly accounts = new Map<string, AccountView>()
  private readonly bindings = new Map<string, BindingView>()
  private readonly runs = new Map<string, StoredRun>()
  private readonly projects = new Map<string, StoredProject>()

  constructor() {
    this.seedDemoProjects()
  }

  private nextUlid(): string {
    let value = this.seq++
    let suffix = ''
    for (let i = 0; i < 16; i += 1) {
      suffix = (CROCKFORD[value % 32] ?? '0') + suffix
      value = Math.floor(value / 32)
    }
    return `01J0MOCK00${suffix}`
  }

  private nextFingerprint(seed: string): string {
    return simpleTag(`${this.fpCounter++}:${seed.length}`).slice(0, 12).toUpperCase()
  }

  reset(): void {
    this.seq = 1
    this.fpCounter = 1
    this.usersByEmail.clear()
    this.usersById.clear()
    this.tokens.clear()
    this.accounts.clear()
    this.bindings.clear()
    this.runs.clear()
    this.projects.clear()
    this.seedDemoProjects()
  }

  readonly fetch = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const rawUrl = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    const url = new URL(rawUrl, 'http://scholarloop.local')
    const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase()
    const headers = new Headers(input instanceof Request ? input.headers : undefined)
    if (init?.headers) new Headers(init.headers).forEach((v, k) => headers.set(k, v))
    const rawBody =
      typeof init?.body === 'string'
        ? init.body
        : init?.body instanceof Uint8Array
          ? new TextDecoder().decode(init.body)
          : ''
    return this.route(method, url.pathname, url.searchParams, headers, rawBody)
  }

  private parseJson(raw: string): { ok: true; data: Record<string, unknown> } | { ok: false; res: Response } {
    if (!raw.trim()) return { ok: false, res: valErr('body', 'JSON object required', 'json_invalid') }
    try {
      const parsed: unknown = JSON.parse(raw)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        return { ok: false, res: valErr('body', 'Input should be a valid dictionary', 'model_type') }
      }
      return { ok: true, data: parsed as Record<string, unknown> }
    } catch {
      return { ok: false, res: valErr('body', 'Invalid JSON payload', 'json_invalid') }
    }
  }

  private requireUser(headers: Headers): { ok: true; user: UserView } | { ok: false; res: Response } {
    const auth = headers.get('authorization') ?? ''
    if (!auth.toLowerCase().startsWith('bearer ')) return { ok: false, res: errRes(401, 'auth_error', 'missing bearer token') }
    const userId = this.tokens.get(auth.slice(7).trim())
    const stored = userId ? this.usersById.get(userId) : undefined
    if (!stored) return { ok: false, res: errRes(401, 'auth_error', 'invalid or expired token') }
    return { ok: true, user: stored.view }
  }

  private validateKey(provider: AccountProvider, apiKey: string): string | null {
    const trimmed = apiKey.trim()
    if (trimmed.length < 8 || trimmed.length > 512) return 'api_key must be 8..512 characters'
    if (provider === 'anthropic') {
      if (ANTHROPIC_OAUTH_RE.test(trimmed)) {
        return 'this looks like a Claude Pro/Max subscription token, not an API key. Anthropic accounts need a Claude Console API key (console.anthropic.com -> API keys).'
      }
      if (!ANTHROPIC_KEY_RE.test(trimmed)) {
        return 'api_key does not look like a Claude Console API key. Anthropic accounts need a Claude Console API key (console.anthropic.com -> API keys).'
      }
    }
    return null
  }

  private parseBudget(raw: unknown): { ok: true; budget: { tokens: number | null; usd: number | null } } | { ok: false; res: Response } {
    if (raw === undefined || raw === null) return { ok: true, budget: { tokens: null, usd: null } }
    if (typeof raw !== 'object' || Array.isArray(raw)) return { ok: false, res: valErr('body.budget', 'budget must be an object') }
    const obj = raw as Record<string, unknown>
    let tokens: number | null = null
    let usd: number | null = null
    if (obj.tokens !== undefined && obj.tokens !== null) {
      if (typeof obj.tokens !== 'number' || !Number.isInteger(obj.tokens) || obj.tokens < 1_000 || obj.tokens > 1_000_000_000) {
        return { ok: false, res: valErr('body.budget.tokens', 'tokens must be an integer in 1,000..1,000,000,000') }
      }
      tokens = obj.tokens
    }
    if (obj.usd !== undefined && obj.usd !== null) {
      if (typeof obj.usd !== 'number' || !Number.isFinite(obj.usd) || obj.usd <= 0 || obj.usd > 1_000_000) {
        return { ok: false, res: valErr('body.budget.usd', 'usd must be > 0 and <= 1,000,000') }
      }
      usd = obj.usd
    }
    return { ok: true, budget: { tokens, usd } }
  }

  private usableCandidatesLeft(binding: BindingView, excludingId: string): number {
    return binding.account_ids.filter((id) => {
      if (id === excludingId) return false
      const acc = this.accounts.get(id)
      return acc !== undefined && acc.status !== 'disabled'
    }).length
  }

  private route(method: string, path: string, query: URLSearchParams, headers: Headers, rawBody: string): Response {
    if (method === 'GET' && path === '/api/health') {
      const body: HealthResponse = {
        status: 'ok', version: '0.1.0', contract_version: 1, env: 'mock',
        policies_loaded: true, rubric_loaded: true,
        providers: {
          mock: true, live_adapters_implemented: ['anthropic', 'openai_compatible'],
          accounts_configured: this.accounts.size, live_providers_enabled: true, account_store_ready: true,
        },
        config_errors: {},
      }
      return jsonRes(200, body)
    }

    if (method === 'GET' && path === '/api/version') {
      return jsonRes(200, { app_version: '0.1.0', contract_version: 1, env: 'mock', provider_default: 'mock' })
    }

    if (method === 'GET' && path === '/api/config') {
      const body: ConfigResponse = {
        contract_version: 1, policies_path: 'config/policies.yaml (mock)', overrides_path: null, project: null,
        policies: {
          run_token_cap: 800_000, iteration_token_cap: 120_000, call_input_cap: 24_000,
          max_iterations: 12, max_parallel: 3, digest_max_tokens: 2_000, recent_window: 3,
          compact_trigger: 0.8, q_min: 70, plateau: { window: 3, min_gain: 1.0 },
          regression: { delta: 3, window: 2 }, oscillation: { window: 4 },
          diminishing: { min_gain_per_10k: 0.2, window: 2 }, issue_churn: 0.3, error_streak: 3,
          model_tiers: {
            planner: 'strong', architect: 'mid', researcher: 'mid', writer: 'mid',
            reviewer: 'strong', validator: 'cheap', compactor: 'cheap',
          },
        },
        rubric: {
          article_types: {
            research: ['abstract', 'introduction', 'methods', 'results', 'discussion', 'conclusion'],
            review: ['abstract', 'introduction', 'methodology', 'synthesis', 'discussion', 'conclusion'],
            position: ['abstract', 'introduction', 'argument', 'counterarguments', 'conclusion'],
            case_study: ['abstract', 'context', 'case_description', 'analysis', 'lessons_learned'],
          },
          gates: [
            'required_sections', 'abstract_word_range', 'no_unverified_citations',
            'citation_style_consistent', 'no_high_severity_issues', 'target_word_tolerance',
            'no_placeholder_text', 'claims_supported',
          ],
          criteria_weights: {
            argument_coherence: 25, evidence_grounding: 25, methodological_rigor: 20,
            clarity_and_style: 15, structural_completeness: 15,
          },
          weights_total: 100, abstract_word_range: { min: 150, max: 300 },
        },
      }
      return jsonRes(200, body)
    }

    if (method === 'GET' && path === '/api/contracts') {
      return jsonRes(200, { contract_version: 1, names: CONTRACT_NAMES, schema_url_template: '/api/contracts/{name}/schema' })
    }

    const contractMatch = /^\/api\/contracts\/([^/]+)\/schema$/.exec(path)
    if (method === 'GET' && contractMatch) {
      const name = decodeURIComponent(contractMatch[1] ?? '')
      if (!CONTRACT_NAMES.includes(name)) return errRes(404, 'not_found', `unknown contract: ${name}`, { name })
      return jsonRes(200, { $schema: 'https://json-schema.org/draft/2020-12/schema', title: name, type: 'object', additionalProperties: false })
    }

    if (method === 'POST' && path === '/api/auth/register') {
      const parsed = this.parseJson(rawBody)
      if (!parsed.ok) return parsed.res
      const email = typeof parsed.data.email === 'string' ? parsed.data.email.trim().toLowerCase() : ''
      const pw = typeof parsed.data.password === 'string' ? parsed.data.password : ''
      if (!email || !EMAIL_RE.test(email)) return valErr('body.email', 'not a valid email address')
      if (pw.length < 8 || pw.length > 256) return valErr('body.password', 'password must be 8..256 characters')
      if (this.usersByEmail.has(email)) return errRes(409, 'conflict', `an account with email '${email}' already exists`)
      const user: UserView = { id: this.nextUlid(), email, created_at: new Date().toISOString() }
      const stored: StoredUser = { view: user, credentialTag: simpleTag(`${email}:${pw}`) }
      this.usersByEmail.set(email, stored)
      this.usersById.set(user.id, stored)
      const token = `mock-jwt-${this.nextUlid()}`
      this.tokens.set(token, user.id)
      return jsonRes(201, { access_token: token, token_type: 'bearer', user })
    }

    if (method === 'POST' && path === '/api/auth/login') {
      const parsed = this.parseJson(rawBody)
      if (!parsed.ok) return parsed.res
      const email = typeof parsed.data.email === 'string' ? parsed.data.email.trim().toLowerCase() : ''
      const pw = typeof parsed.data.password === 'string' ? parsed.data.password : ''
      if (!email || !EMAIL_RE.test(email)) return valErr('body.email', 'not a valid email address')
      if (!pw) return valErr('body.password', 'password is required')
      const existing = this.usersByEmail.get(email)
      if (!existing || existing.credentialTag !== simpleTag(`${email}:${pw}`)) {
        return errRes(401, 'auth_error', 'invalid email or password')
      }
      const token = `mock-jwt-${this.nextUlid()}`
      this.tokens.set(token, existing.view.id)
      return jsonRes(200, { access_token: token, token_type: 'bearer', user: existing.view })
    }

    if (method === 'GET' && path === '/api/auth/me') {
      const auth = this.requireUser(headers)
      return auth.ok ? jsonRes(200, auth.user) : auth.res
    }

    if (path === '/api/accounts') {
      const auth = this.requireUser(headers)
      if (!auth.ok) return auth.res
      if (method === 'GET') {
        return jsonRes(200, [...this.accounts.values()].filter((a) => a.owner_id === auth.user.id))
      }
      if (method === 'POST') {
        const parsed = this.parseJson(rawBody)
        if (!parsed.ok) return parsed.res
        const provider = parsed.data.provider as AccountProvider
        if (!ACCOUNT_PROVIDERS.includes(provider)) return valErr('body.provider', 'invalid provider')
        const label = typeof parsed.data.label === 'string' ? parsed.data.label.trim() : ''
        if (label.length < 1 || label.length > 64) return valErr('body.label', 'label must be 1..64 characters')
        const apiKey = typeof parsed.data.api_key === 'string' ? parsed.data.api_key : ''
        if (apiKey.length < 1 || apiKey.length > 512) return valErr('body.api_key', 'invalid api_key')
        const baseUrl =
          parsed.data.base_url === undefined || parsed.data.base_url === null
            ? null
            : typeof parsed.data.base_url === 'string' ? parsed.data.base_url.trim() : ''
        if (baseUrl !== null && !/^https?:\/\//.test(baseUrl)) return valErr('body.base_url', 'base_url must be an absolute http(s) URL')
        if (provider === 'openai_compatible' && baseUrl === null) return valErr('body.base_url', 'provider=openai_compatible requires base_url')
        if (provider !== 'openai_compatible' && baseUrl !== null) return valErr('body.base_url', `provider=${provider} does not use base_url`)
        const budgetParsed = this.parseBudget(parsed.data.budget)
        if (!budgetParsed.ok) return budgetParsed.res
        const keyErr = this.validateKey(provider, apiKey)
        if (keyErr) return errRes(422, 'contract_violation', keyErr)
        const now = new Date().toISOString()
        const id = this.nextUlid()
        const view: AccountView = {
          id, owner_id: auth.user.id, provider, label, status: 'active', base_url: baseUrl,
          key_fingerprint: this.nextFingerprint(id), key_mask: maskSecret(apiKey.trim()),
          budget: budgetParsed.budget, created_at: now, updated_at: now,
        }
        this.accounts.set(id, view)
        return jsonRes(201, view)
      }
    }

    const testMatch = /^\/api\/accounts\/([^/]+)\/test$/.exec(path)
    if (method === 'POST' && testMatch) {
      const auth = this.requireUser(headers)
      if (!auth.ok) return auth.res
      const accountId = decodeURIComponent(testMatch[1] ?? '')
      const account = this.accounts.get(accountId)
      if (!account || account.owner_id !== auth.user.id) return errRes(404, 'not_found', `account ${accountId} not found`)
      const result: AccountTestResult =
        account.status === 'disabled'
          ? { ok: false, account_id: accountId, provider: account.provider, model: null, latency_ms: 4, error_code: 'account_disabled', error_message: 'account is disabled; enable it before probing' }
          : { ok: true, account_id: accountId, provider: account.provider, model: defaultModelFor(account.provider), latency_ms: 14, error_code: null, error_message: null }
      return jsonRes(200, result)
    }

    const itemMatch = /^\/api\/accounts\/([^/]+)$/.exec(path)
    if (itemMatch) {
      const auth = this.requireUser(headers)
      if (!auth.ok) return auth.res
      const accountId = decodeURIComponent(itemMatch[1] ?? '')
      const account = this.accounts.get(accountId)
      if (!account || account.owner_id !== auth.user.id) return errRes(404, 'not_found', `account ${accountId} not found`)
      if (method === 'GET') return jsonRes(200, account)

      if (method === 'PATCH') {
        const parsed = this.parseJson(rawBody)
        if (!parsed.ok) return parsed.res
        const next: AccountView = { ...account, budget: { ...account.budget } }
        if ('api_key' in parsed.data) {
          if (parsed.data.api_key === null) return errRes(422, 'contract_violation', 'api_key cannot be null; omit it to keep the current key')
          if (typeof parsed.data.api_key !== 'string' || parsed.data.api_key.length > 512) return valErr('body.api_key', 'invalid api_key')
          const keyErr = this.validateKey(account.provider, parsed.data.api_key)
          if (keyErr) return errRes(422, 'contract_violation', keyErr)
          next.key_mask = maskSecret(parsed.data.api_key.trim())
          next.key_fingerprint = this.nextFingerprint(account.id)
        }
        if ('label' in parsed.data && parsed.data.label !== null) {
          const label = typeof parsed.data.label === 'string' ? parsed.data.label.trim() : ''
          if (label.length < 1 || label.length > 64) return valErr('body.label', 'label must be 1..64 characters')
          next.label = label
        }
        if ('base_url' in parsed.data) {
          const rawUrl = parsed.data.base_url
          if (rawUrl !== null && typeof rawUrl !== 'string') return valErr('body.base_url', 'base_url must be string or null')
          const cleaned = typeof rawUrl === 'string' ? rawUrl.trim() : null
          if (cleaned !== null && !/^https?:\/\//.test(cleaned)) return valErr('body.base_url', 'base_url must be an absolute http(s) URL')
          if (cleaned !== null && account.provider !== 'openai_compatible') return errRes(422, 'contract_violation', `provider=${account.provider} does not use base_url`)
          if (cleaned === null && account.provider === 'openai_compatible') return errRes(422, 'contract_violation', 'provider=openai_compatible requires base_url')
          next.base_url = cleaned
        }
        if ('budget' in parsed.data) {
          const budgetParsed = this.parseBudget(parsed.data.budget)
          if (!budgetParsed.ok) return budgetParsed.res
          next.budget = budgetParsed.budget
        }
        if ('status' in parsed.data && parsed.data.status !== null) {
          const st = parsed.data.status as AccountStatus
          if (!ACCOUNT_STATUSES.includes(st)) return valErr('body.status', 'invalid status')
          next.status = st
        }
        next.updated_at = new Date().toISOString()
        this.accounts.set(accountId, next)
        return jsonRes(200, next)
      }

      if (method === 'DELETE') {
        const replacementId = query.get('replacement_account_id') || null
        const disableRoles = query.get('disable_roles') === 'true'
        let replacement: AccountView | null = null
        if (replacementId !== null) {
          if (replacementId === accountId) return errRes(422, 'contract_violation', 'replacement_account_id must differ from the deleted account')
          const cand = this.accounts.get(replacementId)
          if (!cand || cand.owner_id !== auth.user.id) return errRes(404, 'not_found', `account ${replacementId} not found`)
          if (cand.status === 'disabled') return errRes(422, 'contract_violation', 'replacement account is disabled')
          replacement = cand
        }
        const affected = [...this.bindings.entries()]
          .filter(([k, b]) => k.startsWith(`${auth.user.id}:`) && b.account_ids.includes(accountId))
          .map(([, b]) => b)
        const soleEnabled = affected.filter((b) => b.enabled && this.usableCandidatesLeft(b, accountId) === 0)
        if (soleEnabled.length > 0 && replacement === null && !disableRoles) {
          const roles = soleEnabled.map((b) => b.role)
          return errRes(409, 'conflict', `account ${accountId} is the only account of enabled role(s) [${roles.join(', ')}]; pass replacement_account_id or disable_roles=true`, { roles, account_id: accountId })
        }
        if (replacement !== null && affected.some((b) => b.role === 'planner') && replacement.provider !== 'anthropic' && replacement.provider !== 'mock') {
          return errRes(422, 'contract_violation', 'the planner role must bind anthropic (Claude Console key) or mock accounts')
        }
        const disabledRoles: string[] = []
        const replacedIn: string[] = []
        const removedFrom: string[] = []
        const now = new Date().toISOString()
        for (const binding of affected) {
          const key = `${auth.user.id}:${binding.role}`
          if (replacement !== null) {
            const newIds: string[] = []
            for (const item of binding.account_ids) {
              const target = item === accountId ? replacement.id : item
              if (!newIds.includes(target)) newIds.push(target)
            }
            const nextStrategy: BindingStrategy = newIds.length === 1 && binding.strategy !== 'dedicated' ? 'dedicated' : binding.strategy
            this.bindings.set(key, { ...binding, account_ids: newIds, strategy: nextStrategy, updated_at: now })
            replacedIn.push(binding.role)
          } else if (binding.account_ids.length === 1 && binding.account_ids[0] === accountId) {
            if (binding.enabled) disabledRoles.push(binding.role)
            this.bindings.set(key, { ...binding, enabled: false, updated_at: now })
          } else {
            const remaining = binding.account_ids.filter((id) => id !== accountId)
            const nextStrategy: BindingStrategy = remaining.length === 1 && binding.strategy !== 'dedicated' ? 'dedicated' : binding.strategy
            const shouldDisable = binding.enabled && this.usableCandidatesLeft(binding, accountId) === 0
            if (shouldDisable) disabledRoles.push(binding.role)
            this.bindings.set(key, { ...binding, account_ids: remaining, strategy: nextStrategy, enabled: shouldDisable ? false : binding.enabled, updated_at: now })
            removedFrom.push(binding.role)
          }
        }
        this.accounts.delete(accountId)
        const asRoles = (values: string[]): BindingRole[] => values.filter((v): v is BindingRole => ALL_BINDING_ROLES.includes(v as BindingRole))
        const result: DeleteResult = { deleted: true, account_id: accountId, disabled_roles: asRoles(disabledRoles), replaced_in: asRoles(replacedIn), removed_from: asRoles(removedFrom) }
        return jsonRes(200, result)
      }
    }

    const bindingMatch = /^\/api\/agents\/([^/]+)\/binding$/.exec(path)
    if (bindingMatch) {
      const auth = this.requireUser(headers)
      if (!auth.ok) return auth.res
      const role = decodeURIComponent(bindingMatch[1] ?? '') as BindingRole
      if (!ALL_BINDING_ROLES.includes(role)) return valErr('path.role', `unknown binding role '${role}'`)
      const key = `${auth.user.id}:${role}`
      if (method === 'GET') {
        const existing = this.bindings.get(key)
        return existing ? jsonRes(200, existing) : errRes(404, 'not_found', `no binding for role '${role}'`)
      }
      if (method === 'PUT') {
        const parsed = this.parseJson(rawBody)
        if (!parsed.ok) return parsed.res
        const rawIds = parsed.data.account_ids
        if (!Array.isArray(rawIds) || rawIds.length < 1 || rawIds.length > 16) return valErr('body.account_ids', 'account_ids must contain 1..16 account ids')
        const accountIds = rawIds.map((x) => (typeof x === 'string' ? x.trim() : ''))
        if (accountIds.some((x) => !x)) return valErr('body.account_ids', 'each account_id must be non-empty')
        if (new Set(accountIds).size !== accountIds.length) return errRes(422, 'contract_violation', 'account_ids must be unique; a repeated account is not a fallback')
        for (const id of accountIds) {
          const acc = this.accounts.get(id)
          if (!acc || acc.owner_id !== auth.user.id) return errRes(404, 'not_found', `account ${id} not found`)
          if (role === 'planner' && acc.provider !== 'anthropic' && acc.provider !== 'mock') {
            return errRes(422, 'contract_violation', 'the planner role must bind anthropic (Claude Console key) or mock accounts')
          }
        }
        const strategy = (parsed.data.strategy ?? 'failover') as BindingStrategy
        if (!BINDING_STRATEGIES.includes(strategy)) return valErr('body.strategy', 'invalid strategy')
        if ((strategy === 'round_robin' || strategy === 'failover') && accountIds.length < 2) {
          return errRes(422, 'contract_violation', `strategy '${strategy}' needs at least two accounts; use dedicated for a single-account binding`)
        }
        const model = typeof parsed.data.model === 'string' ? parsed.data.model.trim() : ''
        if (model.length < 1 || model.length > 120) return valErr('body.model', 'model must be 1..120 characters')
        const maxIn = parsed.data.max_input_tokens
        const maxOut = parsed.data.max_output_tokens
        if (typeof maxIn !== 'number' || !Number.isInteger(maxIn) || maxIn < 1_000 || maxIn > 2_000_000) return valErr('body.max_input_tokens', 'invalid max_input_tokens')
        if (typeof maxOut !== 'number' || !Number.isInteger(maxOut) || maxOut < 64 || maxOut > 64_000) return valErr('body.max_output_tokens', 'invalid max_output_tokens')
        if (maxOut > maxIn) return errRes(422, 'contract_violation', 'max_output_tokens must not exceed max_input_tokens')
        const enabled = parsed.data.enabled === undefined ? true : Boolean(parsed.data.enabled)
        const view: BindingView = { role, account_ids: accountIds, strategy, model, max_input_tokens: maxIn, max_output_tokens: maxOut, enabled, updated_at: new Date().toISOString() }
        this.bindings.set(key, view)
        return jsonRes(200, view)
      }
    }

    // ------------------------------------------------------------ projects
    if (path === '/api/projects') {
      const auth = this.requireUser(headers)
      if (!auth.ok) return auth.res
      if (method === 'GET') {
        const visible = [...this.projects.values()].filter((p) => this.ownsProject(p, auth.user.id))
        const limit = Math.min(200, Math.max(1, Number(query.get('limit') ?? '50') || 50))
        const cursor = query.get('cursor')
        const start = cursor ? Number(cursor) : 0
        const offset = Number.isFinite(start) && start > 0 ? Math.floor(start) : 0
        const page = visible.slice(offset, offset + limit)
        const next = offset + page.length
        const body: ProjectList = {
          projects: page.map((p) => this.projectViewFor(p, auth.user.id)),
          total: visible.length,
          next_cursor: next < visible.length ? String(next) : null,
          has_more: next < visible.length,
        }
        return jsonRes(200, body)
      }
      if (method === 'POST') {
        const parsed = this.parseJson(rawBody)
        if (!parsed.ok) return parsed.res
        const name = typeof parsed.data.name === 'string' ? parsed.data.name.trim() : ''
        if (!name || name.length > 120) return valErr('body.name', 'name must be 1..120 characters')
        const briefRaw = parsed.data.brief
        if (briefRaw === undefined || briefRaw === null || typeof briefRaw !== 'object' || Array.isArray(briefRaw)) {
          return valErr('body.brief', 'a CONTRACTS v1 Brief object is required')
        }
        const checked = checkBrief(briefRaw as Record<string, unknown>)
        if (!checked.ok) return checked.res
        const duplicate = [...this.projects.values()].some(
          (p) => this.ownsProject(p, auth.user.id) && p.view.name.trim().toLowerCase() === name.toLowerCase(),
        )
        if (duplicate) {
          return errRes(409, 'conflict', `a project named '${name}' already exists`, {
            reason: 'project_exists',
            name,
          })
        }
        const now = new Date().toISOString()
        const id = this.nextUlid()
        const view: ProjectView = {
          id,
          owner_id: auth.user.id,
          name,
          description: typeof parsed.data.description === 'string' ? parsed.data.description : null,
          brief: checked.brief,
          policy_overrides: {},
          status: 'active',
          run_count: 0,
          latest_run_id: null,
          latest_state: null,
          latest_q: null,
          best_q: null,
          created_at: now,
          updated_at: now,
        }
        this.projects.set(id, { view, runIds: [] })
        return jsonRes(201, this.projectViewFor(this.projects.get(id)!, auth.user.id))
      }
    }

    const projectItemMatch = /^\/api\/projects\/([^/]+)$/.exec(path)
    if (projectItemMatch) {
      const auth = this.requireUser(headers)
      if (!auth.ok) return auth.res
      const projectId = decodeURIComponent(projectItemMatch[1] ?? '')
      const stored = this.projects.get(projectId)
      if (!stored || !this.ownsProject(stored, auth.user.id)) {
        return errRes(404, 'not_found', `project ${projectId} not found`)
      }
      if (method === 'GET') return jsonRes(200, this.projectViewFor(stored, auth.user.id))
      if (method === 'PATCH') {
        const parsed = this.parseJson(rawBody)
        if (!parsed.ok) return parsed.res
        const next: ProjectView = { ...stored.view }
        if ('name' in parsed.data && parsed.data.name !== null) {
          const name = typeof parsed.data.name === 'string' ? parsed.data.name.trim() : ''
          if (!name || name.length > 120) return valErr('body.name', 'name must be 1..120 characters')
          next.name = name
        }
        if ('description' in parsed.data) {
          next.description = typeof parsed.data.description === 'string' ? parsed.data.description : null
        }
        if ('brief' in parsed.data && parsed.data.brief !== null) {
          const checked = checkBrief(parsed.data.brief as Record<string, unknown>)
          if (!checked.ok) return checked.res
          next.brief = checked.brief
        }
        if ('status' in parsed.data && (parsed.data.status === 'active' || parsed.data.status === 'archived')) {
          next.status = parsed.data.status
        }
        next.updated_at = new Date().toISOString()
        stored.view = next
        return jsonRes(200, this.projectViewFor(stored, auth.user.id))
      }
      if (method === 'DELETE') {
        const active = stored.runIds
          .map((id) => this.runs.get(id))
          .find((run) => run !== undefined && RUNNING_STATES.includes(run.view.state))
        if (active) {
          return errRes(409, 'conflict', `project ${projectId} has an active run`, {
            reason: 'active_run',
            run_id: active.view.id,
            state: active.view.state,
          })
        }
        this.projects.delete(projectId)
        return new Response(null, { status: 204 })
      }
    }

    const projectRunsMatch = /^\/api\/projects\/([^/]+)\/runs$/.exec(path)
    if (projectRunsMatch) {
      const auth = this.requireUser(headers)
      if (!auth.ok) return auth.res
      const projectId = decodeURIComponent(projectRunsMatch[1] ?? '')
      const project = this.projects.get(projectId)
      if (!project || !this.ownsProject(project, auth.user.id)) {
        return errRes(404, 'not_found', `project ${projectId} not found`)
      }
      if (method === 'GET') {
        const runs = project.runIds
          .map((id) => this.runs.get(id))
          .filter((run): run is StoredRun => run !== undefined)
          .map((run): RunSummary => ({
            id: run.view.id,
            project_id: run.view.project_id,
            kind: run.view.kind === 'quick_edit' ? 'quick_edit' : 'brief',
            state: run.view.state,
            iter: run.view.iter,
            best_q: run.view.best_q,
            checkpoint_kind: (run.view.checkpoint_kind as CheckpointKind | null) ?? null,
            running: run.view.running,
            tokens_spent: run.view.tokens_spent,
            created_at: run.view.created_at,
            updated_at: run.view.updated_at,
          }))
        const body: RunList = { runs, total: runs.length }
        return jsonRes(200, body)
      }
      if (method === 'POST') {
        const parsed = this.parseJson(rawBody)
        if (!parsed.ok) return parsed.res
        const hasBrief = parsed.data.brief !== undefined && parsed.data.brief !== null
        const hasQuick = parsed.data.quick_edit !== undefined && parsed.data.quick_edit !== null
        if (hasBrief === hasQuick) return valErr('body', "provide exactly one of 'brief' or 'quick_edit'")
        let brief: Brief
        if (hasBrief) {
          const checked = checkBrief(parsed.data.brief as Record<string, unknown>)
          if (!checked.ok) return checked.res
          brief = checked.brief
        } else {
          brief = PERSIAN_BRIEF
        }
        const scenario = scenarioFrom(headers)
        const runId = this.nextUlid()
        const qSeries =
          scenario === 'erosion-stop' ? [72, 74, 73, 72] : scenario === 'needs-input' ? [61, 70] : [54, 63, 71, 78]
        const state: RunStateName = scenario === 'erosion-stop' ? 'STOPPED_EROSION' : 'CHECKPOINT'
        const bundle = buildBundle(
          runId,
          projectId,
          auth.user.id,
          brief,
          isRtlLanguage(brief.language) ? 'fa' : 'en',
          qSeries,
          state,
          scenario === 'needs-input' ? 'needs_input' : 'completion',
        )
        const stored: StoredRun = { view: bundle.run, events: bundle.events, bundle }
        this.runs.set(runId, stored)
        project.runIds = [runId, ...project.runIds]
        return jsonRes(202, stored.view)
      }
    }

    // ------------------------------------------------------------ runs
    const runEventsHistoryMatch = /^\/api\/runs\/([^/]+)\/events\/history$/.exec(path)
    if (method === 'GET' && runEventsHistoryMatch) {
      const auth = this.requireUser(headers)
      if (!auth.ok) return auth.res
      const runId = decodeURIComponent(runEventsHistoryMatch[1] ?? '')
      const stored = this.runs.get(runId)
      if (!stored || !this.ownsRun(stored, auth.user.id)) {
        return errRes(404, 'not_found', `run ${runId} not found`)
      }
      const after = Number(query.get('after') ?? '-1')
      const limit = Math.min(1000, Math.max(1, Number(query.get('limit') ?? '200') || 200))
      const events = stored.events.filter((ev) => ev.seq > after).slice(0, limit)
      const last = events[events.length - 1]
      const nextAfter = last && stored.events.some((ev) => ev.seq > last.seq) ? last.seq : null
      return jsonRes(200, { run_id: runId, events, next_after: nextAfter })
    }

    const runEventsMatch = /^\/api\/runs\/([^/]+)\/events$/.exec(path)
    if (method === 'GET' && runEventsMatch) {
      const auth = this.requireUser(headers)
      if (!auth.ok) return auth.res
      const runId = decodeURIComponent(runEventsMatch[1] ?? '')
      const stored = this.runs.get(runId)
      if (!stored || !this.ownsRun(stored, auth.user.id)) {
        return errRes(404, 'not_found', `run ${runId} not found`)
      }
      const after = Number(query.get('after') ?? '-1')
      const frames = stored.events
        .filter((ev) => ev.seq > after)
        .map((ev) => `id: ${ev.seq}\nevent: ${ev.type}\ndata: ${JSON.stringify(ev)}\n\n`)
        .join('')
      return new Response(frames, {
        status: 200,
        headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' },
      })
    }

    const runSubMatch =
      /^\/api\/runs\/([^/]+)(?:\/(decision|stop|digest|ledger|quality|article(?:\/sections\/([^/]+)\/(?:history|diff)|\/snapshots|\/restore)?|usage))?$/.exec(
        path,
      )
    if (runSubMatch) {
      const auth = this.requireUser(headers)
      if (!auth.ok) return auth.res
      const runId = decodeURIComponent(runSubMatch[1] ?? '')
      const sub = runSubMatch[2]
      const sectionId = runSubMatch[3] ? decodeURIComponent(runSubMatch[3]) : null
      const stored = this.runs.get(runId)
      if (!stored || !this.ownsRun(stored, auth.user.id)) {
        return errRes(404, 'not_found', `run ${runId} not found`)
      }
      const bundle = stored.bundle
      if (!sub && method === 'GET') return jsonRes(200, stored.view)

      if (sub === 'stop' && method === 'POST') {
        this.stopRun(stored, 'STOPPED_USER', 'STOP_USER', ['user_requested'])
        return jsonRes(200, stored.view)
      }

      if (sub === 'decision' && method === 'POST') {
        const parsed = this.parseJson(rawBody)
        if (!parsed.ok) return parsed.res
        const option = typeof parsed.data.option === 'string' ? parsed.data.option : ''
        const legal = stored.view.stop_report?.options ?? []
        if (!legal.includes(option as StopOption)) {
          return errRes(409, 'run_conflict', `option '${option}' is not legal in state ${stored.view.state}`, {
            reason: 'invalid_transition',
            run_id: runId,
            state: stored.view.state,
            options: legal,
          })
        }
        this.applyDecision(stored, option as StopOption, parsed.data)
        return jsonRes(200, stored.view)
      }

      if (sub === 'digest' && method === 'GET') {
        const openIssues = stored.view.best_q !== null && stored.view.best_q >= 78 ? [] : ['citation-unverified:src-gopen-1990']
        return jsonRes(200, {
          run_id: runId,
          markdown: `# Run digest\n\n- Q: ${stored.view.best_q ?? 'n/a'}\n- iterations: ${stored.view.q_series.length}\n- open issues: ${openIssues.length}\n`,
          tokens: 148,
          max_tokens: 2000,
          compacted: false,
          latest_q: stored.view.q_series[stored.view.q_series.length - 1] ?? null,
          open_issues: openIssues,
        })
      }
      if (sub === 'ledger' && method === 'GET') return jsonRes(200, bundle.ledger)
      if (sub === 'quality' && method === 'GET') return jsonRes(200, bundle.quality)
      if (sub === 'usage' && method === 'GET') return jsonRes(200, bundle.usage)
      if (sub === 'article' && method === 'GET') return jsonRes(200, bundle.article)

      if (sub === 'article/snapshots' && method === 'GET') return jsonRes(200, bundle.snapshots)

      if (sub === 'article/restore' && method === 'POST') {
        const parsed = this.parseJson(rawBody)
        if (!parsed.ok) return parsed.res
        const snapshotId = typeof parsed.data.snapshot_id === 'string' ? parsed.data.snapshot_id : ''
        const snapshot = bundle.snapshots.snapshots.find((s) => s.id === snapshotId)
        if (!snapshot) return errRes(404, 'not_found', `snapshot ${snapshotId} not found`)
        const dryRun = parsed.data.dry_run === true
        const result = dryRun
          ? { restored: [], unchanged: bundle.article.sections.map((s) => s.id), versions: {} }
          : this.restoreSnapshot(stored, snapshot.id)
        return jsonRes(200, {
          run_id: runId,
          snapshot_id: snapshot.id,
          label: snapshot.label,
          q: snapshot.q ?? null,
          restored: result.restored,
          unchanged: result.unchanged,
          versions: result.versions,
          state: stored.view.state,
          updated_at: new Date().toISOString(),
          dry_run: dryRun,
        })
      }

      if (sectionId) {
        const history = bundle.history[sectionId]
        if (!history) return errRes(404, 'not_found', `section ${sectionId} not found`)
        if (sub === `article/sections/${sectionId}/history` && method === 'GET') {
          return jsonRes(200, { run_id: runId, section_id: sectionId, versions: history })
        }
        if (sub === `article/sections/${sectionId}/diff` && method === 'GET') {
          return jsonRes(200, buildDiff(bundle.article, sectionId))
        }
      }
    }

    if (method === 'GET' && path === '/api/errors/catalog') {
      return jsonRes(200, {
        contract_version: 1,
        envelope: '{"error": {"code", "message", "context"}}',
        codes: [
          { code: 'needs_user_input', http_status: 409, retryable: false, summary: 'The loop needs a human decision.', context_keys: ['missing_fields', 'problems', 'kind'] },
          { code: 'run_conflict', http_status: 409, retryable: false, summary: 'The run state refuses this operation.', context_keys: ['reason', 'run_id', 'state'] },
          { code: 'validation_error', http_status: 422, retryable: false, summary: 'The request body failed validation.', context_keys: ['errors'] },
          { code: 'not_found', http_status: 404, retryable: false, summary: 'No such object, or it belongs to somebody else.', context_keys: [] },
        ],
      })
    }

    return errRes(404, 'not_found', `no route for ${method} ${path}`)
  }

  // ---------------------------------------------------------------- run helpers
  private ownsProject(project: StoredProject, userId: string): boolean {
    return project.view.owner_id === userId || project.view.owner_id === DEMO_OWNER
  }

  private ownsRun(run: StoredRun, userId: string): boolean {
    return run.view.owner_id === userId || run.view.owner_id === DEMO_OWNER || run.view.owner_id === null
  }

  private projectViewFor(project: StoredProject, userId: string): ProjectView {
    const runs = project.runIds
      .map((id) => this.runs.get(id))
      .filter((run): run is StoredRun => run !== undefined)
    const latest = runs[0]
    const view: ProjectView = {
      ...project.view,
      owner_id: userId,
      run_count: runs.length,
      latest_run_id: latest?.view.id ?? null,
      latest_state: latest?.view.state ?? null,
      latest_q: latest ? (latest.view.q_series[latest.view.q_series.length - 1] ?? null) : null,
      best_q: latest?.view.best_q ?? null,
    }
    return view
  }

  private appendEvent(stored: StoredRun, type: string, detail: Record<string, unknown>): void {
    const seq = stored.events.length === 0 ? 0 : (stored.events[stored.events.length - 1]?.seq ?? -1) + 1
    stored.events = [
      ...stored.events,
      {
        seq,
        ts: new Date().toISOString(),
        type,
        state: stored.view.state,
        iter: stored.view.iter,
        detail,
      },
    ]
  }

  private refreshRun(stored: StoredRun, patch: Partial<RunView>): void {
    stored.view = { ...stored.view, ...patch, updated_at: new Date().toISOString() }
  }

  private applyDecision(stored: StoredRun, option: StopOption, payload: Record<string, unknown>): void {
    const now = new Date().toISOString()
    const bundle = stored.bundle
    this.appendEvent(stored, 'user_decision', { option, note: payload.note ?? null })

    if (option === 'finish') {
      this.refreshRun(stored, { state: 'DONE', checkpoint_kind: null, running: false })
      this.appendEvent(stored, 'run_completed', { option })
      return
    }
    if (option === 'rollback') {
      const best = bundle.snapshots.best_snapshot_id
      if (best) this.restoreSnapshot(stored, best)
      this.refreshRun(stored, { state: 'DONE', checkpoint_kind: null, running: false })
      this.appendEvent(stored, 'rollback_applied', { snapshot_id: best })
      return
    }
    if (option === 'edit_goals') {
      const goal = typeof payload.new_goal === 'string' && payload.new_goal.trim() ? payload.new_goal.trim() : null
      this.refreshRun(stored, {
        state: 'CHECKPOINT',
        checkpoint_kind: 'completion',
        goal_override: goal ?? stored.view.goal_override,
        running: false,
      })
      this.appendEvent(stored, 'state_changed', { goal_override: goal })
      return
    }

    // continue / continue_anyway / add_budget: run one more iteration, then pause again.
    const extraTokens = typeof payload.extra_tokens === 'number' ? payload.extra_tokens : 0
    const previous = stored.view.q_series[stored.view.q_series.length - 1] ?? 70
    const nextQ = Math.min(97, previous + (option === 'add_budget' ? 3 : 2))
    const qSeries = [...stored.view.q_series, nextQ]
    const iter = qSeries.length - 1
    const usage = { ...bundle.usage, token_cap: bundle.usage.token_cap + extraTokens, iterations: qSeries.length }
    bundle.usage = usage
    bundle.quality = buildQuality(qSeries, stored.view.id)
    this.refreshRun(stored, {
      state: 'CHECKPOINT',
      checkpoint_kind: 'completion',
      iter,
      q_series: qSeries,
      best_q: Math.max(...qSeries),
      tokens_spent: stored.view.tokens_spent + 4_200,
      running: false,
      decisions: [
        ...stored.view.decisions,
        {
          schema_version: 1,
          iter,
          action: 'OFFER_FINISH',
          reasons: ['completion_candidate'],
          evidence: { Q: nextQ, q_min: 70, extra_tokens: extraTokens },
        },
      ],
    })
    this.appendEvent(stored, 'iteration_started', { iteration: iter, extra_tokens: extraTokens })
    this.appendEvent(stored, 'planner_evaluated', { Q: nextQ })
    this.appendEvent(stored, 'checkpoint_reached', { action: 'OFFER_FINISH', Q: nextQ, best_q: Math.max(...qSeries) })
    stored.view = { ...stored.view, updated_at: now }
  }

  private stopRun(stored: StoredRun, state: RunStateName, action: LoopAction, reasons: string[]): void {
    const best = stored.view.best_snapshot_id
    this.refreshRun(stored, {
      state,
      running: false,
      checkpoint_kind: null,
      stop_report: {
        schema_version: 1,
        run_id: stored.view.id,
        action,
        reasons,
        evidence: { Q: stored.view.q_series[stored.view.q_series.length - 1] ?? 0, best_q: stored.view.best_q },
        q_series: stored.view.q_series,
        current_q: stored.view.q_series[stored.view.q_series.length - 1] ?? null,
        best_q: stored.view.best_q,
        best_iter: stored.view.best_q !== null ? stored.view.q_series.indexOf(stored.view.best_q) : null,
        best_snapshot_id: best,
        iterations: stored.view.q_series.length,
        tokens_spent: stored.view.tokens_spent,
        verdict: stored.view.verdict,
        options: best ? ['continue_anyway', 'edit_goals', 'rollback'] : ['continue_anyway', 'edit_goals'],
        created_at: new Date().toISOString(),
      },
    })
    this.appendEvent(stored, 'run_stopped', { action, reasons })
  }

  private restoreSnapshot(
    stored: StoredRun,
    snapshotId: string,
  ): { restored: string[]; unchanged: string[]; versions: Record<string, number> } {
    const bundle = stored.bundle
    const snapshot = bundle.snapshots.snapshots.find((s) => s.id === snapshotId)
    const now = new Date().toISOString()
    const restored: string[] = []
    const unchanged: string[] = []
    const versions: Record<string, number> = {}
    const sectionIds = snapshot?.section_ids ?? bundle.article.sections.map((s) => s.id)
    // The mock "restores" the two sections the erosion scenario shrank; everything
    // else already matches the snapshot, exactly like the real store reports.
    const shrunk = new Set(['discussion', 'limitations'])
    const sections = bundle.article.sections.map((section) => {
      if (!sectionIds.includes(section.id)) return section
      if (!shrunk.has(section.id)) {
        unchanged.push(section.id)
        return section
      }
      const previous = bundle.history[section.id] ?? []
      const newVersion = section.version + 1
      bundle.history = {
        ...bundle.history,
        [section.id]: [
          ...previous,
          {
            section_id: section.id,
            version: newVersion,
            hash: mockHash(`${section.id}:snapshot:${snapshotId}`),
            words: section.words,
            status: section.status,
            chars_added: 0,
            chars_removed: 140,
            patch_id: null,
            entry_id: null,
            created_at: now,
            q_after: snapshot?.q ?? null,
          },
        ],
      }
      restored.push(section.id)
      versions[section.id] = newVersion
      return { ...section, version: newVersion, hash: mockHash(`${section.id}:snapshot:${snapshotId}`) }
    })
    bundle.article = { ...bundle.article, sections }
    this.appendEvent(stored, 'rollback_applied', { snapshot_id: snapshotId, restored })
    return { restored, unchanged, versions }
  }

  private seedDemoProjects(): void {
    const seeds: Array<{
      id: string
      name: string
      brief: Brief
      language: 'en' | 'fa'
      runs: Array<{ q: number[]; state: RunStateName; checkpoint?: CheckpointKind }>
    }> = [
      {
        id: 'peer-review-2026',
        name: 'Peer review checklists 2026',
        brief: ENGLISH_BRIEF,
        language: 'en',
        runs: [
          { q: [54, 63, 71, 78], state: 'CHECKPOINT', checkpoint: 'completion' },
          { q: [72, 74, 73, 72], state: 'STOPPED_EROSION' },
        ],
      },
      {
        id: 'ai-ethics-fa',
        name: 'اخلاق هوش مصنوعی در آموزش',
        brief: PERSIAN_BRIEF,
        language: 'fa',
        runs: [{ q: [58, 69, 76], state: 'CHECKPOINT', checkpoint: 'completion' }],
      },
    ]
    for (const seed of seeds) {
      const now = new Date().toISOString()
      const projectId = mockId(`project:${seed.id}`)
      const runIds: string[] = []
      for (const [index, run] of seed.runs.entries()) {
        const runId = mockId(`run:${seed.id}:${index}`)
        const bundle = buildBundle(
          runId,
          projectId,
          DEMO_OWNER,
          seed.brief,
          seed.language,
          run.q,
          run.state,
          run.checkpoint ?? (run.state === 'CHECKPOINT' ? 'completion' : null) ?? undefined,
        )
        this.runs.set(runId, { view: bundle.run, events: bundle.events, bundle })
        runIds.push(runId)
      }
      this.projects.set(projectId, {
        view: {
          id: projectId,
          owner_id: DEMO_OWNER,
          name: seed.name,
          description:
            seed.language === 'fa'
              ? 'نمونهٔ نمایشی برای آزمون چیدمان راست‌به‌چپ.'
              : 'Structured review checklists: one semester of observed reviews.',
          brief: seed.brief,
          policy_overrides: {},
          status: 'active',
          run_count: seed.runs.length,
          latest_run_id: runIds[0] ?? null,
          latest_state: seed.runs[0]?.state ?? null,
          latest_q: seed.runs[0]?.q.at(-1) ?? null,
          best_q: Math.max(...(seed.runs[0]?.q ?? [0])),
          created_at: now,
          updated_at: now,
        },
        runIds,
      })
    }
  }
}

export const defaultMockServer = new MockApiServer()
