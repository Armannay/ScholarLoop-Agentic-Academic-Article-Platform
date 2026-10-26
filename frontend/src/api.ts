// Typed REST + SSE client for ScholarLoop (/api/*).
// Requests use same-origin relative paths by default so Vite's proxy (or MockApiServer)
// handles routing without ever exposing localhost to a remote browser preview.

import { defaultMockServer } from './mockServer'
import {
  ALL_BINDING_ROLES,
  type AccountCreate,
  type AccountPatch,
  type AccountTestResult,
  type AccountView,
  type BindingPut,
  type BindingRole,
  type BindingView,
  type ConfigResponse,
  type ContractListResponse,
  type DeleteResult,
  type HealthResponse,
  type LoginRequest,
  type RegisterRequest,
  type RunEvent,
  type TokenResponse,
  type UserView,
  type ValidationFieldError,
} from './types'

export type FetchTransport = (input: string, init?: RequestInit) => Promise<Response>

const metaEnv = (import.meta as { env?: Record<string, string | undefined> }).env ?? {}
const API_BASE = (metaEnv.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const TOKEN_STORAGE_KEY = 'scholarloop.jwt'
const MOCK_STORAGE_KEY = 'scholarloop.mock'

function readStorage(key: string): string | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage.getItem(key) : null
  } catch {
    return null
  }
}

function writeStorage(key: string, value: string | null): void {
  try {
    if (typeof localStorage === 'undefined') return
    if (value === null) localStorage.removeItem(key)
    else localStorage.setItem(key, value)
  } catch {
    // Ignore private-browsing storage quota errors; in-memory state stays authoritative.
  }
}

function detectInitialMockMode(): boolean {
  if (metaEnv.VITE_API_MOCK === 'true' || metaEnv.VITE_API_MOCK === '1') return true
  if (typeof window !== 'undefined') {
    const param = new URLSearchParams(window.location.search).get('mock')
    if (param === '1' || param === 'true') return true
  }
  return readStorage(MOCK_STORAGE_KEY) === '1'
}

let memoryToken: string | null = readStorage(TOKEN_STORAGE_KEY)
let mockMode: boolean = detectInitialMockMode()
let customTransport: FetchTransport | null = null

const unauthorizedListeners = new Set<() => void>()
const mockModeListeners = new Set<(enabled: boolean) => void>()

export function getAccessToken(): string | null {
  return memoryToken
}

export function setAccessToken(token: string | null): void {
  memoryToken = token
  writeStorage(TOKEN_STORAGE_KEY, token)
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
  writeStorage(MOCK_STORAGE_KEY, enabled ? '1' : null)
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
  }
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
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
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

// --- Authenticated SSE reader (Step 10b seam) ------------------------------

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
