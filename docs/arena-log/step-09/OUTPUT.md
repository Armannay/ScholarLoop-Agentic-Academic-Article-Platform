# Step 9 (b) - raw output

Captured 2026-10-27 on Linux, python 3.11.2, no network calls from the suite
(`MockProvider` / scripted agents only). Every command below was run from the repository
root on branch `arena/7a5435e2-scholarloop-agentic-academic-a` (cut from `main` at
`8310102`).

## 1. Read confirmations (one line each)

| Path | Confirmation |
| --- | --- |
| `backend/app/contracts/` (18 modules) | read: `common` (ULID/slug/word-bound types, `extra="forbid"`, `schema_version=1`), `enums` (closed vocabularies; `RunStateName` has all 12 orchestrator states), `run` (`RunRecord`, `RunEvent`, `RUNNING_STATES`), `checkpoint`, `ledger`, `quality` (the 8 gate names), `brief`, `section` (`EMPTY_SECTION_HASH`), `source`, `patch`, `plan`, `task`, `verdict`, `digest`, `llm`, `loop`, `agent`, `account`, `binding`, `registry`. |
| `backend/app/core/errors.py` | read: `ScholarLoopError` with `code`/`http_status`, `NeedsUserInput` (422), `format_validation_errors` for 422 wrappers. |
| `backend/app/providers/base.py` | read: `LLMProvider` Protocol, `estimate_tokens`, `ProviderRouting` (role->binding->account resolution seam). |
| `backend/app/providers/errors.py` | read: `ProviderError` family with class-level `retryable`; `NoAccountAvailable` -> 409. |
| `backend/app/providers/mock.py` | read: scripted `(role, call index)` replies; no transport. |
| `config/policies.yaml` | read: `max_iterations`, plateau/regression/oscillation/diminishing knobs, retry budget 2, per-task timeout knob, `q_min 70`. |
| `docs/openapi/openapi.yaml` (~3147 lines) | read (frozen): 17 runs operations incl. SSE `events`; `active_run.409.json` example makes the one-active-run guard mandatory; `PROJECT_ID_PATTERN ^[a-z0-9][a-z0-9-]{2,63}$`. |
| `docs/openapi/examples/*.json` | read: wire shapes for `run_conflict.409`, `active_run.409`, `quality.200`, `events-history.200`, `section-history.200` (oldest-first), `section-diff.200`, `snapshots.200` (newest-first), `restore` req/res. |
| 9a seams (`loop_controller.py`, `stop.py`, `persistence.py`) | imported, not rewritten: `LoopController.evaluate` (max_iterations cap, plateau, stagnation, diminishing, oscillation), `StopReport`, `RunStore` Protocol + `FileRunStore` (durable JSON per run). |
| 9a simulator `backend/app/scripts/simulate_loop.py` | rerun as smoke on this branch: 6/6 scenarios terminate as expected (improving, plateau, oscillating, regressing, budget_burn, flaky_agent), zero provider tokens spent. |
| `backend/app/api/{main,deps}.py` | read: error handler emits `{"error": exc.to_dict()}`; 422 handler strips input values; DI wires `Orchestrator` on the app state. |
| `backend/tests/conftest.py` | read: `repo_root`/`settings`/`policies`/`rubric`/`mock_provider`/`db_url`/`engine`/`session`/`client`/`auth_headers` fixtures; `asyncio_mode="auto"`. |
| `backend/tests/orchestrator_support.py` | read: `ScriptedPlanner`/`ScriptedAgents`/`CrashingAgents`, `BRIEF_PAYLOAD`, `make_task`/`make_report`/`make_entry` - reused by the new API tests. |

## 2. Spec audit against the frozen openapi.yaml

The runs block of `openapi.yaml` (read fully, not modified) requires, for item 6:

| Endpoint | Status in branch at 9b start | Action |
| --- | --- | --- |
| `POST /projects/{id}/runs` | existed | added 409 `active_run` guard semantics + `ProjectIdPath` pattern |
| `POST /runs/{id}/decision` | existed | distinguished reason codes per spec: `invalid_transition`, `no_best_snapshot`, `missing_budget_extension`, `missing_goal` |
| `POST /runs/{id}/stop`, `GET /runs/{id}`, `GET /runs/{id}/events` (SSE), `/digest`, `/ledger`, `/article`, `/usage` | existed | `response_model` enforcement added for `/article` (`ArticleView`) and `/usage` (`RunUsageView`, alias `in`) |
| `GET /runs/{id}/events/history` | **planned in spec, missing in code** | implemented (`after >= -1` cursor, `limit 1..1000`, `next_after` rules per spec) |
| `GET /runs/{id}/quality` | **planned in spec, missing in code** | implemented (`QualityView`: `latest`, `series`, `best_q`) |
| `GET /runs/{id}/article/sections/{id}/history` | **planned in spec, missing in code** | implemented (oldest-first `VersionInfo[]`) |
| `GET /runs/{id}/article/sections/{id}/diff` | **planned in spec, missing in code** | implemented (`from`/`to` query aliases, v0 = empty baseline `EMPTY_SECTION_HASH`, 409 `invalid_version_range`) |
| `GET /runs/{id}/article/snapshots` | **planned in spec, missing in code** | implemented (newest-first `SnapshotSummary[]`) |
| `POST /runs/{id}/article/restore` | **planned in spec, missing in code** | implemented (`dry_run` preview + real rollback; 409 `restore_while_running` when live) |

Design decisions recorded with the code:

- One-active-run guard: `Orchestrator.create_run` under a `threading.Lock` re-reads the
  store (`_live_run_for(project_id, owner_id)`); "live" = `RUNNING_STATES` + `CHECKPOINT`;
  corrupt records are skipped with a warning; intake errors (422) outrank the guard (bad
  brief beats `active_run`), matching the spec's examples.
- `create_run` is `(project, owner)`-scoped: two owners may run the same project id in
  parallel (the store is per-owner in this build).
- Restore preview mirrors `ArticleStore.restore` ordering; announced versions are
  head+1; a real restore emits a `rollback_applied` run event, then persists.
- Response models with reserved names (`from`, `in`) use
  `ConfigDict(populate_by_name=True, serialize_by_alias=True)`; `ArticleSection` allows
  empty bodies (`str_min_length=0`) because empty sections are legal.

## 3. Test results

New test file `backend/tests/test_runs_article_api.py` (17 tests):

- guard semantics (intake-error outranks guard; live states only; quick_parallel allowed
  for different projects; `RunConflict.context` shape vs `active_run.409.json`);
- decision 409 reason matrix (`invalid_transition`, `no_best_snapshot`,
  `missing_budget_extension`, `missing_goal`);
- `events/history` paging (`after=-1` full, cursor paging with `next_after`, limit cap);
- `quality` empty-then-series shape (`latest`, `series`, `best_q`);
- section history/diff/snapshots wire shapes vs `docs/openapi/examples/*.json`
  (field-for-field), diff v0 empty baseline, `from>to` 409, `to` beyond head 404;
- restore: dry-run preview leaves store untouched, real restore appends head+1 version,
  emits `rollback_applied`, marks `stale_sections` empty afterwards;
- unit-level `_live_run_for`: corrupt record skipped with warning, `DONE`/`STOPPED_*` not
  live.

Pre-existing suites rerun after the guard was added: `test_runs_api.py`,
`test_orchestrator_{runner,persistence,intake,scheduler,loop_controller}.py`,
`test_api.py` - 104 passed, no regressions.

## 4. Quality gates (verbatim tails)

### `make check`

```
ruff check backend  -> All checks passed!
ruff format         -> 182 files left unchanged
mypy                -> Success: no issues found in 119 source files
skill-lint          -> PASS
pytest              -> 1040 passed, 8 skipped, 1 warning in 28.52s
```

(1023 passed on main at `8310102`; +17 new tests.)

### `cd backend && python -m app.scripts.simulate_loop`

```
[ok] improving      state=DONE             iters=3 Q=[50, 65, 85] tokens=0 decisions: CONTINUE -> CONTINUE -> OFFER_FINISH -> OFFER_FINISH
[ok] plateau        state=STOPPED_EROSION  iters=4 Q=[50, 50, 50, 50] tokens=0 decisions: CONTINUE -> CONTINUE -> CONTINUE -> STOP_EROSION
[ok] oscillating    state=STOPPED_EROSION  iters=5 Q=[60, 70, 60, 70, 60] tokens=0 decisions: CONTINUE -> CONTINUE -> CONTINUE -> CONTINUE -> STOP_EROSION
[ok] regressing     state=STOPPED_EROSION  iters=3 Q=[75, 65, 55] tokens=0 decisions: CONTINUE -> CONTINUE -> STOP_EROSION
[ok] budget_burn    state=STOPPED_BUDGET   iters=2 Q=[50, 55] tokens=1200000 decisions: CONTINUE -> STOP_BUDGET
[ok] flaky_agent    state=DONE             iters=2 Q=[55, 85] tokens=0 decisions: CONTINUE -> OFFER_FINISH -> OFFER_FINISH
all scenarios terminated as expected (zero provider tokens spent)
```

## 5. CONTRACT-CHANGE-REQUESTS (listed, not edited)

Carried to the PR body verbatim:

1. `docs/openapi/openapi.yaml` (frozen): the six article/read-model operations added above
   still carry `x-scholarloop-status: planned` although they are now implemented; a
   spec-only status flip is left to the spec authors (no behavioural change).
2. No contract (`backend/app/contracts/`) changes were needed; the frozen contracts
   already express every state, event and payload used here.
