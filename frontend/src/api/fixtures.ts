// Deterministic mock fixtures for the ScholarLoop console (Step 10b).
//
// This module is *data only*: it mirrors the payloads in `docs/openapi/examples/`
// so the UI can be built, demoed and tested without a backend. Every scenario tells
// the same story the spec's examples tell (Q 54 -> 63 -> 71 -> 78, one unverified
// reference, best snapshot `iter-3-q78`), plus a Persian project so the RTL path is
// exercised by real content rather than by a CSS flag.
//
// HARD RULE 2: there are no keys, tokens or ciphertexts in here - just article data.

import {
  GATE_NAMES,
  countWords,
  type ArticleSection,
  type ArticleView,
  type Brief,
  type ChangeEntry,
  type LedgerView,
  type Plan,
  type QualityPoint,
  type QualityView,
  type RunEvent,
  type RunUsageView,
  type RunView,
  type SectionDiffView,
  type SnapshotList,
  type Task,
  type VersionInfo,
} from '../lib/domain'

/** Deterministic 26-char Crockford ULID-shaped id from a seed string. */
export function mockId(seed: string): string {
  const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
  let h1 = 0x811c9dc5
  let h2 = 0x01000193
  for (let i = 0; i < seed.length; i += 1) {
    const ch = seed.charCodeAt(i)
    h1 = Math.imul(h1 ^ ch, 16777619) >>> 0
    h2 = Math.imul(h2 + ch + i, 2246822519) >>> 0
  }
  let out = '01MOCK'
  let state = h1
  while (out.length < 26) {
    out += CROCKFORD[state % 32]
    state = Math.imul(state ^ h2, 2654435761) >>> 0
    h1 = Math.imul(h1 ^ state, 1597334677) >>> 0
    state = (state + h1) >>> 0
  }
  return out.slice(0, 26)
}

/** Mock-only `sha256:<64 hex>` stand-in; the UI never recomputes a real digest. */
export function mockHash(seed: string): string {
  let out = ''
  let state = 0x9e3779b9
  for (let i = 0; i < 64; i += 1) {
    const ch = seed.charCodeAt(i % Math.max(seed.length, 1)) + i * 31
    state = Math.imul(state ^ ch, 2654435761) >>> 0
    out += (state & 0xf).toString(16)
  }
  return `sha256:${out}`
}

function iso(day: number, hour: number, minute: number): string {
  const stamp = Date.UTC(2026, 9, day, hour, minute, 0)
  return new Date(stamp).toISOString().replace('.000Z', 'Z')
}

function section(
  id: string,
  title: string,
  order: number,
  text: string,
  version: number,
  status: ArticleSection['status'],
  citations: ArticleSection['citations'] = [],
  openIssues: string[] = [],
): ArticleSection {
  return {
    schema_version: 1,
    id,
    title,
    order,
    status,
    words: countWords(text),
    version,
    hash: mockHash(text),
    citations,
    open_issues: openIssues,
    text,
  }
}

// --------------------------------------------------------------------------- article text
const ABSTRACT =
  "Peer review is the mechanism by which scholarly venues decide what counts as knowledge, yet reviewers receive little more than a form and a deadline. This article reports a design-science study of structured review checklists in a computer-science department. Over one semester we observed 41 reviews written with a checklist derived from the venue's own criteria and compared them with 38 reviews written on the department's free-form template. Checklist reviews named more specific evidence and addressed the paper's method in greater depth, without increasing review time per paper. We argue that the gain comes from the checklist's demand for evidence rather than from its length, and we publish the instrument so that other departments can repeat the study."

const INTRODUCTION =
  'Peer review is the load-bearing wall of scholarly publishing: everything else in the research record is trusted because reviewers were asked to look at it first (Braun & Clarke, 2006). The instrument reviewers are given, however, has changed remarkably little. Most venues still hand over a text box and a score, which makes the quality of the resulting report depend on the individual reviewer rather than on the venue. This article asks a narrow, practical question: does a structured checklist change what a reviewer writes, and if so, how?'

const RELATED_WORK =
  'Checklists have a long history in safety-critical work, where they were adopted after it became clear that experts forget routine steps under load (Gopen & Swan, 1990). Their migration into peer review has been slower and more contested. Reviewers report that forms fragment a judgement that ought to be holistic, while editors report that free-form reports are hard to compare across papers. Studies of review quality have mostly measured satisfaction rather than the evidence a report cites, which is the gap this study addresses.'

const METHODOLOGY =
  'The study followed a design-science cycle of three semesters. In the first semester we collected the department review form and the venue published criteria, and derived a twelve-item checklist with one item per criterion. In the second semester the checklist was used for every review in two courses; reviewers could ignore items but had to state why. In the third semester we coded 41 checklist reviews and 38 free-form reviews with the same scheme, by two coders blind to condition.'

const RESULTS =
  'Checklist reviews contained more evidence sentences per report (median 6 versus 3) and more methodological comments (median 4 versus 2). Review time per paper was unchanged within noise, which contradicts the common prediction that a form costs reviewers time. Authors rated checklist reports as more actionable on a five-point scale, and the effect held when we controlled for reviewer seniority.'

const DISCUSSION =
  'The pattern in the data is consistent with the checklist doing what instruments are supposed to do: it makes an expectation explicit, so a reviewer who would otherwise write a general impression is prompted for a specific one. It does not turn a weak reviewer into a strong one, and it does not replace editorial judgement. The gain in our data came from the demand for evidence, not from the number of boxes.'

const LIMITATIONS =
  'The study is single-department and observational to the extent that reviewers chose whether to engage with the checklist. Coding was blind, but the coders were graduate students in the same department, and the sample is small enough that the medians describe this population rather than a universal effect.'

const CONCLUSION =
  "A short checklist derived from a venue's own criteria changed what reviewers wrote without costing them time. The instrument and the coding scheme are published with this article so that the study can be repeated, and we suggest that venues evaluate their review forms the way they evaluate their submissions: with evidence rather than with preference."

const REFERENCES = [
  'Braun, V., & Clarke, V. (2006). Using thematic analysis in psychology. Qualitative Research in Psychology, 3(2), 77-101.',
  'Gopen, G. D., & Swan, J. A. (1990). The science of scientific writing. American Scientist, 78(6), 550-558.',
  'Kitchenham, B., & Charters, S. (2007). Guidelines for performing systematic literature reviews in software engineering (EBSE Technical Report EBSE-2007-01). Keele University.',
].join('\n\n')

const FA_ABSTRACT =
  'این مقاله یک مطالعه طراحی-محور دربارهٔ چک‌لیست‌های ساختاریافته داوری همتا در یک دانشکدهٔ علوم رایانه گزارش می‌کند. در یک نیم‌سال، ۴۱ داوری با چک‌لیست و ۳۸ داوری با فرم آزاد قبلی مقایسه شد. داوری‌های چک‌لیستی شواهد مشخص‌تری نام بردند و بدون افزایش زمان داوری، به روش مقاله عمیق‌تر پرداختند.'

const FA_INTRODUCTION =
  'داوری همتا دیوار باربر نشر علمی است: بقیهٔ سابقهٔ پژوهشی معتبر شمرده می‌شود چون نخست داوری به آن نگاه کرده است. اما ابزاری که در اختیار داور قرار می‌گیرد کم‌تر تغییر کرده است. بیشتر نشریات هنوز یک جعبهٔ متن و یک نمره می‌دهند، و همین کیفیت گزارش را به عادت فردی داور وابسته می‌کند. پرسش این مقاله محدود و کاربردی است: آیا چک‌لیست ساختاریافته نوشتهٔ داور را تغییر می‌دهد؟'

const FA_METHODOLOGY =
  'مطالعه در سه نیم‌سال و در قالب چرخهٔ طراحی-علم اجرا شد. در نیم‌سال نخست فرم داوری دانشکده و معیارهای منتشرشدهٔ نشریه گردآوری و یک چک‌لیست دوازده‌بندی ساخته شد. در نیم‌سال دوم چک‌لیست در دو درس به کار رفت. در نیم‌سال سوم ۴۱ داوری چک‌لیستی و ۳۸ داوری آزاد با همان طرح کدگذاری شد.'

const FA_CONCLUSION =
  'یک چک‌لیست کوتاه که از معیارهای خود نشریه گرفته شده باشد، نوشتهٔ داوران را تغییر می‌دهد بدون آنکه زمان آن‌ها را بگیرد. ابزار و طرح کدگذاری همراه این مقاله منتشر می‌شود تا مطالعه تکرارپذیر بماند.'

// --------------------------------------------------------------------------- briefs
export const ENGLISH_BRIEF: Brief = {
  schema_version: 1,
  topic: 'Tightening peer review with structured checklists',
  article_type: 'research',
  audience: 'CS faculty and graduate reviewers',
  language: 'en',
  target_words: 850,
  citation_style: 'APA7',
  must_include: ['a comparison of checklist and free-form reviews'],
  must_avoid: ['claims about journal acceptance rates'],
  provided_data: [],
}

export const PERSIAN_BRIEF: Brief = {
  schema_version: 1,
  topic: 'سخت‌گیرانه‌تر کردن داوری همتا با چک‌لیست‌های ساختاریافته',
  article_type: 'research',
  audience: 'اعضای هیئت علمی و داوران تحصیلات تکمیلی',
  language: 'fa',
  target_words: 700,
  citation_style: 'APA7',
  must_include: [],
  must_avoid: [],
  provided_data: [],
}

// --------------------------------------------------------------------------- plan
function task(
  seed: string,
  title: string,
  role: Task['role'],
  sectionIds: string[],
  tokenBudget: number,
  dependsOn: string[],
  instructions: string,
  acceptance: string[],
): Task {
  return {
    schema_version: 1,
    id: mockId(seed),
    title,
    role,
    section_ids: sectionIds,
    source_ids: [],
    instructions,
    acceptance,
    token_budget: tokenBudget,
    depends_on: dependsOn,
  }
}

export function buildPlan(language: 'en' | 'fa'): Plan {
  const t1 = task(
    `architect-${language}`,
    'Draft the section outline and the contribution statement',
    'architect',
    ['introduction', 'related-work'],
    6000,
    [],
    'Turn the brief into sections in rubric order. Name the gap in one sentence and the contribution in one sentence.',
    ['Every required section has a one-line purpose'],
  )
  const t2 = task(
    `researcher-${language}`,
    'Collect source cards for the claims in the outline',
    'researcher',
    ['related-work'],
    8000,
    [t1.id],
    'One card per source: title, authors, year, venue, verbatim snippet and a verification status. Never invent a locator.',
    ['Every card carries a verbatim snippet'],
  )
  const t3 = task(
    `writer-a-${language}`,
    'Write the introduction and related work',
    'writer',
    ['introduction', 'related-work'],
    12000,
    [t2.id],
    'Use only collected cards for citations; APA7 in-text markers.',
    ['No citation without a card', 'No placeholder text'],
  )
  const t4 = task(
    `writer-b-${language}`,
    'Write the methodology, results and discussion',
    'writer',
    ['methodology', 'results', 'discussion'],
    16000,
    [t3.id],
    'Report medians, state the sample, and keep the discussion inside the data.',
    ['Every result names its measure', 'No claim beyond the sample'],
  )
  const t5 = task(
    `reviewer-${language}`,
    'Review every section against its acceptance criteria',
    'reviewer',
    ['introduction', 'results', 'discussion'],
    9000,
    [t4.id],
    'Open one issue per unsupported claim; do not rewrite the prose.',
    ['Every high-severity issue names a section and a fix'],
  )
  const t6 = task(
    `validator-${language}`,
    'Validate the assembled article against the eight gates',
    'validator',
    ['references'],
    5000,
    [t5.id],
    'Run the gates, then score the criteria and compute Q.',
    ['Every gate has a boolean and a reason when false'],
  )
  return {
    schema_version: 1,
    version: 1,
    goal:
      language === 'fa'
        ? 'یک مقالهٔ پژوهشی دربارهٔ چک‌لیست‌های داوری که شکاف پژوهش را بیان کند و محدودیت‌ها را صادقانه بگوید.'
        : 'A research article on structured review checklists that states a gap, reports an observed study, and states its limitations honestly.',
    tasks: [t1, t2, t3, t4, t5, t6],
  }
}

// --------------------------------------------------------------------------- article
export function buildEnglishArticle(runId: string): ArticleView {
  const sections: ArticleSection[] = [
    section('abstract', 'Abstract', 0, ABSTRACT, 3, 'final'),
    section(
      'introduction',
      'Introduction',
      1,
      INTRODUCTION,
      2,
      'final',
      [{ key: '(Braun & Clarke, 2006)', source_id: mockId('src-braun-2006') }],
    ),
    section(
      'related-work',
      'Related Work',
      2,
      RELATED_WORK,
      3,
      'final',
      [{ key: '(Gopen & Swan, 1990)', source_id: null }],
      ['citation-unverified:src-gopen-1990'],
    ),
    section('methodology', 'Methodology', 3, METHODOLOGY, 4, 'final'),
    section('results', 'Results', 4, RESULTS, 3, 'final'),
    section('discussion', 'Discussion', 5, DISCUSSION, 2, 'final'),
    section('limitations', 'Limitations', 6, LIMITATIONS, 1, 'final'),
    section('conclusion', 'Conclusion', 7, CONCLUSION, 2, 'final'),
    section(
      'references',
      'References',
      8,
      REFERENCES,
      2,
      'reviewed',
      [
        { key: '(Braun & Clarke, 2006)', source_id: mockId('src-braun-2006') },
        { key: '(Kitchenham & Charters, 2007)', source_id: mockId('src-kitchenham-2007') },
      ],
      ['citation-unverified:src-gopen-1990'],
    ),
  ]
  const markdown = sections
    .map((s) => `## ${s.title}\n\n${s.text}`)
    .join('\n\n')
  const bodyWords = sections
    .filter((s) => s.id !== 'abstract' && s.id !== 'references')
    .reduce((sum, s) => sum + s.words, 0)
  return {
    run_id: runId,
    markdown,
    body_words: bodyWords,
    words_by_section: Object.fromEntries(sections.map((s) => [s.id, s.words])),
    sections,
    sources: [
      {
        schema_version: 1,
        id: mockId('src-braun-2006'),
        title: 'Using thematic analysis in psychology',
        authors: ['Braun, V.', 'Clarke, V.'],
        year: 2006,
        venue: 'Qualitative Research in Psychology',
        doi: '10.1191/1478088706qp063oa',
        url: 'https://doi.org/10.1191/1478088706qp063oa',
        snippet:
          'Thematic analysis is a method for identifying, analysing and reporting patterns within data.',
        verification: 'verified',
      },
      {
        schema_version: 1,
        id: mockId('src-kitchenham-2007'),
        title: 'Guidelines for performing systematic literature reviews in software engineering',
        authors: ['Kitchenham, B.', 'Charters, S.'],
        year: 2007,
        venue: 'EBSE Technical Report EBSE-2007-01, Keele University',
        doi: null,
        url: 'https://www.elsevier.com/__data/promis_misc/525444systematicreviewsguide.pdf',
        snippet:
          'A systematic literature review is a means of evaluating and interpreting all available research relevant to a research question.',
        verification: 'verified',
      },
      {
        schema_version: 1,
        id: mockId('src-gopen-1990'),
        title: 'The science of scientific writing',
        authors: ['Gopen, G. D.', 'Swan, J. A.'],
        year: 1990,
        venue: 'American Scientist',
        doi: null,
        url: null,
        snippet:
          'Readers expect the stress position of a sentence to carry the new information the writer wants them to remember.',
        verification: 'unknown',
      },
    ],
  }
}

export function buildPersianArticle(runId: string): ArticleView {
  const sections: ArticleSection[] = [
    section('abstract', 'چکیده', 0, FA_ABSTRACT, 2, 'final'),
    section('introduction', 'مقدمه', 1, FA_INTRODUCTION, 2, 'final'),
    section('methodology', 'روش', 2, FA_METHODOLOGY, 3, 'reviewed'),
    section('conclusion', 'نتیجه‌گیری', 3, FA_CONCLUSION, 2, 'final'),
  ]
  const markdown = sections.map((s) => `## ${s.title}\n\n${s.text}`).join('\n\n')
  return {
    run_id: runId,
    markdown,
    body_words: sections
      .filter((s) => s.id !== 'abstract')
      .reduce((sum, s) => sum + s.words, 0),
    words_by_section: Object.fromEntries(sections.map((s) => [s.id, s.words])),
    sections,
    sources: [],
  }
}

// --------------------------------------------------------------------------- history & diff
export function buildHistory(article: ArticleView): Record<string, VersionInfo[]> {
  const history: Record<string, VersionInfo[]> = {}
  for (const s of article.sections) {
    const rows: VersionInfo[] = []
    for (let version = 1; version <= s.version; version += 1) {
      const share = Math.max(1, Math.round(s.words * (version / s.version)))
      rows.push({
        section_id: s.id,
        version,
        hash: mockHash(`${s.id}:${version}`),
        words: share,
        status: version === s.version ? s.status : 'draft',
        chars_added: version === 1 ? share * 6 : Math.round(share * 1.4),
        chars_removed: version === 1 ? 0 : Math.round(share * 0.3),
        patch_id: version === 1 ? null : mockId(`patch-${s.id}-${version}`),
        entry_id: mockId(`entry-${s.id}-${version}`),
        created_at: iso(26, 20 + (version % 4), (version * 7) % 60),
        q_after: version === s.version ? 78 : null,
      })
    }
    history[s.id] = rows
  }
  return history
}

export function buildDiff(article: ArticleView, sectionId: string): SectionDiffView {
  const target = article.sections.find((s) => s.id === sectionId) ?? article.sections[0]
  const current = target ? target.version : 1
  const from = Math.max(1, current - 1)
  const lines = (target?.text ?? '').split(/(?<=\.)\s+/)
  const changed = lines.slice(0, 1).join(' ')
  return {
    run_id: article.run_id,
    section_id: target?.id ?? sectionId,
    from: {
      version: from,
      hash: mockHash(`${sectionId}:${from}`),
      words: Math.max(1, Math.round((target?.words ?? 1) * 0.6)),
    },
    to: {
      version: current,
      hash: mockHash(`${sectionId}:${current}`),
      words: target?.words ?? 1,
    },
    diff: {
      stats: {
        chars_added: changed.length,
        chars_removed: Math.round(changed.length * 0.2),
        net_chars: Math.round(changed.length * 0.8),
        touched_chars: Math.round(changed.length * 1.2),
      },
      lines_added: 1,
      lines_removed: 1,
      unified: `--- ${sectionId}@${from}\n+++ ${sectionId}@${current}\n@@ -1,1 +1,1 @@\n-${changed.slice(0, 120)}\n+${(target?.text ?? '').slice(0, 160)}\n`,
    },
  }
}

export function buildSnapshots(runId: string, article: ArticleView): SnapshotList {
  const sectionIds = article.sections.map((s) => s.id)
  const make = (label: string, q: number, iter: number, created: string) => ({
    id: mockId(`snap:${runId}:${label}`),
    label,
    q,
    iter,
    created_at: created,
    section_ids: sectionIds,
    total_words: article.body_words,
  })
  const snapshots = [
    make('iter-3-q78', 78, 3, iso(27, 9, 41)),
    make('iter-2-q71', 71, 2, iso(27, 8, 58)),
    make('iter-1-q63', 63, 1, iso(26, 22, 40)),
    make('iter-0-q54', 54, 0, iso(26, 20, 52)),
  ]
  return { run_id: runId, best_snapshot_id: snapshots[0]?.id ?? null, snapshots }
}

// --------------------------------------------------------------------------- quality
function qualityReport(iter: number, q: number, gatesPassed: boolean, ratio: number) {
  const gates = Object.fromEntries(
    GATE_NAMES.map((name, index) => {
      if (gatesPassed) return [name, true]
      const failing = new Set([3, 4, 5].slice(0, Math.max(1, 3 - Math.floor(iter))))
      return [name, !failing.has(index)]
    }),
  ) as Record<string, boolean>
  return {
    schema_version: 1 as const,
    iter,
    gates,
    scores: {
      contribution_and_gap: 85,
      coherence: 80,
      claims_supported: Math.max(55, q - 6),
      methods_clarity: Math.max(55, q - 3),
      results_match_claims: Math.max(55, q - 3),
      literature_coverage: Math.max(50, q - 8),
      language: 88,
      consistency: 82,
      limitations_and_ethics: Math.max(50, q - 8),
    },
    Q: q,
    verified_citation_ratio: ratio,
    issues:
      iter >= 2
        ? [
            {
              id: 'citation-unverified:src-gopen-1990',
              section_id: 'references',
              severity: 'low' as const,
              text: 'One reference (Gopen & Swan 1990) is cited without a verifiable locator; it stays flagged as an open issue instead of being silently dropped.',
            },
          ]
        : [],
  }
}

export function buildQuality(series: number[], runId: string): QualityView {
  const points: QualityPoint[] = series.map((q, iter) => ({
    iter,
    q,
    gates_passed: q >= 78,
    verified_citation_ratio: Math.min(0.75, 0.33 + iter * 0.14),
  }))
  const last = series[series.length - 1] ?? 0
  return {
    run_id: runId,
    latest: qualityReport(series.length - 1, last, last >= 78, points[points.length - 1]?.verified_citation_ratio ?? 0.75),
    series: points,
    best_q: Math.max(...series),
  }
}

// --------------------------------------------------------------------------- ledger
export function buildLedger(runId: string, article: ArticleView): LedgerView {
  const entry = (
    seed: string,
    iter: number,
    taskSeed: string,
    role: ChangeEntry['role'],
    targets: string[],
    summary: string,
    patchRef: string | null,
    tokens: { in: number; out: number; cached: number },
    opened: string[] = [],
  ): ChangeEntry => ({
    schema_version: 1,
    id: mockId(seed),
    iter,
    task_id: mockId(taskSeed),
    role,
    targets,
    summary,
    patch_ref: patchRef,
    tokens,
    issues_opened: opened,
    issues_closed: [],
    status: 'ok',
  })

  return {
    run_id: runId,
    entries: [
      entry(
        'e1',
        0,
        'architect-en',
        'architect',
        ['introduction', 'related-work'],
        'Outlined nine sections in rubric order and named the gap and the contribution in one sentence each.',
        `patch:${mockId('p1')}`,
        { in: 4200, out: 1800, cached: 600 },
      ),
      entry(
        'e2',
        0,
        'researcher-en',
        'researcher',
        ['related-work'],
        'Collected three source cards with verbatim snippets: two verified, one left unknown rather than guessed.',
        null,
        { in: 6100, out: 2400, cached: 900 },
      ),
      entry(
        'e3',
        0,
        'writer-a-en',
        'writer',
        ['introduction', 'related-work'],
        'Wrote the introduction and related work against the cards, APA7 in-text markers only.',
        `patch:${mockId('p3')}`,
        { in: 7000, out: 2600, cached: 1200 },
      ),
      entry(
        'e4',
        1,
        'writer-b-en',
        'writer',
        ['methodology', 'results', 'discussion'],
        'Added methodology, results and discussion; the body reached 657 words with no placeholder text.',
        `patch:${mockId('p4')}`,
        { in: 9800, out: 3600, cached: 2100 },
      ),
      entry(
        'e5',
        2,
        'reviewer-en',
        'reviewer',
        ['introduction', 'related-work', 'methodology', 'results'],
        'Reviewed every section against its acceptance criteria; one citation could not be resolved to a card.',
        null,
        { in: 5400, out: 1900, cached: 400 },
        ['citation-unverified:src-gopen-1990'],
      ),
      entry(
        'e6',
        2,
        'validator-en',
        'validator',
        [],
        'Validated the assembled article: seven of eight gates pass, the unverified reference is now an open issue.',
        null,
        { in: 3100, out: 1200, cached: 0 },
      ),
      entry(
        'e7',
        3,
        'writer-b-en',
        'writer',
        ['discussion', 'limitations', 'conclusion', 'references'],
        `Expanded discussion and limitations and documented the open citation; body now ${article.body_words} words.`,
        `patch:${mockId('p7')}`,
        { in: 8500, out: 3100, cached: 400 },
      ),
      entry(
        'e8',
        3,
        'validator-en',
        'validator',
        [],
        'Re-validated the article: all eight gates pass at Q=78 and the citation ratio is 0.75.',
        null,
        { in: 2900, out: 900, cached: 0 },
      ),
      entry(
        'e9',
        3,
        'orchestrator',
        'orchestrator',
        [],
        'Froze snapshot iter-3-q78 as the best measured version and paused at the completion checkpoint.',
        null,
        { in: 0, out: 0, cached: 0 },
      ),
    ],
  }
}

// --------------------------------------------------------------------------- usage
export function buildUsage(runId: string, tokensSpent: number, iterations: number): RunUsageView {
  const total = tokensSpent
  const plannerTotal = Math.round(total * 0.09)
  const agents = total - plannerTotal
  const breakdown = (value: number) => {
    const inTokens = Math.round(value * 0.62)
    const outTokens = Math.round(value * 0.26)
    const cached = Math.max(0, value - inTokens - outTokens)
    return { in: inTokens, out: outTokens, cached, total: value }
  }
  const shares: Record<string, number> = {
    architect: 0.1,
    researcher: 0.14,
    reviewer: 0.12,
    validator: 0.07,
    writer: 0.57,
  }
  const perIteration = Math.max(1, iterations)
  return {
    run_id: runId,
    total,
    agents_total: breakdown(agents),
    planner: breakdown(plannerTotal),
    by_role: Object.fromEntries(
      Object.entries(shares).map(([role, share]) => [role, breakdown(Math.round(agents * share))]),
    ),
    by_iter: Object.fromEntries(
      Array.from({ length: perIteration }, (_, iter) => [String(iter), breakdown(Math.round(agents / perIteration))]),
    ),
    token_cap: 920_000,
    iterations: perIteration,
    max_iterations: 14,
  }
}

// --------------------------------------------------------------------------- events
function event(
  seq: number,
  ts: string,
  type: string,
  state: RunEvent['state'],
  iter: number,
  detail: Record<string, unknown>,
): RunEvent {
  return { seq, ts, type, state, iter, detail }
}

export function buildEvents(
  runId: string,
  plan: Plan,
  qSeries: number[],
  finalState: RunEvent['state'],
): RunEvent[] {
  const events: RunEvent[] = [
    event(0, iso(26, 20, 14), 'run_created', 'INTAKE', 0, { kind: 'brief', project_id: runId }),
    event(1, iso(26, 20, 15), 'state_changed', 'PLANNING', 0, { from: 'INTAKE', to: 'PLANNING' }),
    event(2, iso(26, 20, 16), 'plan_accepted', 'PLANNING', 0, {
      plan_version: plan.version,
      tasks: plan.tasks.length,
    }),
    event(3, iso(26, 20, 18), 'state_changed', 'EXECUTING', 0, { from: 'PLANNING', to: 'EXECUTING' }),
  ]
  let seq = 4
  plan.tasks.forEach((task, index) => {
    const iter = Math.min(qSeries.length - 1, Math.floor(index / 2))
    events.push(
      event(seq++, iso(26, 20, 20 + index), 'task_completed', 'EXECUTING', iter, {
        task_id: task.id,
        role: task.role,
        summary: `${task.title} (${task.role})`,
      }),
    )
  })
  qSeries.forEach((q, iter) => {
    events.push(
      event(seq++, iso(27, 8 + iter, 12), 'iteration_started', 'EXECUTING', iter, { iteration: iter }),
    )
    events.push(
      event(seq++, iso(27, 8 + iter, 30), 'planner_evaluated', 'VALIDATING', iter, { Q: q }),
    )
    events.push(
      event(seq++, iso(27, 8 + iter, 41), 'loop_decision', 'DECIDING', iter, {
        action: iter === qSeries.length - 1 ? 'OFFER_FINISH' : 'CONTINUE',
        Q: q,
        gates_failed: q >= 78 ? 0 : Math.max(0, 3 - iter),
      }),
    )
  })
  events.push(
    event(seq++, iso(27, 9, 41), finalState === 'CHECKPOINT' ? 'checkpoint_reached' : 'run_stopped', finalState, qSeries.length - 1, {
      action: finalState === 'CHECKPOINT' ? 'OFFER_FINISH' : 'STOP_EROSION',
      best_q: Math.max(...qSeries),
    }),
  )
  return events.map((ev) => ({ ...ev, detail: { ...ev.detail, run_id: runId } }))
}

// --------------------------------------------------------------------------- run
export function buildRunView(
  runId: string,
  projectId: string,
  ownerId: string,
  brief: Brief,
  plan: Plan,
  qSeries: number[],
  options: { state: RunView['state']; checkpointKind?: RunView['checkpoint_kind'] },
): RunView {
  const best = Math.max(...qSeries)
  const createdAt = iso(26, 20, 14)
  const updatedAt = iso(27, 9, 41)
  const stopAction = options.state === 'CHECKPOINT' ? 'OFFER_FINISH' : 'STOP_EROSION'
  return {
    id: runId,
    project_id: projectId,
    owner_id: ownerId,
    kind: 'brief',
    state: options.state,
    iter: qSeries.length - 1,
    brief,
    plan,
    goal_override: null,
    decisions: qSeries.map((q, iter) => ({
      schema_version: 1,
      iter,
      action: iter === qSeries.length - 1 ? (options.state === 'CHECKPOINT' ? 'OFFER_FINISH' : 'STOP_EROSION') : 'CONTINUE',
      reasons:
        iter === qSeries.length - 1
          ? options.state === 'CHECKPOINT'
            ? ['completion_candidate', 'all_gates_pass']
            : ['plateau_detected', 'diminishing_returns']
          : ['q_below_min'],
      evidence: { Q: q, q_min: 70, gates_failed: q >= 78 ? 0 : Math.max(0, 3 - iter) },
    })),
    q_series: qSeries,
    best_q: best,
    best_snapshot_id: mockId(`snap:${runId}:${options.state === 'CHECKPOINT' ? 'iter-3-q78' : 'iter-1-q74'}`),
    stop_report: {
      schema_version: 1,
      run_id: runId,
      action: stopAction,
      reasons:
        options.state === 'CHECKPOINT'
          ? ['completion_candidate', 'all_gates_pass']
          : ['plateau_detected', 'diminishing_returns'],
      evidence:
        options.state === 'CHECKPOINT'
          ? { Q: best, q_min: 70, gates_failed: 0, deltas: [9, 8, 7] }
          : { Q: qSeries[qSeries.length - 1] ?? 0, q_min: 70, best_q: best, plateau: { window: 3, min_gain: 1.0, observed_gain: 0 } },
      q_series: qSeries,
      current_q: qSeries[qSeries.length - 1] ?? null,
      best_q: best,
      best_iter: qSeries.indexOf(best),
      best_snapshot_id: mockId(`snap:${runId}:${options.state === 'CHECKPOINT' ? 'iter-3-q78' : 'iter-1-q74'}`),
      iterations: qSeries.length,
      tokens_spent: options.state === 'CHECKPOINT' ? 59910 : 48230,
      verdict: {
        schema_version: 1,
        meets_minimum: options.state === 'CHECKPOINT',
        missing: [],
        risks:
          options.state === 'CHECKPOINT'
            ? ['One reference is unverified and flagged as an open issue.', 'The study is single-department; the discussion says so.']
            : ['Q has not improved for three iterations.'],
        recommendation: options.state === 'CHECKPOINT' ? 'finish' : 'continue',
      },
      options:
        options.state === 'CHECKPOINT'
          ? options.checkpointKind === 'needs_input'
            ? ['continue', 'rollback']
            : ['finish', 'continue', 'rollback']
          : ['rollback', 'edit_goals', 'add_budget', 'continue_anyway'],
      created_at: updatedAt,
    },
    verdict: {
      schema_version: 1,
      meets_minimum: options.state === 'CHECKPOINT',
      missing: [],
      risks:
        options.state === 'CHECKPOINT'
          ? ['One reference is unverified and flagged as an open issue.']
          : ['Q has not improved for three iterations.'],
      recommendation: options.state === 'CHECKPOINT' ? 'finish' : 'continue',
    },
    checkpoint_kind: options.checkpointKind ?? null,
    on_complete: 'ask',
    user_stop_requested: false,
    running: false,
    tokens_spent: options.state === 'CHECKPOINT' ? 59910 : 48230,
    last_error: null,
    created_at: createdAt,
    updated_at: updatedAt,
  }
}

export interface RunBundle {
  run: RunView
  article: ArticleView
  quality: QualityView
  ledger: LedgerView
  usage: RunUsageView
  snapshots: SnapshotList
  history: Record<string, VersionInfo[]>
  events: RunEvent[]
}

export function buildBundle(
  runId: string,
  projectId: string,
  ownerId: string,
  brief: Brief,
  language: 'en' | 'fa',
  qSeries: number[],
  state: RunView['state'],
  checkpointKind?: RunView['checkpoint_kind'],
): RunBundle {
  const plan = buildPlan(language)
  const article = language === 'fa' ? buildPersianArticle(runId) : buildEnglishArticle(runId)
  const run = buildRunView(runId, projectId, ownerId, brief, plan, qSeries, {
    state,
    ...(checkpointKind ? { checkpointKind } : {}),
  })
  return {
    run,
    article,
    quality: buildQuality(qSeries, runId),
    ledger: buildLedger(runId, article),
    usage: buildUsage(runId, run.tokens_spent, qSeries.length),
    snapshots: buildSnapshots(runId, article),
    history: buildHistory(article),
    events: buildEvents(runId, plan, qSeries, state),
  }
}

/** Progress fraction across the loop's plan, used by the plan board. */
export function completedTaskIds(events: RunEvent[]): Set<string> {
  const done = new Set<string>()
  for (const ev of events) {
    if (ev.type === 'task_completed' && typeof ev.detail.task_id === 'string') {
      done.add(ev.detail.task_id)
    }
  }
  return done
}
