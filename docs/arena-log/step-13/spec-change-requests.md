# SPEC-CHANGE-REQUESTS — `docs/openapi/openapi.yaml` (frozen; not edited)

`docs/openapi/openapi.yaml` was read and **not modified** in this step. Everything
below is a request to the spec authors, listed here and in the PR body rather than
applied. No CONTRACTS v1 model (`backend/app/contracts/`) was touched, so no
CONTRACT-CHANGE-REQUEST is raised: every payload on this surface is already expressible
with the frozen contracts.

## 1. `x-scholarloop-status: planned` on eight operations that are served

`POST /api/projects/{project_id}/runs` and the rest of the runs/article block are
served by `backend/app/api/routers/runs.py`. The following still say `planned` in the
spec while being implemented (the first six since Step 9b, the seventh since Step 11,
the eighth now):

| operation | spec status | served since |
| --- | --- | --- |
| `GET /api/projects/{project_id}/runs` | planned | this step |
| `GET /api/runs/{run_id}/events/history` | planned | Step 9b |
| `GET /api/runs/{run_id}/quality` | planned | Step 9b |
| `GET /api/runs/{run_id}/article/sections/{section_id}/history` | planned | Step 9b |
| `GET /api/runs/{run_id}/article/sections/{section_id}/diff` | planned | Step 9b |
| `GET /api/runs/{run_id}/article/snapshots` | planned | Step 9b |
| `POST /api/runs/{run_id}/article/restore` | planned | Step 9b |
| `GET/POST /api/projects`, `GET /api/projects/{project_id}` | planned | Step 11 |

`docs/openapi/README.md` (documentation, not the spec) now tracks the real status, and
`backend/tests/test_openapi_conformance.py` fails the build if the two disagree on any
path, method, parameter, status code or payload field.

## 2. `ProjectId` must also accept a project ULID

The frozen parameter is:

```yaml
ProjectId:
  schema: { type: string, pattern: '^[a-z0-9][a-z0-9-]{2,63}$' }
```

"When it was written, a `project_id` was an opaque label stored on each run." Step 11
added a real `projects` table whose `id` is a ULID and whose `name` is the human label
(its own `spec-change-requests.md` #3 already asks for exactly this change). The console
creates runs with `project.id`, and a ULID (`01M5Z0X96Y46B0GTT36PSYZBQA`) does not
match the slug pattern, so `POST /api/projects/{ulid}/runs` answered `422` on the
merged code.

The router now accepts the union:

```text
^(?:[a-z0-9][a-z0-9-]{2,63}|[0-7][0-9A-HJKMNP-TV-Z]{25})$
```

a **strict superset** of the frozen pattern: no request the spec allows is refused.
Requested spec change: `ProjectId` becomes
`oneOf: [$ref Ulid, pattern '^[a-z0-9][a-z0-9-]{2,63}$']` (or `$ref Ulid` only, with
`name` as the label), and the parameter's "not a ULID" description is dropped.

## 3. `run_id` / `section_id` are opaque strings to the router

The spec types them `$ref Ulid` / `$ref SectionId`. The router takes `str` (like
`/api/projects/{project_id}` does), so a malformed id answers the `404 run_not_found`
the spec declares for those operations instead of a `422` it does not declare. The
wire *responses* match the spec; only the served parameter metadata is looser.
Requested spec change: none required if the 404 semantic is intended — otherwise say
so and the router will validate the pattern.

## 4. `GET /api/errors/catalog` is still not wired

The `errors` tag, `ErrorCode`, and several descriptions point at
`GET /api/errors/catalog`. No such route exists (it is outside the Runs/Article
surface this step owns). The error models added to `backend/app/api/schemas.py`
therefore describe `code` as "opaque, documented per endpoint" rather than pointing at
a catalog that would 404. Either wire the endpoint or drop the references.

## 5. The projects tag predates the project table

Unchanged from Step 11's list (#1, #2, #4, #5, #6): the tag's description ("metadata is
derived from its runs"), `ProjectView`'s missing `owner_id`/`brief`/`policy_overrides`/
`status`, the absent `PATCH`/`DELETE`, and the missing cursor parameter are all out of
date. `GET /api/projects/{project_id}/runs` is the last of that family to be wired and
is now served — it stays in this list only because the spec says `planned`.
