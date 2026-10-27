import { useCallback, useEffect, useMemo, useState } from 'react'
import { getRun, getRunLedger, getRunQuality, getRunUsage, stopRun } from './api'
import { CheckpointModal } from './CheckpointModal'
import { PlanBoard } from './PlanBoard'
import { QualityChart } from './QualityChart'
import { StopReportView } from './StopReportView'
import { StatusPill } from './StatusPill'
import { UsageMeters } from './UsageMeters'
import {
  formatInt,
  formatTimestamp,
  runStateLabel,
  runStateTone,
  shortId,
} from './format'
import { useAsync } from './useApi'
import { useRunEvents } from './useRunEvents'
import {
  TERMINAL_STATES,
  type LedgerView,
  type Plan,
  type QualityView,
  type RunUsageView,
  type RunView,
} from './types'

const RUN_POLL_MS = 4000

export interface RunPageProps {
  runId: string
  onOpenArticle: (runId: string) => void
  onBack: () => void
}

function eventSummary(detail: Record<string, unknown>): string {
  if (typeof detail.summary === 'string') return detail.summary
  const parts = Object.entries(detail)
    .filter(([key]) => key !== 'run_id' && key !== 'summary')
    .map(([key, value]) =>
      typeof value === 'object' && value !== null
        ? `${key}=${JSON.stringify(value)}`
        : `${key}=${String(value)}`,
    )
  return parts.join(' · ') || '—'
}

export function RunPage({ runId, onOpenArticle, onBack }: RunPageProps): React.JSX.Element {
  const loadRun = useCallback((signal?: AbortSignal) => getRun(runId, signal), [runId])
  const loadQuality = useCallback((signal?: AbortSignal) => getRunQuality(runId, signal), [runId])
  const loadUsage = useCallback((signal?: AbortSignal) => getRunUsage(runId, signal), [runId])
  const loadLedger = useCallback((signal?: AbortSignal) => getRunLedger(runId, signal), [runId])

  const run = useAsync<RunView>(loadRun, RUN_POLL_MS)
  const quality = useAsync<QualityView>(loadQuality, RUN_POLL_MS)
  const usage = useAsync<RunUsageView>(loadUsage, RUN_POLL_MS)
  const ledger = useAsync<LedgerView>(loadLedger, RUN_POLL_MS)
  const feed = useRunEvents(runId)

  const [localRun, setLocalRun] = useState<RunView | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [stopping, setStopping] = useState(false)
  const [autoOpened, setAutoOpened] = useState<string | null>(null)

  const current = useMemo<RunView | null>(() => {
    const fetched = run.data
    if (!fetched) return localRun
    if (!localRun) return fetched
    return new Date(localRun.updated_at).getTime() >= new Date(fetched.updated_at).getTime()
      ? localRun
      : fetched
  }, [run.data, localRun])

  const isSettled = current ? TERMINAL_STATES.includes(current.state) : false
  const needsInput = current?.checkpoint_kind === 'needs_input'

  // A needs_input checkpoint opens itself exactly once per run: the loop is blocked on
  // the user, so making them hunt for a button would stall the run.
  useEffect(() => {
    if (!current || !needsInput) return
    if (autoOpened === current.id) return
    setAutoOpened(current.id)
    setModalOpen(true)
  }, [current, needsInput, autoOpened])

  const handleStop = async (): Promise<void> => {
    setStopping(true)
    setActionError(null)
    try {
      const updated = await stopRun(runId)
      setLocalRun(updated)
      run.reload()
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : 'Stop failed')
    } finally {
      setStopping(false)
    }
  }

  if (run.loading && !current) {
    return (
      <section className="panel" aria-busy="true">
        <p className="dim">Loading run {shortId(runId)}…</p>
      </section>
    )
  }

  if (run.error && !current) {
    return (
      <section className="panel panel-bad">
        <h2>Run unavailable</h2>
        <p className="mono small">{run.error}</p>
        <div className="btn-row">
          <button type="button" className="btn" onClick={run.reload}>
            Try again
          </button>
          <button type="button" className="btn" onClick={onBack}>
            Back to projects
          </button>
        </div>
      </section>
    )
  }

  if (!current) {
    return (
      <section className="panel">
        <div className="empty-state">No run data yet.</div>
      </section>
    )
  }

  const rawPlan = current.plan
  const plan: Plan | null =
    rawPlan && 'tasks' in rawPlan && Array.isArray((rawPlan as Plan).tasks) ? (rawPlan as Plan) : null
  const options = current.stop_report?.options ?? []

  return (
    <div className="stack">
      <section className="panel">
        <div className="panel-head-wrap">
          <div>
            <div className="role-title-row">
              <h2>Run {shortId(current.id)}</h2>
              <StatusPill tone={runStateTone(current.state)} label={runStateLabel(current.state)} />
              {current.running && <StatusPill tone="ok" label="live" />}
            </div>
            <p className="subtitle mono small">
              project {current.project_id} · kind {current.kind} · iteration {current.iter} ·{' '}
              {current.brief && 'language' in current.brief ? String(current.brief.language) : '—'} · updated{' '}
              {formatTimestamp(current.updated_at)}
            </p>
          </div>
          <div className="btn-row">
            <button type="button" className="btn" onClick={onBack}>
              ← Projects
            </button>
            <button type="button" className="btn" onClick={() => onOpenArticle(current.id)}>
              Open article
            </button>
            {options.length > 0 && (
              <button type="button" className="btn btn-primary" onClick={() => setModalOpen(true)}>
                {needsInput ? 'Answer the run…' : 'Review checkpoint…'}
              </button>
            )}
            {!isSettled && (
              <button type="button" className="btn btn-danger" onClick={() => void handleStop()} disabled={stopping}>
                {stopping ? 'Stopping…' : 'Stop run'}
              </button>
            )}
          </div>
        </div>

        {actionError && (
          <div className="notice notice-bad" role="alert">
            {actionError}
          </div>
        )}
        {current.last_error && (
          <div className="notice notice-warn" role="status">
            <span className="mono small">last error: {current.last_error}</span>
          </div>
        )}
        {current.user_stop_requested && (
          <div className="notice notice-warn" role="status">
            Stop requested — the loop finishes the current wave before it stops.
          </div>
        )}
        {current.state === 'CHECKPOINT' && (
          <div className="notice notice-warn" role="status">
            <span>
              The loop is waiting for you ({current.checkpoint_kind ?? 'checkpoint'}). Nothing runs until a
              decision is made.
            </span>
            {options.length > 0 && (
              <button type="button" className="btn btn-sm btn-primary" onClick={() => setModalOpen(true)}>
                Decide
              </button>
            )}
          </div>
        )}
      </section>

      <div className="run-grid">
        <section className="panel" aria-label="Quality per iteration">
          <h2>Quality</h2>
          <p className="subtitle">
            Q per iteration with the eight hard gates of the latest report. A run is never
            “done” below Q min.
          </p>
          {quality.error ? (
            <div className="notice notice-bad" role="alert">
              {quality.error}
              <button type="button" className="btn btn-sm" onClick={quality.reload}>
                Retry
              </button>
            </div>
          ) : (
            <QualityChart
              quality={quality.data}
              qMin={70}
              currentIter={current.q_series.length - 1}
            />
          )}
        </section>

        <section className="panel" aria-label="Budgets">
          <h2>Budgets</h2>
          <p className="subtitle">Tokens and iterations against the caps this run obeys.</p>
          {usage.error ? (
            <div className="notice notice-bad" role="alert">
              {usage.error}
              <button type="button" className="btn btn-sm" onClick={usage.reload}>
                Retry
              </button>
            </div>
          ) : (
            <UsageMeters usage={usage.data} tokensSpent={current.tokens_spent} />
          )}
        </section>
      </div>

      <section className="panel" aria-label="Plan board">
        <h2>Plan</h2>
        <p className="subtitle">
          Tasks grouped by the role that owns them. A task is done when the run log carries a
          completed event for it.
        </p>
        <PlanBoard plan={plan} events={feed.events} iter={current.iter} />
      </section>

      <div className="run-grid">
        <section className="panel" aria-label="Live change log">
          <div className="panel-head">
            <h2>Live activity</h2>
            <StatusPill
              tone={feed.error ? 'bad' : feed.live ? 'ok' : 'idle'}
              label={feed.error ? 'stream error' : feed.live ? 'streaming' : 'idle'}
            />
          </div>
          <p className="subtitle">
            Server-Sent Events from <code>GET /api/runs/{'{id}'}/events</code>, replayed from the
            last sequence number on every reconnect.
          </p>
          {feed.error && (
            <div className="notice notice-bad" role="alert">
              <span className="mono small">{feed.error}</span>
              <button type="button" className="btn btn-sm" onClick={feed.reconnect}>
                Reconnect
              </button>
            </div>
          )}
          {feed.events.length === 0 ? (
            <div className="empty-state" role="status">
              No events yet. The feed fills as soon as the run records its first action.
            </div>
          ) : (
            <ol className="event-feed" aria-live="polite">
              {[...feed.events]
                .sort((a, b) => b.seq - a.seq)
                .map((event) => (
                  <li key={event.seq} className="event-row">
                    <span className="mono small dim">#{event.seq}</span>
                    <span className="badge mono">{event.type}</span>
                    <span className="mono small dim">iter {event.iter}</span>
                    <span className="small">{eventSummary(event.detail)}</span>
                    <span className="mono small dim event-ts">{formatTimestamp(event.ts)}</span>
                  </li>
                ))}
            </ol>
          )}
        </section>

        <section className="panel" aria-label="Stop report">
          <h2>{current.state === 'CHECKPOINT' ? 'Checkpoint report' : 'Stop report'}</h2>
          <p className="subtitle">Why the loop paused, what it measured, and what you can do.</p>
          <StopReportView
            run={current}
            quality={quality.data}
            usage={usage.data}
            onOpenCheckpoint={options.length > 0 ? () => setModalOpen(true) : undefined}
          />
        </section>
      </div>

      <section className="panel" aria-label="Change ledger">
        <h2>Change ledger</h2>
        <p className="subtitle">
          Append-only: one entry per action, with the token cost and a patch pointer instead of a
          patch body. Orchestrator entries record state changes only.
        </p>
        {ledger.error ? (
          <div className="notice notice-bad" role="alert">
            {ledger.error}
            <button type="button" className="btn btn-sm" onClick={ledger.reload}>
              Retry
            </button>
          </div>
        ) : ledger.data && ledger.data.entries.length > 0 ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Iter</th>
                  <th scope="col">Role</th>
                  <th scope="col">Summary</th>
                  <th scope="col">Targets</th>
                  <th scope="col">Tokens</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {ledger.data.entries.map((entry) => (
                  <tr key={entry.id}>
                    <td className="mono">{entry.iter}</td>
                    <td>
                      <span className="badge mono">{entry.role}</span>
                    </td>
                    <td>{entry.summary}</td>
                    <td className="mono small dim">{(entry.targets ?? []).join(', ') || '—'}</td>
                    <td className="mono small">
                      {formatInt((entry.tokens?.in ?? 0) + (entry.tokens?.out ?? 0) + (entry.tokens?.cached ?? 0))}
                    </td>
                    <td>
                      <StatusPill
                        tone={entry.status === 'ok' ? 'ok' : entry.status === 'needs_user_input' ? 'warn' : 'bad'}
                        label={entry.status}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-state" role="status">
            {ledger.loading ? 'Loading ledger…' : 'The ledger is empty — no action has been recorded yet.'}
          </div>
        )}
      </section>

      <CheckpointModal
        run={current}
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onDecided={(updated) => {
          setLocalRun(updated)
          run.reload()
          quality.reload()
          usage.reload()
          ledger.reload()
          feed.reconnect()
        }}
      />
    </div>
  )
}
