// Account administration: add, disable, rotate and delete provider accounts.
//
// The key is write-only. It is typed here, sent once over the API, and never
// rendered again: the backend answers with a fingerprint, and that fingerprint is
// the only thing this panel ever shows. That is the whole point of HARD RULE 2 -
// the operator can prove which key is in use without the console ever holding it.

import { useCallback, useEffect, useState } from 'react'
import {
  createAccount,
  deleteAccount,
  fetchAccounts,
  rotateAccountKey,
  setAccountStatus,
} from './api'
import type { AccountPublic, AccountProvider, AccountStatus } from './types'
import { StatusPill } from './StatusPill'

const PROVIDERS: AccountProvider[] = ['anthropic', 'openai_compatible', 'mock']

function statusTone(status: AccountStatus): 'ok' | 'warn' | 'idle' {
  if (status === 'active') return 'ok'
  if (status === 'degraded') return 'warn'
  return 'idle'
}

export function AccountsPanel(): React.JSX.Element {
  const [accounts, setAccounts] = useState<AccountPublic[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [provider, setProvider] = useState<AccountProvider>('anthropic')
  const [label, setLabel] = useState('')
  const [key, setKey] = useState('')

  const load = useCallback(async (): Promise<void> => {
    try {
      setAccounts(await fetchAccounts())
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'request failed')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const run = async (action: () => Promise<unknown>): Promise<void> => {
    setBusy(true)
    try {
      await action()
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'request failed')
    } finally {
      setBusy(false)
    }
  }

  const onSubmit = (event: React.FormEvent): void => {
    event.preventDefault()
    const trimmedKey = key.trim()
    const trimmedLabel = label.trim()
    if (trimmedLabel === '') {
      setError('the account needs a label')
      return
    }
    void run(async () => {
      await createAccount({
        provider,
        label: trimmedLabel,
        ...(trimmedKey === '' ? {} : { key: trimmedKey }),
      })
      setLabel('')
      setKey('')
    })
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Provider accounts</h2>
        {accounts.length > 0 && <StatusPill tone="ok" label={`${accounts.length} configured`} />}
      </div>
      <p className="subtitle">
        Keys are encrypted by the account store and resolved at call time. The console only ever
        sees a fingerprint.
      </p>

      {error && <p className="mono bad">{error}</p>}

      {accounts.length > 0 && (
        <table className="table">
          <tbody>
            {accounts.map((account) => (
              <tr key={account.id}>
                <th scope="row">{account.label}</th>
                <td className="mono">{account.provider}</td>
                <td>
                  <StatusPill tone={statusTone(account.status)} label={account.status} />
                </td>
                <td className="mono dim">{account.key_fingerprint}</td>
                <td>
                  <span className="row-actions">
                    <button
                      type="button"
                      className="chip"
                      disabled={busy}
                      onClick={() =>
                        void run(() =>
                          setAccountStatus(
                            account.id,
                            account.status === 'disabled' ? 'active' : 'disabled',
                          ),
                        )
                      }
                    >
                      {account.status === 'disabled' ? 'enable' : 'disable'}
                    </button>
                    <button
                      type="button"
                      className="chip"
                      disabled={busy}
                      onClick={() => {
                        const next = window.prompt('New provider key (stored encrypted, never shown)')
                        if (next === null || next.trim() === '') return
                        void run(() => rotateAccountKey(account.id, next.trim()))
                      }}
                    >
                      rotate key
                    </button>
                    <button
                      type="button"
                      className="chip"
                      disabled={busy}
                      onClick={() => {
                        if (!window.confirm(`Delete the account "${account.label}"?`)) return
                        void run(() => deleteAccount(account.id))
                      }}
                    >
                      delete
                    </button>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <form className="account-form" onSubmit={onSubmit}>
        <label className="field">
          <span>provider</span>
          <select
            value={provider}
            onChange={(event) => setProvider(event.target.value as AccountProvider)}
          >
            {PROVIDERS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>label</span>
          <input
            value={label}
            placeholder="work-anthropic"
            onChange={(event) => setLabel(event.target.value)}
          />
        </label>
        <label className="field">
          <span>key</span>
          <input
            type="password"
            value={key}
            placeholder="only mock accounts may be added without one"
            autoComplete="off"
            onChange={(event) => setKey(event.target.value)}
          />
        </label>
        <button type="submit" className="chip chip-active" disabled={busy}>
          add account
        </button>
      </form>
    </section>
  )
}
