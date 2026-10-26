import { useEffect, useState, type FormEvent } from 'react'
import { putBinding } from './api'
import { StatusPill, type Tone } from './StatusPill'
import {
  BINDING_STRATEGIES,
  EXECUTING_AGENT_ROLES,
  PRIMARY_BINDING_ROLES,
  type AccountProvider,
  type AccountView,
  type BindingPut,
  type BindingRole,
  type BindingStrategy,
  type BindingView,
  type Policies,
} from './types'

const ROLE_DESCRIPTIONS: Record<BindingRole, string> = {
  planner:
    'Plans task waves and evaluates verdicts from the bounded claude.md digest. Never writes article text. Requires anthropic (Claude Console) or mock accounts.',
  architect: 'Designs section structure, ordering, and argument outline against the rubric.',
  researcher: 'Executes scoped search/DOI tools and produces verified SourceCards.',
  writer: 'Drafts and applies delta Patches to assigned sections within token budgets.',
  reviewer: 'Audits prose against rubric criteria, opening and closing tracked issues.',
  validator: 'Runs quality gates, citation checks, and emits the iteration QualityReport.',
  compactor: 'Optional LLM history compactor for older ledger entries when claude.md nears cap.',
}

const MODEL_SUGGESTIONS: readonly string[] = [
  'claude-opus-4',
  'claude-sonnet-4-5',
  'claude-sonnet-4',
  'claude-haiku-4',
  'gpt-4.1',
  'gpt-4.1-mini',
  'gpt-4o-mini',
  'mock-strong-1',
  'mock-default',
  'mock-cheap-1',
]

function suggestedModel(provider: AccountProvider | undefined, tier: string): string {
  if (provider === 'openai_compatible') {
    if (tier === 'strong') return 'gpt-4.1'
    if (tier === 'cheap') return 'gpt-4o-mini'
    return 'gpt-4.1-mini'
  }
  if (provider === 'mock') {
    if (tier === 'strong') return 'mock-strong-1'
    if (tier === 'cheap') return 'mock-cheap-1'
    return 'mock-default'
  }
  if (tier === 'strong') return 'claude-opus-4'
  if (tier === 'cheap') return 'claude-haiku-4'
  return 'claude-sonnet-4'
}

function isAccountAllowedForRole(role: BindingRole, acc: AccountView): boolean {
  if (role === 'planner') {
    return acc.provider === 'anthropic' || acc.provider === 'mock'
  }
  return true
}

export interface AgentsPageProps {
  accounts: readonly AccountView[]
  bindings: Readonly<Record<BindingRole, BindingView | null>>
  policies: Policies | null
  loading: boolean
  onRefresh: () => void
  onOpenAccounts: () => void
}

export function AgentsPage({
  accounts,
  bindings,
  policies,
  loading,
  onRefresh,
  onOpenAccounts,
}: AgentsPageProps): React.JSX.Element {
  const [showCompactor, setShowCompactor] = useState<boolean>(() => bindings.compactor !== null)
  const [quickBusy, setQuickBusy] = useState(false)
  const [quickMessage, setQuickMessage] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null)

  const defaultInputCap = policies?.call_input_cap ?? 24_000
  const unconfiguredPrimary = PRIMARY_BINDING_ROLES.filter((role) => bindings[role] === null)
  const activeAccounts = accounts.filter((a) => a.status !== 'disabled')

  const handleQuickBind = async (): Promise<void> => {
    setQuickBusy(true)
    setQuickMessage(null)
    try {
      const boundRoles: string[] = []
      const skippedRoles: string[] = []
      for (const role of unconfiguredPrimary) {
        const candidate = activeAccounts.find((a) => isAccountAllowedForRole(role, a))
        if (!candidate) {
          skippedRoles.push(role)
          continue
        }
        const tier = policies?.model_tiers[role] ?? (role === 'planner' ? 'strong' : 'mid')
        await putBinding(role, {
          account_ids: [candidate.id],
          strategy: 'dedicated',
          model: suggestedModel(candidate.provider, tier),
          max_input_tokens: defaultInputCap,
          max_output_tokens: 4_096,
          enabled: true,
        })
        boundRoles.push(role)
      }
      onRefresh()
      if (boundRoles.length > 0) {
        const suffix =
          skippedRoles.length > 0
            ? ` (skipped ${skippedRoles.join(', ')}: needs anthropic or mock account)`
            : ''
        setQuickMessage({
          tone: 'ok',
          text: `Configured default bindings for: ${boundRoles.join(', ')}${suffix}.`,
        })
      } else {
        setQuickMessage({
          tone: 'bad',
          text: 'No eligible active accounts found for the unconfigured roles.',
        })
      }
    } catch (cause) {
      setQuickMessage({
        tone: 'bad',
        text: cause instanceof Error ? cause.message : 'Quick bind failed',
      })
    } finally {
      setQuickBusy(false)
    }
  }

  return (
    <div className="stack">
      <section className="panel">
        <div className="panel-head-wrap">
          <div>
            <h2>Agent Role Bindings</h2>
            <p className="subtitle">
              Every task in a Plan belongs to one role. Bindings store ordered account IDs only —
              keys and account status are resolved at call time.
            </p>
          </div>
          <div className="btn-row">
            <label className="inline-toggle">
              <input
                type="checkbox"
                checked={showCompactor}
                onChange={(e) => setShowCompactor(e.target.checked)}
              />
              <span>Show auxiliary compactor</span>
            </label>
            <button type="button" className="btn" onClick={onRefresh} disabled={loading}>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>
        </div>

        {accounts.length === 0 && (
          <div className="notice notice-warn">
            <span>
              Add at least one provider account before saving role bindings (Anthropic Console or
              MockProvider for the Planner).
            </span>
            <button type="button" className="btn btn-sm btn-primary" onClick={onOpenAccounts}>
              Go to Accounts
            </button>
          </div>
        )}

        {activeAccounts.length > 0 && unconfiguredPrimary.length > 0 && (
          <div className="notice notice-idle">
            <span>
              <strong>{unconfiguredPrimary.length}</strong> primary role(s) unconfigured (
              <span className="mono">{unconfiguredPrimary.join(', ')}</span>).
            </span>
            <button
              type="button"
              className="btn btn-sm"
              disabled={quickBusy}
              onClick={() => void handleQuickBind()}
            >
              {quickBusy ? 'Binding…' : 'Bind unconfigured roles with defaults'}
            </button>
          </div>
        )}

        {quickMessage && (
          <div
            className={quickMessage.tone === 'ok' ? 'notice notice-ok' : 'notice notice-bad'}
            role="status"
          >
            <span>{quickMessage.text}</span>
            <button type="button" className="btn-link" onClick={() => setQuickMessage(null)}>
              dismiss
            </button>
          </div>
        )}
      </section>

      <RoleBindingCard
        role="planner"
        heading="Planner (planning & evaluation only)"
        binding={bindings.planner}
        accounts={accounts}
        tier={policies?.model_tiers.planner ?? 'strong'}
        defaultInputTokens={defaultInputCap}
        defaultOutputTokens={4_096}
        onSaved={onRefresh}
      />

      <div className="role-grid">
        {EXECUTING_AGENT_ROLES.map((role) => (
          <RoleBindingCard
            key={role}
            role={role}
            heading={role}
            binding={bindings[role]}
            accounts={accounts}
            tier={policies?.model_tiers[role] ?? 'mid'}
            defaultInputTokens={defaultInputCap}
            defaultOutputTokens={4_096}
            onSaved={onRefresh}
          />
        ))}
      </div>

      {showCompactor && (
        <RoleBindingCard
          role="compactor"
          heading="compactor (auxiliary digest summariser)"
          binding={bindings.compactor}
          accounts={accounts}
          tier={policies?.model_tiers.compactor ?? 'cheap'}
          defaultInputTokens={defaultInputCap}
          defaultOutputTokens={256}
          onSaved={onRefresh}
        />
      )}

      <datalist id="scholarloop-model-suggestions">
        {MODEL_SUGGESTIONS.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>
    </div>
  )
}

interface RoleBindingCardProps {
  role: BindingRole
  heading: string
  binding: BindingView | null
  accounts: readonly AccountView[]
  tier: string
  defaultInputTokens: number
  defaultOutputTokens: number
  onSaved: () => void
}

function RoleBindingCard({
  role,
  heading,
  binding,
  accounts,
  tier,
  defaultInputTokens,
  defaultOutputTokens,
  onSaved,
}: RoleBindingCardProps): React.JSX.Element {
  const firstEligible = accounts.find((a) => a.status !== 'disabled' && isAccountAllowedForRole(role, a))

  const [selectedIds, setSelectedIds] = useState<string[]>(() =>
    binding ? [...binding.account_ids] : firstEligible ? [firstEligible.id] : [],
  )
  const [strategy, setStrategy] = useState<BindingStrategy>(() =>
    binding ? binding.strategy : 'dedicated',
  )
  const [model, setModel] = useState<string>(() =>
    binding ? binding.model : suggestedModel(firstEligible?.provider, tier),
  )
  const [maxInput, setMaxInput] = useState<string>(() =>
    String(binding ? binding.max_input_tokens : defaultInputTokens),
  )
  const [maxOutput, setMaxOutput] = useState<string>(() =>
    String(binding ? binding.max_output_tokens : defaultOutputTokens),
  )
  const [enabled, setEnabled] = useState<boolean>(() => (binding ? binding.enabled : true))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [savedNote, setSavedNote] = useState<string | null>(null)

  useEffect(() => {
    if (binding) {
      setSelectedIds([...binding.account_ids])
      setStrategy(binding.strategy)
      setModel(binding.model)
      setMaxInput(String(binding.max_input_tokens))
      setMaxOutput(String(binding.max_output_tokens))
      setEnabled(binding.enabled)
    }
  }, [binding])

  const accountById = new Map(accounts.map((a) => [a.id, a]))
  const usableCount = selectedIds.filter((id) => {
    const acc = accountById.get(id)
    return acc !== undefined && acc.status !== 'disabled'
  }).length

  const pillTone: Tone = !binding
    ? 'idle'
    : !binding.enabled
      ? 'idle'
      : usableCount === 0
        ? 'warn'
        : 'ok'

  const pillLabel = !binding
    ? 'unconfigured'
    : !binding.enabled
      ? 'disabled'
      : usableCount === 0
        ? 'no active account'
        : `enabled · ${binding.strategy}`

  const toggleAccount = (accountId: string): void => {
    setSavedNote(null)
    setError(null)
    setSelectedIds((prev) => {
      const exists = prev.includes(accountId)
      const next = exists ? prev.filter((id) => id !== accountId) : [...prev, accountId]
      if (next.length < 2 && (strategy === 'round_robin' || strategy === 'failover')) {
        setStrategy('dedicated')
      }
      if (!binding && next.length === 1 && !exists) {
        const acc = accountById.get(accountId)
        if (acc) setModel(suggestedModel(acc.provider, tier))
      }
      return next
    })
  }

  const moveAccount = (index: number, delta: -1 | 1): void => {
    setSavedNote(null)
    setSelectedIds((prev) => {
      const targetIndex = index + delta
      if (targetIndex < 0 || targetIndex >= prev.length) return prev
      const copy = [...prev]
      const current = copy[index]
      const other = copy[targetIndex]
      if (current === undefined || other === undefined) return prev
      copy[index] = other
      copy[targetIndex] = current
      return copy
    })
  }

  const handleSave = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault()
    setError(null)
    setSavedNote(null)
    if (selectedIds.length === 0) {
      setError('Select at least one provider account for this role.')
      return
    }
    if ((strategy === 'round_robin' || strategy === 'failover') && selectedIds.length < 2) {
      setError(`Strategy "${strategy}" requires at least 2 accounts; choose "dedicated" or add a fallback account.`)
      return
    }
    const cleanModel = model.trim()
    if (!cleanModel) {
      setError('Model identifier is required.')
      return
    }
    const inTokens = Number(maxInput)
    const outTokens = Number(maxOutput)
    if (!Number.isInteger(inTokens) || inTokens < 1_000 || inTokens > 2_000_000) {
      setError('Max input tokens must be an integer between 1,000 and 2,000,000.')
      return
    }
    if (!Number.isInteger(outTokens) || outTokens < 64 || outTokens > 64_000) {
      setError('Max output tokens must be an integer between 64 and 64,000.')
      return
    }
    if (outTokens > inTokens) {
      setError('Max output tokens must not exceed max input tokens.')
      return
    }

    setSaving(true)
    try {
      const payload: BindingPut = {
        account_ids: selectedIds,
        strategy,
        model: cleanModel,
        max_input_tokens: inTokens,
        max_output_tokens: outTokens,
        enabled,
      }
      const saved = await putBinding(role, payload)
      setSavedNote(
        `Saved (${saved.enabled ? 'enabled' : 'disabled'}, ${saved.strategy}, ${saved.account_ids.length} account${saved.account_ids.length === 1 ? '' : 's'}).`,
      )
      onSaved()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to save binding')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className={role === 'planner' ? 'panel panel-accent' : 'panel'}>
      <div className="panel-head-wrap">
        <div className="role-title-row">
          <h2>{heading}</h2>
          <span className="badge mono">tier:{tier}</span>
        </div>
        <StatusPill tone={pillTone} label={pillLabel} />
      </div>
      <p className="subtitle">{ROLE_DESCRIPTIONS[role]}</p>

      {error && (
        <div className="notice notice-bad" role="alert">
          {error}
        </div>
      )}
      {savedNote && (
        <div className="notice notice-ok" role="status">
          {savedNote}
        </div>
      )}

      <form onSubmit={(e) => void handleSave(e)} className="form-stack">
        <div className="field">
          <span className="field-label">Candidate account(s)</span>
          {accounts.length === 0 ? (
            <span className="dim small">No accounts available yet.</span>
          ) : (
            <div className="account-pick-list">
              {accounts.map((acc) => {
                const allowed = isAccountAllowedForRole(role, acc)
                const checked = selectedIds.includes(acc.id)
                return (
                  <label
                    key={acc.id}
                    className={
                      !allowed
                        ? 'account-pick account-pick-disabled'
                        : checked
                          ? 'account-pick account-pick-selected'
                          : 'account-pick'
                    }
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={!allowed && !checked}
                      onChange={() => toggleAccount(acc.id)}
                    />
                    <span className="strong">{acc.label}</span>
                    <span className="badge mono">{acc.provider}</span>
                    <span className="mono dim small">{acc.key_mask}</span>
                    {acc.status !== 'active' && (
                      <span className="warn-text mono small">({acc.status})</span>
                    )}
                    {!allowed && (
                      <span className="dim small">— planner requires anthropic or mock</span>
                    )}
                  </label>
                )
              })}
            </div>
          )}
        </div>

        {selectedIds.length > 0 && (
          <div className="field">
            <span className="field-label">Resolution order ({selectedIds.length} selected)</span>
            <div className="order-list">
              {selectedIds.map((id, idx) => {
                const acc = accountById.get(id)
                return (
                  <div key={id} className="order-item mono">
                    <span>
                      #{idx + 1} {acc ? `${acc.label} (${acc.provider})` : `${id.slice(0, 10)}… [deleted]`}
                    </span>
                    {selectedIds.length > 1 && (
                      <span className="order-btns">
                        <button
                          type="button"
                          className="btn-icon"
                          aria-label={`Move ${acc?.label ?? id} up`}
                          disabled={idx === 0}
                          onClick={() => moveAccount(idx, -1)}
                        >
                          ↑
                        </button>
                        <button
                          type="button"
                          className="btn-icon"
                          aria-label={`Move ${acc?.label ?? id} down`}
                          disabled={idx === selectedIds.length - 1}
                          onClick={() => moveAccount(idx, 1)}
                        >
                          ↓
                        </button>
                      </span>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}

        <div className="form-grid">
          <label className="field">
            <span className="field-label">Strategy</span>
            <select
              value={strategy}
              onChange={(e) => setStrategy(e.target.value as BindingStrategy)}
            >
              {BINDING_STRATEGIES.map((st) => {
                const requiresMulti = st === 'round_robin' || st === 'failover'
                return (
                  <option key={st} value={st} disabled={requiresMulti && selectedIds.length < 2}>
                    {st}
                    {requiresMulti && selectedIds.length < 2 ? ' (needs ≥2 accounts)' : ''}
                  </option>
                )
              })}
            </select>
          </label>

          <label className="field">
            <span className="field-label">Model</span>
            <input
              type="text"
              required
              maxLength={120}
              list="scholarloop-model-suggestions"
              placeholder="e.g. claude-sonnet-4"
              value={model}
              onChange={(e) => setModel(e.target.value)}
            />
          </label>

          <label className="field">
            <span className="field-label">Max input tokens</span>
            <input
              type="number"
              required
              min={1000}
              max={2000000}
              step={1000}
              value={maxInput}
              onChange={(e) => setMaxInput(e.target.value)}
            />
          </label>

          <label className="field">
            <span className="field-label">Max output tokens</span>
            <input
              type="number"
              required
              min={64}
              max={64000}
              step={64}
              value={maxOutput}
              onChange={(e) => setMaxOutput(e.target.value)}
            />
          </label>
        </div>

        <div className="role-footer">
          <label className="inline-toggle">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            <span>Role enabled</span>
          </label>
          <div className="btn-row">
            {binding?.updated_at && (
              <span className="dim small mono">
                updated {new Date(binding.updated_at).toLocaleTimeString()}
              </span>
            )}
            <button
              type="submit"
              className="btn btn-primary btn-sm"
              disabled={saving || accounts.length === 0}
            >
              {saving ? 'Saving…' : binding ? 'Update binding' : 'Save binding'}
            </button>
          </div>
        </div>
      </form>
    </section>
  )
}
