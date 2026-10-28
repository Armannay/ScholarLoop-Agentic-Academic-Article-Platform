# SPEC-CHANGE-REQUESTS — `docs/openapi/` (not contracts)

`docs/openapi/` is spec-only and was read, not modified. The Step 10b screens follow it
where it exists and diverge nowhere except where it is silent; each gap is listed here so
the next API step can close it. **CONTRACTS v1 is untouched and no CONTRACT-CHANGE-REQUEST
is required.**

## 1. `POST /api/projects` — body shape and status

The spec's `ProjectCreate` is `{id (slug), name, description?}` and is marked `planned`.
Step 11 shipped a stored project whose body is `{name, description?, brief, policy_overrides?, status?}`
with a ULID `id` in the response, and the Step 10b Brief form creates exactly that: a
project is the Brief it writes against. Requests:

* `ProjectCreate` gains a required `brief` (a CONTRACTS v1 `Brief`) and an optional
  `policy_overrides`; `id` disappears from the request (the server assigns a ULID).
* `ProjectView` gains `owner_id`, `brief`, `policy_overrides`, `status` (`active | archived`) and
  drops the slug pattern on `id`.
* `x-scholarloop-status` for `GET/POST /api/projects` and `GET/PATCH/DELETE /{id}` becomes
  `implemented` (Step 11), and `GET /api/projects` gains `next_cursor` / `has_more` /
  `cursor` (keyset pagination).
* The console sends `{name, brief, status}`; the mock accepts it verbatim.

## 2. `GET /api/projects/{project_id}/runs` — still `planned`

The project page lists a project's runs (newest first) and the console calls it. Step 11's
`StoreRunsQuery` already selects exactly those records, so this is a thin adapter away.
Until then the route is served by the mock only.

## 3. `GET /api/runs/{run_id}/quality` — still `planned`

The Q chart needs one `QualityReport` per iteration (eight gate booleans + the scores) and
the series. `RunView` deliberately carries only `q_series`/`best_q`, so this endpoint (or a
`latest_report` on `RunView`, which would make every poll larger) is required. The console
renders the planned shape exactly; the mock implements it.

## 4. `GET /api/runs/{run_id}/events/history` — still `planned`

The live feed reconnects with `?after=<last seq>`, which covers the browser case, but a
freshly opened tab has no cursor, so the JSON twin (`events`, `next_after`) is what the
console uses to rebuild the timeline. Mock-only today.

## 5. Article history, diff, snapshots and restore — still `planned`

`ArticleViewer` calls all four. The article store already exposes `history()`, `diff()`,
`snapshots()`, `best_snapshot()` and `restore()`; the routes are not wired. Two details the
spec should pin while wiring:

* `restore` reports `restored` / `unchanged` / `versions` (new version per changed section,
  never a rewrite) — the console prints exactly this and shows "nothing to restore" when
  every section already matches.
* `GET .../diff` defaults to `from = to - 1`, which the version-history table relies on for
  its "vs prev" column.

## 6. `RunUsageView` reports tokens, not monetary values

There is a `token_cap` and a `max_iterations`, but no billed-USD figure or USD cap, and
`config/policies.yaml` has no spend ceiling. An earlier Step 10b UI derived dollar
estimates from an uncontracted rate table and a token-cap equivalent. That estimate path
was removed in the 2026-10-28 follow-up; the UI now explicitly reports cost as
unavailable and shows token usage by role only.

Request: add `usd_spent: number` and `usd_cap: number | null` to `RunUsageView` (and a
`usd_cap` knob to policies). Until those values are provided by the API, do not infer
monetary amounts from token counts.

## 7. (resolved) the console keeps no second copy of the spec

An earlier draft of this work had a hand-maintained `frontend/src/openapi.json` next to
`docs/openapi/openapi.yaml`, which is exactly the drift this step was asked to avoid. The
shell's generated client (`npm run api:gen` → `src/api/openapi.gen.d.ts`) replaced it, so
there is no request here — kept as a note so the decision is on the record.

## 8. `needs_user_input` stays 409

Recorded again because the console now depends on it: intake's missing-field answer is
`409 needs_user_input` with `context.missing_fields[]`, which the Brief form turns into
inline questions. A change to 422 (as some API briefs expect) would break that mapping;
the console switches on `error.code`, so it would still work, but the spec and the code
should keep agreeing.
