// Thin fetch wrapper around the ScholarLoop API.
//
// Requests go to a *relative* path by default, so they are same-origin and the Vite
// dev server proxies /api to the backend. That matters in a sandboxed preview: the
// browser is not where the backend runs, so it must never be handed "localhost:8000".
// VITE_API_BASE_URL exists for deployments that serve the API from another origin.

import type {
  ConfigResponse,
  ContractListResponse,
  HealthResponse,
} from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export class ApiError extends Error {
  readonly status: number
  readonly code: string | undefined

  constructor(message: string, status: number, code?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: 'application/json' },
    ...init,
  })

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
    const detail = extractDetail(payload)
    throw new ApiError(detail.message, response.status, detail.code)
  }
  return payload as T
}

function extractDetail(payload: unknown): { message: string; code?: string } {
  if (payload && typeof payload === 'object') {
    const record = payload as Record<string, unknown>
    const error = record.error
    if (error && typeof error === 'object') {
      const nested = error as Record<string, unknown>
      const message = typeof nested.message === 'string' ? nested.message : 'request failed'
      const code = typeof nested.code === 'string' ? nested.code : undefined
      return code === undefined ? { message } : { message, code }
    }
    const detail = record.detail
    if (detail && typeof detail === 'object') {
      const nested = detail as Record<string, unknown>
      const message = typeof nested.message === 'string' ? nested.message : 'request failed'
      const code = typeof nested.code === 'string' ? nested.code : undefined
      return code === undefined ? { message } : { message, code }
    }
    if (typeof detail === 'string') return { message: detail }
  }
  return { message: 'request failed' }
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return request<HealthResponse>('/api/health', signal ? { signal } : undefined)
}

export async function fetchConfig(signal?: AbortSignal): Promise<ConfigResponse> {
  return request<ConfigResponse>('/api/config', signal ? { signal } : undefined)
}

export async function fetchContracts(signal?: AbortSignal): Promise<ContractListResponse> {
  return request<ContractListResponse>('/api/contracts', signal ? { signal } : undefined)
}

export async function fetchContractSchema(name: string, signal?: AbortSignal): Promise<unknown> {
  return request<unknown>(`/api/contracts/${encodeURIComponent(name)}/schema`, signal ? { signal } : undefined)
}
