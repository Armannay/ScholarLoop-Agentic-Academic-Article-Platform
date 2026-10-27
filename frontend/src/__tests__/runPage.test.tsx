import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { RunPage } from '../RunPage'
import { MockApiServer } from '../mockServer'
import { seededProjectByName, runsOf, signIn } from '../test/helpers'

let server: MockApiServer

beforeEach(() => {
  server = new MockApiServer()
})

async function checkpointRunId(): Promise<string> {
  await signIn(server, 'runs@example.org')
  const project = await seededProjectByName('Peer review checklists')
  const runs = await runsOf(project.id)
  const checkpoint = runs.find((run) => run.state === 'CHECKPOINT')
  if (!checkpoint) throw new Error('no checkpoint run seeded')
  return checkpoint.id
}

describe('RunPage (state badge, plan board, SSE change log, meters, Q chart)', () => {
  it('renders the run state, the plan grouped by role, the gates and the budget meters', async () => {
    const runId = await checkpointRunId()
    const { container } = render(
      <RunPage runId={runId} onOpenArticle={vi.fn()} onBack={vi.fn()} />,
    )

    // State badge (the report repeats the label, so both are fine).
    expect((await screen.findAllByText('Waiting for you')).length).toBeGreaterThan(0)

    // Plan board: one column per role that owns a task, and the task titles.
    await waitFor(() => {
      expect(container.querySelectorAll('.role-column').length).toBeGreaterThanOrEqual(4)
    })
    expect(container.querySelectorAll('.role-column').length).toBe(5)
    expect(
      screen.getAllByText('Draft the section outline and the contribution statement').length,
    ).toBeGreaterThan(0)
    expect(container.querySelectorAll('.task-card').length).toBe(6)

    // Q chart: one bullet per iteration plus eight gate chips.
    await waitFor(() => {
      expect(container.querySelectorAll('.gate-chip').length).toBe(8)
    })
    expect(container.querySelectorAll('.chart-dot').length).toBe(4)
    expect(container.querySelectorAll('.gate-chip-ok').length).toBe(8)

    // Token and cost meters with real caps.
    const tokens = await screen.findByRole('progressbar', { name: 'Tokens' })
    const tokensNow = Number(tokens.getAttribute('aria-valuenow'))
    expect(tokensNow).toBeGreaterThan(0)
    expect(tokensNow).toBeLessThan(100) // 59,910 of a 920,000 cap
    expect(screen.getByRole('progressbar', { name: 'Iterations' })).toBeTruthy()
    const cost = screen.getByRole('progressbar', { name: 'Est. cost' })
    expect(cost.getAttribute('aria-valuetext')).toMatch(/% of cap/)
    expect(screen.getByText(/no USD cap in policies/i)).toBeTruthy()
  })

  it('streams the change log over SSE and shows the ledger audit trail', async () => {
    const runId = await checkpointRunId()
    const { container } = render(
      <RunPage runId={runId} onOpenArticle={vi.fn()} onBack={vi.fn()} />,
    )

    await waitFor(() => {
      expect(container.querySelectorAll('.event-row').length).toBeGreaterThan(5)
    })
    const feed = container.querySelector('.event-feed')
    expect(feed).toBeTruthy()
    expect(feed?.textContent ?? '').toContain('run_created')
    expect(feed?.textContent ?? '').toContain('task_completed')
    expect(feed?.textContent ?? '').toContain('checkpoint_reached')

    // The ledger is the audited version of the same story.
    await waitFor(() => {
      expect(container.querySelectorAll('.data-table tbody tr').length).toBeGreaterThan(5)
    })
    expect(screen.getByText(/Froze snapshot iter-3-q78/i)).toBeTruthy()
  })

  it('opens the checkpoint dialog with exactly the legal options and finishes the run', async () => {
    const runId = await checkpointRunId()
    const onOpenArticle = vi.fn()
    render(<RunPage runId={runId} onOpenArticle={onOpenArticle} onBack={vi.fn()} />)

    await screen.findAllByText('Waiting for you')
    fireEvent.click(screen.getByRole('button', { name: /review checkpoint/i }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/checkpoint reached/i)).toBeTruthy()

    // Options come from stop_report.options: finish, continue, rollback — and nothing else.
    const radios = within(dialog).getAllByRole('radio') as HTMLInputElement[]
    expect(radios.map((radio) => radio.value)).toEqual(['finish', 'continue', 'rollback'])

    fireEvent.click(radios[0]!)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Finish' }))

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull()
    })
    await waitFor(() => {
      expect(screen.getAllByText('Done').length).toBeGreaterThan(0)
    })
    expect(onOpenArticle).not.toHaveBeenCalled()
  })

  it('opens the article viewer through the header action', async () => {
    const runId = await checkpointRunId()
    const onOpenArticle = vi.fn()
    render(<RunPage runId={runId} onOpenArticle={onOpenArticle} onBack={vi.fn()} />)
    await screen.findAllByText('Waiting for you')
    fireEvent.click(screen.getByRole('button', { name: /open article/i }))
    expect(onOpenArticle).toHaveBeenCalledWith(runId)
  })

  it('shows a load error state with a retry when the run does not exist', async () => {
    await signIn(server, 'missing@example.org')
    render(<RunPage runId="01ZZZZZZZZZZZZZZZZZZZZZZZZ" onOpenArticle={vi.fn()} onBack={vi.fn()} />)
    expect(await screen.findByText(/run unavailable/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /try again/i })).toBeTruthy()
  })

  it('renders the stop report with evidence for a stopped run', async () => {
    await signIn(server, 'erosion@example.org')
    const project = await seededProjectByName('Peer review checklists')
    const runs = await runsOf(project.id)
    const stopped = runs.find((run) => String(run.state).startsWith('STOPPED'))
    if (!stopped) throw new Error('no stopped run seeded')

    const { container } = render(
      <RunPage runId={stopped.id} onOpenArticle={vi.fn()} onBack={vi.fn()} />,
    )
    expect((await screen.findAllByText(/Stopped · erosion/i)).length).toBeGreaterThan(0)
    expect(await screen.findByText('Evidence (4 keys)')).toBeTruthy()
    expect(screen.getAllByText('plateau_detected').length).toBeGreaterThan(0)
    expect(container.querySelector('.reason-list')).toBeTruthy()
    // A stopped run offers the erosion options, not Finish.
    fireEvent.click(screen.getByRole('button', { name: /review checkpoint/i }))
    const dialog = await screen.findByRole('dialog')
    const radios = within(dialog).getAllByRole('radio') as HTMLInputElement[]
    expect(radios.map((radio) => radio.value)).toEqual([
      'rollback',
      'edit_goals',
      'add_budget',
      'continue_anyway',
    ])
  })
})
