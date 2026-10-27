import { useEffect, useId, useRef, useState } from 'react'
import { ApiError, submitDecision } from './api'
import type { RunView, StopOption, UserDecisionRequest } from './types'

export interface CheckpointModalProps {
  run: RunView
  open: boolean
  onClose: () => void
  onDecided: (run: RunView) => void
}

interface OptionCopy {
  label: string
  description: string
  needsBudget?: boolean
  needsGoal?: boolean
}

const OPTION_COPY: Record<StopOption, OptionCopy> = {
  finish: {
    label: 'Finish',
    description: 'Accept the article as it stands and close the run.',
  },
  continue: {
    label: 'Continue with extra budget',
    description: 'Grant more tokens and run another iteration before asking again.',
    needsBudget: true,
  },
  rollback: {
    label: 'Roll back',
    description: 'Restore the best-measured snapshot as a new version and finish.',
  },
  edit_goals: {
    label: 'Edit goals',
    description: 'Replace the run goal; the next plan is written against it.',
    needsGoal: true,
  },
  add_budget: {
    label: 'Continue with extra budget',
    description: 'Raise the token cap and keep iterating.',
    needsBudget: true,
  },
  continue_anyway: {
    label: 'Continue anyway',
    description: 'Ignore the erosion stop and run another iteration.',
  },
}

const DEFAULT_EXTRA_TOKENS = 200_000
const DEFAULT_EXTRA_ITERATIONS = 2

/**
 * The checkpoint dialog: Finish / Continue with extra budget / Edit goals / Roll back.
 *
 * The legal options come from `run.stop_report.options` - the console never hard-codes
 * which decision applies to the current state. Keyboard: Escape closes, Tab is trapped
 * inside the dialog, and the first option is focused on open.
 */
export function CheckpointModal({ run, open, onClose, onDecided }: CheckpointModalProps): React.JSX.Element | null {
  const titleId = useId()
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const options = run.stop_report?.options ?? []
  const [selected, setSelected] = useState<StopOption | null>(options[0] ?? null)
  const [extraTokens, setExtraTokens] = useState(String(DEFAULT_EXTRA_TOKENS))
  const [extraIterations, setExtraIterations] = useState(String(DEFAULT_EXTRA_ITERATIONS))
  const [newGoal, setNewGoal] = useState(run.goal_override ?? '')
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setSelected(run.stop_report?.options?.[0] ?? null)
    setError(null)
    setNote('')
  }, [open, run.id, run.stop_report])

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab' || !dialogRef.current) return
      const focusable = dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled])',
      )
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (!first || !last) return
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    const firstButton = dialogRef.current?.querySelector<HTMLElement>('input[type="radio"]')
    firstButton?.focus()
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  if (!open) return null

  const copy = selected ? OPTION_COPY[selected] : null
  const needsInput = run.checkpoint_kind === 'needs_input'

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault()
    if (!selected) {
      setError('Choose one of the options the run allows.')
      return
    }
    const payload: UserDecisionRequest = { option: selected }
    if (copy?.needsBudget) {
      const tokens = Number(extraTokens)
      if (!Number.isInteger(tokens) || tokens < 1) {
        setError('Extra tokens must be a positive whole number.')
        return
      }
      payload.extra_tokens = tokens
      const iterations = Number(extraIterations)
      if (Number.isInteger(iterations) && iterations >= 1) payload.extra_iterations = iterations
    }
    if (copy?.needsGoal) {
      if (!newGoal.trim()) {
        setError('A new goal is required to edit the goals.')
        return
      }
      payload.new_goal = newGoal.trim()
    }
    if (note.trim()) payload.note = note.trim()
    setSubmitting(true)
    setError(null)
    try {
      const updated = await submitDecision(run.id, payload)
      onDecided(updated)
      onClose()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : 'Decision failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        ref={dialogRef}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="panel-head-wrap">
          <div>
            <h2 id={titleId}>
              {needsInput ? 'The run needs your input' : 'Checkpoint reached'}
            </h2>
            <p className="subtitle">
              {needsInput
                ? 'An agent asked for information only you have. Nothing is written into the article without a plan.'
                : `Q ${run.stop_report?.current_q ?? '—'} · best ${run.stop_report?.best_q ?? '—'} · ${run.q_series.length} iterations. Choose what happens next.`}
            </p>
          </div>
          <button type="button" className="btn btn-sm" onClick={onClose} aria-label="Close checkpoint dialog">
            Close
          </button>
        </div>

        {run.stop_report && run.stop_report.reasons.length > 0 && (
          <p className="mono small dim">reasons: {run.stop_report.reasons.join(', ')}</p>
        )}

        {error && (
          <div className="notice notice-bad" role="alert">
            {error}
          </div>
        )}

        <form className="form-stack" onSubmit={(event) => void handleSubmit(event)}>
          {options.length === 0 ? (
            <div className="empty-state" role="status">
              The run published no legal options for this state. Reload the run to refresh it.
            </div>
          ) : (
            <fieldset className="option-list">
              <legend className="field-label">Allowed here</legend>
              {options.map((option) => {
                const optionCopy = OPTION_COPY[option]
                return (
                  <label key={option} className={selected === option ? 'radio-card radio-card-active' : 'radio-card'}>
                    <input
                      type="radio"
                      name="checkpoint-option"
                      value={option}
                      checked={selected === option}
                      onChange={() => {
                        setSelected(option)
                        setError(null)
                      }}
                    />
                    <div>
                      <div className="strong">{optionCopy.label}</div>
                      <div className="field-hint">{optionCopy.description}</div>
                    </div>
                  </label>
                )
              })}
            </fieldset>
          )}

          {copy?.needsBudget && (
            <div className="form-grid">
              <label className="field">
                <span className="field-label">Extra tokens</span>
                <input
                  type="number"
                  min={1}
                  step={1000}
                  value={extraTokens}
                  onChange={(event) => setExtraTokens(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">Extra iterations</span>
                <input
                  type="number"
                  min={1}
                  max={200}
                  step={1}
                  value={extraIterations}
                  onChange={(event) => setExtraIterations(event.target.value)}
                />
              </label>
            </div>
          )}

          {copy?.needsGoal && (
            <label className="field">
              <span className="field-label">New run goal</span>
              <textarea
                rows={3}
                maxLength={800}
                value={newGoal}
                onChange={(event) => setNewGoal(event.target.value)}
                placeholder="What 'done' means now, in one or two sentences."
              />
            </label>
          )}

          <label className="field">
            <span className="field-label">
              Note to the Planner {needsInput ? '(answer the agent here)' : '(optional)'}
            </span>
            <textarea
              rows={3}
              maxLength={4000}
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder={
                needsInput
                  ? 'Paste the missing information — it is handed to the Planner, never spliced into the text.'
                  : 'Anything the next iteration should know.'
              }
            />
          </label>

          <div className="form-actions">
            <button type="submit" className="btn btn-primary" disabled={submitting || !selected}>
              {submitting ? 'Sending…' : selected ? OPTION_COPY[selected].label : 'Choose an option'}
            </button>
            <button type="button" className="btn" onClick={onClose} disabled={submitting}>
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
