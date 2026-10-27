import { useCallback, useMemo, useState } from 'react'
import {
  getRun,
  getRunArticle,
  getSectionDiff,
  getSectionHistory,
  listArticleSnapshots,
  restoreArticleSnapshot,
} from '../lib/api'
import { downloadArticleMarkdown } from '../lib/exportMarkdown'
import {
  formatInt,
  formatTimestamp,
  readingDirection,
  sectionStatusTone,
  shortId,
  statusLabel,
} from '../lib/format'
import { useAsync } from '../lib/useAsync'
import { StatusPill } from '../components'
import type {
  ArticleSection,
  ArticleView,
  Brief,
  SectionDiffView,
  SectionHistoryView,
  SnapshotList,
  RunView,
  VersionInfo,
} from '../lib/domain'

export interface ArticleProps {
  runId: string
  /** Optional: the project's brief, when the caller already has it. */
  brief?: Brief | Record<string, unknown> | null | undefined
  onBack: () => void
}

function asBrief(raw: Brief | Record<string, unknown> | null | undefined): Brief | null {
  if (!raw || typeof raw !== 'object') return null
  const candidate = raw as Partial<Brief>
  if (typeof candidate.language !== 'string' || typeof candidate.topic !== 'string') return null
  return candidate as Brief
}

/**
 * The article, its history and its snapshots.
 *
 * Three things this screen refuses to fake: bodies come from `/article` only, history
 * travels by version/hash/word count, and a restore is reported as new versions rather
 * than as a rewrite. The reading direction is taken from the brief's BCP-47 tag, so a
 * `fa` or `ar` article renders RTL with `lang`/`dir` set on the article element.
 */
export function Article({ runId, brief = null, onBack }: ArticleProps): React.JSX.Element {
  const loadArticle = useCallback((signal?: AbortSignal) => getRunArticle(runId, signal), [runId])
  const loadSnapshots = useCallback((signal?: AbortSignal) => listArticleSnapshots(runId, signal), [runId])
  const loadRun = useCallback((signal?: AbortSignal) => getRun(runId, signal), [runId])
  const article = useAsync<ArticleView>(loadArticle)
  const snapshots = useAsync<SnapshotList>(loadSnapshots)
  // The brief carries the language, and the language decides the reading direction.
  const run = useAsync<RunView>(loadRun)

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [history, setHistory] = useState<SectionHistoryView | null>(null)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [diff, setDiff] = useState<SectionDiffView | null>(null)
  const [diffError, setDiffError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null)

  const parsedBrief = useMemo(
    () => asBrief(brief) ?? asBrief(run.data?.brief ?? null),
    [brief, run.data],
  )
  const language = parsedBrief?.language ?? null
  const direction = readingDirection(language)
  const sections = article.data?.sections ?? []
  const selected: ArticleSection | null = sections.find((s) => s.id === selectedId) ?? sections[0] ?? null

  const openSection = async (sectionId: string): Promise<void> => {
    setSelectedId(sectionId)
    setHistory(null)
    setDiff(null)
    setHistoryError(null)
    setDiffError(null)
    try {
      setHistory(await getSectionHistory(runId, sectionId))
    } catch (cause) {
      setHistoryError(cause instanceof Error ? cause.message : 'history unavailable')
    }
  }

  const showDiff = async (sectionId: string, from?: number, to?: number): Promise<void> => {
    setDiffError(null)
    try {
      const result = await getSectionDiff(runId, sectionId, {
        ...(from !== undefined ? { from } : {}),
        ...(to !== undefined ? { to } : {}),
      })
      setDiff(result)
    } catch (cause) {
      setDiffError(cause instanceof Error ? cause.message : 'diff unavailable')
    }
  }

  const handleExport = (): void => {
    if (!article.data) return
    const result = downloadArticleMarkdown(article.data, parsedBrief)
    setNotice({ tone: 'ok', text: `Exported ${result.filename} (${formatInt(result.bytes)} bytes).` })
  }

  const handleRestore = async (snapshotId: string): Promise<void> => {
    setBusy(true)
    setNotice(null)
    try {
      const result = await restoreArticleSnapshot(runId, { snapshot_id: snapshotId })
      setNotice({
        tone: 'ok',
        text:
          result.restored.length === 0
            ? `Nothing to restore: all ${result.unchanged.length} sections already match ${result.label}.`
            : `Restored ${result.restored.length} section(s) from ${result.label} as new versions: ${result.restored
                .map((id) => `${id}@v${result.versions[id] ?? '?'}`)
                .join(', ')}.`,
      })
      article.reload()
      snapshots.reload()
      if (selected) await openSection(selected.id)
    } catch (cause) {
      setNotice({ tone: 'bad', text: cause instanceof Error ? cause.message : 'restore failed' })
    } finally {
      setBusy(false)
    }
  }

  if (article.loading && !article.data) {
    return (
      <section className="panel" aria-busy="true">
        <p className="dim">Loading article…</p>
      </section>
    )
  }

  if (article.error && !article.data) {
    return (
      <section className="panel panel-bad">
        <h2>Article unavailable</h2>
        <p className="mono small">{article.error}</p>
        <div className="btn-row">
          <button type="button" className="btn" onClick={article.reload}>
            Try again
          </button>
          <button type="button" className="btn" onClick={onBack}>
            Back to run
          </button>
        </div>
      </section>
    )
  }

  if (!article.data) {
    return (
      <section className="panel">
        <div className="empty-state">No article yet.</div>
      </section>
    )
  }

  const data = article.data

  return (
    <div className="stack">
      <section className="panel">
        <div className="panel-head-wrap">
          <div>
            <div className="role-title-row">
              <h2>{parsedBrief?.topic ?? `Article ${shortId(data.run_id)}`}</h2>
              {language && <span className="badge mono">{language}</span>}
              {direction === 'rtl' && <span className="badge mono">RTL</span>}
            </div>
            <p className="subtitle mono small">
              {data.sections.length} sections · {formatInt(data.body_words)} body words ·{' '}
              {data.sources.length} source cards
            </p>
          </div>
          <div className="btn-row">
            <button type="button" className="btn" onClick={onBack}>
              ← Run
            </button>
            <button type="button" className="btn btn-primary" onClick={handleExport}>
              Export .md
            </button>
            <button type="button" className="btn" disabled title="DOCX and PDF arrive with a converter step">
              Export DOCX / PDF (later)
            </button>
          </div>
        </div>

        {notice && (
          <div className={notice.tone === 'ok' ? 'notice notice-ok' : 'notice notice-bad'} role="status">
            <span>{notice.text}</span>
            <button type="button" className="btn-link" onClick={() => setNotice(null)}>
              dismiss
            </button>
          </div>
        )}
      </section>

      <div className="article-layout">
        <section className="panel" aria-label="Article body">
          <h2>Article</h2>
          <p className="subtitle">
            Assembled markdown as the validator measured it. Every section is versioned
            independently; nothing here is rewritten by a restore.
          </p>
          {sections.length === 0 ? (
            <div className="empty-state" role="status">
              The article has no sections yet — the Architect creates them from the rubric.
            </div>
          ) : (
            <div className="article-body" dir={direction} lang={language ?? undefined}>
              {sections.map((section) => (
                <article key={section.id} className="article-section">
                  <header className="article-section-head">
                    <h3>{section.title}</h3>
                    <div className="chips-inline">
                      <StatusPill tone={sectionStatusTone(section.status)} label={statusLabel(section.status)} />
                      <span className="badge mono">v{section.version}</span>
                      <span className="badge mono">{formatInt(section.words)} w</span>
                    </div>
                  </header>
                  {(section.open_issues ?? []).length > 0 && (
                    <ul className="article-issues mono small">
                      {section.open_issues.map((issue) => (
                        <li key={issue}>{issue}</li>
                      ))}
                    </ul>
                  )}
                  <div className="article-text">
                    {section.text
                      ? section.text.split('\n\n').map((paragraph, index) => (
                          <p key={`${section.id}-p${index}`}>{paragraph}</p>
                        ))
                      : <p className="dim">(empty section)</p>}
                  </div>
                  <div className="btn-row">
                    <button type="button" className="btn btn-sm" onClick={() => void openSection(section.id)}>
                      Version history
                    </button>
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() => {
                        setSelectedId(section.id)
                        void showDiff(section.id)
                      }}
                    >
                      Diff last change
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        <div className="stack">
          <section className="panel" aria-label="Version history">
            <h2>Version history</h2>
            <p className="subtitle">
              {selected ? `Section ${selected.id}` : 'Choose a section'} — versions travel by hash and word
              count, never by body.
            </p>
            {historyError && (
              <div className="notice notice-bad" role="alert">
                {historyError}
              </div>
            )}
            {!history ? (
              <div className="empty-state" role="status">
                Open a section to see the versions that produced it.
              </div>
            ) : history.versions.length === 0 ? (
              <div className="empty-state" role="status">
                This section has no recorded versions yet.
              </div>
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">v</th>
                      <th scope="col">Status</th>
                      <th scope="col">Words</th>
                      <th scope="col">+/- chars</th>
                      <th scope="col">Q</th>
                      <th scope="col">Diff</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.versions.map((version: VersionInfo) => (
                      <tr key={version.version}>
                        <td className="mono">v{version.version}</td>
                        <td>{version.status}</td>
                        <td className="mono">{formatInt(version.words)}</td>
                        <td className="mono small">
                          +{version.chars_added} / -{version.chars_removed}
                        </td>
                        <td className="mono">{version.q_after ?? '—'}</td>
                        <td>
                          <button
                            type="button"
                            className="btn btn-sm"
                            onClick={() =>
                              void showDiff(
                                history.section_id,
                                Math.max(1, version.version - 1),
                                version.version,
                              )
                            }
                          >
                            vs prev
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="panel" aria-label="Diff">
            <h2>Diff</h2>
            {diffError && (
              <div className="notice notice-bad" role="alert">
                {diffError}
              </div>
            )}
            {!diff ? (
              <div className="empty-state" role="status">
                Pick a version to compare it with its predecessor.
              </div>
            ) : (
              <div className="stack-sm">
                <p className="subtitle mono small">
                  {diff.section_id} v{diff.from.version} → v{diff.to.version} · +{diff.diff.stats.chars_added}{' '}
                  / -{diff.diff.stats.chars_removed} chars · {diff.diff.lines_added} added lines
                </p>
                <pre className="diff-block" aria-label="Unified diff">
                  {diff.diff.unified.split('\n').map((line, index) => (
                    <span
                      key={`${index}-${line.slice(0, 8)}`}
                      className={
                        line.startsWith('+') && !line.startsWith('+++')
                          ? 'diff-add'
                          : line.startsWith('-') && !line.startsWith('---')
                            ? 'diff-del'
                            : line.startsWith('@@')
                              ? 'diff-hunk'
                              : undefined
                      }
                    >
                      {line}
                      {'\n'}
                    </span>
                  ))}
                </pre>
              </div>
            )}
          </section>

          <section className="panel" aria-label="Snapshots">
            <div className="panel-head">
              <h2>Snapshots</h2>
              <StatusPill
                tone={snapshots.error ? 'bad' : snapshots.data ? 'ok' : 'idle'}
                label={snapshots.data ? `${snapshots.data.snapshots.length}` : '—'}
              />
            </div>
            <p className="subtitle">
              A snapshot is the rollback target. Restoring appends a new version for every section
              whose text differs.
            </p>
            {snapshots.error && (
              <div className="notice notice-bad" role="alert">
                {snapshots.error}
                <button type="button" className="btn btn-sm" onClick={snapshots.reload}>
                  Retry
                </button>
              </div>
            )}
            {!snapshots.data ? (
              <div className="empty-state" role="status">
                {snapshots.loading ? 'Loading snapshots…' : 'No snapshots recorded.'}
              </div>
            ) : snapshots.data.snapshots.length === 0 ? (
              <div className="empty-state" role="status">
                No snapshots yet — one is frozen after every iteration that measured the article.
              </div>
            ) : (
              <ul className="snapshot-list">
                {snapshots.data.snapshots.map((snapshot) => {
                  const isBest = snapshot.id === snapshots.data?.best_snapshot_id
                  return (
                    <li key={snapshot.id} className={isBest ? 'snapshot-row snapshot-best' : 'snapshot-row'}>
                      <div>
                        <span className="strong mono">{snapshot.label}</span>
                        {isBest && <span className="badge mono"> best</span>}
                        <div className="small dim mono">
                          Q {snapshot.q ?? '—'} · iter {snapshot.iter ?? '—'} ·{' '}
                          {formatInt(snapshot.total_words)} words · {formatTimestamp(snapshot.created_at)}
                        </div>
                      </div>
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={busy}
                        onClick={() => void handleRestore(snapshot.id)}
                      >
                        {busy ? 'Restoring…' : 'Restore'}
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        </div>
      </div>
    </div>
  )
}
