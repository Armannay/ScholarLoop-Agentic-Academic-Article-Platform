import { useState, type FormEvent } from 'react'
import {
  ACCOUNT_PROVIDERS,
  ACCOUNT_STATUSES,
  ALL_BINDING_ROLES,
  type AccountCreate,
  type AccountPatch,
  type AccountProvider,
  type AccountStatus,
  type AccountTestResult,
  type AccountView,
  type BindingRole,
  type BindingView,
  type DeleteResult,
} from './types'
import { ApiError, createAccount, deleteAccount, patchAccount, testAccount } from './api'
import { StatusPill, type Tone } from './StatusPill'

const PROVIDER_LABELS: Record<AccountProvider, string> = {
  anthropic: 'Anthropic (Claude Console)',
  openai_compatible: 'OpenAI-compatible',
  mock: 'MockProvider',
}

function statusTone(status: AccountStatus): Tone {
  return status === 'active' ? 'ok' : status === 'degraded' ? 'warn' : 'bad'
}

function formatBudget(budget: { tokens: number | null; usd: number | null }): string {
  const parts: string[] = []
  if (budget.tokens !== null) parts.push(`${budget.tokens.toLocaleString()} tok/mo`)
  if (budget.usd !== null) parts.push(`$${budget.usd.toFixed(2)}/mo`)
  return parts.length > 0 ? parts.join(' · ') : 'Unlimited'
}

function parseBudgetInputs(
  tokensText: string,
  usdText: string,
): { ok: true; budget: { tokens: number | null; usd: number | null } } | { ok: false; error: string } {
  let tokens: number | null = null
  let usd: number | null = null
  if (tokensText.trim() !== '') {
    const n = Number(tokensText.trim())
    if (!Number.isInteger(n) || n < 1_000 || n > 1_000_000_000) {
      return { ok: false, error: 'Monthly token budget must be an integer in 1,000..1,000,000,000.' }
    }
    tokens = n
  }
  if (usdText.trim() !== '') {
    const n = Number(usdText.trim())
    if (!Number.isFinite(n) || n <= 0 || n > 1_000_000) {
      return { ok: false, error: 'Monthly USD budget must be > 0 and <= 1,000,000.' }
    }
    usd = n
  }
  return { ok: true, budget: { tokens, usd } }
}

interface DeleteConflictState {
  account: AccountView
  roles: string[]
  message: string
  mode: 'replace' | 'disable'
  replacementId: string
  error: string | null
  submitting: boolean
}

export interface AccountsPageProps {
  accounts: readonly AccountView[]
  bindings: Readonly<Record<BindingRole, BindingView | null>>
  loading: boolean
  error: string | null
  onRefresh: () => void
}

function BudgetFields({
  tokens,
  usd,
  onTokens,
  onUsd,
}: {
  tokens: string
  usd: string
  onTokens: (val: string) => void
  onUsd: (val: string) => void
}): React.JSX.Element {
  return (
    <>
      <label className="field">
        <span className="field-label">Monthly token budget (blank = unlimited)</span>
        <input
          type="number"
          min={1000}
          max={1000000000}
          step={1000}
          placeholder="Unlimited (1,000 – 1,000,000,000)"
          value={tokens}
          onChange={(e) => onTokens(e.target.value)}
        />
      </label>
      <label className="field">
        <span className="field-label">Monthly USD budget (blank = unlimited)</span>
        <input
          type="number"
          min={0.01}
          max={1000000}
          step="0.01"
          placeholder="Unlimited (e.g. 50.00)"
          value={usd}
          onChange={(e) => onUsd(e.target.value)}
        />
      </label>
    </>
  )
}

export function AccountsPage({
  accounts,
  bindings,
  loading,
  error,
  onRefresh,
}: AccountsPageProps): React.JSX.Element {
  const [showAdd, setShowAdd] = useState(false)
  const [addProvider, setAddProvider] = useState<AccountProvider>('anthropic')
  const [addLabel, setAddLabel] = useState('')
  const [addKey, setAddKey] = useState('')
  const [addBaseUrl, setAddBaseUrl] = useState('')
  const [addBudgetTokens, setAddBudgetTokens] = useState('')
  const [addBudgetUsd, setAddBudgetUsd] = useState('')
  const [addBusy, setAddBusy] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)

  const [editing, setEditing] = useState<AccountView | null>(null)
  const [editLabel, setEditLabel] = useState('')
  const [editStatus, setEditStatus] = useState<AccountStatus>('active')
  const [editBaseUrl, setEditBaseUrl] = useState('')
  const [editKey, setEditKey] = useState('')
  const [editBudgetTokens, setEditBudgetTokens] = useState('')
  const [editBudgetUsd, setEditBudgetUsd] = useState('')
  const [editBusy, setEditBusy] = useState(false)
  const [editError, setEditError] = useState<string | null>(null)

  const [testingId, setTestingId] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, AccountTestResult>>({})
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [conflict, setConflict] = useState<DeleteConflictState | null>(null)
  const [banner, setBanner] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null)

  const rolesForAccount = (accountId: string): BindingRole[] =>
    ALL_BINDING_ROLES.filter((role) => bindings[role]?.account_ids.includes(accountId))

  const openEdit = (acc: AccountView): void => {
    setEditing(acc)
    setEditLabel(acc.label)
    setEditStatus(acc.status)
    setEditBaseUrl(acc.base_url ?? '')
    setEditKey('')
    setEditBudgetTokens(acc.budget.tokens !== null ? String(acc.budget.tokens) : '')
    setEditBudgetUsd(acc.budget.usd !== null ? String(acc.budget.usd) : '')
    setEditError(null)
  }

  const handleCreate = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault()
    const budgetCheck = parseBudgetInputs(addBudgetTokens, addBudgetUsd)
    if (!budgetCheck.ok) {
      setAddError(budgetCheck.error)
      return
    }
    if (addProvider === 'openai_compatible' && !/^https?:\/\//.test(addBaseUrl.trim())) {
      setAddError('OpenAI-compatible accounts require an absolute http:// or https:// Base URL.')
      return
    }
    setAddBusy(true)
    setAddError(null)
    try {
      const secret = addKey
      setAddKey('')
      const payload: AccountCreate = {
        provider: addProvider,
        label: addLabel.trim(),
        api_key: secret,
        budget: budgetCheck.budget,
      }
      if (addProvider === 'openai_compatible') payload.base_url = addBaseUrl.trim()
      const created = await createAccount(payload)
      setAddLabel('')
      setAddBaseUrl('')
      setAddBudgetTokens('')
      setAddBudgetUsd('')
      setShowAdd(false)
      setBanner({
        tone: 'ok',
        text: `Added "${created.label}" (${created.key_mask}, fingerprint ${created.key_fingerprint}).`,
      })
      onRefresh()
    } catch (cause) {
      setAddError(cause instanceof Error ? cause.message : 'Failed to add account')
    } finally {
      setAddBusy(false)
    }
  }

  const handlePatch = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault()
    if (!editing) return
    const budgetCheck = parseBudgetInputs(editBudgetTokens, editBudgetUsd)
    if (!budgetCheck.ok) {
      setEditError(budgetCheck.error)
      return
    }
    if (editing.provider === 'openai_compatible' && !/^https?:\/\//.test(editBaseUrl.trim())) {
      setEditError('OpenAI-compatible accounts require an absolute http:// or https:// Base URL.')
      return
    }
    setEditBusy(true)
    setEditError(null)
    try {
      const rotatedSecret = editKey.trim()
      setEditKey('')
      const payload: AccountPatch = {
        label: editLabel.trim(),
        status: editStatus,
        budget: budgetCheck.budget,
      }
      if (editing.provider === 'openai_compatible') payload.base_url = editBaseUrl.trim()
      if (rotatedSecret.length > 0) payload.api_key = rotatedSecret
      const updated = await patchAccount(editing.id, payload)
      const note = rotatedSecret ? ` Key rotated → fingerprint ${updated.key_fingerprint}.` : ''
      setEditing(null)
      setBanner({ tone: 'ok', text: `Updated "${updated.label}".${note}` })
      onRefresh()
    } catch (cause) {
      setEditError(cause instanceof Error ? cause.message : 'Failed to update account')
    } finally {
      setEditBusy(false)
    }
  }

  const handleTest = async (acc: AccountView): Promise<void> => {
    setTestingId(acc.id)
    try {
      const result = await testAccount(acc.id)
      setTestResults((prev) => ({ ...prev, [acc.id]: result }))
    } catch (cause) {
      setTestResults((prev) => ({
        ...prev,
        [acc.id]: {
          ok: false,
          account_id: acc.id,
          provider: acc.provider,
          model: null,
          latency_ms: 0,
          error_code: cause instanceof ApiError ? (cause.code ?? 'error') : 'error',
          error_message: cause instanceof Error ? cause.message : 'Test request failed',
        },
      }))
    } finally {
      setTestingId(null)
    }
  }

  const formatDeleteSummary = (label: string, res: DeleteResult): string => {
    const details: string[] = []
    if (res.replaced_in.length > 0) details.push(`replaced in: ${res.replaced_in.join(', ')}`)
    if (res.disabled_roles.length > 0) details.push(`disabled roles: ${res.disabled_roles.join(', ')}`)
    if (res.removed_from.length > 0) details.push(`removed from: ${res.removed_from.join(', ')}`)
    return `Deleted "${label}"${details.length > 0 ? ` (${details.join('; ')})` : ''}.`
  }

  const eligibleReplacements = (target: AccountView, affectedRoles: readonly string[]): AccountView[] => {
    const plannerBound = affectedRoles.includes('planner')
    return accounts.filter(
      (cand) =>
        cand.id !== target.id &&
        cand.status !== 'disabled' &&
        (!plannerBound || cand.provider === 'anthropic' || cand.provider === 'mock'),
    )
  }

  const handleDeleteAttempt = async (acc: AccountView): Promise<void> => {
    setDeletingId(acc.id)
    setBanner(null)
    try {
      const res = await deleteAccount(acc.id)
      setBanner({ tone: 'ok', text: formatDeleteSummary(acc.label, res) })
      onRefresh()
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        const ctxRoles = Array.isArray(cause.context?.roles)
          ? (cause.context.roles as unknown[]).filter((r): r is string => typeof r === 'string')
          : rolesForAccount(acc.id)
        const candidates = eligibleReplacements(acc, ctxRoles)
        setConflict({
          account: acc,
          roles: ctxRoles,
          message: cause.message,
          mode: candidates.length > 0 ? 'replace' : 'disable',
          replacementId: candidates[0]?.id ?? '',
          error: null,
          submitting: false,
        })
      } else {
        setBanner({ tone: 'bad', text: cause instanceof Error ? cause.message : 'Delete failed' })
      }
    } finally {
      setDeletingId(null)
    }
  }

  const handleResolveConflict = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault()
    if (!conflict) return
    if (conflict.mode === 'replace' && !conflict.replacementId) {
      setConflict({ ...conflict, error: 'Select an active replacement account or choose Disable roles.' })
      return
    }
    setConflict({ ...conflict, submitting: true, error: null })
    try {
      const res =
        conflict.mode === 'replace'
          ? await deleteAccount(conflict.account.id, { replacementAccountId: conflict.replacementId })
          : await deleteAccount(conflict.account.id, { disableRoles: true })
      const label = conflict.account.label
      setConflict(null)
      setBanner({ tone: 'ok', text: formatDeleteSummary(label, res) })
      onRefresh()
    } catch (cause) {
      setConflict((prev) =>
        prev
          ? { ...prev, submitting: false, error: cause instanceof Error ? cause.message : 'Delete failed' }
          : null,
      )
    }
  }

  const conflictCandidates = conflict ? eligibleReplacements(conflict.account, conflict.roles) : []

  return (
    <div className="stack">
      <section className="panel">
        <div className="panel-head-wrap">
          <div>
            <h2>Provider Accounts</h2>
            <p className="subtitle">
              Keys are encrypted at rest with AES-256-GCM and resolved at call time. Only the 4-char
              mask and 12-char ciphertext fingerprint leave the API.
            </p>
          </div>
          <div className="btn-row">
            <button type="button" className="btn" onClick={onRefresh} disabled={loading}>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                setShowAdd((open) => !open)
                setAddError(null)
              }}
            >
              {showAdd ? 'Close form' : '+ Add account'}
            </button>
          </div>
        </div>

        {banner && (
          <div className={banner.tone === 'ok' ? 'notice notice-ok' : 'notice notice-bad'} role="status">
            <span>{banner.text}</span>
            <button type="button" className="btn-link" onClick={() => setBanner(null)}>
              dismiss
            </button>
          </div>
        )}

        {error && (
          <div className="notice notice-bad" role="alert">
            {error}
          </div>
        )}

        {showAdd && (
          <form onSubmit={(e) => void handleCreate(e)} className="subpanel form-stack">
            <h3>Add Provider Credential</h3>
            {addError && (
              <div className="notice notice-bad" role="alert">
                {addError}
              </div>
            )}
            <div className="form-grid">
              <label className="field">
                <span className="field-label">Provider</span>
                <select
                  value={addProvider}
                  onChange={(e) => setAddProvider(e.target.value as AccountProvider)}
                >
                  {ACCOUNT_PROVIDERS.map((p) => (
                    <option key={p} value={p}>
                      {PROVIDER_LABELS[p]}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field">
                <span className="field-label">Label</span>
                <input
                  type="text"
                  required
                  maxLength={64}
                  placeholder="e.g. lab-anthropic"
                  value={addLabel}
                  onChange={(e) => setAddLabel(e.target.value)}
                />
              </label>

              <label className="field field-span-2">
                <span className="field-label">API key (write-only)</span>
                <input
                  type="password"
                  required
                  minLength={8}
                  maxLength={512}
                  autoComplete="off"
                  placeholder={
                    addProvider === 'anthropic'
                      ? 'Claude Console key (console.anthropic.com)'
                      : addProvider === 'mock'
                        ? 'mock-placeholder-key'
                        : 'Bearer API key'
                  }
                  value={addKey}
                  onChange={(e) => setAddKey(e.target.value)}
                />
                <span className="field-hint">
                  {addProvider === 'anthropic'
                    ? 'Requires a Claude Console API key. Claude Pro/Max subscription session tokens are rejected.'
                    : addProvider === 'mock'
                      ? 'Deterministic local MockProvider — any 8+ char placeholder string is accepted.'
                      : 'Encrypted immediately; never returned in any API response or log.'}
                </span>
              </label>

              {addProvider === 'openai_compatible' && (
                <label className="field field-span-2">
                  <span className="field-label">Base URL</span>
                  <input
                    type="url"
                    required
                    maxLength={500}
                    placeholder="https://openrouter.ai/api/v1"
                    value={addBaseUrl}
                    onChange={(e) => setAddBaseUrl(e.target.value)}
                  />
                </label>
              )}

              <BudgetFields
                tokens={addBudgetTokens}
                usd={addBudgetUsd}
                onTokens={setAddBudgetTokens}
                onUsd={setAddBudgetUsd}
              />
            </div>

            <div className="form-actions">
              <button type="submit" className="btn btn-primary" disabled={addBusy}>
                {addBusy ? 'Encrypting & saving…' : 'Save account'}
              </button>
              <button type="button" className="btn" onClick={() => setShowAdd(false)}>
                Cancel
              </button>
            </div>
          </form>
        )}

        {accounts.length === 0 ? (
          <div className="empty-state">
            {loading
              ? 'Loading provider accounts…'
              : 'No provider accounts configured yet. Add an Anthropic Console, OpenAI-compatible, or MockProvider account to bind agent roles.'}
          </div>
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Account</th>
                  <th>Provider</th>
                  <th>Status</th>
                  <th>Masked Key / Fingerprint</th>
                  <th>Monthly Budget</th>
                  <th>Bound Roles</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((acc) => {
                  const usedBy = rolesForAccount(acc.id)
                  const probe = testResults[acc.id]
                  return (
                    <tr key={acc.id}>
                      <td>
                        <div className="strong">{acc.label}</div>
                        {acc.base_url && <div className="mono dim small">{acc.base_url}</div>}
                      </td>
                      <td>
                        <span className="badge mono">{acc.provider}</span>
                      </td>
                      <td>
                        <StatusPill tone={statusTone(acc.status)} label={acc.status} />
                      </td>
                      <td className="mono">
                        <div>{acc.key_mask}</div>
                        <div className="dim small" title="SHA-256 prefix of AES-GCM ciphertext">
                          fp:{acc.key_fingerprint}
                        </div>
                      </td>
                      <td className="mono">{formatBudget(acc.budget)}</td>
                      <td>
                        {usedBy.length === 0 ? (
                          <span className="dim small">unbound</span>
                        ) : (
                          <div className="chips-inline">
                            {usedBy.map((r) => (
                              <span key={r} className="badge mono">
                                {r}
                              </span>
                            ))}
                          </div>
                        )}
                      </td>
                      <td>
                        <div className="btn-row">
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={testingId === acc.id}
                            onClick={() => void handleTest(acc)}
                          >
                            {testingId === acc.id ? 'Testing…' : 'Test connection'}
                          </button>
                          <button type="button" className="btn btn-sm" onClick={() => openEdit(acc)}>
                            Edit / Rotate
                          </button>
                          <button
                            type="button"
                            className="btn btn-sm btn-danger"
                            disabled={deletingId === acc.id}
                            onClick={() => void handleDeleteAttempt(acc)}
                          >
                            {deletingId === acc.id ? 'Deleting…' : 'Delete'}
                          </button>
                        </div>
                        {probe && (
                          <div className={probe.ok ? 'probe-result probe-ok mono' : 'probe-result probe-bad mono'}>
                            {probe.ok
                              ? `OK · ${probe.model ?? acc.provider} · ${probe.latency_ms} ms`
                              : `${probe.error_code ?? 'failed'}: ${probe.error_message ?? 'probe failed'} (${probe.latency_ms} ms)`}
                          </div>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {editing && (
        <section className="panel panel-accent" aria-label={`Edit account ${editing.label}`}>
          <div className="panel-head-wrap">
            <div>
              <h2>Edit & Rotate Key — {editing.label}</h2>
              <p className="subtitle mono">
                {editing.provider} · current mask {editing.key_mask} · fingerprint {editing.key_fingerprint}
              </p>
            </div>
            <button type="button" className="btn btn-sm" onClick={() => setEditing(null)}>
              Close
            </button>
          </div>

          {editError && (
            <div className="notice notice-bad" role="alert">
              {editError}
            </div>
          )}

          <form onSubmit={(e) => void handlePatch(e)} className="form-stack">
            <div className="form-grid">
              <label className="field">
                <span className="field-label">Label</span>
                <input
                  type="text"
                  required
                  maxLength={64}
                  value={editLabel}
                  onChange={(e) => setEditLabel(e.target.value)}
                />
              </label>

              <label className="field">
                <span className="field-label">Status</span>
                <select value={editStatus} onChange={(e) => setEditStatus(e.target.value as AccountStatus)}>
                  {ACCOUNT_STATUSES.map((st) => (
                    <option key={st} value={st}>
                      {st}
                    </option>
                  ))}
                </select>
              </label>

              {editing.provider === 'openai_compatible' && (
                <label className="field field-span-2">
                  <span className="field-label">Base URL</span>
                  <input
                    type="url"
                    required
                    maxLength={500}
                    value={editBaseUrl}
                    onChange={(e) => setEditBaseUrl(e.target.value)}
                  />
                </label>
              )}

              <label className="field field-span-2">
                <span className="field-label">Rotate API key (optional)</span>
                <input
                  type="password"
                  minLength={8}
                  maxLength={512}
                  autoComplete="off"
                  placeholder={`Leave empty to keep ${editing.key_mask}`}
                  value={editKey}
                  onChange={(e) => setEditKey(e.target.value)}
                />
                <span className="field-hint">
                  Supplying a new key re-encrypts with a fresh DEK and updates the ciphertext fingerprint.
                </span>
              </label>

              <BudgetFields
                tokens={editBudgetTokens}
                usd={editBudgetUsd}
                onTokens={setEditBudgetTokens}
                onUsd={setEditBudgetUsd}
              />
            </div>

            <div className="form-actions">
              <button type="submit" className="btn btn-primary" disabled={editBusy}>
                {editBusy ? 'Saving…' : 'Save changes'}
              </button>
              <button type="button" className="btn" onClick={() => setEditing(null)}>
                Cancel
              </button>
            </div>
          </form>
        </section>
      )}

      {conflict && (
        <section className="panel panel-warn" role="dialog" aria-label="Resolve protected account deletion">
          <div className="panel-head-wrap">
            <div>
              <h2>409 Delete Protection — {conflict.account.label}</h2>
              <p className="subtitle">
                Deleting this credential would orphan enabled role binding(s):{' '}
                <strong className="mono">{conflict.roles.join(', ') || 'bound role'}</strong>.
              </p>
            </div>
            <button type="button" className="btn btn-sm" onClick={() => setConflict(null)}>
              Cancel
            </button>
          </div>

          <p className="mono small dim">{conflict.message}</p>

          {conflict.error && (
            <div className="notice notice-bad" role="alert">
              {conflict.error}
            </div>
          )}

          <form onSubmit={(e) => void handleResolveConflict(e)} className="form-stack">
            <label className="radio-card">
              <input
                type="radio"
                name="conflict-mode"
                checked={conflict.mode === 'replace'}
                disabled={conflictCandidates.length === 0}
                onChange={() => setConflict({ ...conflict, mode: 'replace', error: null })}
              />
              <div>
                <div className="strong">Swap in a replacement account, then delete</div>
                <div className="field-hint">
                  Passes <code>?replacement_account_id=…</code> so enabled roles keep running.
                  {conflict.roles.includes('planner') && ' (Planner requires active anthropic or mock.)'}
                </div>
                {conflictCandidates.length > 0 ? (
                  <select
                    className="mt-8"
                    value={conflict.replacementId}
                    disabled={conflict.mode !== 'replace'}
                    onChange={(e) => setConflict({ ...conflict, replacementId: e.target.value, error: null })}
                  >
                    {conflictCandidates.map((cand) => (
                      <option key={cand.id} value={cand.id}>
                        {cand.label} ({cand.provider} · {cand.key_mask})
                      </option>
                    ))}
                  </select>
                ) : (
                  <div className="small warn-text mt-8">
                    No eligible active replacement account available. Add another account or disable roles below.
                  </div>
                )}
              </div>
            </label>

            <label className="radio-card">
              <input
                type="radio"
                name="conflict-mode"
                checked={conflict.mode === 'disable'}
                onChange={() => setConflict({ ...conflict, mode: 'disable', error: null })}
              />
              <div>
                <div className="strong">Disable affected role(s) and delete</div>
                <div className="field-hint">
                  Passes <code>?disable_roles=true</code>; disables{' '}
                  <code className="mono">{conflict.roles.join(', ') || 'affected roles'}</code> and deletes{' '}
                  <strong>{conflict.account.label}</strong>.
                </div>
              </div>
            </label>

            <div className="form-actions">
              <button type="submit" className="btn btn-danger" disabled={conflict.submitting}>
                {conflict.submitting
                  ? 'Deleting…'
                  : conflict.mode === 'replace'
                    ? 'Replace & delete account'
                    : 'Disable roles & delete account'}
              </button>
              <button type="button" className="btn" onClick={() => setConflict(null)}>
                Keep account
              </button>
            </div>
          </form>
        </section>
      )}
    </div>
  )
}
