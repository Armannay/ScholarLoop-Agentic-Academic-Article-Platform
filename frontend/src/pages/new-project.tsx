import { useMemo, useRef, useState } from 'react'
import { BriefForm } from '../components'
import { ApiError, createProject, createRun } from '../lib/api'
import type { Brief, ProjectView, RunView } from '../lib/domain'

export interface NewProjectProps {
  onCreated: (project: ProjectView) => void
  onCancel: () => void
  onRunStarted: (run: RunView) => void
}

/**
 * New project: the Brief form, inline validation, and the "NEEDS_INPUT" round trip.
 *
 * Client-side validation mirrors deterministic intake but never replaces it: the form
 * blocks on its own findings, and if the API still answers `409 needs_user_input` the
 * missing fields are turned into the same inline questions and the first one is
 * focused. Nothing is defaulted, exactly like the orchestrator.
 */
/** Read a string[] out of an ApiError's machine-readable context. */
function readStringArray(error: unknown, key: string): readonly string[] {
  if (!(error instanceof ApiError)) return []
  const raw = error.context?.[key]
  if (!Array.isArray(raw)) return []
  return raw.filter((item): item is string => typeof item === 'string')
}

export function NewProject({ onCreated, onCancel, onRunStarted }: NewProjectProps): React.JSX.Element {
  const [startRun, setStartRun] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)
  const [missingFields, setMissingFields] = useState<readonly string[]>([])
  const [problems, setProblems] = useState<readonly string[]>([])
  const wizardRef = useRef<HTMLDivElement | null>(null)

  const handleSubmit = async (payload: { name: string; brief: Brief }): Promise<void> => {
    setSubmitting(true)
    setServerError(null)
    setMissingFields([])
    setProblems([])
    let created: ProjectView | null = null
    try {
      created = await createProject({
        name: payload.name,
        brief: payload.brief,
        status: 'active',
      })
      const project: ProjectView = created
      onCreated(project)
      if (startRun) {
        const run = await createRun(project.id, { brief: payload.brief, on_complete: 'ask' })
        onRunStarted(run)
      }
    } catch (cause) {
      if (cause instanceof ApiError) {
        if (cause.status === 409 && cause.code === 'needs_user_input') {
          setMissingFields(readStringArray(cause, 'missing_fields'))
          setProblems(readStringArray(cause, 'problems'))
          setServerError(
            `Intake needs ${readStringArray(cause, 'missing_fields').length > 0 ? readStringArray(cause, 'missing_fields').join(', ') : 'more information'} before this brief can run.`,
          )
          wizardRef.current?.scrollIntoView?.({ block: 'start' })
        } else if (cause.fieldErrors.length > 0) {
          setServerError(cause.message)
          setProblems(cause.fieldErrors.map((error) => `${error.loc}: ${error.msg}`))
        } else {
          setServerError(cause.message)
        }
      } else {
        setServerError(cause instanceof Error ? cause.message : 'Could not create the project')
      }
    } finally {
      setSubmitting(false)
    }
  }

  const serverNotice = useMemo(() => {
    if (!serverError) return null
    return (
      <div className="notice notice-warn" role="alert">
        <div>
          <strong>NEEDS_INPUT</strong> — {serverError}
          {problems.length > 0 && (
            <ul className="list mono small">
              {problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          )}
        </div>
      </div>
    )
  }, [serverError, problems])

  return (
    <div className="stack" ref={wizardRef}>
      <section className="panel">
        <div className="panel-head-wrap">
          <div>
            <h2>New project</h2>
            <p className="subtitle">
              A project <em>is</em> the brief it writes against. The Brief contract is validated
              field by field before any agent runs, and a field only you can supply is asked for —
              never defaulted.
            </p>
          </div>
          <button type="button" className="btn" onClick={onCancel}>
            Cancel
          </button>
        </div>

        {serverNotice}

        <BriefForm
          submitting={submitting}
          highlightFields={missingFields}
          onSubmit={(payload) => void handleSubmit(payload)}
          footerExtra={
            <label className="inline-toggle">
              <input
                type="checkbox"
                checked={startRun}
                onChange={(event) => setStartRun(event.target.checked)}
              />
              <span>Start a run as soon as the project exists</span>
            </label>
          }
        />
      </section>
    </div>
  )
}
