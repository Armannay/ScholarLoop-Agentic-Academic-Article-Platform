import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, createProject, createRun, listProjectRuns } from '../lib/api'
import { NewProject } from '../pages/new-project'
import { Project } from '../pages/project'
import { MockApiServer } from '../api/mockServer'
import { seededProjectByName, signIn } from '../test/helpers'
import { ENGLISH_BRIEF } from '../api/fixtures'

let server: MockApiServer

beforeEach(() => {
  server = new MockApiServer()
})

function fill(label: RegExp, value: string): void {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

describe('New project (Brief form -> project -> run)', () => {
  it('creates the project, starts a run and hands both ids back', async () => {
    await signIn(server, 'new-project@example.org')
    const onCreated = vi.fn()
    const onRunStarted = vi.fn()

    render(<NewProject onCreated={onCreated} onCancel={vi.fn()} onRunStarted={onRunStarted} />)

    fill(/project name/i, 'peer-review-copy')
    fill(/^topic/i, 'Tightening peer review with structured checklists')
    fill(/audience/i, 'CS faculty and graduate reviewers')
    fill(/language/i, 'en')
    fill(/target words/i, '850')
    fill(/must include/i, 'a comparison of checklist and free-form reviews')

    fireEvent.click(screen.getByRole('button', { name: /create project & run/i }))

    await waitFor(() => {
      expect(onCreated).toHaveBeenCalledTimes(1)
    })
    const project = onCreated.mock.calls[0]?.[0] as { id: string; name: string }
    expect(project.name).toBe('peer-review-copy')

    await waitFor(() => {
      expect(onRunStarted).toHaveBeenCalledTimes(1)
    })
    const run = onRunStarted.mock.calls[0]?.[0] as { id: string; project_id: string }
    expect(run.project_id).toBe(project.id)

    const runs = await listProjectRuns(project.id)
    expect(runs.runs).toHaveLength(1)
  })

  it('can create the project without starting a run', async () => {
    await signIn(server, 'no-run@example.org')
    const onRunStarted = vi.fn()
    const onCreated = vi.fn()
    render(<NewProject onCreated={onCreated} onCancel={vi.fn()} onRunStarted={onRunStarted} />)

    fireEvent.click(screen.getByLabelText(/start a run as soon as/i))
    fill(/project name/i, 'quiet-project')
    fill(/^topic/i, 'Tightening peer review with structured checklists')
    fill(/audience/i, 'Reviewers')
    fireEvent.click(screen.getByRole('button', { name: /create project & run/i }))

    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1))
    expect(onRunStarted).not.toHaveBeenCalled()
  })
})

describe('deterministic intake over the API (NEEDS_INPUT)', () => {
  it('answers 409 needs_user_input naming every missing Brief field', async () => {
    await signIn(server, 'intake@example.org')
    await expect(
      createProject({
        name: 'incomplete',
        brief: { topic: 'Tightening peer review with structured checklists' } as never,
      }),
    ).rejects.toMatchObject({ status: 409, code: 'needs_user_input' })

    try {
      await createProject({ name: 'incomplete-2', brief: { topic: 'only a topic here' } as never })
    } catch (cause) {
      const error = cause as ApiError
      expect(error.status).toBe(409)
      expect(error.code).toBe('needs_user_input')
      expect(error.context?.missing_fields).toEqual(
        expect.arrayContaining(['audience', 'language', 'target_words', 'citation_style']),
      )
    }
  })

  it('answers 422 with per-field errors for a malformed value', async () => {
    await signIn(server, 'invalid@example.org')
    await expect(
      createProject({ name: 'bad-brief', brief: { ...ENGLISH_BRIEF, target_words: 12 } }),
    ).rejects.toMatchObject({ status: 422, code: 'validation_error' })

    try {
      await createProject({ name: 'bad-brief-2', brief: { ...ENGLISH_BRIEF, target_words: 12 } })
    } catch (cause) {
      const error = cause as ApiError
      expect(error.fieldErrors.map((field) => field.loc)).toContain('body.brief.target_words')
    }
  })

  it('refuses a duplicate project name with 409 project_exists', async () => {
    await signIn(server, 'dupe@example.org')
    await createProject({ name: 'once', brief: ENGLISH_BRIEF })
    await expect(createProject({ name: 'Once', brief: ENGLISH_BRIEF })).rejects.toMatchObject({
      status: 409,
      code: 'conflict',
    })
  })

  it('creates runs through the project route and lists them newest first', async () => {
    await signIn(server, 'runs-api@example.org')
    const project = await createProject({ name: 'two-runs', brief: ENGLISH_BRIEF })
    await createRun(project.id, { brief: ENGLISH_BRIEF })
    await createRun(project.id, { brief: ENGLISH_BRIEF }, 'needs-input')
    const runs = await listProjectRuns(project.id)
    expect(runs.runs).toHaveLength(2)
    expect(runs.runs[0]?.checkpoint_kind).toBe('needs_input')
  })
})

describe('ProjectPage', () => {
  it('shows the brief, the run history and opens a run', async () => {
    await signIn(server, 'project-page@example.org')
    const seeded = await seededProjectByName('Peer review checklists')
    const onOpenRun = vi.fn()
    render(
      <Project
        projectId={seeded.id}
        onOpenRun={onOpenRun}
        onBack={vi.fn()}
        onNewProject={vi.fn()}
      />,
    )

    expect(await screen.findByText(seeded.name)).toBeTruthy()
    expect(screen.getByText(seeded.brief?.topic ?? '')).toBeTruthy()
    expect(screen.getByText(/±15%/)).toBeTruthy()

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Open' }).length).toBe(2)
    })
    fireEvent.click(screen.getAllByRole('button', { name: 'Open' })[0]!)
    expect(onOpenRun).toHaveBeenCalledTimes(1)
  })

  it('starts another run from the project and follows it', async () => {
    await signIn(server, 'project-start@example.org')
    const seeded = await seededProjectByName('Peer review checklists')
    const onOpenRun = vi.fn()
    render(
      <Project
        projectId={seeded.id}
        onOpenRun={onOpenRun}
        onBack={vi.fn()}
        onNewProject={vi.fn()}
      />,
    )
    await screen.findByText(seeded.name)
    fireEvent.change(screen.getByLabelText(/mock scenario/i), {
      target: { value: 'erosion-stop' },
    })
    fireEvent.click(screen.getByRole('button', { name: /start a run/i }))
    await waitFor(() => expect(onOpenRun).toHaveBeenCalledTimes(1))

    const runs = await listProjectRuns(seeded.id)
    expect(runs.runs.some((run) => run.state === 'STOPPED_EROSION')).toBe(true)
    expect(runs.runs.length).toBe(3)
  })
})
