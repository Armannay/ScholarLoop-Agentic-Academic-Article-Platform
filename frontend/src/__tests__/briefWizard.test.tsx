import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { BriefForm } from '../components'

function fill(label: RegExp | string, value: string): void {
  const field = screen.getByLabelText(label) as HTMLInputElement | HTMLTextAreaElement
  fireEvent.change(field, { target: { value } })
}

describe('BriefWizard (Brief form with inline validation)', () => {
  it('shows inline errors on submit, focuses the first invalid field and does not submit', () => {
    const onSubmit = vi.fn()
    render(<BriefForm submitting={false} highlightFields={[]} onSubmit={onSubmit} />)

    fireEvent.click(screen.getByRole('button', { name: /create project & run/i }))

    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByRole('alert').textContent ?? '').toMatch(/need attention/i)
    expect(screen.getByText('Project name is required.')).toBeTruthy()
    expect(screen.getByText(/topic is required/i)).toBeTruthy()
    // Focus moved to the first invalid control, which is also marked invalid.
    const name = screen.getByLabelText(/project name/i)
    expect(document.activeElement).toBe(name)
    expect(name.getAttribute('aria-invalid')).toBe('true')
  })

  it('does not shout before a field is touched', () => {
    render(<BriefForm submitting={false} highlightFields={[]} onSubmit={vi.fn()} />)
    expect(screen.queryByText(/topic is required/i)).toBeNull()
    fireEvent.blur(screen.getByLabelText(/topic/i))
    expect(screen.getByText(/topic is required/i)).toBeTruthy()
  })

  it('turns a NEEDS_INPUT answer from the server into inline questions', () => {
    render(<BriefForm submitting={false} highlightFields={['audience', 'topic']} onSubmit={vi.fn()} />)
    expect(screen.getByLabelText(/audience/i).getAttribute('aria-invalid')).toBe('true')
    expect(screen.getByLabelText(/^topic/i).getAttribute('aria-invalid')).toBe('true')
    expect(screen.getByText(/target audience is required/i)).toBeTruthy()
    // A highlighted field also points its description at the error message.
    expect(screen.getByLabelText(/audience/i).getAttribute('aria-describedby')).toBe('brief-audience-error')
  })

  it('submits the exact Brief contract shape once everything is valid', () => {
    const onSubmit = vi.fn()
    render(<BriefForm submitting={false} highlightFields={[]} onSubmit={onSubmit} />)

    fill(/project name/i, 'peer-review-2026')
    fill(/^topic/i, 'Tightening peer review with structured checklists')
    fill(/audience/i, 'CS faculty and graduate reviewers')
    fill(/language/i, 'en')
    fill(/target words/i, '850')
    fill(/must include/i, 'a comparison of checklist and free-form reviews')

    fireEvent.click(screen.getByRole('button', { name: /create project & run/i }))

    expect(onSubmit).toHaveBeenCalledTimes(1)
    const payload = onSubmit.mock.calls[0]?.[0] as {
      name: string
      brief: Record<string, unknown>
    }
    expect(payload.name).toBe('peer-review-2026')
    expect(payload.brief).toMatchObject({
      schema_version: 1,
      topic: 'Tightening peer review with structured checklists',
      article_type: 'research',
      audience: 'CS faculty and graduate reviewers',
      language: 'en',
      target_words: 850,
      citation_style: 'APA7',
      must_include: ['a comparison of checklist and free-form reviews'],
      must_avoid: [],
    })
  })

  it('reflects the ±15% word tolerance live and blocks a contradictory brief', () => {
    const onSubmit = vi.fn()
    render(<BriefForm submitting={false} highlightFields={[]} onSubmit={onSubmit} />)
    expect(screen.getByText(/722–977 words/)).toBeTruthy()

    fill(/project name/i, 'contradiction-demo')
    fill(/^topic/i, 'Tightening peer review with structured checklists')
    fill(/audience/i, 'Reviewers')
    fill(/must include/i, 'structured checklists')
    fill(/must avoid/i, 'Structured checklists')

    fireEvent.click(screen.getByRole('button', { name: /create project & run/i }))
    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getAllByText(/overlap/i).length).toBeGreaterThan(0)
  })

  it('disables the submit button while a submit is in flight', () => {
    render(<BriefForm submitting highlightFields={[]} onSubmit={vi.fn()} />)
    const button = screen.getByRole('button', { name: /validating/i }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
  })
})
