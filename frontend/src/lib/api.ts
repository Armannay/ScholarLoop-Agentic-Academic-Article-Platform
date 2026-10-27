// The console's view of the typed client.
//
// The transport and the wire types are generated from `docs/openapi/openapi.yaml`
// (`src/api/client.ts` + `src/api/openapi.gen.d.ts`, refreshed by `npm run api:gen`).
// This module is the seam the screens import, for two reasons:
//
//  1. The generated types mirror the *JSON Schema* exactly, including `$defs`
//     bookkeeping and optional-vs-required nuances that the payload does not have. The
//     screens use the `lib/domain` view types instead, so the narrowing happens here,
//     once, instead of at every call site.
//  2. Two conveniences: `submitDecision` (the client's `answerRunDecision`, named for
//     the checkpoint modal) and `startRun`, which lets the *mock* pick which ending a new
//     run has (completion, erosion stop, needs_input) through a header the real API
//     ignores.

import * as client from '../api/client'
import type {
  ArticleView,
  Brief,
  LedgerView,
  ProjectView,
  QualityView,
  RestoreRequest,
  RestoreView,
  RunEventList,
  RunList,
  RunUsageView,
  RunView,
  SectionDiffView,
  SectionHistoryView,
  SnapshotList,
  StopOption,
} from './domain'

export { ApiError, getAccessToken, setAccessToken, subscribeUnauthorized } from '../api/client'
export { fetchHealth, fetchCurrentUser, loginUser, registerUser } from '../api/client'

/** A project as the screens read it (the spec's view plus the stored record). */
export type Project = ProjectView & {
  owner_id?: string | undefined
  brief?: Brief | undefined
  policy_overrides?: Record<string, unknown> | undefined
  status?: 'active' | 'archived' | undefined
}

export interface ProjectPage {
  projects: Project[]
  total: number
  next_cursor?: string | null | undefined
  has_more?: boolean | undefined
}

export interface UserDecisionRequest {
  option: StopOption
  extra_tokens?: number | null | undefined
  extra_iterations?: number | null | undefined
  new_goal?: string | null | undefined
  note?: string | null | undefined
  wait?: boolean | undefined
}

const narrow = <T>(value: unknown): T => value as T

export async function listProjects(signal?: AbortSignal): Promise<ProjectPage> {
  return narrow<ProjectPage>(await client.listProjects(signal))
}

export async function createProject(payload: {
  name: string
  description?: string | null
  brief: Brief
  policy_overrides?: Record<string, unknown>
  status?: 'active' | 'archived'
}): Promise<Project> {
  return narrow<Project>(
    await client.createProject(payload as unknown as Parameters<typeof client.createProject>[0]),
  )
}

export async function getProject(projectId: string, signal?: AbortSignal): Promise<Project> {
  return narrow<Project>(await client.getProject(projectId, signal))
}

export async function listProjectRuns(projectId: string, signal?: AbortSignal): Promise<RunList> {
  return client.listProjectRuns(projectId, signal)
}

export async function getRun(runId: string, signal?: AbortSignal): Promise<RunView> {
  return narrow<RunView>(await client.getRun(runId, signal))
}

export async function stopRun(runId: string): Promise<RunView> {
  return narrow<RunView>(await client.stopRun(runId))
}

export async function getRunEventHistory(
  runId: string,
  params?: { after?: number; limit?: number },
  signal?: AbortSignal,
): Promise<RunEventList> {
  return client.getRunEventHistory(runId, params, signal)
}

export async function getRunLedger(runId: string, signal?: AbortSignal): Promise<LedgerView> {
  return narrow<LedgerView>(await client.getRunLedger(runId, signal))
}

export async function getRunQuality(runId: string, signal?: AbortSignal): Promise<QualityView> {
  return narrow<QualityView>(await client.getRunQuality(runId, signal))
}

export async function getRunUsage(runId: string, signal?: AbortSignal): Promise<RunUsageView> {
  return client.getRunUsage(runId, signal)
}

export async function getRunArticle(runId: string, signal?: AbortSignal): Promise<ArticleView> {
  return narrow<ArticleView>(await client.getRunArticle(runId, signal))
}

export async function getSectionHistory(
  runId: string,
  sectionId: string,
  signal?: AbortSignal,
): Promise<SectionHistoryView> {
  return client.getSectionHistory(runId, sectionId, signal)
}

export async function getSectionDiff(
  runId: string,
  sectionId: string,
  params?: { from?: number; to?: number },
  signal?: AbortSignal,
): Promise<SectionDiffView> {
  return client.getSectionDiff(runId, sectionId, params, signal)
}

export async function listArticleSnapshots(
  runId: string,
  signal?: AbortSignal,
): Promise<SnapshotList> {
  return client.listArticleSnapshots(runId, signal)
}

export async function restoreArticleSnapshot(
  runId: string,
  payload: { snapshot_id: string; dry_run?: boolean },
): Promise<RestoreView> {
  return client.restoreArticleSnapshot(runId, payload as RestoreRequest)
}

/** Answer a checkpoint or a stop. */
export async function submitDecision(
  runId: string,
  payload: UserDecisionRequest,
): Promise<RunView> {
  return narrow<RunView>(
    await client.answerRunDecision(
      runId,
      payload as Parameters<typeof client.answerRunDecision>[1],
    ),
  )
}

/**
 * Start a run, optionally choosing a mock scenario.
 *
 * `scenario` is only meaningful against the in-memory mock (`VITE_API_MOCK=true` or the
 * test fetch stub); the real API ignores the header and runs the loop.
 */
export async function createRun(
  projectId: string,
  payload: Record<string, unknown>,
  scenario?: string,
): Promise<RunView> {
  const headers: Record<string, string> = {
    accept: 'application/json',
    'content-type': 'application/json',
  }
  const token = client.getAccessToken()
  if (token) headers.authorization = `Bearer ${token}`
  if (scenario) headers['x-scholarloop-scenario'] = scenario
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/runs`, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
  })
  const text = await response.text()
  const parsed: unknown = text ? JSON.parse(text) : null
  if (!response.ok) {
    const body = parsed as
      | { error?: { code?: string; message?: string; context?: Record<string, unknown> } }
      | null
    throw new client.ApiError(
      body?.error?.message ?? `request failed with ${response.status}`,
      response.status,
      body?.error?.code,
      body?.error?.context,
    )
  }
  return narrow<RunView>(parsed)
}

/** The SSE URL for a run's event log (the shell's `useEventSource` takes a URL). */
export function runEventsUrl(
  runId: string,
  options: { after?: number; once?: boolean } = {},
): string {
  const params = new URLSearchParams()
  if (options.after !== undefined) params.set('after', String(options.after))
  if (options.once) params.set('once', 'true')
  const query = params.toString()
  return `/api/runs/${encodeURIComponent(runId)}/events${query ? `?${query}` : ''}`
}
