# ScholarLoop — Agentic Academic Article Platform

ScholarLoop writes academic articles with a **deterministic orchestrator**. A
planning-only Planner (Claude) returns a todo list; five agents (architect,
researcher, writer, reviewer, validator) execute it and return patches plus a
`ChangeEntry`. History is an append-only ledger; the Planner only ever reads a bounded
digest. Everything is measured in tokens and every decision is explainable.

Start here:

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, the loop's state machine, one iteration end to end.
* [docs/DECISIONS.md](docs/DECISIONS.md) — ADR template and the first five decisions.
* [docs/schemas/](docs/schemas/) — generated JSON Schema for every CONTRACTS v1 model.
* [rules/root.rules.md](rules/root.rules.md) — the hard rules every agent obeys.
* [.env.example](.env.example) — every environment variable, placeholders only.

## Quick start

```bash
cp .env.example .env    # fill in APP_MASTER_KEY (the account store stays sealed without it)
make install            # create .venv and install pinned deps
make check              # ruff + mypy --strict + pytest  -> the gate every step passes
make schemas            # export docs/schemas from the contracts
make migrate            # apply the account-store schema (alembic)
make run                # API on http://localhost:8000  (docs at /docs)

make frontend-install   # npm ci in frontend/
make frontend-dev       # UI  on http://localhost:5173 (proxies /api -> :8000)
make frontend-check     # typecheck + vitest (jsdom) + production build
make frontend-e2e       # Playwright smoke test (once: npx playwright install chromium)
```

Docker: `make up` (backend :8000, frontend :5173), `make down`.
Postgres instead of SQLite: `docker compose --profile postgres up`.

## Layout

```
backend/app/
  contracts/   Pydantic v2 wire format (extra=forbid, schema_version=1)
  core/        settings, policies, rubric, ids, hashing, redaction, logging
  providers/   LLMProvider Protocol, ProviderError family, MockProvider, live
               adapters (Anthropic, OpenAI-compatible), call-time account pool
  accounts/    encrypted account store, argon2+JWT auth, bindings, usage, audit
  projects/    project CRUD surface: brief + policy overrides, keyset pagination,
               RunsQuery seam (active-run guard for PATCH/DELETE)
  memory/      append-only ledger, bounded digest, compactor, source store
  article/     section store, patch application, versioning, snapshots, assembly
  agents/      generic Agent runtime, context builder, registry, governor seam
  skills/      lazy SKILL.md loader, deterministic selection, lint CLI
  tools/       deny-by-default scoped tool registry (concrete tools: Step 6)
  api/         FastAPI: health, config, contracts, auth, accounts, agent bindings,
               projects, runs
  governor/    nested budgets, token estimator, cache-friendly context, telemetry,
               model tiers
  planner/     plan/delta/evaluate (never writes article text)
  orchestrator/ deterministic loop: intake, state machine, scheduler,
               loop controller, checkpoints, run persistence (Step 9)
backend/tests/ contracts, config, providers, accounts, memory, article, agents,
               skills, tools, governor, API, account store, secret scan
config/        policies.yaml (loop knobs), rubric.yaml (gates, criteria, sections)
rules/         root.rules.md + agents/<role>.rules.md   (<= 400 tokens each)
skills/        <name>/SKILL.md prompt skills             (<= 400 tokens each, linted)
prompts/       per-role prompt assets + golden answers; planner/ (<= 700 tokens)
frontend/      React + Vite + TypeScript console: router, layout, in-memory auth
               session, shell components, generated OpenAPI client (api/), the
               projects/briefs screens, run view (plan board, SSE change log, Q
               chart, meters), checkpoint modal, stop report, article viewer with
               history/diff/restore and .md export, plus the in-memory mock that
               dev and tests run on. Vitest (jsdom) + Playwright.
docs/          ARCHITECTURE.md, DECISIONS.md, schemas/
```

## Working agreements

1. `make check` must be green before a change is considered done.
2. Tests and CI use `MockProvider` only — never a live provider call.
3. No secrets anywhere: provider keys live in the encrypted account store (`APP_MASTER_KEY` protects them) and are write-only through the API; `.env.example` holds placeholders; CI scans the tree and the logging handlers redact.
4. Contracts are frozen for the duration of a step: send a CONTRACT-CHANGE-REQUEST instead of editing `backend/app/contracts/`.
5. Third-party API details go behind an adapter and carry a `# VERIFY` marker.
6. Bounded by design: the digest ≤ 2,000 tokens, `summary` and `snippet` ≤ 60 words, patches not rewrites.

## Status

Foundation complete: contracts, policy/rubric config, rules, provider interface with
MockProvider, API health/config surface, CI, and the article store (independently
versioned sections, patch application with optimistic concurrency, snapshots with a
best-so-far pointer, markdown assembly, erosion signals). Next: accounts (encrypted
store), the governor, the ledger/digest, the five agents, the Planner, and the loop
controller.
Foundation and accounts complete: contracts, policy/rubric config, rules, API, CI;
encrypted runtime account store with per-user ownership, bindings (planner included),
usage roll-ups and an append-only audit trail; live Anthropic and OpenAI-compatible
adapters behind the frozen `LLMProvider` protocol; and a provider pool that resolves
accounts at call time with dedicated / round_robin / failover strategies and a circuit
breaker. Next: the governor, the ledger/digest, the article store with patch
application, the five agents, the Planner, and the loop controller.

Step 5 (generic agent runtime) complete: `Agent.run(task, run_state)` builds its prompt
through `ContextBuilder`, runs the governor precheck, makes one JSON-schema-constrained
call, validates the `AgentResult`/`Patch` contracts, performs at most one minimal repair
(validation errors plus the previous output only) and returns measured token usage; a
typed role registry with enable/disable and call-time bindings; a lazy
`skills/<name>/SKILL.md` loader with deterministic two-skill selection and
`make skill-lint`; and a deny-by-default, project-scoped tool registry that validates
arguments, marks results untrusted and caps their size.

Step 5 (governor) complete: `app/governor/` makes spend predictable. Nested budgets
(`account-month -> run -> iteration -> call`) reserve before a call and settle after it,
warn at 80 % and refuse at 100 %; `TokenEstimator` uses the provider tokeniser or
`chars / 4` plus a 15 % margin and reports measured drift; `ContextBuilder` assembles a
byte-stable cacheable prefix (rules, rubric excerpt, skill index, brief) then
semi-stable references then the volatile task, trimming in a documented order (digest
detail, optional skills, source-card notes) and refusing rather than cutting the task;
`TelemetryRecorder` reports tokens by role and iteration, cache-hit ratio and token ROI;
`TierResolver` maps policies' tiers to model ids with per-binding override. Next: the
concrete tools (Step 6), the five role agents (Step 7), the Planner, and the loop
controller.

Step 7 (role agents) complete: `app/agents/roles/` implements architect, researcher,
writer, reviewer and validator on the Step 5 runtime. Each role owns a prompt asset
(`prompts/<role>.md`) and a rules file (`rules/agents/<role>.rules.md`, ≤ 400 tokens),
returns a strict JSON answer validated as `AgentResult` plus a role document, and
enforces its own acceptance checks deterministically: the architect plans rubric section
ids whose targets add up to the brief, the researcher may only return cards a
`scholar_search` result (or the source store) actually holds, the writer owns one section
per task and may only cite supplied cards inside the word window, the reviewer reads
dirty sections only and cannot emit prose or a patch, and the validator sets the eight
hard gates from `text_checks`/`citation_check`/`doi_verify` and computes `Q` from the
reviewer's latest scores. `prompts/<role>.golden.json` holds one worked input→answer per
role, replayed by the test-suite through `MockProvider`.

Step 8 (planner) complete: `app/planner/` plans and evaluates and never writes article
text. `PlanValidator` owns the mandatory distribution (one valid role per task, a DAG,
acceptance criteria and a positive budget per task, at most 8 tasks, the summed budgets
inside the remaining run budget, section ids that exist or are created by an earlier
architect task, and no task for the Planner itself), and a rejected draft is retried at
most twice with the validator's error list and nothing else. `probes.py` computes the 8
hard gates deterministically and builds a compact article view (headings, per-section word
and citation counts, first and last two sentences, <= 3 excerpts of <= 300 tokens; full
text only on a user-opted deep check). `evaluation.py` merges the model's soft-criterion
scores with those gates and can only ever make the verdict stricter - a failing gate means
`meets_minimum=false` whatever the model says. `events.py` admits planner calls only at
events (run start, empty queue, a newly failing gate, a completed milestone, the first
plateau - which buys the one strategy change), at most once per iteration. Prompts live in
`app/planner/prompts.py` under a 700-token ceiling with capped output and JSON-schema
answers, mirrored in `prompts/planner/` and pinned by tests.

Step 11 (projects) complete: `app/projects/` stores a project as a real row (Alembic
revision `0002_projects`): id, owner, name, description, the CONTRACTS v1 `Brief` it
writes against, a validated policy-override block, status and timestamps.
`/api/projects` offers create, keyset-paginated list (`limit` + opaque `cursor`,
newest first), read, patch and delete, all owner-scoped with foreign ids reading as
404. A bad brief or policy override is a 422 whose `context.errors[]` names each
offending field (`brief.target_words`, `policy_overrides.q_min`) and never echoes a
submitted value. PATCH and DELETE answer 409 `conflict` with
`context.reason=active_run` while a run of the project is still working, read through
the `RunsQuery` Protocol (in-memory fake for tests, `StoreRunsQuery` over the persisted
run records in production); a `CHECKPOINT` run does not block an edit, because the
loop is waiting for the user.

Step 10b (console) complete — the feature screens on top of the console shell: **New
project** (a Brief form with inline validation mirroring deterministic intake, and a
`409 needs_user_input` turned into the same inline questions), **Run view** (state badge,
plan board grouped by the role that owns each task, live change log over SSE, token /
iteration / cost meters against the caps, Q-per-iteration chart with a chip per hard
gate), **Checkpoint modal** (exactly `stop_report.options`: Finish, Continue with extra
budget, Edit goals, Roll back; focus-trapped, Escape closes, `needs_input` opens itself),
**Stop report** (reasons, Q series, best snapshot, spend, risks, failed gates, evidence),
**Article viewer** (sections, per-section history, unified diff, snapshot restore as new
versions, `dir=rtl` + `lang` for `fa`/`ar`) and **Export .md** (DOCX/PDF stay deferred and
visibly disabled). Everything renders from the OpenAPI-generated client; the bearer token
stays in memory and no key material is drawn anywhere. `make frontend-check` is the gate
(tsc, 47 Vitest tests, Vite build); `make frontend-e2e` runs the Playwright smoke test of
project -> run -> checkpoint -> article -> export against the in-memory mock
(`VITE_API_MOCK=true`). This step also fixes a real bug in the shell's SSE hook: its
`connect()` could never open a stream because the "closed" flag was never cleared.
