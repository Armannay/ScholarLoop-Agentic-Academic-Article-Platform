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
make install            # create .venv and install pinned deps
make check              # ruff + mypy --strict + pytest  -> the gate every step passes
make schemas            # export docs/schemas from the contracts
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
  providers/   LLMProvider Protocol, ProviderError family, MockProvider
  api/         FastAPI: health, effective config, contract schemas
  accounts/ governor/ memory/ article/ agents/ planner/ orchestrator/   # next steps
backend/tests/ contract round-trip, config loaders, MockProvider, API, secret scan
config/        policies.yaml (loop knobs), rubric.yaml (gates, criteria, sections)
rules/         root.rules.md + agents/<role>.rules.md   (<= 400 tokens each)
frontend/      React + Vite + TypeScript status console
docs/          ARCHITECTURE.md, DECISIONS.md, schemas/
```

## Working agreements

1. `make check` must be green before a change is considered done.
2. Tests and CI use `MockProvider` only — never a live provider call.
3. No secrets anywhere: keys live in the encrypted account store; `.env.example` holds placeholders; CI scans the tree and the logging handler redacts.
4. Contracts are frozen for the duration of a step: send a CONTRACT-CHANGE-REQUEST instead of editing `backend/app/contracts/`.
5. Third-party API details go behind an adapter and carry a `# VERIFY` marker.
6. Bounded by design: the digest ≤ 2,000 tokens, `summary` and `snippet` ≤ 60 words, patches not rewrites.

## Status

Foundation complete: contracts, policy/rubric config, rules, provider interface with
MockProvider, API health/config surface, CI. Next: accounts (encrypted store), the
governor, the ledger/digest, the article store with patch application, the five agents,
the Planner, and the loop controller.
