import { describe, expect, it } from 'vitest'
import { parseRoute, routeToHash } from '../App'

describe('hash routing (every screen has a URL)', () => {
  it('parses the project, run and article routes', () => {
    expect(parseRoute('')).toEqual({ page: 'projects' })
    expect(parseRoute('#projects')).toEqual({ page: 'projects' })
    expect(parseRoute('#projects/new')).toEqual({ page: 'new-project' })
    expect(parseRoute('#projects/01M5Z0X96Y46B0GTT36PSYZBQA')).toEqual({
      page: 'project',
      projectId: '01M5Z0X96Y46B0GTT36PSYZBQA',
    })
    expect(parseRoute('#runs/01M5Z0X96Y000000000000005H')).toEqual({
      page: 'run',
      runId: '01M5Z0X96Y000000000000005H',
    })
    expect(parseRoute('#runs/01M5Z0X96Y000000000000005H/article')).toEqual({
      page: 'article',
      runId: '01M5Z0X96Y000000000000005H',
    })
  })

  it('keeps the pre-existing console routes working', () => {
    expect(parseRoute('#accounts')).toEqual({ page: 'accounts' })
    expect(parseRoute('#/agents')).toEqual({ page: 'agents' })
    expect(parseRoute('#status')).toEqual({ page: 'status' })
  })

  it('falls back to the project list instead of rendering a blank page', () => {
    expect(parseRoute('#nonsense')).toEqual({ page: 'projects' })
    expect(parseRoute('#runs/')).toEqual({ page: 'projects' })
  })

  it('round-trips a route through its hash', () => {
    const routes = [
      { page: 'projects' as const },
      { page: 'new-project' as const },
      { page: 'project' as const, projectId: 'abc' },
      { page: 'run' as const, runId: 'RUN1' },
      { page: 'article' as const, runId: 'RUN1' },
      { page: 'accounts' as const },
    ]
    for (const route of routes) {
      expect(parseRoute(routeToHash(route))).toEqual(route)
    }
  })
})
