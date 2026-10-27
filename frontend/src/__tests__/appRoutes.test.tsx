import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { setAccessToken } from '../api/client'
import { MockApiServer } from '../api/mockServer'
import { App } from '../app'
import { installMockFetch, signIn } from '../test/helpers'

let server: MockApiServer

beforeEach(() => {
  server = new MockApiServer()
  // No session leaks between tests: the token lives in the client's memory only.
  setAccessToken(null)
  installMockFetch(server)
  window.history.replaceState(null, '', '#/')
})

/**
 * The shell as a whole: hash routing, auth hydration, and the walk a user makes in one
 * session. This is the browser-less twin of `playwright/step-10b.spec.ts`.
 */
describe('App shell navigation (projects -> run -> article)', () => {
  it('shows the login page for any route when nobody is signed in', async () => {
    window.history.replaceState(null, '', '#/runs/01M5Z0X96Y000000000000005H')
    render(<App />)
    expect(await screen.findByText('Sign in to ScholarLoop')).toBeTruthy()
  })

  it('walks from the project list to the article viewer without losing the session', async () => {
    await signIn(server, 'walk@example.org')
    window.history.replaceState(null, '', '#/projects')
    render(<App />)

    expect(await screen.findByText('Peer review checklists 2026')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Peer review checklists 2026' }))
    await waitFor(() => {
      expect(window.location.hash).toMatch(/^#\/projects\/.+/)
    })
    expect(await screen.findByRole('button', { name: /start a run/i })).toBeTruthy()

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Open' }).length).toBe(2)
    })
    fireEvent.click(screen.getAllByRole('button', { name: 'Open' })[0]!)
    await waitFor(() => {
      expect(window.location.hash).toMatch(/^#\/runs\/.+/)
    })
    expect(await screen.findByRole('heading', { name: /^Run / })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /open article/i }))
    await waitFor(() => {
      expect(window.location.hash).toMatch(/^#\/runs\/.+\/article$/)
    })
    expect(await screen.findByRole('button', { name: /export \.md/i })).toBeTruthy()
    expect(await screen.findByText('Methodology')).toBeTruthy()
  })

  it('keeps the shell home reachable and unknown routes honest', async () => {
    await signIn(server, 'home@example.org')
    window.history.replaceState(null, '', '#/nope/nope')
    render(<App />)
    expect(await screen.findByText('Not found')).toBeTruthy()
  })
})
