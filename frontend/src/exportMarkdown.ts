// Export the assembled article as a `.md` file, entirely in the browser.
//
// The bytes come from `GET /api/runs/{id}/article` (the same markdown the article
// viewer renders), so an export can never disagree with what the reviewer read.
// DOCX and PDF are deliberately not implemented yet: they need a converter the
// console does not ship, and a generated file that silently loses the references
// would be worse than no file.

import type { ArticleView, Brief } from './types'

export function renderArticleMarkdown(article: ArticleView, brief?: Brief | null): string {
  const title =
    brief && typeof brief.topic === 'string' && brief.topic.trim()
      ? brief.topic.trim()
      : `Article ${article.run_id}`
  const frontMatter = [
    '---',
    `title: "${title.replace(/"/g, '\\"')}"`,
    brief ? `language: ${brief.language}` : null,
    brief ? `citation_style: ${brief.citation_style}` : null,
    brief ? `article_type: ${brief.article_type}` : null,
    `run_id: ${article.run_id}`,
    `body_words: ${article.body_words}`,
    '---',
    '',
  ]
    .filter((line): line is string => line !== null)
    .join('\n')

  const references =
    article.sections.find((section) => section.id === 'references')?.text.trim() ?? ''
  const body = article.sections
    .filter((section) => section.id !== 'references')
    .map((section) => ({ section, text: section.text.trim() }))
    .filter(({ text }) => text.length > 0)

  const rendered = body
    .map(({ section, text }) => `## ${section.title}\n\n${text}`)
    .join('\n\n')

  const sourceList =
    article.sources.length > 0
      ? `\n\n## Source cards\n\n${article.sources
          .map(
            (card) =>
              `- ${card.title} (${card.year}) — ${card.venue} — ${card.doi ? `doi:${card.doi}` : card.url ?? 'no locator'} — ${card.verification}`,
          )
          .join('\n')}`
      : ''

  return `${frontMatter}# ${title}\n\n${rendered}${
    references ? `\n\n## References\n\n${references}` : ''
  }${sourceList}\n`
}

export interface ExportResult {
  filename: string
  bytes: number
}

/** Build the markdown and hand it to the browser as a download. */
export function downloadArticleMarkdown(
  article: ArticleView,
  brief?: Brief | null,
  filename?: string,
): ExportResult {
  const markdown = renderArticleMarkdown(article, brief)
  const name = filename ?? `scholarloop-${article.run_id}.md`
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.rel = 'noopener'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // Revoke on the next tick so Safari has time to start the download.
  setTimeout(() => URL.revokeObjectURL(url), 0)
  return { filename: name, bytes: blob.size }
}
