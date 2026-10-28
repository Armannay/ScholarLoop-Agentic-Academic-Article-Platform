import { render, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { setAccessToken } from '../api/client'
import { MockApiServer } from '../api/mockServer'
import { useRunFeed } from '../lib/useRunFeed'
import { installMockFetch, runsOf, seededProjectByName, signIn } from '../test/helpers'
import type { RunEvent } from '../lib/domain'

let server: MockApiServer

beforeEach(() => {
  server = new MockApiServer()
  setAccessToken(null)
  installMockFetch(server)
})

function Feed({ runId }: { runId: string }): React.JSX.Element {
  const feed = useRunFeed(runId)
  return (
    <ul data-testid="feed">
      {feed.events.map((event: RunEvent) => (
        <li key={event.seq}>
          {event.seq}:{event.type}
        </li>
      ))}
    </ul>
  )
}

/**
 * Regression guard for the shell's `useEventSource`: its `closedRef` started `true` and
 * was never cleared on connect, so the guard in `connect()` turned every connection into
 * a no-op and no frame was ever delivered. A stream that never connects still renders a
 * page, which is why the shell's own smoke test could not see it.
 */
describe('useEventSource (shell SSE hook) delivers the run log', () => {
  it('streams frames, in order and without duplicates', async () => {
    await signIn(server, 'sse@example.org')
    const project = await seededProjectByName('Peer review checklists')
    const runs = await runsOf(project.id)
    const runId = runs[0]?.id
    if (!runId) throw new Error('no seeded run')

    const { getByTestId } = render(<Feed runId={runId} />)
    await waitFor(() => {
      expect(getByTestId('feed').children.length).toBeGreaterThan(5)
    })
    const rows = Array.from(getByTestId('feed').children).map((node) => node.textContent ?? '')
    expect(rows[0]).toBe('0:run_created')
    const sequences = rows.map((row) => Number(row.split(':')[0]))
    expect(new Set(sequences).size).toBe(sequences.length)
    expect([...sequences].sort((a, b) => a - b)).toEqual(sequences)
  })
})
