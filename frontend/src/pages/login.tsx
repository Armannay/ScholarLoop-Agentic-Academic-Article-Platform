import { useState } from 'react'
import { useAuth } from '../api/auth'
import { ApiError } from '../api/client'
import { useToast } from '../components/Toast'
import { navigate } from '../router'

export function Login(): React.JSX.Element {
  const { login, register, loading, sessionNotice } = useAuth()
  const { toast } = useToast()
  const [mode, setMode] = useState<'signin' | 'register'>('signin')
  const [email, setEmail] = useState<string>('')
  const [password, setPassword] = useState<string>('')
  const [error, setError] = useState<string>('')

  const submit = async (event: React.FormEvent, action: 'signin' | 'register'): Promise<void> => {
    event.preventDefault()
    setError('')
    try {
      await (action === 'signin' ? login : register)(email, password)
      navigate('/')
    } catch (cause) {
      const message =
        cause instanceof ApiError
          ? `${cause.code}: ${cause.message}`
          : cause instanceof Error
            ? cause.message
            : String(cause)
      setError(message)
      toast({ title: 'Sign in failed', message, variant: 'bad' })
    }
  }

  return (
    <section className="panel auth-card">
      <form className="form-stack" onSubmit={(event) => submit(event, mode)}>
        <h2>{mode === 'signin' ? 'Sign in to ScholarLoop' : 'Create an account'}</h2>
        {sessionNotice && (
          <p className="notice notice-warn">{sessionNotice}</p>
        )}
        {error && <p className="notice notice-bad">{error}</p>}
        <div className="field">
          <label className="field-label" htmlFor="login-email">
            Email
          </label>
          <input
            id="login-email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="login-password">
            Password
          </label>
          <input
            id="login-password"
            type="password"
            autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
            minLength={10}
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          <p className="field-hint">At least 10 characters. The session lives in memory only; closing the tab ends it.</p>
        </div>
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={loading}>
            {loading ? 'Working…' : mode === 'signin' ? 'Sign in' : 'Create account'}
          </button>
          <button
            type="button"
            className="btn btn-link"
            onClick= {() => setMode(mode === 'signin' ? 'register' : 'signin')}
          >
            {mode === 'signin' ? 'Need an account? Register' : 'Already have an account? Sign in'}
          </button>
        </div>
      </form>
      <p className="mono small mt-8">
        No backend? Enable the Prism mock (<code>npm run mock</code>) and the dev proxy sends
        <code>/api</code> to it.
      </p>
    </section>
  )
}
