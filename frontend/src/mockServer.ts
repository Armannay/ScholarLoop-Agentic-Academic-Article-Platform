// Stateful OpenAPI-conformant mock server for ScholarLoop REST and SSE routes.
// Runs identically in browser memory, Node 22 tests, or Vite dev middleware.
// HARD RULE 2: plaintext keys are validated for shape, masked, and immediately discarded.

import {
  ACCOUNT_PROVIDERS,
  ACCOUNT_STATUSES,
  ALL_BINDING_ROLES,
  BINDING_STRATEGIES,
  type AccountProvider,
  type AccountStatus,
  type AccountTestResult,
  type AccountView,
  type BindingRole,
  type BindingStrategy,
  type BindingView,
  type ConfigResponse,
  type DeleteResult,
  type HealthResponse,
  type RunEvent,
  type RunStateName,
  type RunView,
  type UserView,
} from './types'

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
        const result: DeleteResult = { deleted: true, account_id: accountId, disabled_roles: disabledRoles, replaced_in: replacedIn, removed_from: removedFrom }
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

    const projectRunsMatch = /^\/api\/projects\/([^/]+)\/runs$/.exec(path)
    if (method === 'POST' && projectRunsMatch) {
      const auth = this.requireUser(headers)
      if (!auth.ok) return auth.res
      const projectId = decodeURIComponent(projectRunsMatch[1] ?? '')
      const parsed = this.parseJson(rawBody)
      if (!parsed.ok) return parsed.res
      const hasBrief = parsed.data.brief !== undefined && parsed.data.brief !== null
      const hasQuick = parsed.data.quick_edit !== undefined && parsed.data.quick_edit !== null
      if (hasBrief === hasQuick) return valErr('body', "provide exactly one of 'brief' or 'quick_edit'")
      const id = this.nextUlid()
      const now = new Date().toISOString()
      const view: RunView = {
        id, project_id: projectId, owner_id: auth.user.id, kind: hasBrief ? 'brief' : 'quick_edit',
        state: 'CHECKPOINT', iter: 1, brief: hasBrief ? (parsed.data.brief as Record<string, unknown>) : null,
        plan: null, goal_override: null, decisions: [], q_series: [76], best_q: 76, best_snapshot_id: 'snap-1',
        stop_report: {
          run_id: id, action: 'OFFER_FINISH', reasons: ['mock checkpoint reached'], evidence: {},
          q_series: [76], current_q: 76, best_q: 76, best_iter: 1, best_snapshot_id: 'snap-1',
          iterations: 1, tokens_spent: 1200,
          verdict: { meets_minimum: true, missing: [], risks: [], recommendation: 'finish' },
          options: ['finish', 'continue', 'rollback'], created_at: now,
        },
        verdict: { meets_minimum: true, missing: [], risks: [], recommendation: 'finish' },
        checkpoint_kind: 'completion', on_complete: 'ask', user_stop_requested: false,
        running: false, tokens_spent: 1200, last_error: null, created_at: now, updated_at: now,
      }
      const events: RunEvent[] = [
        { seq: 0, ts: now, type: 'run_created', state: 'INTAKE', iter: 0, detail: { project_id: projectId } },
        { seq: 1, ts: now, type: 'checkpoint_reached', state: 'CHECKPOINT', iter: 1, detail: { q: 76 } },
      ]
      this.runs.set(id, { view, events })
      return jsonRes(202, view)
    }

    const runEventsMatch = /^\/api\/runs\/([^/]+)\/events$/.exec(path)
    if (method === 'GET' && runEventsMatch) {
      const auth = this.requireUser(headers)
      if (!auth.ok) return auth.res
      const runId = decodeURIComponent(runEventsMatch[1] ?? '')
      const stored = this.runs.get(runId)
      if (!stored || stored.view.owner_id !== auth.user.id) return errRes(404, 'not_found', `run ${runId} not found`)
      const after = Number(query.get('after') ?? '-1')
      const frames = stored.events.filter((ev) => ev.seq > after).map((ev) => `id: ${ev.seq}\nevent: ${ev.type}\ndata: ${JSON.stringify(ev)}\n\n`).join('')
      return new Response(frames, { status: 200, headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' } })
    }

    const runSubMatch = /^\/api\/runs\/([^/]+)(?:\/(decision|stop|digest|ledger|article|usage))?$/.exec(path)
    if (runSubMatch) {
      const auth = this.requireUser(headers)
      if (!auth.ok) return auth.res
      const runId = decodeURIComponent(runSubMatch[1] ?? '')
      const sub = runSubMatch[2]
      const stored = this.runs.get(runId)
      if (!stored || stored.view.owner_id !== auth.user.id) return errRes(404, 'not_found', `run ${runId} not found`)
      if (!sub && method === 'GET') return jsonRes(200, stored.view)
      if (sub === 'stop' && method === 'POST') {
        stored.view = { ...stored.view, state: 'STOPPED_USER', updated_at: new Date().toISOString() }
        return jsonRes(200, stored.view)
      }
      if (sub === 'decision' && method === 'POST') {
        const parsed = this.parseJson(rawBody)
        if (!parsed.ok) return parsed.res
        const nextState: RunStateName = parsed.data.option === 'finish' ? 'DONE' : 'EXECUTING'
        stored.view = { ...stored.view, state: nextState, updated_at: new Date().toISOString() }
        return jsonRes(200, stored.view)
      }
      if (sub === 'digest' && method === 'GET') return jsonRes(200, { run_id: runId, markdown: '# Digest\n', tokens: 12, max_tokens: 2000, compacted: false, latest_q: stored.view.best_q, open_issues: [] })
      if (sub === 'ledger' && method === 'GET') return jsonRes(200, { run_id: runId, entries: [] })
      if (sub === 'article' && method === 'GET') return jsonRes(200, { run_id: runId, markdown: '', sections: [], sources: [] })
      if (sub === 'usage' && method === 'GET') return jsonRes(200, { total: stored.view.tokens_spent, by_role: {}, by_iter: {} })
    }

    return errRes(404, 'not_found', `no route for ${method} ${path}`)
  }
}

export const defaultMockServer = new MockApiServer()
