# Step 10b — Frontend: projects, runs, checkpoints, article

Saved verbatim as the record of what was asked. Two placeholders arrived empty (`<...>`
for the extra files, and the task body wrapped in `< >`); what was received is reproduced
exactly, and how each gap was resolved is recorded in `OUTPUT.md` §1.

---

## Context

```
CONTEXT: Repo https://github.com/Armannay/ScholarLoop-Agentic-Academic-Article-Platform. Create your branch from the latest main.
FIRST read these files (do not modify them) and confirm in one line each that you read them: everything under backend/app/contracts/, backend/app/core/errors.py, backend/app/providers/{base,errors,mock}.py, config/policies.yaml, plus the EXTRA FILES below. Contracts are frozen (tag <contracts-v1.1>). If you need a contract change, do NOT edit it: list it under CONTRACT-CHANGE-REQUESTS.
Quality gate: make check must pass. Open a PR to main when done, filling Step / Depends on / Contract changes. Save this prompt and your raw output in docs/arena-log/step-<NN>/.
```

## Task

```
< New Project (Brief form with inline validation and NEEDS_INPUT questions); Run view (state badge, plan board with tasks grouped by role, live change-log feed over SSE, token and cost meters against caps, Q-per-iteration chart with gate chips); Checkpoint modal (Finish, Continue with extra budget, Edit goals, Roll back) and Stop report view with evidence; Article viewer (sections, version history, diff, snapshot restore, RTL-capable when language is fa or ar); Export .md (DOCX and PDF later).
```

## Rules

```
RULES: typed API client generated from OpenAPI; never render or store secrets (no localStorage for keys or tokens beyond the session); loading, empty and error states; keyboard accessible; Vitest unit tests plus a Playwright smoke test for the key flows.0b (project, run and article pages), after the shell is merged. Use the “10b” part of the “Step 10” prompt with the same addition. Until the shell is merged, D should work only with local mock data and the Brief and SectionState types.
```

## Extra files

```
<...>
```

---

## How the prompt was read

* **"after the shell is merged."** The shell landed on `main` while this step was in
  progress (PR #27, `arena/b04b6275-…`: router, layout, auth session, shared components,
  `useEventSource`, the generated typed client, Prism dev setup). This branch was
  rebased onto that `main` and the feature pages were ported onto it: **one** client
  (`src/api/client.ts` over `src/api/openapi.gen.d.ts`), **one** router, **one** set of
  components. Nothing from the earlier parallel shell survives.
* **"D should work only with local mock data and the Brief and SectionState types"**
  applied only *until* the shell existed. The screens are built on the merged shell and
  on the generated types from the OpenAPI spec; the mock data (`src/api/fixtures.ts` +
  `src/api/mockServer.ts`) is kept as the dev/test transport so every screen also runs
  with no backend, no Prism and no keys.
* **"the '10b' part of the 'Step 10' prompt"**: the screens in the task body, in the
  order a user meets them.
* **"the same addition"**: the process constraints above (read-first confirmations,
  frozen contracts, `make check`, a PR with Step / Depends on / Contract changes, and
  this log).
