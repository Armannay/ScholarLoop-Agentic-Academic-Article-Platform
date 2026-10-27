# Step 10b — Console: projects, run view, checkpoints and the article

**Step:** 10b — the "10b" half of the frontend work (New Project, Run view, Checkpoint
modal, Stop report, Article viewer, `.md` export), on top of the console shell.

**Depends on:** the **console shell** (PR #27: router, layout, auth session, shared
components, `useEventSource`, generated typed client — this branch is rebased onto it and
builds the feature pages *on* it), the **OpenAPI 3.1 spec** (PR #19), and the read models
the screens render: Step 9b (runs, `RunView`, state machine, `StopReport`, SSE log),
Step 11 (`/api/projects`), Step 5 (article store: history, diff, snapshots, restore) and
Step 8 (`Plan`, `QualityReport`).

**Contract changes:** **none.** No file under `backend/app/contracts/` is touched and
`make schemas-check` still passes; everything the new screens render already exists in
CONTRACTS v1. The gaps found are in the *surface* spec and in the policy file — written
up in `docs/arena-log/step-10b/spec-change-requests.md`, never edited into the spec.

## What a user can now do

1. **New project** — a Brief form whose inline validation mirrors deterministic intake:
   required fields, BCP-47 language, 300..30,000 target words with the ±15% tolerance
   shown live, contradictory `must_include`/`must_avoid` refused, each instruction capped
   at 40 words. A `409 needs_user_input` from the API becomes the same inline questions,
   and focus moves to the first field that needs attention.
2. **Run view** — state badge (12-state vocabulary), a plan board grouping the Planner's
   tasks by the role that owns them (done/ready/blocked derived from the event log), the
   live change log over SSE with de-duplication and backoff reconnect, token / iteration
   / cost meters against the caps, and a Q-per-iteration chart with one chip per hard gate
   (plus a `<table>` twin).
3. **Checkpoint modal** — Finish · Continue with extra budget · Edit goals · Roll back,
   rendered from `stop_report.options` so only legal decisions are offered. Tab is
   trapped, Escape closes, the first option is focused, and a `needs_input` checkpoint
   opens itself because the loop is blocked on the user.
4. **Stop report** — reasons, Q series, best snapshot, iterations, tokens and estimated
   cost, the Planner's named risks, the failed gates, and the raw `evidence` object.
5. **Article viewer** — sections with status/version/word count and open issues, a
   per-section version list, a unified diff, snapshot restore reported as *new versions*
   (never a rewrite), and `dir=rtl` + `lang` when the Brief language is `fa` or `ar`.
6. **Export `.md`** — front matter + sections + references + source cards, downloaded
   from `GET /article` so the file cannot disagree with the screen. DOCX/PDF are a
   visibly disabled "later" button, not a silently lossy converter.

Plus the project list and a project page that starts another run from the project's own
brief.

## Built on the merged shell (not beside it)

The shell landed while this step was in progress (`arena/b04b6275-…`, PR #27: router,
layout, auth session, shared components, `useEventSource`, typed client). This branch was
rebased onto that `main` and the screens were ported onto it, so there is exactly one
client, one router and one component set:

* `src/lib/api.ts` is the only place the generated JSON-Schema types are narrowed to the
  view types; every screen calls it.
* `src/lib/domain.ts` owns the view types plus the deterministic rules (Brief
  validation, word count, ±15%, RTL detection).
* `src/router.tsx` gained `:param` segments (`/projects/:projectId`, `/runs/:runId`,
  `/runs/:runId/article`) — additive; static routes and `navigate` are unchanged.
* `src/layout.tsx` gained the Projects tab; `src/app.tsx` the route table.
* The in-memory mock (`src/api/mockServer.ts` + `fixtures.ts`, with an English and a
  Persian seeded project) is the dev/test transport behind `VITE_API_MOCK=true`, so every
  screen also runs with no backend, no Prism and no keys.

## Rules from the brief, and where they are enforced

| Rule | Where |
| --- | --- |
| typed API client generated from OpenAPI | `src/api/openapi.gen.d.ts` (`npm run api:gen`), consumed by `src/api/client.ts`; nothing hand-writes a wire shape |
| never render or store secrets; no `localStorage` beyond the session | the shell keeps the JWT in module memory; no new persistence was added and no screen renders key material |
| loading, empty and error states | every panel: explicit loading copy, an empty sentence, and an error with Retry (the SSE panel adds Reconnect) |
| keyboard accessible | modal focus trap + Escape + first-radio focus, labelled controls, `aria-invalid`/`aria-describedby` per field, focus to the first invalid control, chart table twin |
| Vitest unit tests | 47 tests in 9 files (Brief validation, wizard, run view, checkpoint decision, stop report, article viewer, export, routing, app walk, SSE hook) |
| Playwright smoke test for the key flows | `playwright/step-10b.spec.ts` + a config-driven `webServer` on the mock; CI job added |

## Bug fixed in the shell

`src/api/useEventSource.ts` initialised `closedRef` to `true` and never cleared it on
connect, so `connect()` returned immediately and **no SSE frame was ever delivered** —
the shell's smoke test only asserted that the page rendered, which cannot see a stream
that never opens. `connect()` now clears the flag, and
`src/__tests__/useEventSource.test.tsx` is the regression guard (first frame, ordering,
no duplicates).

## Verification

```
make check              ruff + mypy --strict + skill lint + pytest  -> 1162 passed, 8 skipped
make frontend-check     tsc --noEmit + vitest run + vite build      -> 47 passed, built in 1.68s
secret scan             no secret-shaped string anywhere
dev-server smoke        projects (2 seeded) -> run state/options -> SSE frame -> article (9 sections)
```

**Not executed here:** `npm run e2e`. Downloading the Playwright browsers is blocked in
this sandbox (`ECONNRESET` to `cdn.playwright.dev`). The spec typechecks, the CI `e2e` job
runs it on `ubuntu-latest`, and the same walk is exercised browser-lessly in
`appRoutes.test.tsx`, `runPage.test.tsx`, `articleViewer.test.tsx` and
`useEventSource.test.tsx`.

## Files

New: `frontend/src/lib/{domain,api,useAsync,useRunFeed,format,costs,exportMarkdown}.ts`,
`frontend/src/api/{fixtures,mockServer}.ts`,
`frontend/src/components/{BriefForm,PlanBoard,QualityChart,UsageMeters,CheckpointModal,StopReport,StatusPill}.tsx`,
`frontend/src/pages/{projects,new-project,project,run,article}.tsx`,
`frontend/src/test/*`, `frontend/src/__tests__/*` (9 suites),
`frontend/{vitest.config.ts}`, `frontend/playwright/step-10b.spec.ts`,
`docs/arena-log/step-10b/`.

Touched (additive): `frontend/src/{app.tsx,router.tsx,layout.tsx,styles.css}`,
`frontend/src/pages/home.tsx`, `frontend/src/api/useEventSource.ts` (the connect fix),
`frontend/{vite.config.ts,playwright.config.ts,package.json,tsconfig.json,index.html,.env.example}`,
`Makefile`, `.github/workflows/ci.yml`, `.gitignore`, `README.md`.

Removed: the shell's `playwright/smoke.test.ts` (it spawned Vite and Prism by hand and
depended on `docs/openapi/node_modules`; the config-driven webServer replaces it) and the
10a-era screens the shell had already deleted.

## Known deltas / follow-ups

* Four routes the console renders are `planned` in the spec and served by the mock only:
  `GET /api/projects/{id}/runs`, `/runs/{id}/quality`, `/runs/{id}/events/history`, and the
  article history/diff/snapshots/restore quartet.
* `RunUsageView` has no USD field, so the cost meter is a labelled estimate against the
  USD equivalent of the token cap (request §6 in `spec-change-requests.md`).
* `POST /api/projects` still differs between the spec (`{id, name}`) and the code
  (`{name, brief, …}`, ULID id); the console follows the code (request §1).
