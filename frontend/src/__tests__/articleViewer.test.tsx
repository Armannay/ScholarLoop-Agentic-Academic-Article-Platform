import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Article } from '../pages/article'
import { MockApiServer } from '../api/mockServer'
import { seededProjectByName, runsOf, signIn } from '../test/helpers'
import type { Project } from '../lib/api'

let server: MockApiServer

beforeEach(() => {
  server = new MockApiServer()
})

async function englishProject(): Promise<{ project: Project; runId: string }> {
  await signIn(server, 'article@example.org')
  const project = await seededProjectByName('Peer review checklists')
  const runs = await runsOf(project.id)
  const run = runs.find((row) => row.state === 'CHECKPOINT')
  if (!run) throw new Error('no checkpoint run seeded')
  return { project, runId: run.id }
}

describe('ArticleViewer (sections, history, diff, restore, RTL, export)', () => {
  it('renders every section with its status, word count and citations', async () => {
    const { project, runId } = await englishProject()
    const { container } = render(
      <Article runId={runId} brief={project.brief ?? null} onBack={vi.fn()} />,
    )

    expect(await screen.findByText('Methodology')).toBeTruthy()
    expect(screen.getByText('References')).toBeTruthy()
    expect(container.querySelectorAll('.article-section').length).toBe(9)
    // The open citation issue is visible rather than silently dropped.
    expect(screen.getAllByText('citation-unverified:src-gopen-1990').length).toBeGreaterThan(0)
    // Language of the brief is carried on the body element for hyphenation/quotes.
    const body = container.querySelector('.article-body')
    expect(body?.getAttribute('dir')).toBe('ltr')
    expect(body?.getAttribute('lang')).toBe('en')
  })

  it('opens a section history and compares two versions', async () => {
    const { project, runId } = await englishProject()
    const { container } = render(
      <Article runId={runId} brief={project.brief ?? null} onBack={vi.fn()} />,
    )
    await screen.findByText('Methodology')

    const firstSectionButtons = screen.getAllByRole('button', { name: /version history/i })
    fireEvent.click(firstSectionButtons[0]!)

    await waitFor(() => {
      expect(container.querySelector('.data-table tbody')).toBeTruthy()
    })
    expect(screen.getByText(/Section abstract/)).toBeTruthy()

    const diffButtons = screen.getAllByRole('button', { name: /vs prev/i })
    fireEvent.click(diffButtons[0]!)
    await waitFor(() => {
      expect(container.querySelector('.diff-block')).toBeTruthy()
    })
    expect(container.querySelector('.diff-block')?.textContent ?? '').toContain('@@')
  })

  it('restores a snapshot and reports it as new versions, not a rewrite', async () => {
    const { project, runId } = await englishProject()
    render(<Article runId={runId} brief={project.brief ?? null} onBack={vi.fn()} />)
    await screen.findByText('Methodology')

    const snapshotPanel = screen.getByLabelText('Snapshots')
    await waitFor(() => {
      expect(within(snapshotPanel).getAllByRole('button', { name: /restore/i }).length).toBe(4)
    })
    expect(within(snapshotPanel).getByText('iter-3-q78')).toBeTruthy()

    fireEvent.click(within(snapshotPanel).getAllByRole('button', { name: /restore/i })[0]!)
    await waitFor(() => {
      expect(document.querySelector('.notice')?.textContent ?? '').toMatch(/restored 2 section/i)
    })
    expect(screen.getByText(/discussion@v\d+/)).toBeTruthy()
  })

  it('exports the assembled article as markdown', async () => {
    const { project, runId } = await englishProject()
    const createObjectURL = vi.fn(() => 'blob:mock')
    const revokeObjectURL = vi.fn()
    // Replace only the two object-URL hooks: `URL` itself is still needed by the client.
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, configurable: true })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)

    render(<Article runId={runId} brief={project.brief ?? null} onBack={vi.fn()} />)
    await screen.findByText('Methodology')
    fireEvent.click(screen.getByRole('button', { name: /export \.md/i }))

    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(clickSpy).toHaveBeenCalledTimes(1)
    expect(document.querySelector('.notice')?.textContent ?? '').toMatch(/exported scholarloop-.*\.md/i)
    // DOCX/PDF are visibly deferred rather than silently missing.
    expect(screen.getByRole('button', { name: /export docx \/ pdf \(later\)/i })).toBeTruthy()

    clickSpy.mockRestore()
  })

  it('renders a Persian article right-to-left', async () => {
    await signIn(server, 'rtl@example.org')
    const project = await seededProjectByName('اخلاق')
    const runs = await runsOf(project.id)
    const run = runs[0]
    if (!run) throw new Error('no fa run seeded')

    const { container } = render(
      <Article runId={run.id} brief={project.brief ?? null} onBack={vi.fn()} />,
    )
    expect(await screen.findByText('مقدمه')).toBeTruthy()
    const body = container.querySelector('.article-body')
    expect(body?.getAttribute('dir')).toBe('rtl')
    expect(body?.getAttribute('lang')).toBe('fa')
    expect(screen.getByText('RTL')).toBeTruthy()
  })

  it('shows an error state when the article cannot be loaded', async () => {
    await signIn(server, 'missing-article@example.org')
    render(<Article runId="01ZZZZZZZZZZZZZZZZZZZZZZZZ" brief={null} onBack={vi.fn()} />)
    expect(await screen.findByText(/article unavailable/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /try again/i })).toBeTruthy()
  })
})
