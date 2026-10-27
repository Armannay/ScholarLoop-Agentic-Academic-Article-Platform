# Step 11 — Projects API: raw output

Captured 2026-10-27 on Linux x86_64, Python 3.11.2, in this branch
(`arena/d6c2a9f0-scholarloop-agentic-academic-a`), from a clean `.venv`
(`pip install -e ".[dev]"`: fastapi 0.143.1, SQLAlchemy 2.1.4, alembic 1.20.0,
pydantic 2.14.1, pytest 9.1.1, ruff 0.17.0, mypy 2.4.0).

## 1. Files read before writing anything (unmodified)

Every file below was read in full; each confirmation is one line.

| Path | Confirmed read |
| --- | --- |
| `backend/app/contracts/__init__.py` | registry re-exports; contracts are the bottom of the dependency graph. |
| `backend/app/contracts/common.py` | `Contract` = `extra="forbid"` + stripping, `VersionedContract` pinned to `schema_version: Literal[1]`, `Ulid`/`NonEmptyStr`/`word_bounded` definitions. |
| `backend/app/contracts/account.py` | `Account`/`AccountPublic`/`MonthlyBudget`; key material is ciphertext-only, `key_fingerprint` over the ciphertext. |
| `backend/app/contracts/agent.py` | `AgentResult` = entry + patches + source cards + sections + optional report; only the validator reports. |
| `backend/app/contracts/binding.py` | `AgentBinding` (role, ordered account ids, strategy, model, caps, enabled). |
| `backend/app/contracts/brief.py` | `Brief` fields, `MIN/MAX_TARGET_WORDS`, the `must_include`/`must_avoid` non-overlap model validator, `word_tolerance_*`. |
| `backend/app/contracts/enums.py` | the closed vocabularies (`ArticleType`, `CitationStyle`, `AgentRole`, `ORCHESTRATOR_ROLE`, `ModelTier`, ...). |
| `backend/app/contracts/ledger.py` | `ChangeEntry`, `TokenUsage` spelled `in/out/cached`, `patch_ref_for`. |
| `backend/app/contracts/llm.py` | `SystemBlock`/`LLMMessage`/`LLMRequest`/`LLMResponse` and the prompt caps. |
| `backend/app/contracts/loop.py` | `LoopDecision` and the seven `LoopAction` values. |
| `backend/app/contracts/patch.py` | op verbs and the literal-match payload rules. |
| `backend/app/contracts/plan.py` | `Plan` shape, task DAG and the plan-level invariants. |
| `backend/app/contracts/quality.py` | `GATE_NAMES`, `QualityIssue`, `QualityReport` (Q and the eight gates). |
| `backend/app/contracts/registry.py` | `CONTRACTS` mapping + `json_schema_for` (23 names). |
| `backend/app/contracts/section.py` | `CitationRef`, `SectionState` lifecycle (`empty -> draft -> reviewed -> final`). |
| `backend/app/contracts/source.py` | `SourceCard`, the 60-word snippet ceiling, DOI normalisation. |
| `backend/app/contracts/task.py` | `Task` (exactly one role), acceptance list, token budget. |
| `backend/app/contracts/verdict.py` | `Verdict`, `meets_minimum` vs `recommendation` consistency rules. |
| `backend/app/core/errors.py` | `ScholarLoopError` carries `code` + `http_status`; `ConflictError` is 409, `NotFoundError` 404, `format_validation_errors`. |
| `backend/app/providers/base.py` | `LLMProvider` Protocol, `estimate_tokens` = ceil(chars/4), `prompt_tokens`. |
| `backend/app/providers/errors.py` | `ProviderError` family with `retryable` as a class fact. |
| `backend/app/providers/mock.py` | `MockProvider` scripting, failure injection, deterministic usage; no network, no keys. |
| `config/policies.yaml` | budgets, digest settings, `q_min`, erosion knobs, `error_streak`, `model_tiers`, empty `projects:` block. |
| `backend/app/accounts/**` (extra) | models, db (`init_db`/`session_scope`), crypto envelope, auth (argon2 + HS256 JWT, `AuthContext`), schemas, service (ownership → 404, audit detail redacted, bindings, usage, resolver). |
| `backend/app/api/**` (extra) | `create_app` + lifespan, `deps` (`CurrentUserDep`, `SessionDep`, `PoliciesDep`), routers (health/config/auth/accounts/agents/runs), `schemas`, error handlers (the 422 handler strips `input`). |
| `docs/openapi/**` (extra) | `openapi.yaml` (3,147 lines: conventions, projects/runs/article/usage/errors, components), README (mock scenarios, typed client, verification gates, CONTRACT-CHANGE-REQUESTS 1–8), examples (incl. `examples/projects/*`). |

Also read for context: `backend/app/core/policies.py`, `app/core/clock.py`, `app/core/ids.py`,
`app/core/logging.py`, `app/core/redaction.py`, `app/core/settings.py`,
`app/orchestrator/{state_machine,persistence,stop}.py`, `app/migrations/{env.py,versions/0001_account_store.py}`,
`alembic.ini`, `Makefile`, `pyproject.toml`, `backend/tests/{conftest,factories,test_accounts_api,test_runs_api,test_migrations,test_no_key_leak,test_api}.py`,
`docs/DECISIONS.md`, `docs/CONTRACT_CHANGELOG.md`, `docs/arena-log/step-{02,06,07,08,10}/`.

## 2. `make check` (the gate)

```
$ make check
.venv/bin/ruff check backend          -> All checks passed!
.venv/bin/ruff format --check backend -> 202 files already formatted
.venv/bin/mypy                        -> Success: no issues found in 138 source files
skill-lint                            -> 7 skill(s) OK in skills
.venv/bin/python -m pytest            -> 1145 passed, 8 skipped, 1 warning in 32.51s
```

Full capture: [`make-check.txt`](make-check.txt). Baseline before this step was
`1096 passed, 8 skipped`; Step 11 adds 49 tests (24 HTTP + 23 domain in the two
projects files, 2 in `test_migrations.py`).

## 3. The new tests, alone

```
$ .venv/bin/python -m pytest backend/tests/test_projects_api.py \
      backend/tests/test_projects_service.py backend/tests/test_migrations.py
.....................................................                    [100%]
53 passed, 1 warning in 7.07s
```

Coverage, by the brief's checklist:

| Brief item | Tests |
| --- | --- |
| CRUD | `test_create_and_read_round_trip`, `test_patch_changes_only_the_fields_it_names`, `test_delete_removes_the_project`, `test_patch_with_no_fields_is_422`, `test_patch_rejects_a_null_name`, `test_create_rejects_a_duplicate_name_with_409` |
| Authorization | `test_every_project_route_requires_auth`, `test_another_users_project_is_invisible` (B reads/patches/deletes A's project → **404**, B's list stays empty, A's row untouched; the same name is allowed for two owners), `test_unknown_project_id_is_404`, `test_another_users_run_does_not_block_my_project` |
| Pagination | `test_pagination_visits_every_project_exactly_once` (5 projects, limit 2 → 2+2+1 pages, no duplicates, `total` constant, `next_cursor` null last), `test_limit_is_bounded`, `test_invalid_cursor_is_a_422_on_the_cursor_field` (4 malformed cursors), `test_cursor_round_trips_an_aware_timestamp`, `test_cursor_normalises_a_naive_timestamp_to_utc`, `test_cursor_rejects_malformed_input` (7 cases), `test_service_pages_newest_first_and_clamps_the_limit` |
| Validation | `test_invalid_brief_reports_every_field_and_creates_nothing` (missing `target_words`, bad `citation_style`, contradictory `must_include`/`must_avoid`; per-field `loc`s; nothing created; submitted value never echoed), `test_invalid_policy_overrides_are_field_errors` (unknown key → `policy_overrides.q_mni`; `q_min: 500`; nested `plateau.min_gain: -1`), `test_patch_validates_the_brief_too`, `test_valid_brief_is_returned_json_safe`, `test_invalid_brief_errors_name_each_field_and_never_echo_values`, `test_policy_overrides_are_validated_and_stored_verbatim`, `test_policy_overrides_reject_unknown_keys_and_bad_values` |
| 409 active run | `test_patch_and_delete_are_refused_while_a_run_is_active` (PATCH/DELETE 409 `conflict`/`active_run` with `run_id` + `state`; project untouched; after the run settles both succeed), `test_checkpoint_is_not_an_active_run` |
| No secrets in logs | `test_brief_contents_never_reach_logs_or_the_audit_trail` (brief canary absent from `caplog.text` and from `audit_log.detail`; the stored brief does contain it), `test_in_memory_seam_picks_the_newest_running_run`, `test_store_seam_reads_the_real_run_store` |
| Migration | `test_projects_revision_creates_the_constraints_pagination_relies_on` (indexes + unique (owner, name) + FK), `test_migrated_database_accepts_project_writes`, plus the existing column-drift test now covering `projects` |
| Seam efficiency | `test_summaries_answer_a_page_in_one_pass`, `test_store_summaries_group_several_projects`, `test_list_page_reads_the_runs_seam_once_per_page` (a page is one pass over the run store, not one per project) |

## 4. What was built

New:

```
backend/app/projects/__init__.py       package map + exports
backend/app/projects/models.py         ProjectRow (shared metadata, unique (owner,name),
                                       ix_projects_owner_created)
backend/app/projects/schemas.py        ProjectCreate/Patch/View/List, ProjectStatus
backend/app/projects/validation.py     Brief + policy-override validation -> per-field 422
backend/app/projects/pagination.py     opaque keyset cursor (created_at, id), base64url JSON
backend/app/projects/runs.py           RunsQuery Protocol, InMemoryRunsQuery, StoreRunsQuery
backend/app/projects/service.py        CRUD, ownership, page mechanics, active-run guard
backend/app/api/routers/projects.py    GET/POST /api/projects, GET/PATCH/DELETE /{id}
backend/app/migrations/versions/0002_projects.py
backend/tests/test_projects_api.py     24 HTTP tests
backend/tests/test_projects_service.py 23 domain tests
```

Changed (one line each, all additive):

* `backend/app/api/main.py` — include the projects router.
* `backend/app/api/deps.py` — `get_runs_query` (override → run store → empty fake),
  `get_project_service`, `ProjectServiceDep`, `RunsQueryDep`.
* `backend/app/api/routers/runs.py` — docstring: there is still no Project *contract*,
  but there is a project *table* now, and a run's `project_id` may be its ULID.
* `backend/app/accounts/db.py` — `init_db` also registers the projects table (local
  import, so `app.accounts` does not depend on `app.projects` at import time).
* `backend/app/migrations/env.py` — import `app.projects.models` to register metadata.
* `backend/app/api/routers/__init__.py` — export the new router module.
* `backend/tests/test_migrations.py` — `projects` joins `EXPECTED_TABLES`; two new tests.
* `README.md` — layout + a Step 11 status paragraph.
* `docs/DECISIONS.md` — ADR-0008 (store projects in a table).
* `docs/openapi/**` — **not touched** (see `spec-change-requests.md`).
* `docs/ARCHITECTURE.md` — **not touched**: it is capped at 1,500 words by
  `backend/tests/test_rules_docs.py`, and adding a `projects` component row would have
  forced unrelated prose edits. The module is documented in `README.md`, ADR-0008 and
  this log instead.

## 5. Decisions worth knowing (and where they are recorded)

1. **A project is a row, not a derivation.** ADR-0008; the spec's own
   CONTRACT-CHANGE-REQUEST 7 asked for this record. `docs/openapi` still describes the
   old derivation, so the delta is written down instead of edited into the spec.
2. **`id` is a ULID and `name` is the label.** Every other row in the database is a
   ULID, and the spec's slug would collide with the run `project_id` namespace that
   already accepts free-form labels.
3. **`brief` and `policy_overrides` are validated by their owners.** The frozen
   `Brief` model decides `brief`; `deep_merge` + `Policies` decide the overrides, so an
   unknown policy key is a 422 at project-write time, exactly as it is in
   `config/policies.yaml`.
4. **422 errors never echo submitted values.** `context.errors[]` carries only
   `{loc, msg, type}`. A pydantic cross-field message can quote the offending values
   (`must_include and must_avoid overlap …: ['…']`), so project validation scrubs every
   submitted string (≥ 8 chars) out of the message and runs `redact()` over it. A
   brief containing a pasted credential therefore cannot come back in a response or a
   log line.
5. **Keyset, not offset.** `(created_at DESC, id DESC)` with the ULID as tie-break and
   an index with exactly that tuple; `next_cursor` is opaque base64url JSON so it can
   gain a sort key later without a client change. `total` is the whole list.
6. **`CHECKPOINT` is not an active run.** The 409 guard uses `RUNNING_STATES`; at a
   checkpoint the loop is waiting for a human, and refusing an edit then would
   deadlock the console. A stopped run (`STOPPED_*`) and `DONE` do not block either.
7. **`RunsQuery` is a Protocol with two implementations.** `InMemoryRunsQuery` is the
   fake the tests pin (and the honest default when no run store exists);
   `StoreRunsQuery` reads the persisted `RunRecord`s read-only, so the projects API can
   never resurrect, mutate or race a run. Step 9b is already merged, so the real
   adapter is wired, but the fake keeps the API tests orchestrator-free.
8. **Audit detail holds field names, never values.** `project.create` records the name,
   status and policy *keys*; `project.patch` records `["brief", "name"]`-style lists.
   The brief itself (which may quote user data) never reaches `audit_log.detail` or a
   log line — pinned by a test.

## 6. CONTRACT-CHANGE-REQUESTS

**None.** No file under `backend/app/contracts/` was edited; `docs/schemas/` is
untouched (`make schemas-check` would still pass). A project never crosses an agent,
ledger or provider boundary, so it does not need a frozen wire contract.

## 7. Open questions handed to the next step

* `docs/openapi/openapi.yaml` — apply the seven spec edits in
  [`spec-change-requests.md`](spec-change-requests.md) (projects tag status, view
  fields, ULID id, PATCH/DELETE, cursor members, active-run 409 reason).
* `POST /api/projects/{project_id}/runs` still accepts any label and does not require
  a project row. Requiring one (404 `project_not_found` otherwise) is a one-line
  change but belongs to the step that owns the run API.
* `GET /api/projects/{project_id}/runs` (the spec's planned run history) is not wired;
  `StoreRunsQuery` already has the matching records, so it is a thin adapter away.
