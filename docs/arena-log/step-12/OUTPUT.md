# Step 12 — Frontend 10b: raw output

Captured 2026-10-27 on Linux x86_64, Python 3.11.2, Node 22.22.3, npm 10.9.8, in this
branch (`arena/65578699-scholarloop-agentic-academic-a`, cut from `main` at
`63ada01`, which already contains Steps 1–11).

## 1. Files read before writing anything (unmodified)

Every file below was read in full; each confirmation is one line.

| Path | Confirmed read |
| --- | --- |
| `backend/app/contracts/__init__.py` | the package re-exports every contract; contracts sit at the bottom of the dependency graph and import nothing from `orchestrator`, `api` or `agents`. |
| `backend/app/contracts/common.py` | `Contract` is `extra="forbid"` + whitespace-stripping, `VersionedContract` pins `schema_version: Literal[1]`, `Ulid`/`NonEmptyStr`/`SectionId`/`word_bounded` are the shared annotated types. |
| `backend/app/contracts/account.py` | `Account` stores only `key_ciphertext`; `public()` returns `AccountPublic` with a 12-char fingerprint over the ciphertext — no plaintext field exists to leak. |
| `backend/app/contracts/agent.py` | `AgentResult` = entry + patches + source cards + section state + optional report; only the validator may return a report. |
| `backend/app/contracts/binding.py` | `AgentBinding` holds ordered account ids, a strategy, a model and per-call token caps — never accounts or keys. |
| `backend/app/contracts/brief.py` | `Brief` fields, `MIN/MAX_TARGET_WORDS` (300..30,000), the `must_include`/`must_avoid` non-overlap rule, the 40-word instruction cap, `word_tolerance_*` (±15%). |
| `backend/app/contracts/enums.py` | every closed vocabulary (`ArticleType`, `CitationStyle`, `AgentRole`, `ORCHESTRATOR_ROLE`, `SectionStatus`, `LoopAction`, `VerificationStatus`, …). |
| `backend/app/contracts/ledger.py` | `ChangeEntry` (60-word summary, `patch_ref` pointer, orchestrator role allowed, never a `patch_ref`), `TokenUsage` spelled `{in,out,cached}`. |
| `backend/app/contracts/llm.py` | `SystemBlock`/`LLMMessage`/`LLMRequest`/`LLMResponse`; last message must be the user's. |
| `backend/app/contracts/loop.py` | `LoopDecision` with snake_case reason codes and a free-form `evidence` object — the payload the Stop report renders. |
| `backend/app/contracts/patch.py` | the four op verbs and their per-op payload rules; whitespace is payload. |
| `backend/app/contracts/plan.py` | `Plan` = version + goal + tasks, acyclic by construction, with `of_role`/`role_distribution` — the shape the plan board groups. |
| `backend/app/contracts/quality.py` | `GATE_NAMES` (the eight hard gates), `QualityIssue`, `QualityReport` with every gate present and `Q` on 0..100. |
| `backend/app/contracts/registry.py` | `CONTRACTS` (23 names) + `json_schema_for`, which is what `docs/schemas/` and the OpenAPI `$ref`s are built from. |
| `backend/app/contracts/section.py` | `SectionState` (`order`, `version`, `hash`, `words`, `citations`, `status` lifecycle) and `CitationRef` with its unresolved-site case. |
| `backend/app/contracts/source.py` | `SourceCard` (60-word snippet, DOI normalisation, locator required, verification starts `unknown`). |
| `backend/app/contracts/task.py` | `Task` carries exactly one role, an acceptance list and a token budget. |
| `backend/app/contracts/verdict.py` | `Verdict` and its `meets_minimum` vs `recommendation` consistency rules. |
| `backend/app/core/errors.py` | `ScholarLoopError` carries `code` + `http_status`; `NeedsUserInput` is **409**, `ContractViolation` 422, `BudgetExceeded` 429. |
| `backend/app/providers/base.py` | `LLMProvider` Protocol, `estimate_tokens = ceil(chars/4)`, `prompt_tokens`. |
| `backend/app/providers/errors.py` | the `ProviderError` family with `retryable` as a class fact. |
| `backend/app/providers/mock.py` | `MockProvider`: scripted replies keyed by `(role, call index)`, injectable timeout/rate-limit/bad-JSON, deterministic usage, no network and no key field. |
| `config/policies.yaml` | budgets (`run_token_cap`, `iteration_token_cap`, `call_input_cap`, `max_iterations`, `max_parallel`), digest knobs, `q_min: 70`, the erosion detectors, `error_streak`, `model_tiers` — **no USD cap**, which the cost meter has to say out loud. |
| `backend/app/api/**` (extra) | `create_app` + lifespan, `deps` (`CurrentUserDep`, `SessionDep`, `get_pool`), `schemas`, the routers: health/config/auth/accounts/agents/**projects**/**runs**. |
| `backend/app/article/store.py` (extra) | versioned sections, `apply_patch`/`stale` rules, `history()` (metadata only), `diff()`, `snapshot()`, `best_snapshot()`, `restore()` appending new versions. |
| `backend/app/orchestrator/**` (extra) | `intake.py` (deterministic Brief validation, `needs_input`), `state_machine.py` (12 states, `CHECKPOINT` transitions, `RunEvent`), `stop.py` (`StopReport`, `options_for`, `UserDecision`), `errors.py` (`RunConflict`, `InvalidTransition`, `IntakeNeedsInput`), `persistence.py`, `runner.py`, `loop_controller.py`. |
| `docs/openapi/openapi.yaml` + `examples/**` (extra) | the 36-operation UI spec, every example (run checkpoint/erosion/needs-input, article, history, diff, snapshots, restore, quality, usage, errors) and its README, including the CONTRACT-CHANGE-REQUESTS list. |
| `frontend/src/**` (extra, the 10a shell) | `App.tsx`, `api.ts` (typed REST + `parseSseFrames`/`streamRunEvents`), `types.ts`, `mockServer.ts`, `auth.tsx`, `AccountsPage`, `AgentsPage`, `StatusConsole`, `styles.css`, `__tests__/app.test.tsx`, `vite.config.ts`. |
| `docs/arena-log/step-{02,06,07,08,10,11}/` (extra) | the house style for the log, the PR body and the honesty rules (a deviation is written down, never papered over). |
| `Makefile`, `pyproject.toml`, `.github/workflows/ci.yml`, `README.md` | the gates and where a new one belongs. |

**Contracts were not modified.** `git diff --stat` shows no file under
`backend/app/contracts/` and `make schemas-check` still passes.

## 2. What was built

New files (`frontend/`):

```
src/fixtures.ts            deterministic mock data: 2 projects (en + fa), 3 runs,
                           plan, article, history, diff, snapshots, quality, ledger,
                           usage, SSE log; source of every screen's demo content
src/format.ts              state labels/tones, token & USD formatting, RTL direction
src/costs.ts               documented USD estimate table + token-cap equivalent
src/useRunEvents.ts        SSE feed: resume from the last `seq`, de-dup, bounded reconnect
src/BriefWizard.tsx        the Brief form (inline validation, NEEDS_INPUT questions)
src/NewProjectPage.tsx     project creation + optional immediate run
src/ProjectsPage.tsx       project list (derived run facts)
src/ProjectPage.tsx        project brief, run history, start-a-run
src/RunPage.tsx            state badge, plan board, SSE change log, meters, Q chart,
                           checkpoint entry points, stop report, ledger
src/PlanBoard.tsx          tasks grouped by the role that owns them, derived progress
src/QualityChart.tsx       Q-per-iteration SVG chart + gate chips + table twin
src/UsageMeters.tsx        token / iteration / cost meters against the caps
src/CheckpointModal.tsx    Finish · Continue with extra budget · Edit goals · Roll back
src/StopReportView.tsx     reasons, Q series, snapshot, spend, risks, failed gates, evidence
src/ArticleViewer.tsx      sections, version history, diff, snapshot restore, RTL
src/exportMarkdown.ts      .md download built from /article (+ front matter)
src/test/{setup,helpers}.ts
src/__tests__/*.test.{ts,tsx}  eight new suites (see §3)
e2e/smoke.spec.ts          Playwright smoke: project -> run -> checkpoint -> article
playwright.config.ts       webServer = vite dev with VITE_API_MOCK=true
vitest.config.ts           jsdom + setup file + `src/**/*.test.{ts,tsx}`
```

Changed (one line each, all additive):

* `src/api.ts` — projects (CRUD, runs), runs (get/decision/stop/events history/digest/
  ledger/quality/usage), article (view/history/diff/snapshots/restore); the access token
  now lives in **`sessionStorage`** only, and any `localStorage` token from an older build
  is deleted on read.
* `src/types.ts` — the OpenAPI-generated schema is the source of the wire types
  (`components`, `operations`, `paths` re-exported; `Wire<T>` drops the JSON-Schema
  `$defs` bookkeeping), plus the Brief/SectionState/Plan helpers the UI needs
  (`countWords`, `wordToleranceRange`, `isRtlLanguage`, `validateBriefDraft`).
* `src/mockServer.ts` — the mock now serves projects, run lifecycle (decision/stop), the
  SSE log, quality, usage, ledger, article, section history/diff/snapshots/restore and
  the error catalog, and seeds one English and one Persian project so every screen has
  content on first sign-in.
* `src/App.tsx` — hash routing for `#projects`, `#projects/new`, `#projects/<id>`,
  `#runs/<id>`, `#runs/<id>/article`, with the existing `#accounts|#agents|#status` tabs
  kept; new **Projects** tab.
* `src/styles.css` — plan board, meters, chart, gate chips, modal, feeds, article/diff.
* `src/openapi.json` — the console's runtime subset gains the projects, run-history,
  quality, article-history/diff/snapshots/restore and error-catalog paths.
* `src/__tests__/app.test.tsx` — ported from `node:test` to Vitest and updated for the
  project-scoped run route (a run now belongs to a project; the mock seeds one).
* `frontend/package.json` — `test` = `vitest run`, `client` = regenerate the typed client,
  `prelint`/`pretest` regenerate it, `e2e` = Playwright; new devDeps: vitest, jsdom,
  @testing-library/{react,dom}, @playwright/test, openapi-typescript.
* `frontend/tsconfig.json` — includes `e2e`, `vitest.config.ts`, `playwright.config.ts`.
* `Makefile` — `frontend-test`, `frontend-check`, `frontend-e2e`.
* `.github/workflows/ci.yml` — the frontend job also runs `npm test`; a new `e2e` job
  installs Chromium and runs the smoke test.
* `.gitignore` — `frontend/src/generated/` (regenerated, never committed), Playwright
  reports.
* `README.md` — the console paragraph and the new make targets.

## 3. Commands and their output

```
$ make check
.venv/bin/ruff check backend          All checks passed!
.venv/bin/ruff format --check backend 204 files already formatted
.venv/bin/mypy                        Success: no issues found in 138 source files
skill-lint                            7 skill(s) OK in skills
.venv/bin/python -m pytest            1145 passed, 8 skipped, 1 warning in 32.91s
```

Full capture: [`make-check.txt`](make-check.txt). The backend is untouched, so the count
is the same as Step 11's baseline.

```
$ cd frontend && npm run lint && npm test && npm run build     # = make frontend-check
prelint  ✨ openapi-typescript 7.13.0  -> src/generated/api-schema.d.ts
lint     tsc --noEmit                  (clean)
test     vitest run
           ✓ src/__tests__/brief.test.ts            (10)
           ✓ src/__tests__/briefWizard.test.tsx      (6)
           ✓ src/__tests__/runPage.test.tsx          (6)
           ✓ src/__tests__/articleViewer.test.tsx (6)
           ✓ src/__tests__/projectFlow.test.tsx      (8)
           ✓ src/__tests__/appRoutes.test.tsx        (3)
           ✓ src/__tests__/routing.test.ts           (4)
           ✓ src/__tests__/exportMarkdown.test.ts    (3)
           ✓ src/__tests__/app.test.tsx              (6)
         Test Files  9 passed (9)
              Tests  52 passed (52)
build    vite build -> dist/index.html 0.64 kB, css 15.07 kB, js 357.53 kB
```

Full capture: [`frontend-check.txt`](frontend-check.txt).

```
$ .venv/bin/python -m app.scripts.secret_scan --root .
secret scan clean under .
```

**Dev-server smoke (real HTTP, mock middleware).** The Vite dev server was started with
`VITE_API_MOCK=true` and exercised with `curl`, i.e. the same path a browser takes:

```
GET  /                                   -> 200 (index.html, react-refresh injected)
POST /api/auth/register                  -> 201, bearer token
GET  /api/projects                       -> 200 [('Peer review checklists 2026', 2, 'CHECKPOINT'),
                                                    ('اخلاق هوش مصنوعی در آموزش', 1, 'CHECKPOINT')]
POST /api/projects                       -> 201 (project created)
POST /api/projects/{id}/runs             -> 202 (run created)
POST /api/projects (partial brief)       -> 409 {"code":"needs_user_input",
                                                 "context":{"missing_fields":
                                                 ["article_type","audience","language",
                                                  "target_words","citation_style"], ...}}
```

**Playwright: the spec exists and typechecks, but could not be executed here.** The
sandbox's egress allowlist does not include `cdn.playwright.dev`, which is where the
browser binaries come from:

```
$ npx playwright install chromium
Failed to install browsers
Error: Failed to download Chrome for Testing ... caused by Error: Download failure
  (ECONNRESET, host: 'cdn.playwright.dev', port: 443)
```

So the honest statement is: `e2e/smoke.spec.ts` is written, compiles under
`tsc --noEmit`, and CI runs it on `ubuntu-latest`
(`npx playwright install --with-deps chromium && npm run e2e`), but it has **not been
executed in this environment**. The browser-less twin of the same walk — project list →
project → run → article, and the checkpoint decision, and the RTL article — *is*
executed here, in `src/__tests__/appRoutes.test.tsx`, `runPage.test.tsx` and
`articleViewer.test.tsx`.

## 4. Decisions worth knowing

1. **Types come from the spec, not from a hand-written mirror.** `src/generated/api-schema.d.ts`
   is produced by `openapi-typescript` from `docs/openapi/openapi.yaml` in `prelint` and
   `pretest`, and is git-ignored, so a committed copy cannot go stale. `types.ts` aliases
   those schemas (`components['schemas']['RunView']`, …) and only hand-writes the two
   things the spec does not model as schemas: the Brief draft validator and the UI's
   composite view types (`ArticleView`, `LedgerView`, `QualityView`).
2. **No token in `localStorage`, ever.** The JWT moved to `sessionStorage` and an old
   `localStorage` entry is actively deleted on read. The Brief form keeps its fields in
   component state only; the API key fields from 10a are still write-only and cleared
   immediately after submit.
3. **The checkpoint's options are data.** The modal renders `run.stop_report.options`,
   not a hard-coded list, so a `needs_input` checkpoint offers Continue/Roll back and a
   `STOPPED_EROSION` offers Roll back/Edit goals/Continue anyway/Add budget — both pinned
   by tests.
4. **The plan board is derived from the event stream.** A task is "done" when an SSE
   `task_completed` carries its id (the same `completed_task_ids` semantics the
   orchestrator uses on the ledger), so the board cannot disagree with the run.
5. **Two sources of truth for the change log, on purpose.** The SSE feed is the live
   activity (`type`, `iter`, detail), the ledger endpoint is the audited record
   (role, summary, targets, tokens, status). The screen shows both; they answer different
   questions.
6. **The cost meter is labelled, not faked.** `RunUsageView` publishes `token_cap` and
   iteration limits but no USD figure, and `config/policies.yaml` has no USD cap. The
   meter multiplies the reported tokens by a documented per-1M-token table
   (`src/costs.ts`) and measures against the USD *equivalent* of the token cap, with that
   sentence on screen. A real `usd_spent`/`usd_cap` pair is a spec request (§6).
7. **RTL is content, not a flag.** The reading direction is derived from the Brief's
   BCP-47 language (`fa`, `ar`), set as `dir` + `lang` on the article element, and the
   mock ships a Persian project with real Persian sections so the layout is exercised by
   text rather than by a checkbox.
8. **Export is `.md` only, and says so.** DOCX/PDF need a converter the console does not
   ship; the button is present, disabled and labelled "later" instead of silently
   producing a lossy file. The markdown comes from `GET /article` (front matter + sections
   + references + source cards), so an export cannot disagree with the viewer.
9. **Every screen has a URL.** Hash routing (`#runs/<id>/article`) keeps back/forward and
   copy-paste working without a router dependency, and unknown hashes land on the project
   list instead of a blank page.
10. **Loading, empty and error are three distinct states everywhere.** Each panel renders
    its own skeleton text, its own "nothing here yet" sentence, and its own error with a
    Retry button; the run page additionally reports stream errors with a Reconnect.
11. **Keyboard access is part of the contract, not a polish pass.** The modal traps Tab,
    closes on Escape and focuses its first radio; the Brief form focuses the first invalid
    control on a failed submit and wires `aria-invalid`/`aria-describedby` per field; the
    chart has a `<table>` twin; every control is a real `<button>`/`<label>`.

## 5. CONTRACT-CHANGE-REQUESTS

**None.** No file under `backend/app/contracts/` was edited or needed to be: everything the
new screens render already exists in CONTRACTS v1 (`Brief`, `Task`, `Plan`, `SectionState`,
`ChangeEntry`, `SourceCard`, `QualityReport`, `Verdict`, `LoopDecision`, `TokenUsage`).
The gaps this step did find are in the *surface* spec (`docs/openapi/`) and in the policy
file, not in the contracts — listed in
[`spec-change-requests.md`](spec-change-requests.md).

## 6. Known gaps / follow-ups

* **Playwright was not executed here** (browser download blocked — see §3). CI runs it.
* `frontend/src/openapi.json` is a second, hand-maintained copy of the spec's path list
  (the Step 10a runtime subset). It now includes every operation the console calls, but
  it can drift from `docs/openapi/openapi.yaml`. Follow-up: derive it from the YAML at
  build time (or delete it and validate the mock's responses against the YAML with
  `ajv`, as `docs/openapi/scripts/verify.mjs` already does). Recorded as a spec request.
* The mock's SSE endpoint replays the log and closes, so the client reconnects (bounded,
  with de-duplication). The real backend tails live and closes when the run settles; the
  hook handles both, but only the reconnect path is exercised in tests.
* `GET /api/projects/{project_id}/runs`, `/quality`, `/events/history` and the article
  history/diff/snapshot routes are marked `planned` in `docs/openapi/` and served by the
  **mock** only. The console renders them exactly as specified; wiring them to the
  orchestrator is a backend step.
* `POST /api/projects` in the real backend is a Step 11 route with a different payload
  shape (`id` + `name` there, `name` + `brief` here per the brief). The console follows
  the *brief* (a project owns its Brief); see the spec request.
