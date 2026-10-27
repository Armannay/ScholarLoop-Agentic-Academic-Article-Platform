# Step 12 — Frontend 10b (projects, runs, checkpoints, article)

Saved verbatim as the record of what was asked. Two placeholders arrived empty
(`<...>` for the extra files, `< New Project ... >` as the task body); what was received
is reproduced exactly, and how each gap was resolved is recorded in `OUTPUT.md` §1.

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

* "the shell is merged": the Step 10a shell (auth, accounts, agents, status, mock server,
  typed client seam, SSE reader) **is** on `main` — PR #17, merged — and the Step 10
  OpenAPI spec is on `main` too (PR #19). So 10b builds on the real shell rather than on
  "local mock data and the Brief and SectionState types" alone; the mock server is kept
  and extended so every new screen also runs with zero backend.
* "the '10b' part of the 'Step 10' prompt": the screens listed in the task body, in the
  order the user meets them.
* "the same addition": the process constraints above (read-first confirmations, frozen
  contracts, `make check`, PR with Step / Depends on / Contract changes, arena log).
