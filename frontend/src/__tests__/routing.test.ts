import { describe, expect, it } from 'vitest'
import { matchPath, navigate } from '../router'

describe('hash routing (every screen has a URL)', () => {
  it('matches static routes exactly', () => {
    expect(matchPath('/projects', '/projects')).toEqual({})
    expect(matchPath('/projects', '/projects/new')).toBeNull()
    expect(matchPath('/', '/')).toEqual({})
    expect(matchPath('/', '/projects')).toBeNull()
  })

  it('captures named parameters', () => {
    expect(matchPath('/projects/:projectId', '/projects/01M5Z0X96Y46B0GTT36PSYZBQA')).toEqual({
      projectId: '01M5Z0X96Y46B0GTT36PSYZBQA',
    })
    expect(matchPath('/runs/:runId', '/runs/01M5Z0X96Y000000000000005H')).toEqual({
      runId: '01M5Z0X96Y000000000000005H',
    })
    expect(matchPath('/runs/:runId/article', '/runs/RUN1/article')).toEqual({ runId: 'RUN1' })
    expect(matchPath('/runs/:runId/article', '/runs/RUN1')).toBeNull()
  })

  it('decodes percent-encoded segments and survives trailing slashes', () => {
    expect(matchPath('/projects/:projectId', '/projects/a%20b')).toEqual({ projectId: 'a b' })
    expect(matchPath('/projects', '/projects/')).toEqual({})
  })

  it('writes the hash through one entry point', () => {
    window.history.replaceState(null, '', '#/')
    navigate('/runs/RUN1/article')
    expect(window.location.hash).toBe('#/runs/RUN1/article')
  })
})
