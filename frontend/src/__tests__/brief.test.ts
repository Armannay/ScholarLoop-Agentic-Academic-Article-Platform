import { describe, expect, it } from 'vitest'
import {
  countWords,
  isRtlLanguage,
  validateBriefDraft,
  wordToleranceRange,
  type BriefDraftInput,
} from '../types'

function draft(overrides: Partial<BriefDraftInput> = {}): BriefDraftInput {
  return {
    topic: 'Tightening peer review with structured checklists',
    article_type: 'research',
    audience: 'CS faculty and graduate reviewers',
    language: 'en',
    target_words: '850',
    citation_style: 'APA7',
    must_include: [],
    must_avoid: [],
    provided_data: [],
    ...overrides,
  }
}

describe('Brief inline validation (mirrors deterministic intake)', () => {
  it('accepts a complete brief', () => {
    const result = validateBriefDraft(draft())
    expect(result.valid).toBe(true)
    expect(result.fieldErrors).toEqual({})
    expect(result.missingFields).toEqual([])
  })

  it('names every missing field instead of defaulting one', () => {
    const result = validateBriefDraft(
      draft({ topic: '   ', audience: '', language: '', target_words: '', citation_style: '' }),
    )
    expect(result.valid).toBe(false)
    expect(result.missingFields.sort()).toEqual(
      ['audience', 'citation_style', 'language', 'target_words', 'topic'].sort(),
    )
    expect(result.fieldErrors.topic).toMatch(/required/i)
    expect(result.fieldErrors.target_words).toMatch(/required/i)
  })

  it('rejects a topic that is too vague, with the intake problem code', () => {
    const result = validateBriefDraft(draft({ topic: 'Peer review' }))
    expect(result.valid).toBe(false)
    expect(result.problems).toContain('topic_too_vague')
    expect(result.fieldErrors.topic).toMatch(/at least 3 words/i)
  })

  it('bounds target words to 300..30,000 and demands an integer', () => {
    for (const value of ['299', '30001', '850.5', 'lots']) {
      const result = validateBriefDraft(draft({ target_words: value }))
      expect(result.valid).toBe(false)
      expect(result.fieldErrors.target_words).toMatch(/between 300 and 30,000/i)
    }
    expect(validateBriefDraft(draft({ target_words: '300' })).valid).toBe(true)
    expect(validateBriefDraft(draft({ target_words: '30000' })).valid).toBe(true)
  })

  it('rejects a language tag that is not BCP-47', () => {
    for (const value of ['e', 'english language', 'en_GB!']) {
      const result = validateBriefDraft(draft({ language: value }))
      expect(result.valid).toBe(false)
      expect(result.problems).toContain('language_not_bcp47')
    }
    for (const value of ['en', 'en-GB', 'fa', 'ar', 'zh-Hant']) {
      expect(validateBriefDraft(draft({ language: value })).valid).toBe(true)
    }
  })

  it('refuses contradictory must_include / must_avoid', () => {
    const result = validateBriefDraft(
      draft({
        must_include: ['a comparison of checklist and free-form reviews'],
        must_avoid: ['A Comparison of checklist and free-form reviews'],
      }),
    )
    expect(result.valid).toBe(false)
    expect(result.problems).toContain('contradictory_constraints')
    expect(result.fieldErrors.must_include).toMatch(/overlap/i)
    expect(result.fieldErrors.must_avoid).toMatch(/overlap/i)
  })

  it('caps each instruction at 40 words', () => {
    const long = Array.from({ length: 41 }, (_, index) => `word${index}`).join(' ')
    const result = validateBriefDraft(draft({ must_include: [long] }))
    expect(result.valid).toBe(false)
    expect(result.problems).toContain('invalid_must_include')
  })

  it('keeps the ±15% tolerance identical to the contract', () => {
    expect(wordToleranceRange(850)).toEqual({ low: 722, high: 977 })
  })

  it('detects RTL languages for the viewer', () => {
    expect(isRtlLanguage('fa')).toBe(true)
    expect(isRtlLanguage('ar-EG')).toBe(true)
    expect(isRtlLanguage('en')).toBe(false)
    expect(isRtlLanguage(null)).toBe(false)
  })

  it('counts words the way the backend does', () => {
    expect(countWords('')).toBe(0)
    expect(countWords('  one   two\nthree ')).toBe(3)
  })
})
