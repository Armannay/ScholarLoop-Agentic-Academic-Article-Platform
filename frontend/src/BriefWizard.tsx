import { useMemo, useRef, useState, type ReactNode } from 'react'
import {
  ARTICLE_TYPES,
  CITATION_STYLES,
  MAX_TARGET_WORDS,
  MIN_TARGET_WORDS,
  validateBriefDraft,
  type ArticleType,
  type Brief,
  type CitationStyle,
} from './types'
import { formatInt } from './format'

export interface BriefWizardProps {
  submitting: boolean
  /** Fields the API said it needs (`409 needs_user_input` -> missing_fields). */
  highlightFields: readonly string[]
  onSubmit: (payload: { name: string; brief: Brief }) => void
  footerExtra?: ReactNode
}

const ARTICLE_TYPE_LABELS: Record<ArticleType, string> = {
  research: 'Research article',
  review: 'Review article',
  position: 'Position paper',
  case_study: 'Case study',
}

const DEFAULT_LANGUAGE = 'en'

function linesToArray(value: string): string[] {
  return value
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
}

/**
 * The Brief form.
 *
 * Validation runs on every keystroke (so the counter and the messages are live) but a
 * message is only *shown* once the field has been blurred or the form submitted once —
 * nobody wants to be told their empty topic is invalid before they have typed.
 *
 * Accessibility: every control has a label, `aria-invalid` and `aria-describedby` point
 * at its own error, the summary is a `role="alert"`, and a failed submit moves focus to
 * the first invalid control.
 */
export function BriefWizard({ submitting, highlightFields, onSubmit, footerExtra }: BriefWizardProps): React.JSX.Element {
  const [name, setName] = useState('')
  const [topic, setTopic] = useState('')
  const [articleType, setArticleType] = useState<ArticleType>('research')
  const [audience, setAudience] = useState('')
  const [language, setLanguage] = useState(DEFAULT_LANGUAGE)
  const [targetWords, setTargetWords] = useState('850')
  const [citationStyle, setCitationStyle] = useState<CitationStyle>('APA7')
  const [mustInclude, setMustInclude] = useState('')
  const [mustAvoid, setMustAvoid] = useState('')
  const [providedData, setProvidedData] = useState('')
  const [touched, setTouched] = useState<Record<string, boolean>>({})
  const [submitAttempted, setSubmitAttempted] = useState(false)
  const formRef = useRef<HTMLFormElement | null>(null)

  const validation = useMemo(
    () =>
      validateBriefDraft({
        topic,
        article_type: articleType,
        audience,
        language,
        target_words: targetWords,
        citation_style: citationStyle,
        must_include: linesToArray(mustInclude),
        must_avoid: linesToArray(mustAvoid),
        provided_data: linesToArray(providedData),
      }),
    [topic, articleType, audience, language, targetWords, citationStyle, mustInclude, mustAvoid, providedData],
  )

  const nameError = name.trim() ? null : 'Project name is required.'

  const show = (field: string): boolean =>
    submitAttempted || touched[field] === true || highlightFields.includes(field)

  const errorFor = (field: string): string | undefined => {
    if (field === 'name') return show('name') && nameError ? nameError : undefined
    return show(field) ? validation.fieldErrors[field] : undefined
  }

  const fieldClass = (field: string): string =>
    errorFor(field) ? 'field field-invalid' : 'field'

  const describedBy = (field: string): string | undefined =>
    errorFor(field) ? `brief-${field}-error` : undefined

  const blur = (field: string): void => setTouched((previous) => ({ ...previous, [field]: true }))

  const focusFirstInvalid = (): void => {
    const order = [
      'name',
      'topic',
      'audience',
      'language',
      'target_words',
      'article_type',
      'citation_style',
      'must_include',
      'must_avoid',
    ]
    const first =
      order.find((field) => field === 'name' ? Boolean(nameError) : Boolean(validation.fieldErrors[field])) ??
      null
    if (!first) return
    const element = formRef.current?.querySelector<HTMLElement>(`[name="${first}"]`)
    element?.focus()
    element?.scrollIntoView?.({ block: 'center' })
  }

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>): void => {
    event.preventDefault()
    setSubmitAttempted(true)
    setTouched((previous) => ({ ...previous, name: true }))
    if (nameError || !validation.valid) {
      focusFirstInvalid()
      return
    }
    const brief: Brief = {
      schema_version: 1,
      topic: topic.trim(),
      article_type: articleType,
      audience: audience.trim(),
      language: language.trim(),
      target_words: Number(targetWords),
      citation_style: citationStyle,
      must_include: linesToArray(mustInclude),
      must_avoid: linesToArray(mustAvoid),
      provided_data: linesToArray(providedData),
    }
    onSubmit({ name: name.trim(), brief })
  }

  const errorCount =
    Object.keys(validation.fieldErrors).length + (nameError ? 1 : 0)
  const showSummary = submitAttempted && errorCount > 0
  const targetNumber = Number(targetWords)
  const toleranceOk = Number.isFinite(targetNumber) && targetNumber >= MIN_TARGET_WORDS && targetNumber <= MAX_TARGET_WORDS

  const renderError = (field: string): React.JSX.Element | null => {
    const message = errorFor(field)
    if (!message) return null
    return (
      <span className="field-error" id={`brief-${field}-error`}>
        {message}
      </span>
    )
  }

  return (
    <form className="form-stack" onSubmit={handleSubmit} ref={formRef} noValidate>
      {showSummary && (
        <div className="notice notice-bad" role="alert">
          {errorCount} field{errorCount === 1 ? '' : 's'} need attention before this brief can run.
        </div>
      )}

      <div className="form-grid">
        <label className={fieldClass('name')}>
          <span className="field-label">Project name (unique to you)</span>
          <input
            name="name"
            type="text"
            maxLength={120}
            value={name}
            aria-invalid={errorFor('name') ? true : undefined}
            aria-describedby={describedBy('name')}
            onChange={(event) => setName(event.target.value)}
            onBlur={() => blur('name')}
            placeholder="e.g. peer-review-2026"
          />
          {renderError('name')}
        </label>

        <label className="field">
          <span className="field-label">Article type</span>
          <select
            name="article_type"
            value={articleType}
            aria-invalid={errorFor('article_type') ? true : undefined}
            aria-describedby={describedBy('article_type')}
            onChange={(event) => setArticleType(event.target.value as ArticleType)}
            onBlur={() => blur('article_type')}
          >
            {ARTICLE_TYPES.map((type) => (
              <option key={type} value={type}>
                {ARTICLE_TYPE_LABELS[type]}
              </option>
            ))}
          </select>
          {renderError('article_type')}
        </label>

        <label className={`${fieldClass('topic')} field-span-2`}>
          <span className="field-label">Topic — one sentence stating the subject</span>
          <input
            name="topic"
            type="text"
            value={topic}
            aria-invalid={errorFor('topic') ? true : undefined}
            aria-describedby={describedBy('topic')}
            onChange={(event) => setTopic(event.target.value)}
            onBlur={() => blur('topic')}
            placeholder="Tightening peer review with structured checklists"
          />
          {renderError('topic')}
        </label>

        <label className={`${fieldClass('audience')} field-span-2`}>
          <span className="field-label">Audience</span>
          <input
            name="audience"
            type="text"
            value={audience}
            aria-invalid={errorFor('audience') ? true : undefined}
            aria-describedby={describedBy('audience')}
            onChange={(event) => setAudience(event.target.value)}
            onBlur={() => blur('audience')}
            placeholder="CS faculty and graduate reviewers"
          />
          {renderError('audience')}
        </label>

        <label className={fieldClass('language')}>
          <span className="field-label">Language (BCP-47)</span>
          <input
            name="language"
            type="text"
            list="brief-language-options"
            maxLength={35}
            value={language}
            aria-invalid={errorFor('language') ? true : undefined}
            aria-describedby={describedBy('language')}
            onChange={(event) => setLanguage(event.target.value)}
            onBlur={() => blur('language')}
            placeholder="en, en-GB, fa, ar"
          />
          <span className="field-hint">
            Persian (`fa`) and Arabic (`ar`) articles render right-to-left in the viewer.
          </span>
          {renderError('language')}
        </label>

        <label className={fieldClass('target_words')}>
          <span className="field-label">Target words</span>
          <input
            name="target_words"
            type="number"
            min={MIN_TARGET_WORDS}
            max={MAX_TARGET_WORDS}
            step={50}
            value={targetWords}
            aria-invalid={errorFor('target_words') ? true : undefined}
            aria-describedby={describedBy('target_words')}
            onChange={(event) => setTargetWords(event.target.value)}
            onBlur={() => blur('target_words')}
          />
          <span className="field-hint">
            {MIN_TARGET_WORDS.toLocaleString()}–{MAX_TARGET_WORDS.toLocaleString()}; the total-words
            gate allows ±15%
            {toleranceOk
              ? ` (${formatInt(Math.floor(targetNumber * 0.85))}–${formatInt(Math.floor(targetNumber * 1.15))} words).`
              : '.'}
          </span>
          {renderError('target_words')}
        </label>

        <label className={fieldClass('citation_style')}>
          <span className="field-label">Citation style</span>
          <select
            name="citation_style"
            value={citationStyle}
            aria-invalid={errorFor('citation_style') ? true : undefined}
            aria-describedby={describedBy('citation_style')}
            onChange={(event) => setCitationStyle(event.target.value as CitationStyle)}
            onBlur={() => blur('citation_style')}
          >
            {CITATION_STYLES.map((style) => (
              <option key={style} value={style}>
                {style}
              </option>
            ))}
          </select>
          {renderError('citation_style')}
        </label>

        <label className={fieldClass('must_include')}>
          <span className="field-label">Must include (one instruction per line)</span>
          <textarea
            name="must_include"
            rows={3}
            value={mustInclude}
            aria-invalid={errorFor('must_include') ? true : undefined}
            aria-describedby={describedBy('must_include')}
            onChange={(event) => setMustInclude(event.target.value)}
            onBlur={() => blur('must_include')}
            placeholder="a comparison of checklist and free-form reviews"
          />
          {renderError('must_include')}
        </label>

        <label className={fieldClass('must_avoid')}>
          <span className="field-label">Must avoid (one instruction per line)</span>
          <textarea
            name="must_avoid"
            rows={3}
            value={mustAvoid}
            aria-invalid={errorFor('must_avoid') ? true : undefined}
            aria-describedby={describedBy('must_avoid')}
            onChange={(event) => setMustAvoid(event.target.value)}
            onBlur={() => blur('must_avoid')}
            placeholder="claims about journal acceptance rates"
          />
          {renderError('must_avoid')}
        </label>

        <label className="field field-span-2">
          <span className="field-label">Provided data or quotes (verbatim, optional)</span>
          <textarea
            name="provided_data"
            rows={3}
            value={providedData}
            onChange={(event) => setProvidedData(event.target.value)}
            placeholder="Paste data the article may cite. Empty is allowed — the researcher then has to find real sources, never invent them."
          />
          <span className="field-hint">
            Citations to this data are treated as verified by construction. Leave empty if you
            have none.
          </span>
        </label>
      </div>

      <datalist id="brief-language-options">
        <option value="en" />
        <option value="en-GB" />
        <option value="fa" />
        <option value="ar" />
        <option value="de" />
        <option value="fr" />
        <option value="es" />
      </datalist>

      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={submitting}>
          {submitting ? 'Validating & creating…' : 'Create project & run'}
        </button>
        {footerExtra}
      </div>
    </form>
  )
}
