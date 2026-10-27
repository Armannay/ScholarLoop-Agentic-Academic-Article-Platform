import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { App } from '../App'
import { setAccessToken, setCustomTransport } from '../api'
import { MockApiServer } from '../mockServer'
import { signIn } from '../test/helpers'

let server: MockApiServer

beforeEach(() => {
  server = new MockApiServer()
  // No session leaks between tests: the token lives in memory + sessionStorage.
  setAccessToken(null)
  setCustomTransport((url, init) => server.fetch(url, init))
  window.history.replaceState(null, '', '#projects')
})

/**
 * The shell as a whole: hash routing, auth hydration, and the four screens a user walks
 * through in one session. This is the browser-less twin of `e2e/smoke.spec.ts`.
 */
describe('App shell navigation (projects -> run -> article)', () => {
  it('walks from the project list to the article viewer without losing the session', async () => {
    await signIn(server, 'walk@example.org')
    render(<App />)

    // Project list.
    expect(await screen.findByText('Peer review checklists 2026')).toBeTruthy()
    expect(window.location.hash).toBe('#projects')

    // Open the seeded project.
    fireEvent.click(screen.getByRole('button', { name: 'Peer review checklists 2026' }))
    expect(await screen.findByRole('button', { name: /start a run/i })).toBeTruthy()
    expect(window.location.hash).toMatch(/^#projects\/.+/)

    // Open its newest run.
    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Open' }).length).toBe(2)
    })
    fireEvent.click(screen.getAllByRole('button', { name: 'Open' })[0]!)
    await waitFor(() => {
      expect(window.location.hash).toMatch(/^#runs\/.+/)
    })
    expect(await screen.findByRole('heading', { name: /^Run / })).toBeTruthy()

    // And its article.
    fireEvent.click(screen.getByRole('button', { name: /open article/i }))
    await waitFor(() => {
      expect(window.location.hash).toMatch(/^#runs\/.+\/article$/)
    })
    expect(await screen.findByRole('button', { name: /export \.md/i })).toBeTruthy()
    expect(await screen.findByText('Methodology')).toBeTruthy()
  })

  it('shows the sign-in screen when nobody is signed in, whatever the hash says', async () => {
    window.history.replaceState(null, '', '#runs/01M5Z0X96Y000000000000005H')
    render(<App />)
    expect(await screen.findByText('Sign in to ScholarLoop')).toBeTruthy()
  })

  it('keeps the pre-existing console tabs reachable', async () => {
    await signIn(server, 'tabs@example.org')
    render(<App />)
    await screen.findByText('Peer review checklists 2026')
    fireEvent.click(screen.getByRole('button', { name: /accounts/i }))
    expect(await screen.findByRole('heading', { name: 'Provider Accounts' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /status & schemas/i }))
    expect(await screen.findByRole('heading', { name: 'Runtime' })).toBeTruthy()
  })
})
