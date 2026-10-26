import { useState, type FormEvent } from 'react'
import { ApiError } from './api'
import { useAuth } from './auth'
import type { ValidationFieldError } from './types'

export function AuthPage({ mockMode }: { mockMode: boolean }): React.JSX.Element {
  const { login, register, sessionNotice } = useAuth()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<readonly ValidationFieldError[]>([])

  const switchMode = (next: 'login' | 'register'): void => {
    setMode(next)
    setError(null)
    setFieldErrors([])
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault()
    const cleanEmail = email.trim().toLowerCase()
    if (!cleanEmail) {
      setError('Email is required.')
      return
    }
    if (mode === 'register' && password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }
    setSubmitting(true)
    setError(null)
    setFieldErrors([])
    try {
      const secret = password
      setPassword('')
      if (mode === 'login') {
        await login(cleanEmail, secret)
      } else {
        await register(cleanEmail, secret)
      }
    } catch (cause) {
      if (cause instanceof ApiError) {
        setError(cause.message)
        setFieldErrors(cause.fieldErrors)
      } else {
        setError(cause instanceof Error ? cause.message : 'Authentication failed')
      }
    } finally {
      setSubmitting(false)
    }
  }

  const handleDemoSignIn = async (): Promise<void> => {
    setSubmitting(true)
    setError(null)
    setFieldErrors([])
    const demoEmail = 'operator@example.org'
    const demoCred = 'placeholder-demo-1'
    try {
      try {
        await login(demoEmail, demoCred)
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          await register(demoEmail, demoCred)
        } else {
          throw err
        }
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Demo sign-in failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="auth-wrap">
      <div className="panel auth-card">
        <div className="segmented" role="tablist" aria-label="Authentication mode">
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'login'}
            className={mode === 'login' ? 'seg-btn seg-active' : 'seg-btn'}
            onClick={() => switchMode('login')}
          >
            Sign in
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'register'}
            className={mode === 'register' ? 'seg-btn seg-active' : 'seg-btn'}
            onClick={() => switchMode('register')}
          >
            Create account
          </button>
        </div>

        <h2>{mode === 'login' ? 'Sign in to ScholarLoop' : 'Create operator account'}</h2>
        <p className="subtitle">
          Provider accounts and agent bindings are isolated per user. Passwords are verified with
          Argon2id; JWT bearer tokens authorise API requests.
        </p>

        {sessionNotice && <div className="notice notice-warn">{sessionNotice}</div>}

        {error && (
          <div className="notice notice-bad" role="alert">
            <div>{error}</div>
            {fieldErrors.length > 0 && (
              <ul className="list mono">
                {fieldErrors.map((fe, idx) => (
                  <li key={`${fe.loc}-${idx}`}>
                    {fe.loc}: {fe.msg}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        <form onSubmit={(e) => void handleSubmit(e)} className="form-stack">
          <label className="field">
            <span className="field-label">Email</span>
            <input
              type="email"
              required
              maxLength={255}
              autoComplete="email"
              placeholder="researcher@university.edu"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>

          <label className="field">
            <span className="field-label">Password</span>
            <input
              type="password"
              required
              minLength={mode === 'register' ? 8 : 1}
              maxLength={256}
              autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
              placeholder={mode === 'register' ? 'At least 8 characters' : 'Your password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            {mode === 'register' && (
              <span className="field-hint">Minimum 8 characters (up to 256).</span>
            )}
          </label>

          <div className="form-actions">
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting
                ? 'Submitting…'
                : mode === 'login'
                  ? 'Sign in'
                  : 'Register & sign in'}
            </button>
            {mockMode && (
              <button
                type="button"
                className="btn"
                disabled={submitting}
                onClick={() => void handleDemoSignIn()}
              >
                Quick demo sign-in (mock)
              </button>
            )}
          </div>
        </form>
      </div>
    </section>
  )
}
