import { useCallback, useState } from 'react'
import { createRun, getProject, listProjectRuns } from '../lib/api'
import {
  formatInt,
  formatTimestamp,
  readingDirection,
  runStateLabel,
  runStateTone,
} from '../lib/format'
import { StatusPill } from '../components'
import { useAsync } from '../lib/useAsync'
import type { RunList, RunView, RunStateName } from '../lib/domain'
import type { Project } from '../lib/api'

export interface ProjectProps {
  projectId: string
  onOpenRun: (runId: string) => void
  onBack: () => void
  onNewProject: () => void
}

const SCENARIOS: Array<{ value: string; label: string }> = [
  { value: '', label: 'Default scenario' },
  { value: 'completion', label: 'Reaches completion (Q 78)' },
  { value: 'erosion-stop', label: 'Stops on erosion' },
  { value: 'needs-input', label: 'Pauses for user input' },
]

/**
 * One project: its brief, its runs, and the button that starts another run.
 *
 * Starting a run sends the project's own brief, so a run can never contradict the
 * project it belongs to. The scenario picker exists only in mock mode, where the
 * fixtures need a way to show the three endings; it is sent as a header the backend
 * ignores.
 */
export function Project({ projectId, onOpenRun, onBack, onNewProject }: ProjectProps): React.JSX.Element {
  const loadProject = useCallback((signal?: AbortSignal) => getProject(projectId, signal), [projectId])
  const loadRuns = useCallback((signal?: AbortSignal) => listProjectRuns(projectId, signal), [projectId])
  const project = useAsync<Project>(loadProject)
  const runs = useAsync<RunList>(loadRuns)
  const [scenario, setScenario] = useState('')
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const startRun = async (): Promise<void> => {
    const brief = project.data?.brief
    if (!brief) {
      setError('This project has no brief to run against.')
      return
    }
    setStarting(true)
    setError(null)
    try {
      const created: RunView = await createRun(
        projectId,
        { brief, on_complete: 'ask' },
        scenario || undefined,
      )
      runs.reload()
      project.reload()
      onOpenRun(created.id)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not start the run')
    } finally {
      setStarting(false)
    }
  }

  if (project.loading && !project.data) {
    return (
      <section className="panel" aria-busy="true">
        <p className="dim">Loading project…</p>
      </section>
    )
  }

  if (project.error && !project.data) {
    return (
      <section className="panel panel-bad">
        <h2>Project unavailable</h2>
        <p className="mono small">{project.error}</p>
        <div className="btn-row">
          <button type="button" className="btn" onClick={project.reload}>
            Try again
          </button>
          <button type="button" className="btn" onClick={onBack}>
            Back to projects
          </button>
        </div>
      </section>
    )
  }

  if (!project.data) {
    return (
      <section className="panel">
        <div className="empty-state">No project.</div>
      </section>
    )
  }

  const brief = project.data.brief
  const direction = readingDirection(brief?.language ?? null)

  return (
    <div className="stack">
      <section className="panel">
        <div className="panel-head-wrap">
          <div>
            <div className="role-title-row">
              <h2>{project.data.name}</h2>
              <span className="badge mono">{project.data.status ?? 'active'}</span>
            </div>
            <p className="subtitle">{project.data.description ?? brief?.topic ?? '—'}</p>
          </div>
          <div className="btn-row">
            <button type="button" className="btn" onClick={onBack}>
              ← Projects
            </button>
            <button type="button" className="btn" onClick={onNewProject}>
              + New project
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void startRun()}
              disabled={starting || !brief}
            >
              {starting ? 'Starting…' : 'Start a run'}
            </button>
          </div>
        </div>

        {error && (
          <div className="notice notice-bad" role="alert">
            {error}
          </div>
        )}

        {brief && (
          <div className="brief-summary" dir={direction}>
            <dl className="kv">
              <dt>Topic</dt>
              <dd>{brief.topic}</dd>
              <dt>Article type</dt>
              <dd className="mono">{brief.article_type}</dd>
              <dt>Audience</dt>
              <dd>{brief.audience}</dd>
              <dt>Language</dt>
              <dd className="mono">
                {brief.language}
                {direction === 'rtl' ? ' (right-to-left)' : ''}
              </dd>
              <dt>Target words</dt>
              <dd className="mono">
                {formatInt(brief.target_words)} (±15%: {formatInt(Math.floor(brief.target_words * 0.85))}–
                {formatInt(Math.floor(brief.target_words * 1.15))})
              </dd>
              <dt>Citation style</dt>
              <dd className="mono">{brief.citation_style}</dd>
              <dt>Must include</dt>
              <dd>{brief.must_include?.length ? brief.must_include.join('; ') : '—'}</dd>
              <dt>Must avoid</dt>
              <dd>{brief.must_avoid?.length ? brief.must_avoid.join('; ') : '—'}</dd>
              <dt>Provided data</dt>
              <dd>
                {brief.provided_data?.length
                  ? `${brief.provided_data.length} item(s), treated as verified`
                  : 'none — sources must be real or flagged'}
              </dd>
            </dl>
          </div>
        )}

        <div className="btn-row start-row">
          <label className="field field-inline">
            <span className="field-label">Mock scenario</span>
            <select value={scenario} onChange={(event) => setScenario(event.target.value)}>
              {SCENARIOS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <span className="small dim">
            Mock mode only: the header is ignored by the real API, which runs the loop.
          </span>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head-wrap">
          <div>
            <h2>Runs</h2>
            <p className="subtitle">Newest first. A run is one attempt at the article.</p>
          </div>
          <button type="button" className="btn btn-sm" onClick={runs.reload} disabled={runs.loading}>
            {runs.loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>

        {runs.error && (
          <div className="notice notice-bad" role="alert">
            <span className="mono small">{runs.error}</span>
            <button type="button" className="btn btn-sm" onClick={runs.reload}>
              Retry
            </button>
          </div>
        )}

        {runs.loading && !runs.data ? (
          <div className="empty-state" role="status" aria-busy="true">
            Loading runs…
          </div>
        ) : !runs.data || runs.data.runs.length === 0 ? (
          <div className="empty-state" role="status">
            No runs yet. “Start a run” hands this brief to the orchestrator.
          </div>
        ) : (
          <ul className="run-list">
            {runs.data.runs.map((run) => (
              <li key={run.id} className="run-row">
                <div>
                  <button type="button" className="btn-link mono strong" onClick={() => onOpenRun(run.id)}>
                    {run.id}
                  </button>
                  <div className="small dim mono">
                    {run.kind} · iteration {run.iter} · {formatInt(run.tokens_spent)} tokens ·{' '}
                    {formatTimestamp(run.updated_at)}
                  </div>
                </div>
                <div className="chips-inline">
                  {run.best_q !== null && run.best_q !== undefined && (
                    <span className="badge mono">Q {formatInt(run.best_q)}</span>
                  )}
                  <StatusPill
                    tone={runStateTone(run.state as RunStateName)}
                    label={runStateLabel(run.state)}
                  />
                </div>
                <button type="button" className="btn btn-sm" onClick={() => onOpenRun(run.id)}>
                  Open
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
