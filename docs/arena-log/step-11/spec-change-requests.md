# SPEC-CHANGE-REQUESTS — `docs/openapi/openapi.yaml` (not contracts)

`docs/openapi/` is spec-only and was read, not modified. The Step 11 brief asked for a
real projects resource (table, ULID, brief, policy overrides, status, cursor
pagination, PATCH/DELETE), while the spec's `projects` tag still describes projects
*derived from run records* and marks `GET/POST /api/projects` as `planned`. The code
follows the brief; this file lists what the spec needs so the next step can bring the
two back in line. **CONTRACTS v1 is untouched and no CONTRACT-CHANGE-REQUEST is
required** — a project never crosses an agent, ledger or provider boundary.

1. **The projects tag is no longer `planned`.** `GET /api/projects`,
   `POST /api/projects` and `GET /api/projects/{project_id}` are served today by
   `backend/app/api/routers/projects.py`; their `x-scholarloop-status` should read
   `implemented`. The "Projects group runs... metadata is derived from its runs"
   paragraph and the `ProjectView`/`ProjectCreate` descriptions are out of date.

2. **`ProjectView` needs the stored record.** Today it is `additionalProperties:
   false` and carries only `id`, `name`, `description`, `run_count`, `latest_*`,
   `best_q`, `created_at`, `updated_at`. The API now also returns `owner_id`,
   `brief` (a CONTRACTS v1 `Brief`), `policy_overrides` (object) and `status`
   (`active | archived`). `run_count`, `latest_*` and `best_q` stay derived, as the
   spec wants — they are computed through `RunsQuery`.

3. **`id` is a ULID, not a slug.** `ProjectCreate.id` (`^[a-z0-9][a-z0-9-]{2,63}$`)
   and `ProjectView.id` should become
   `$ref: '#/components/schemas/Ulid'`, with `name` (≤ 120 chars, unique per owner)
   carrying the human label. The `ProjectId` parameter's pattern description
   ("not a ULID") should become a ULID `$ref`, and the `GET
   /api/projects/{project_id}/runs` path keeps working because a run's `project_id`
   is the project's ULID. `ProjectCreate` then requires `name` + `brief` (and accepts
   optional `description`, `policy_overrides`, `status`) instead of `id` + `name`.

4. **`ProjectPatch` and `DELETE /api/projects/{project_id}` are missing.** The brief
   asks for both. Document `PATCH` (all members optional; explicit `null` clears
   `description` and `policy_overrides`; `name`/`brief` cannot be cleared) and
   `DELETE` (204, no body). Both answer `409` with `error.code=conflict`,
   `error.context.reason=active_run`, `context.run_id` and `context.state` while a run
   of the project is in a running state; `CHECKPOINT` is not active.

5. **`ProjectList` needs the cursor.** Add nullable `next_cursor` (opaque string) and
   boolean `has_more`, and a `Cursor` query parameter (`cursor`, max 512 chars) beside
   `Limit`; ordering is `created_at DESC, id DESC`. An unreadable cursor is `422`
   `validation_error` with `context.errors[0].loc = "cursor"`.

6. **Two documented 409 reasons for projects.** `ProjectExists`
   (`context.reason=project_exists`) is served as written; the active-run refusal
   (`context.reason=active_run`) is the same envelope but a different reason, as
   `POST /api/projects/{project_id}/runs` already documents.

7. **`GET /api/projects/{project_id}/runs` is still unimplemented** (not part of this
   step's brief; the runs are reachable through the run API). It should stay
   `planned` until a step wires it to `RunsQuery`.
