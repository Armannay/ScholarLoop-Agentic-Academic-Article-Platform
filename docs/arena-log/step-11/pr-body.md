# Step 11 — Projects API (backend)

**Step:** 11 — Projects API (backend half of the Projects PR; the first project step
after the API-design PR, "first the Projects PR (backend), after the API PR is merged").

**Depends on:** Step 1 (auth: argon2 + HS256 JWT, `AuthContext`, `CurrentUserDep`,
`users` table) and Step 9b (run persistence: `RunRecord.state`/`project_id`,
`RUNNING_STATES`, `RunStore`), plus the Step 10 OpenAPI design (`docs/openapi/`,
read-only here).

**Contract changes:** **none.** No file under `backend/app/contracts/` was edited,
`docs/schemas/` is untouched (`make schemas-check` still passes), and a project never
crosses an agent, ledger or provider boundary, so it does not need a frozen wire
contract. `docs/openapi/openapi.yaml` is also unmodified; the spec deltas this step
implies are written up in `docs/arena-log/step-11/spec-change-requests.md` instead of
being edited into the spec.

## What this adds

A project is now a stored, owner-scoped row instead of something derived from run
records (ADR-0008):

* **`projects` table** (Alembic `0002_projects`): ULID `id`, `owner_id` → `users.id`,
  `name` (unique per owner), `description`, `brief` JSON, `policy_overrides` JSON,
  `status` (`active | archived`), `created_at`, `updated_at`, plus the
  `(owner_id, created_at, id)` index the pagination walks.
* **`/api/projects`** — `POST` (201), `GET` (keyset list), `GET /{id}`, `PATCH /{id}`,
  `DELETE /{id}` (204). All require the Step 1 bearer token and are scoped to the
  caller: a foreign project is **404**, never 403.
* **Validation by the owners of the rules**: `brief` is validated by the frozen
  `Brief` contract, `policy_overrides` by `deep_merge` + `Policies` (unknown keys and
  out-of-range values rejected). A bad payload is `422 validation_error` with
  `context.errors[] = [{loc, msg, type}]` naming each field (`brief.target_words`,
  `policy_overrides.q_min`, …), and nothing is written.
* **Cursor pagination**: `limit` (1..200, default 50) + opaque `cursor`; newest first
  by `(created_at DESC, id DESC)`; `ProjectList` carries `total`, `next_cursor`,
  `has_more`. A malformed cursor is a 422 on `loc=cursor`.
* **409 active-run guard**: `PATCH`/`DELETE` answer `409 conflict` with
  `context.reason=active_run`, `context.run_id` and `context.state` while a run of the
  project is in `RUNNING_STATES`. `CHECKPOINT` does not block (the loop is waiting for
  a human). The guard reads through a **`RunsQuery` Protocol** with two
  implementations: `InMemoryRunsQuery` (the fake the API tests use) and
  `StoreRunsQuery` (read-only over the persisted `RunRecord`s, wired by default now
  that Step 9b is merged).
* **No secrets anywhere**: no key material is involved; audit detail records *which*
  fields changed (never values), validation messages are scrubbed of any submitted
  string ≥ 8 chars and pass through `redact()`, and a test pins that a canary inside a
  brief never appears in `caplog` or `audit_log.detail`.

## Files

New: `backend/app/projects/{__init__,models,schemas,validation,pagination,runs,service}.py`,
`backend/app/api/routers/projects.py`,
`backend/app/migrations/versions/0002_projects.py`,
`backend/tests/test_projects_api.py`, `backend/tests/test_projects_service.py`,
`docs/arena-log/step-11/`.

Touched (additive): `api/main.py` (router), `api/deps.py` (`get_runs_query`,
`get_project_service`), `api/routers/{__init__,runs}.py` (export + docstring),
`accounts/db.py` (`init_db` registers the table), `migrations/env.py` (metadata),
`tests/test_migrations.py` (`projects` in `EXPECTED_TABLES`, two new tests),
`README.md`, `docs/DECISIONS.md` (ADR-0008). `docs/ARCHITECTURE.md` is deliberately
untouched: a test caps it at 1,500 words, and a component row would have forced
unrelated prose edits.

## Tests

`make check` is green:

```
ruff check + ruff format --check   -> clean
mypy --strict (backend/app)        -> Success: no issues found in 138 source files
skill-lint                         -> 7 skill(s) OK
pytest                             -> 1145 passed, 8 skipped (baseline: 1096 passed, 8 skipped)
```

New coverage (49 tests):

* CRUD round trip, patch-subsets, delete-then-404, duplicate name → 409
  `project_exists`, empty patch → 422, null name/brief → 422.
* Authorization: every route 401 without a token; user B cannot read, patch or delete
  user A's project (all 404), B's list stays empty, A's project untouched; two owners
  may reuse a name.
* Pagination: 5 projects at `limit=2` → pages of 2+2+1, no duplicates, stable `total`,
  `next_cursor` null on the last page; bounded `limit`; 4 malformed cursors → 422
  `loc=cursor`; cursor round-trip/UTC normalisation/7 rejection cases at unit level.
* Validation: missing/invalid brief fields, the cross-field contradiction, unknown and
  out-of-range policy keys, nested override errors, and that the submitted canary is
  never echoed back or persisted.
* 409: PATCH and DELETE refused while `EXECUTING` (with `run_id`/`state`), allowed
  after the run settles, allowed at `CHECKPOINT`, and another owner's run does not
  block my project. `RunsQuery` semantics pinned for both implementations.
* Migration: `0002` creates the table, the unique `(owner_id, name)` constraint, the
  keyset index and the FK; a migrated database accepts a real project insert.
* Seam efficiency: a page of N projects is one pass over the run store, not N
  (`summaries` on the Protocol, pinned by a counting fake).
* Logs: no brief content in `caplog` text or the audit trail.

## Known deltas / follow-ups

* `docs/openapi/openapi.yaml` still marks the projects operations `planned` and
  describes them as derived from runs. Seven concrete edits are listed in
  `docs/arena-log/step-11/spec-change-requests.md` (status, view fields, ULID id,
  PATCH/DELETE, cursor members, active-run 409 reason, the still-planned
  `/api/projects/{project_id}/runs`). The spec was read, not changed.
* `POST /api/projects/{project_id}/runs` still accepts any label and does not require
  a project row; requiring one belongs to the step that owns the run API.
* `GET /api/projects/{project_id}/runs` (spec-side run history) is not wired yet;
  `StoreRunsQuery` already selects exactly those records.
