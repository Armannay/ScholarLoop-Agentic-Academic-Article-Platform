# Step 13 — Runs and Article API: conformance pass (raw output)

Captured 2026-10-28 on Linux, Python 3.11.2, branch
`arena/d10d61a0-scholarloop-agentic-academic-a`, base `d4a0deb` (= `main`, PR #31).
No live API calls: `MockProvider`, scripted agents and the real `FileRunStore` only.

## 0. What the base already contained

The Step 9b brief says to import the 9a modules and implement the Runs/Article
endpoints. On this base, 9b had already been merged (PR #25) and Step 11 had added the
stored-projects API (PR #24), so the work was a **conformance pass**: prove every
operation against the frozen spec, and close the gaps the proofs found. Nothing from
9a was rewritten — `loop_controller.py`, `stop.py` and `scripts/simulate_loop.py` are
byte-identical (`git diff` on them is empty).

Starting state of the sixteen operations in the Runs/Article block:

| # | operation | routed | matched the spec |
| --- | --- | --- | --- |
| 1 | `GET /api/projects/{project_id}/runs` | **no** | — |
| 2 | `POST /api/projects/{project_id}/runs` | yes | payload ✓, documented 401/409/422 ✗ |
| 3 | `GET /api/runs/{run_id}` | yes | ✓, documented 401/404 ✗ |
| 4 | `POST /api/runs/{run_id}/decision` | yes | ✓, documented 401/404/409/422 ✗ |
| 5 | `POST /api/runs/{run_id}/stop` | yes | ✓, documented 401/404 ✗ |
| 6 | `GET /api/runs/{run_id}/events` (SSE) | yes | ✓, documented 401/404 ✗ |
| 7 | `GET /api/runs/{run_id}/events/history` | yes | ✓, documented 401/404 ✗ |
| 8 | `GET /api/runs/{run_id}/digest` | yes | ✓, documented 401/404 ✗ |
| 9 | `GET /api/runs/{run_id}/ledger` | yes | ✓, documented 401/404 ✗ |
| 10 | `GET /api/runs/{run_id}/quality` | yes | ✓, documented 401/404 ✗ |
| 11 | `GET /api/runs/{run_id}/article` | yes | ✓, documented 401/404 ✗ |
| 12 | `GET …/article/sections/{section_id}/history` | yes | ✓, documented 401/404 ✗ |
| 13 | `GET …/article/sections/{section_id}/diff` | yes | ✓, documented 401/404/409 ✗ |
| 14 | `GET …/article/snapshots` | yes | ✓, documented 401/404 ✗ |
| 15 | `POST …/article/restore` | yes | ✓, documented 401/404/409/422 ✗ |
| 16 | `GET /api/runs/{run_id}/usage` | yes | ✓, documented 401/404 ✗ |

Two further mismatches were found by comparing the served `/openapi.json` to the YAML:
`RunView.kind` was a bare `string` (spec: `$ref RunKind`) and `RunView.checkpoint_kind`
was a bare `string` (spec: `$ref CheckpointKind`).

## 1. What changed

### 1.1 `GET /api/projects/{project_id}/runs` (the missing operation)

* `backend/app/orchestrator/runner.py`: `Orchestrator.project_runs(project_id, *,
  owner_id, limit) -> (list[RunRecord], int)` — newest first by `created_at` (id as the
  deterministic tie-break), `total` is the unpaged count, read from persisted records
  only so listing can never start, revive or mutate a run. A corrupt record is skipped
  with a warning (`_readable_records`) instead of failing the page.
* `backend/app/api/routers/runs.py`: `RunSummary` + `RunList` (`RunList` =
  `{runs, total}`), the route with the spec's `Limit` (default 50, `1..200`), and the
  404 rule: a project is addressable when it exists as a stored project **or** has at
  least one run. Anything else — including another owner's project — reads as
  `404 not_found` (`context.project_id`), never an empty page that would confirm the
  label exists.

### 1.2 The served OpenAPI now declares the spec's failures

`backend/app/api/schemas.py` gained the documentation twins of the runtime error
handler: `ErrorBody`, `ErrorEnvelope`, `ValidationFieldError`, `ValidationErrorContext`,
`ValidationErrorBody`, `ValidationErrorEnvelope`. Every Runs/Article route declares
`responses=` with exactly the status codes the spec lists (401, 404, 409, and 422 where
the spec has a body), so `/openapi.json` no longer documents only `200` and FastAPI's
`HTTPValidationError`. The handlers still build plain dicts — an exception handler must
not be able to fail validation.

### 1.3 Wire enum names match the spec

`RunView.kind` / `RunSummary.kind` are a `RunKind` enum (`brief | quick_edit`) instead
of a bare string, and `RunView.checkpoint_kind` is the existing `CheckpointKind`
(`completion | needs_input | evaluation_failed`). `RunKind` is declared in the router
next to the view with a one-line `RunKind.of(IntakeKind)` conversion: the wire component
is named what a generated client expects, while the orchestrator keeps its own
`IntakeKind`.

### 1.4 `project_id` accepts a slug *and* a project ULID

The frozen parameter allows `^[a-z0-9][a-z0-9-]{2,63}$`; Step 11 made stored projects
ULID-keyed (its `spec-change-requests.md` #3 asks the spec to switch `ProjectId` to a
ULID) and the console calls `POST /api/projects/{project.id}/runs`. On the merged code
that answered `422` for every stored project. The router now accepts the union
`^(?:[a-z0-9][a-z0-9-]{2,63}|[0-7][0-9A-HJKMNP-TV-Z]{25})$` — a strict **superset** of
the spec, so no request the spec allows is refused. Recorded as spec-change-request 2.

### 1.5 The proof, as a test

`backend/tests/test_openapi_conformance.py` parses `docs/openapi/openapi.yaml` and the
served `/openapi.json` and compares, for all sixteen operations: presence, methods,
parameter sets/requiredness/types/bounds/defaults, status codes, error-envelope
components, success and request-body components, and — for the 25 wire models — the
field set, the base type of every field, `additionalProperties`, and that the app never
requires a field the spec does not. Three deviations are asserted **by name**
(opaque `run_id`/`section_id`, the widened `project_id`, FastAPI's automatic `422`)
so a fourth cannot appear silently. A deliberate mutation (`page_size` added to
`RunList`) fails the suite; removing it passes again.

## 2. Spec audit, after the change

The one-off spec-vs-served comparison that found the gaps above is now permanent:
`backend/tests/test_openapi_conformance.py` performs the same diff on every run (paths
15, methods 16, parameters, codes, payload fields), so the numbers cannot rot in a log.

Payload schemas: `RunView`, `RunSummary`, `RunList`, `RunCreate`, `UserDecision`,
`DigestView`, `LedgerView`, `QualityView`, `QualityPoint`, `ArticleView`,
`ArticleSection`, `RunUsageView`, `UsageBreakdown`, `RunEventList`, `RunEvent`,
`SectionHistoryView`, `VersionInfo`, `SectionDiffView`, `TextDiff`, `SnapshotList`,
`SnapshotSummary`, `RestoreRequest`, `RestoreView`, `ErrorEnvelope`,
`ValidationErrorEnvelope` — same field sets, no extra required fields.

## 3. Tests

New (23):

* `backend/tests/test_runs_list_api.py` (14) — auth; unknown label 404 (`not_found`,
  `context.project_id`); project-id pattern; another owner's project 404 and no rows
  leaking into any list; a stored project with no runs answers `{runs: [], total: 0}`;
  a legacy slug with runs stays listable; row fields equal to the frozen example
  document; newest-first with `total` beyond the page; default limit 50 of 55; limit
  bounds 0/201/"many" → 422; corrupt record skipped; `project_runs` with
  `owner_id=None` (internal) and `limit=0`; and a run driven to its checkpoint by the
  scripted orchestrator, whose row mirrors the persisted record (state, iter, best_q,
  tokens) with no brief, plan, decisions or sections.
* `backend/tests/test_openapi_conformance.py` (9) — the audit above plus "every
  operation rejects an anonymous caller with 401" and "`project_id` accepts a slug and
  a ULID but not `Bad Project`".

Pre-existing suites: unchanged, still green (see `make-check.txt`).

```
$ python -m pytest backend/tests/test_runs_list_api.py backend/tests/test_openapi_conformance.py -q
23 passed
```

Full gate — `docs/arena-log/step-13/make-check.txt`:

```
ruff check backend                  All checks passed!
ruff format --check backend         207 files already formatted
mypy (strict, backend/app)          Success: no issues found in 138 source files
skill-lint                          7 skill(s) OK in skills
pytest                              1187 passed, 8 skipped, 1 warning in 39.89s
```

Coverage gate (the CI step added on `main` by PR #31) — `coverage.txt`:
orchestrator 92%, governor 93%, memory 96%, all above the 80% floor. No new
`# pragma: no cover` was added.

## 3.1 The 9a loop controller, untouched

```
$ python -m app.scripts.simulate_loop
[ok] improving      state=DONE             iters=3 Q=[50, 65, 85] tokens=0
[ok] plateau        state=STOPPED_EROSION  iters=4 Q=[50, 50, 50, 50]
[ok] oscillating    state=STOPPED_EROSION  iters=5 Q=[60, 70, 60, 70, 60]
[ok] regressing     state=STOPPED_EROSION  iters=3 Q=[75, 65, 55]
[ok] budget_burn    state=STOPPED_BUDGET   iters=2 Q=[50, 55] tokens=1200000
[ok] flaky_agent    state=DONE             iters=2 Q=[55, 85]
all scenarios terminated as expected (zero provider tokens spent)
```

## 4. Read confirmations

| Path | How it was read this session |
| --- | --- |
| `backend/app/contracts/` (18 modules) | `common.py` (Contract base, `Ulid`, `SectionId`, word bounds) and `enums.py` (closed vocabularies) read in full; the other sixteen are byte-identical to the commit Step 9b was written against — `git diff --stat` on the directory is empty, so the frozen v1.1 models are unchanged. |
| `backend/app/core/errors.py` | read: `ScholarLoopError` carries `code` + `http_status`; `NotFoundError` = 404 `not_found`, `NeedsUserInput` = 409, `ConflictError` = 409. |
| `config/policies.yaml` | read (schema_version 1, `policies:` block, empty `projects:` overrides); used through `Policies` only. |
| `docs/openapi/openapi.yaml` (3147 lines) | read the whole Runs/Article/Usage block, `components.parameters`, `components.responses`, and every schema those operations reference — **not modified**. |
| `docs/openapi/examples/runs/list-project-runs.response.200.json` | read and used as the row-field contract in the list test. |
| `backend/app/article/store.py` | read the `SectionStorage` protocol and `Snapshot`/`VersionInfo` shapes behind `/article`, `/history`, `/diff`, `/snapshots`, `/restore`. |
| `backend/app/orchestrator/{runner,persistence,state_machine,intake}.py` | read; `FileRunStore` is the only store the API writes through. 9a modules untouched. |
| `backend/app/providers/base.py` | read: `LLMProvider` protocol + `ProviderRouting` (call-time role → binding → account), which the router wiring uses and does not bypass. |
| `backend/app/api/{main,deps}.py`, `app/projects/*` | read: exception handlers emit `{"error": …}`; `ProjectServiceDep` is the 404 oracle for the list endpoint. |

## 5. Deviations and follow-ups

`spec-change-requests.md` in this directory lists all five: the frozen
`x-scholarloop-status: planned` strings on eight served operations, `ProjectId` needing
the ULID arm, the opaque `run_id`/`section_id` metadata, `GET /api/errors/catalog`
still unwired, and Step 11's outstanding projects-tag items. `docs/openapi/README.md`
(documentation, not the spec) now carries the true status table and a pointer here.
