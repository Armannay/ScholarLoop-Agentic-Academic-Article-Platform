// Typed REST + SSE client for ScholarLoop (/api/*), backed by the OpenAPI 3.1 schema.
// HARD RULE 2 & Security Rule: Never render or store secrets; no localStorage is used for
// keys or tokens beyond the browser session (tokens live in memory + sessionStorage only).

import { defaultMockServer } from './mockServer'
import {
  ALL_BINDING_ROLES,
  type AccountCreate,
  type AccountPatch,
  type AccountTestResult,
  type AccountView,
  type ArticleView,
  type BindingPut,
  type BindingRole,
  type BindingView,
  type ConfigResponse,
  type ContractListResponse,
  type DeleteResult,
  type DigestView,
  type HealthResponse,
  type LedgerView,
  type LoginRequest,
  type ProjectCreate,
  type ProjectList,
  type ProjectPatch,
  type ProjectView,
  type QualityView,
  type RegisterRequest,
  type RestoreRequest,
  type RestoreView,
  type RunCreateRequest,
  type RunEvent,
  type RunEventList,
  type RunList,
  type RunUsageView,
  type RunView,
  type SectionDiffView,
  type SectionHistoryView,
  type SnapshotList,
  type TokenResponse,
  type UserDecisionRequest,
  type UserView,
  type ValidationFieldError,
} from './types'

export type FetchTransport = (input: string, init?: RequestInit) => Promise<Response>

const metaEnv = (import.meta as { env?: Record<string, string | undefined> }).env ?? {}
const API_BASE = (metaEnv.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const TOKEN_SESSION_KEY = 'scholarloop.jwt'
const MOCK_SESSION_KEY = 'scholarloop.mock'

function readSessionValue(key: string): string | null {
  try {
    if (typeof localStorage !== 'undefined') {
      // Never persist tokens or keys in localStorage beyond the session.
      localStorage.removeItem(TOKEN_SESSION_KEY)
    }
    return typeof sessionStorage !== 'undefined' ? sessionStorage.getItem(key) : null
  } catch {
    return null
  }
}

function writeSessionValue(key: string, value: string | null): void {
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.removeItem(TOKEN_SESSION_KEY)
    }
    if (typeof sessionStorage === 'undefined') return
    if (value === null) sessionStorage.removeItem(key)
    else sessionStorage.setItem(key, value)
  } catch {
    // In-memory state stays authoritative when storage is unavailable.
  }
}

function detectInitialMockMode(): boolean {
  if (metaEnv.VITE_API_MOCK === 'true' || metaEnv.VITE_API_MOCK === '1') return true
  if (typeof window !== 'undefined') {
    const param = new URLSearchParams(window.location.search).get('mock')
    if (param === '1' || param === 'true') return true
  }
  return readSessionValue(MOCK_SESSION_KEY) === '1'
}

let memoryToken: string | null = readSessionValue(TOKEN_SESSION_KEY)
let mockMode: boolean = detectInitialMockMode()
let customTransport: FetchTransport | null = null

const unauthorizedListeners = new Set<() => void>()
const mockModeListeners = new Set<(enabled: boolean) => void>()

export function getAccessToken(): string | null {
  return memoryToken
}

export function setAccessToken(token: string | null): void {
  memoryToken = token
  writeSessionValue(TOKEN_SESSION_KEY, token)
}

export function subscribeUnauthorized(listener: () => void): () => void {
  unauthorizedListeners.add(listener)
  return () => {
    unauthorizedListeners.delete(listener)
  }
}

export function isMockMode(): boolean {
  return mockMode
}

export function setMockMode(enabled: boolean): void {
  if (mockMode === enabled) return
  mockMode = enabled
  writeSessionValue(MOCK_SESSION_KEY, enabled ? '1' : null)
  for (const listener of mockModeListeners) listener(enabled)
}

export function subscribeMockMode(listener: (enabled: boolean) => void): () => void {
  mockModeListeners.add(listener)
  return () => {
    mockModeListeners.delete(listener)
  }
}

export function setCustomTransport(transport: FetchTransport | null): void {
  customTransport = transport
}

function activeTransport(): FetchTransport {
  if (customTransport) return customTransport
  if (mockMode) return (url, init) => defaultMockServer.fetch(url, init)
  return (url, init) => fetch(url, init)
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string | undefined
  readonly context: Record<string, unknown> | undefined
  readonly fieldErrors: readonly ValidationFieldError[]
  readonly missingFields: readonly string[]
  readonly problems: readonly string[]

  constructor(
    message: string,
    status: number,
    code?: string | undefined,
    context?: Record<string, unknown> | undefined,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.context = context
    this.fieldErrors = parseFieldErrors(context)
    this.missingFields = parseStringArray(context?.missing_fields)
    this.problems = parseStringArray(context?.problems)
  }
}

function parseStringArray(raw: unknown): string[] {
  if (!Array.isArray(raw)) return []
  return raw.filter((item): item is string => typeof item === 'string')
}

function parseFieldErrors(context?: Record<string, unknown>): ValidationFieldError[] {
  if (!context || !Array.isArray(context.errors)) return []
  const out: ValidationFieldError[] = []
  for (const item of context.errors) {
    if (item && typeof item === 'object') {
      const rec = item as Record<string, unknown>
      if (typeof rec.loc === 'string' && typeof rec.msg === 'string') {
        out.push({
          loc: rec.loc,
          msg: rec.msg,
          type: typeof rec.type === 'string' ? rec.type : 'value_error',
        })
      }
    }
  }
  return out
}

function extractDetail(payload: unknown): {
  message: string
  code?: string | undefined
  context?: Record<string, unknown> | undefined
} {
  if (payload && typeof payload === 'object') {
    const record = payload as Record<string, unknown>
    const candidate =
      record.error && typeof record.error === 'object'
        ? (record.error as Record<string, unknown>)
        : record.detail && typeof record.detail === 'object'
          ? (record.detail as Record<string, unknown>)
          : null
    if (candidate) {
      const message = typeof candidate.message === 'string' ? candidate.message : 'request failed'
      const code = typeof candidate.code === 'string' ? candidate.code : undefined
      const context =
        candidate.context && typeof candidate.context === 'object' && !Array.isArray(candidate.context)
          ? (candidate.context as Record<string, unknown>)
          : undefined
      return { message, code, context }
    }
    if (typeof record.detail === 'string') return { message: record.detail }
  }
  return { message: 'request failed' }
}

interface RequestOptions {
  method?: string | undefined
  body?: unknown
  signal?: AbortSignal | undefined
  auth?: boolean | undefined
  headers?: Record<string, string> | undefined
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...(options.headers ?? {}),
  }
  const sentToken = options.auth ? memoryToken : null
  if (sentToken) {
    headers.Authorization = `Bearer ${sentToken}`
  }

  const init: RequestInit = {
    method: options.method ?? 'GET',
    headers,
  }
  if (options.signal) init.signal = options.signal
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(options.body)
  }

  const response = await activeTransport()(`${API_BASE}${path}`, init)
  const text = await response.text()
  let payload: unknown = undefined
  if (text.length > 0) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = undefined
    }
  }

  if (!response.ok) {
    if (response.status === 401 && sentToken) {
      for (const listener of unauthorizedListeners) listener()
    }
    const detail = extractDetail(payload)
    throw new ApiError(detail.message, response.status, detail.code, detail.context)
  }

  return payload as T
}

// --- Public status & schema endpoints --------------------------------------

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return request<HealthResponse>('/api/health', { signal })
}

export async function fetchConfig(signal?: AbortSignal): Promise<ConfigResponse> {
  return request<ConfigResponse>('/api/config', { signal })
}

export async function fetchContracts(signal?: AbortSignal): Promise<ContractListResponse> {
  return request<ContractListResponse>('/api/contracts', { signal })
}

export async function fetchContractSchema(name: string, signal?: AbortSignal): Promise<unknown> {
  return request<unknown>(`/api/contracts/${encodeURIComponent(name)}/schema`, { signal })
}

// --- Auth endpoints --------------------------------------------------------

export async function registerUser(payload: RegisterRequest): Promise<TokenResponse> {
  return request<TokenResponse>('/api/auth/register', { method: 'POST', body: payload })
}

export async function loginUser(payload: LoginRequest): Promise<TokenResponse> {
  return request<TokenResponse>('/api/auth/login', { method: 'POST', body: payload })
}

export async function fetchCurrentUser(signal?: AbortSignal): Promise<UserView> {
  return request<UserView>('/api/auth/me', { auth: true, signal })
}

// --- Accounts endpoints ----------------------------------------------------

export async function listAccounts(signal?: AbortSignal): Promise<AccountView[]> {
  return request<AccountView[]>('/api/accounts', { auth: true, signal })
}

export async function createAccount(payload: AccountCreate): Promise<AccountView> {
  return request<AccountView>('/api/accounts', { method: 'POST', body: payload, auth: true })
}

export async function getAccount(accountId: string, signal?: AbortSignal): Promise<AccountView> {
  return request<AccountView>(`/api/accounts/${encodeURIComponent(accountId)}`, { auth: true, signal })
}

export async function patchAccount(accountId: string, payload: AccountPatch): Promise<AccountView> {
  return request<AccountView>(`/api/accounts/${encodeURIComponent(accountId)}`, {
    method: 'PATCH',
    body: payload,
    auth: true,
  })
}

export interface DeleteAccountOptions {
  replacementAccountId?: string | undefined
  disableRoles?: boolean | undefined
}

export async function deleteAccount(
  accountId: string,
  options: DeleteAccountOptions = {},
): Promise<DeleteResult> {
  const params = new URLSearchParams()
  if (options.replacementAccountId) {
    params.set('replacement_account_id', options.replacementAccountId)
  }
  if (options.disableRoles) {
    params.set('disable_roles', 'true')
  }
  const qs = params.toString()
  const url = `/api/accounts/${encodeURIComponent(accountId)}${qs ? `?${qs}` : ''}`
  return request<DeleteResult>(url, { method: 'DELETE', auth: true })
}

export async function testAccount(accountId: string): Promise<AccountTestResult> {
  return request<AccountTestResult>(`/api/accounts/${encodeURIComponent(accountId)}/test`, {
    method: 'POST',
    auth: true,
  })
}

// --- Agent Bindings endpoints ----------------------------------------------

export async function fetchBinding(
  role: BindingRole,
  signal?: AbortSignal,
): Promise<BindingView | null> {
  try {
    return await request<BindingView>(`/api/agents/${encodeURIComponent(role)}/binding`, {
      auth: true,
      signal,
    })
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null
    throw err
  }
}

export async function fetchAllBindings(
  roles: readonly BindingRole[] = ALL_BINDING_ROLES,
  signal?: AbortSignal,
): Promise<Record<BindingRole, BindingView | null>> {
  const entries = await Promise.all(
    roles.map(async (role) => [role, await fetchBinding(role, signal)] as const),
  )
  const record = {
    planner: null,
    architect: null,
    researcher: null,
    writer: null,
    reviewer: null,
    validator: null,
    compactor: null,
  } as Record<BindingRole, BindingView | null>
  for (const [role, view] of entries) {
    record[role] = view
  }
  return record
}

export async function putBinding(role: BindingRole, payload: BindingPut): Promise<BindingView> {
  return request<BindingView>(`/api/agents/${encodeURIComponent(role)}/binding`, {
    method: 'PUT',
    body: payload,
    auth: true,
  })
}

// --- Projects endpoints (Step 10b / 11) ------------------------------------

export async function listProjects(
  options: { limit?: number | undefined; cursor?: string | undefined; signal?: AbortSignal | undefined } = {},
): Promise<ProjectList> {
  const params = new URLSearchParams()
  if (options.limit !== undefined) params.set('limit', String(options.limit))
  if (options.cursor) params.set('cursor', options.cursor)
  const qs = params.toString()
  return request<ProjectList>(`/api/projects${qs ? `?${qs}` : ''}`, {
    auth: true,
    signal: options.signal,
  })
}

export async function createProject(payload: ProjectCreate): Promise<ProjectView> {
  return request<ProjectView>('/api/projects', {
    method: 'POST',
    body: payload,
    auth: true,
  })
}

export async function getProject(projectId: string, signal?: AbortSignal): Promise<ProjectView> {
  return request<ProjectView>(`/api/projects/${encodeURIComponent(projectId)}`, {
    auth: true,
    signal,
  })
}

export async function patchProject(
  projectId: string,
  payload: ProjectPatch,
): Promise<ProjectView> {
  return request<ProjectView>(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: 'PATCH',
    body: payload,
    auth: true,
  })
}

export async function deleteProject(projectId: string): Promise<void> {
  await request<void>(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: 'DELETE',
    auth: true,
  })
}

export async function listProjectRuns(
  projectId: string,
  signal?: AbortSignal,
): Promise<RunList> {
  return request<RunList>(`/api/projects/${encodeURIComponent(projectId)}/runs`, {
    auth: true,
    signal,
  })
}

// --- Runs endpoints (Step 10b) ---------------------------------------------

export async function createRun(
  projectId: string,
  payload: RunCreateRequest,
  options: { scenario?: string | undefined } = {},
): Promise<RunView> {
  const headers: Record<string, string> = {}
  if (options.scenario) {
    headers['X-ScholarLoop-Scenario'] = options.scenario
  }
  return request<RunView>(`/api/projects/${encodeURIComponent(projectId)}/runs`, {
    method: 'POST',
    body: payload,
    auth: true,
    headers,
  })
}

export async function getRun(runId: string, signal?: AbortSignal): Promise<RunView> {
  return request<RunView>(`/api/runs/${encodeURIComponent(runId)}`, {
    auth: true,
    signal,
  })
}

export async function submitDecision(
  runId: string,
  payload: UserDecisionRequest,
): Promise<RunView> {
  return request<RunView>(`/api/runs/${encodeURIComponent(runId)}/decision`, {
    method: 'POST',
    body: payload,
    auth: true,
  })
}

export async function stopRun(runId: string): Promise<RunView> {
  return request<RunView>(`/api/runs/${encodeURIComponent(runId)}/stop`, {
    method: 'POST',
    auth: true,
  })
}

export async function getRunEventHistory(
  runId: string,
  options: { after?: number | undefined; limit?: number | undefined; signal?: AbortSignal | undefined } = {},
): Promise<RunEventList> {
  const params = new URLSearchParams()
  if (options.after !== undefined) params.set('after', String(options.after))
  if (options.limit !== undefined) params.set('limit', String(options.limit))
  const qs = params.toString()
  return request<RunEventList>(
    `/api/runs/${encodeURIComponent(runId)}/events/history${qs ? `?${qs}` : ''}`,
    { auth: true, signal: options.signal },
  )
}

export async function getRunDigest(runId: string, signal?: AbortSignal): Promise<DigestView> {
  return request<DigestView>(`/api/runs/${encodeURIComponent(runId)}/digest`, {
    auth: true,
    signal,
  })
}

export async function getRunLedger(runId: string, signal?: AbortSignal): Promise<LedgerView> {
  return request<LedgerView>(`/api/runs/${encodeURIComponent(runId)}/ledger`, {
    auth: true,
    signal,
  })
}

export async function getRunQuality(runId: string, signal?: AbortSignal): Promise<QualityView> {
  return request<QualityView>(`/api/runs/${encodeURIComponent(runId)}/quality`, {
    auth: true,
    signal,
  })
}

export async function getRunUsage(runId: string, signal?: AbortSignal): Promise<RunUsageView> {
  return request<RunUsageView>(`/api/runs/${encodeURIComponent(runId)}/usage`, {
    auth: true,
    signal,
  })
}

// --- Article, Section History, Diff & Snapshots endpoints (Step 10b) -------

export async function getRunArticle(runId: string, signal?: AbortSignal): Promise<ArticleView> {
  return request<ArticleView>(`/api/runs/${encodeURIComponent(runId)}/article`, {
    auth: true,
    signal,
  })
}

export async function getSectionHistory(
  runId: string,
  sectionId: string,
  signal?: AbortSignal,
): Promise<SectionHistoryView> {
  return request<SectionHistoryView>(
    `/api/runs/${encodeURIComponent(runId)}/article/sections/${encodeURIComponent(sectionId)}/history`,
    { auth: true, signal },
  )
}

export async function getSectionDiff(
  runId: string,
  sectionId: string,
  options: { from?: number | undefined; to?: number | undefined; signal?: AbortSignal | undefined } = {},
): Promise<SectionDiffView> {
  const params = new URLSearchParams()
  if (options.from !== undefined) params.set('from', String(options.from))
  if (options.to !== undefined) params.set('to', String(options.to))
  const qs = params.toString()
  return request<SectionDiffView>(
    `/api/runs/${encodeURIComponent(runId)}/article/sections/${encodeURIComponent(sectionId)}/diff${qs ? `?${qs}` : ''}`,
    { auth: true, signal: options.signal },
  )
}

export async function listArticleSnapshots(
  runId: string,
  signal?: AbortSignal,
): Promise<SnapshotList> {
  return request<SnapshotList>(`/api/runs/${encodeURIComponent(runId)}/article/snapshots`, {
    auth: true,
    signal,
  })
}

export async function restoreArticleSnapshot(
  runId: string,
  payload: RestoreRequest,
): Promise<RestoreView> {
  return request<RestoreView>(`/api/runs/${encodeURIComponent(runId)}/article/restore`, {
    method: 'POST',
    body: payload,
    auth: true,
  })
}

// --- Authenticated SSE reader (Step 10b) -----------------------------------

export function parseSseFrames(chunkBuffer: string): { events: RunEvent[]; rest: string } {
  const blocks = chunkBuffer.split('\n\n')
  const rest = blocks.pop() ?? ''
  const events: RunEvent[] = []
  for (const block of blocks) {
    const dataLines: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trimStart())
      }
    }
    if (dataLines.length > 0) {
      const parsed: unknown = JSON.parse(dataLines.join('\n'))
      events.push(parsed as RunEvent)
    }
  }
  return { events, rest }
}

export async function streamRunEvents(
  runId: string,
  onEvent: (event: RunEvent) => void,
  options: { after?: number | undefined; once?: boolean | undefined; signal?: AbortSignal | undefined } = {},
): Promise<void> {
  const params = new URLSearchParams()
  if (options.after !== undefined) params.set('after', String(options.after))
  if (options.once) params.set('once', 'true')
  const qs = params.toString()
  const path = `${API_BASE}/api/runs/${encodeURIComponent(runId)}/events${qs ? `?${qs}` : ''}`
  const headers: Record<string, string> = { Accept: 'text/event-stream' }
  if (memoryToken) headers.Authorization = `Bearer ${memoryToken}`
  const init: RequestInit = { method: 'GET', headers }
  if (options.signal) init.signal = options.signal

  const response = await activeTransport()(path, init)
  if (!response.ok) {
    const text = await response.text()
    let payload: unknown = undefined
    try {
      payload = text ? JSON.parse(text) : undefined
    } catch {
      payload = undefined
    }
    const detail = extractDetail(payload)
    throw new ApiError(detail.message, response.status, detail.code, detail.context)
  }
  if (!response.body) {
    const text = await response.text()
    const { events } = parseSseFrames(text)
    for (const ev of events) onEvent(ev)
    return
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parsed = parseSseFrames(buffer)
    buffer = parsed.rest
    for (const ev of parsed.events) onEvent(ev)
  }
  buffer += decoder.decode()
  if (buffer.trim()) {
    const parsed = parseSseFrames(`${buffer}\n\n`)
    for (const ev of parsed.events) onEvent(ev)
  }
}
