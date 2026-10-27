import { vi } from 'vitest'
import { registerUser, setAccessToken } from '../api/client'
import { MockApiServer } from '../api/mockServer'
import { listProjectRuns, listProjects, type Project } from '../lib/api'
import type { RunSummary } from '../lib/domain'

export const TEST_PASSWORD = 'placeholder-pass'

/**
 * Route the client's `fetch` to the in-memory mock.
 *
 * The client always goes through `fetch`, so stubbing the global is the whole wiring -
 * and it means the tests exercise the same request path the browser does (headers,
 * error envelopes, SSE frames) instead of calling functions directly.
 */
export function installMockFetch(server: MockApiServer): void {
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) =>
    server.fetch(input as string, init),
  )
}

/** Fresh mock + a signed-in session (token in the client's memory only). */
export async function signIn(server: MockApiServer, email = 'operator@example.org'): Promise<void> {
  installMockFetch(server)
  setAccessToken(null)
  const response = await registerUser({ email, password: TEST_PASSWORD })
  setAccessToken(response.access_token)
}

/** The seeded demo projects are visible to every signed-in user. */
export async function seededProjects(): Promise<Project[]> {
  return (await listProjects()).projects
}

export async function seededProjectByName(fragment: string): Promise<Project> {
  const projects = await seededProjects()
  const match = projects.find((project) => project.name.includes(fragment))
  if (!match) throw new Error(`no seeded project matching ${fragment}`)
  return match
}

export async function runsOf(projectId: string): Promise<RunSummary[]> {
  return (await listProjectRuns(projectId)).runs
}
