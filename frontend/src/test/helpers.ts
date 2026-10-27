import { registerUser, setAccessToken, setCustomTransport } from '../api'
import { MockApiServer } from '../mockServer'
import type { ProjectView, RunSummary } from '../types'
import { listProjectRuns, listProjects } from '../api'

export const TEST_PASSWORD = 'placeholder-pass'

/** Point the client at a fresh in-memory mock and sign a user in. */
export async function signIn(server: MockApiServer, email = 'operator@example.org'): Promise<void> {
  setCustomTransport((url, init) => server.fetch(url, init))
  setAccessToken(null)
  const response = await registerUser({ email, password: TEST_PASSWORD })
  setAccessToken(response.access_token)
}

/** The seeded demo projects are visible to every signed-in user. */
export async function seededProjects(): Promise<ProjectView[]> {
  const page = await listProjects({ limit: 50 })
  return page.projects
}

export async function seededProjectByName(fragment: string): Promise<ProjectView> {
  const projects = await seededProjects()
  const match = projects.find((project) => project.name.includes(fragment))
  if (!match) throw new Error(`no seeded project matching ${fragment}`)
  return match
}

export async function runsOf(projectId: string): Promise<RunSummary[]> {
  const list = await listProjectRuns(projectId)
  return list.runs
}
