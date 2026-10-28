import { describe, expect, it } from 'vitest'
import { renderArticleMarkdown } from '../lib/exportMarkdown'
import { ENGLISH_BRIEF, PERSIAN_BRIEF, buildEnglishArticle, buildPersianArticle } from '../api/fixtures'

const RUN_ID = '01M5Z0X96Y000000000000005H'

describe('markdown export', () => {
  it('writes front matter, sections in order, references and source cards', () => {
    const article = buildEnglishArticle(RUN_ID)
    const markdown = renderArticleMarkdown(article, ENGLISH_BRIEF)

    expect(markdown.startsWith('---\n')).toBe(true)
    expect(markdown).toContain(`title: "${ENGLISH_BRIEF.topic}"`)
    expect(markdown).toContain('language: en')
    expect(markdown).toContain('citation_style: APA7')
    expect(markdown).toContain(`run_id: ${RUN_ID}`)
    expect(markdown).toContain(`# ${ENGLISH_BRIEF.topic}`)
    expect(markdown).toContain('## Introduction')
    expect(markdown).toContain('## References')
    expect(markdown).toContain('## Source cards')
    expect(markdown).toContain('doi:10.1191/1478088706qp063oa')

    // References are appended once, not repeated in the body.
    const referencesHeadings = markdown.match(/^## References$/gm) ?? []
    expect(referencesHeadings).toHaveLength(1)
    const introIndex = markdown.indexOf('## Introduction')
    const discussionIndex = markdown.indexOf('## Discussion')
    expect(introIndex).toBeLessThan(discussionIndex)
  })

  it('keeps RTL text intact and does not transliterate it', () => {
    const article = buildPersianArticle(RUN_ID)
    const markdown = renderArticleMarkdown(article, PERSIAN_BRIEF)
    expect(markdown).toContain('language: fa')
    expect(markdown).toContain('## مقدمه')
    expect(markdown).toContain('داوری همتا')
  })

  it('falls back to the run id when no brief is available', () => {
    const article = buildPersianArticle(RUN_ID)
    const markdown = renderArticleMarkdown(article, null)
    expect(markdown).toContain(`# Article ${RUN_ID}`)
    expect(markdown).not.toContain('language:')
  })
})
