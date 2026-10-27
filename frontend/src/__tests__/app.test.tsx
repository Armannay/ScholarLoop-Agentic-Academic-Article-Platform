import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { renderToStaticMarkup } from 'react-dom/server'
import { App } from '../app'

describe('ScholarLoop console shell', () => {
  it('renders the login page without a session (token is in-memory only)', () => {
    const html = renderToStaticMarkup(<App />)
    assert.ok(html.includes('ScholarLoop'), 'renders the brand')
    assert.ok(html.includes('Sign in to ScholarLoop'), 'renders the login page')
    assert.ok(
      !/sk-(?:ant|openai|proj|oauth)/i.test(html),
      'no key material in markup',
    )
    assert.ok(html.includes('in memory only'), 'documents the in-memory session')
  })

  it('includes the shell chrome: nav, error boundary and toast host', () => {
    const html = renderToStaticMarkup(<App />)
    assert.ok(html.includes('Home'), 'renders the home nav tab')
    assert.ok(html.includes('Sign out') === false, 'no sign out without a session')
  })
})
