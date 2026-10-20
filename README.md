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
  memory/      append-only ledger, bounded digest, compactor, source store
  article/     section store, patch application, versioning, snapshots, assembly
  agents/      generic Agent runtime, context builder, registry, governor seam
  skills/      lazy SKILL.md loader, deterministic selection, lint CLI
  tools/       deny-by-default scoped tool registry (concrete tools: Step 6)
  api/         FastAPI: health, config, contracts, auth, accounts, agent bindings
  governor/ planner/ orchestrator/                          # next steps
backend/tests/ contracts, config, providers, accounts, memory, article, agents,
               skills, tools, API, account store, secret scan
config/        policies.yaml (loop knobs), rubric.yaml (gates, criteria, sections)
rules/         root.rules.md + agents/<role>.rules.md   (<= 400 tokens each)
skills/        <name>/SKILL.md prompt skills             (<= 400 tokens each, linted)
frontend/      React + Vite + TypeScript status console
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
arguments, marks results untrusted and caps their size. Next: the governor, the concrete
tools (Step 6), the five role agents (Step 7), the Planner, and the loop controller.
