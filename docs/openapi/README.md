# ScholarLoop UI API - OpenAPI 3.1

The contract between the console and the API: **Projects**, **Runs**, **Article**,
**Usage** and one **Errors** envelope, plus the `auth` / `health` / `config` /
`accounts` / `bindings` calls every screen touches.

This directory is spec-only. It generates a typed client and a mock; it contains no
backend code, and it never modifies the frozen CONTRACTS v1 models in
`backend/app/contracts/`.

```
docs/openapi/
  openapi.yaml              the spec (30 paths, 36 operations, 54 components + 8 contracts)
  redocly.yaml              lint rules (also the openapi-typescript entry point)
  package.json              lint / verify / mock / client scripts
  tsconfig.json             typecheck config for the generated client
  examples/                 one Example Object per request and response - see below
  mock/prism.config.yaml    mock host/port + the three scenarios
  mock/scenarios/*.yaml     per-scenario example overlays
  mock/server.mjs           composes spec+overlay and execs Prism
  scripts/verify.mjs        example<->schema gate (ajv, contract schemas included)
  scripts/client-check.mjs  regenerates the client and typechecks it
  generated/                build output, git-ignored
```

## Quick start

```bash
cd docs/openapi
npm install          # Redocly, Prism, openapi-typescript, ajv (~1 min, once)

npm run check        # lint + example validation + client typecheck  (the gate)
npm run mock         # Prism mock on http://0.0.0.0:4010, completion scenario
```

`make openapi-check` runs the same gate from the repository root, and
`make openapi-mock` starts the mock (`SCENARIO=… PORT=…` optional).

## The mock

```bash
npm run mock                      # completion: Q=78, waiting for finish/continue
npm run mock -- erosion-stop      # Q plateaued 72-74, STOPPED_EROSION, rollback offered
npm run mock -- needs-input       # CHECKPOINT needs_input, action=ASK_USER
npm run mock -- completion --port 4011 --verboseLevel debug
```

Prism runs in **static** mode: it serves the examples in `examples/`, never random
data, so the console can be built and demoed without a backend. Every scenario is a
YAML overlay in `mock/scenarios/` that pins which example a response serves first -
the spec itself is untouched, and the composed document
(`.mock.<scenario>.openapi.yaml`) is generated and git-ignored.

Two Prism behaviours worth knowing:

* **Authentication is enforced.** Prism answers `401` to an operation with a bearer
  security requirement when the header is missing, so send any token:
  `curl -H 'Authorization: Bearer anything' http://localhost:4010/api/runs/…`.
  `POST /api/auth/login` and `/api/auth/register` have `security: []` and need no token.
* **`Prefer:` selects variants.** `Prefer: code=409` picks the 409 response,
  `Prefer: code=409,example=active_run` picks one of its named examples. Request
  bodies are still validated - a body that violates the schema answers `422` before
  `Prefer` is consulted.

```bash
RID=01M5Z0X96Y46B0GTT36PSYZBQA
curl -s -H 'Authorization: Bearer x' localhost:4010/api/runs/$RID | jq '.state, .q_series'
curl -s -H 'Authorization: Bearer x' localhost:4010/api/runs/$RID/quality | jq '.latest.Q'
curl -s -H 'Authorization: Bearer x' -H 'Prefer: code=409' \
  -X POST localhost:4010/api/projects/peer-review-2026/runs -d @examples/runs/create-run.request.json
curl -sN -H 'Authorization: Bearer x' "localhost:4010/api/runs/$RID/events?once=true"
```

## The typed client

```bash
npm run client        # → generated/api-schema.d.ts  (openapi-typescript)
npm run client:check  # regenerate, then tsc --noEmit over the generated types
npm run typecheck     # tsc --noEmit on whatever is already generated
```

`generated/api-schema.d.ts` exports `paths`, `components`, `operations` and `$defs`.
The console imports it, e.g.

```ts
import type { components, operations } from '../docs/openapi/generated/api-schema'

type RunView = components['schemas']['RunView']
type CreateRun = operations['createRun']['requestBody']
type RunState = components['schemas']['RunStateName'] // a closed 12-member union
```

The output is git-ignored on purpose: the spec is the source of truth, and a committed
generated file is a second one that silently goes stale. `npm run client:check` proves
the generated types still compile *and* still describe the API (it asserts that
`RunView.state` is the closed state union, that `ArticleSection.text` exists, that the
create-run body is present, that the operation ids the console calls exist, and that an
invalid state string fails to compile).

## Verifying the spec

`npm run check` is three gates:

| gate | command | what it proves |
| --- | --- | --- |
| structure | `npm run lint` | Redocly validates OpenAPI 3.1, no unresolved refs, every operation has an id/summary/description/tag, no orphaned components |
| semantics | `npm run verify` | every example validates against the schema it is attached to (including the CONTRACTS v1 JSON Schemas), every operation has examples, every example file is referenced, every SSE frame parses into a `RunEvent`, `ArticleSection` stays field-identical to `SectionState`+`text`, and all three scenarios are still valid and example-complete |
| client | `npm run client:check` | the spec generates a TypeScript client that compiles and whose types the console can rely on |

`verify.mjs` also enforces the one rule Redocly cannot express: every operation must
declare a 4xx response **except** `/api/health` and `/api/version`, which answer before
auth or config exists.

## Examples

One Example Object per request body and per response, named
`<operation>.<request|response>.<status>[.<variant>].json`:

```json
{
  "summary": "200 - paused at the completion checkpoint",
  "description": "Why this payload looks the way it does; what the console should do with it.",
  "value": { "…": "…" }
}
```

SSE examples are YAML (`examples/runs/events.*.sse.yaml`) because the payload is a
string of frames, not JSON:

```yaml
value: |-
  id: 5
  event: checkpoint_reached
  data: {"seq":5,"ts":"…","type":"checkpoint_reached","state":"CHECKPOINT",…}
```

Every fixture tells **one** story, so the files can be read as a session: Ada runs the
`peer-review-2026` project on structured review checklists; the run reaches Q=78 with
all eight gates passing and waits at the completion checkpoint; the erosion scenario
reuses the same project with Q plateaued at 72-74; the needs-input scenario pauses
because one reference cannot be verified. Word counts and hashes in the fixtures are
computed from the text they describe (`sha256:` of the exact body), not invented.

Third-party names in the fixtures (Braun & Clarke 2006, Kitchenham & Charters 2007,
Gopen & Swan 1990) are real publications used as plausible reference data; the quotes
are illustrative. The API key in `examples/accounts/` is deliberately not a key.

## Endpoints

30 paths, 36 operations. `x-scholarloop-status` on every operation says whether the
FastAPI router serves it today (`implemented`) or whether it is designed here for the
console (`planned`).

| tag | endpoint | status |
| --- | --- | --- |
| projects | `GET/POST /api/projects` | planned |
| projects | `GET /api/projects/{project_id}` | planned |
| projects | `GET /api/projects/{project_id}/runs` | planned |
| runs | `POST /api/projects/{project_id}/runs` | implemented |
| runs | `GET /api/runs/{run_id}` | implemented |
| runs | `POST /api/runs/{run_id}/decision` | implemented |
| runs | `POST /api/runs/{run_id}/stop` | implemented |
| runs | `GET /api/runs/{run_id}/events` (SSE) | implemented |
| runs | `GET /api/runs/{run_id}/events/history` | planned |
| runs | `GET /api/runs/{run_id}/digest` | implemented |
| runs | `GET /api/runs/{run_id}/ledger` | implemented |
| runs | `GET /api/runs/{run_id}/quality` | planned |
| article | `GET /api/runs/{run_id}/article` | implemented |
| article | `GET /api/runs/{run_id}/article/sections/{section_id}/history` | planned |
| article | `GET /api/runs/{run_id}/article/sections/{section_id}/diff` | planned |
| article | `GET /api/runs/{run_id}/article/snapshots` | planned |
| article | `POST /api/runs/{run_id}/article/restore` | planned |
| usage | `GET /api/runs/{run_id}/usage` | implemented |
| errors | `GET /api/errors/catalog` | planned |
| auth | `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me` | implemented |
| health | `GET /api/health`, `GET /api/version` | implemented |
| config | `GET /api/config`, `GET /api/contracts`, `GET /api/contracts/{name}/schema` | implemented |
| accounts | `GET/POST /api/accounts`, `GET/PATCH/DELETE /api/accounts/{account_id}`, `POST …/test` | implemented |
| bindings | `GET/PUT /api/agents/{role}/binding` | implemented |

**Note on the endpoint list.** The task that produced this spec referred to an
"endpoint list above" that did not survive into the prompt. Rather than invent one, the
list above is derived from the routers that exist (`backend/app/api/routers/*.py`), the
run orchestrator's read models, and the article store's capabilities - and each gap is
marked `planned` rather than quietly presented as shipped.

## Contracts

Wire payloads are **CONTRACTS v1** models, `$ref`-ed straight out of `docs/schemas/`
(which `make schemas` exports from the Pydantic models, and `make schemas-check` keeps
in step):

```yaml
Brief:
  $ref: '../schemas/Brief.json'
```

Included: `Brief`, `Plan`, `ChangeEntry`, `SectionState`, `SourceCard`,
`QualityReport`, `Verdict`, `LoopDecision` - the eight the UI actually renders. Their
nested models (`Task`, `TokenUsage`, `CitationRef`, `QualityIssue`, the enums) come
along inside those documents, and `openapi-typescript` promotes them to named types.

To expose another contract, add the same two lines under `components/schemas` in
`openapi.yaml` and reference `#/components/schemas/<Name>`. Never copy a contract by
hand: `verify.mjs` validates every example against the real JSON Schema, so a copy
would drift within a week.

The remaining fifteen contract names (`Account`, `AccountPublic`, `AgentBinding`,
`AgentResult`, `LLMMessage`, `LLMRequest`, `LLMResponse`, `MonthlyBudget`, `Patch`,
`PatchOp`, `QualityIssue`, `SystemBlock`, `Task`, `TokenUsage`, `CitationRef`) are not
part of the UI surface: they describe storage, internal agent I/O or provider calls.
`GET /api/contracts` lists all 23 for tools that want them.

## Errors

One envelope for every expected failure:

```json
{"error": {"code": "needs_user_input", "message": "intake needs user input: target_words", "context": {"missing_fields": ["target_words"], "problems": [], "kind": "brief"}}}
```

* `code` is the closed set in `#/components/schemas/ErrorCode` (24 codes, published with
  statuses and retry hints by `GET /api/errors/catalog`). Switch on it; never parse
  `message`.
* `context` carries the machine-readable detail: `missing_fields` for
  `needs_user_input`, `reason` for `conflict`/`run_conflict`, `errors[]` for
  `validation_error`.
* `422` never echoes the submitted value - the handler strips it so a rejected
  `api_key` cannot reappear in a response or a log line.

The two conditions the console must render distinctly:

| condition | wire status | code | example |
| --- | --- | --- | --- |
| the loop needs a human | `409` | `needs_user_input` | `examples/errors/needs_user_input.409.json` |
| a second run on a busy project | `409` | `run_conflict`, `context.reason=active_run` | `examples/errors/active_run.409.json` |
| an answer to a `needs_input` checkpoint is incomplete | `422` | `validation_error` | `examples/errors/needs_input.422.json` |

The task brief called the second one "409 active-run" and a related case "422
needs_input"; the wire values above are what `backend/app/core/errors.py` and
`app/orchestrator/errors.py` actually produce, and the spec follows the code. See
`CONTRACT-CHANGE-REQUESTS` below.

## Server-Sent Events

`GET /api/runs/{run_id}/events` replays the persisted log and then tails it. The frame
format is fixed:

```
id: <seq>
event: <type>
data: <RunEvent JSON>

```

`id:` equals `payload.seq` and `event:` equals `payload.type`, so `?after=<last id>`
resumes without gaps, and `EventSource.onmessage` plus named listeners both work.
`?once=true` replays what exists and closes. The stream closes on its own when the run
settles (`DONE`, `STOPPED_*`, `CHECKPOINT`). The JSON twin -
`GET /api/runs/{run_id}/events/history` - exists for reconnects that lost their cursor.

Event types (stable snake_case): `run_created`, `state_changed`, `iteration_started`,
`plan_accepted`, `task_completed`, `task_failed`, `patch_rejected`,
`source_card_rejected`, `section_update_rejected`, `planner_failed`,
`planner_evaluated`, `loop_decision`, `checkpoint_reached`, `stop_requested`,
`user_decision`, `rollback_applied`, `continue_once_used`, `validation_missing`,
`run_completed`, `run_stopped`. Clients must ignore types they do not know.

## Design notes

Decisions worth knowing before you use the spec:

* **No Project table.** CONTRACTS v1 stores `project_id` on each run record, so a
  project is derived (distinct ids the owner has run, with counts and the latest
  state). `ProjectView` is therefore a *view*, and reserving a label before the first
  run is a spec-side addition marked `planned`.
* **Runs never carry article bodies.** `RunView` is polled every second while a run is
  live; it carries state, plan, decisions, the Q series and the stop report. Bodies are
  on `/article`; history and snapshots travel by hash and version.
* **The stop report is the UI's state machine.** `stop_report.options` lists exactly
  the buttons that are legal right now, so the console never hard-codes which decision
  applies.
* **Keys are write-only.** No response schema in this spec has a field that can carry
  key material; `AccountView` exposes a 12-character fingerprint and a `****1234` mask.
* **Ownership is 404, never 403.** A foreign id must be indistinguishable from a
  missing one.
* **Two shapes for "budget".** The account surface uses `BudgetPatch` (writable,
  nullable) while the contract is `MonthlyBudget`; they mirror each other but are not
  the same type. Called out below.

## CONTRACT-CHANGE-REQUESTS

Contracts are frozen at `contracts-v1.1`; nothing here edits them. These are the
changes this spec would ask for, with the reason the UI needs them:

1. **`ArticleSection` is a surface type, not a contract.** The article view sends
   `SectionState` plus the body text. Because `SectionState` is `extra=forbid`, the view
   cannot be an `allOf`; the spec flattens it and `verify.mjs` asserts it stays
   field-for-field equal. A first-class `ArticleSection` contract (or a `SectionBody`
   wrapper) would remove the duplicated schema.
2. **`StopReport` and `UserDecision` are not contracts.** Both appear on the wire
   (`RunView.stop_report`, the decision body, the SSE `checkpoint_reached` detail) but
   live in `app/orchestrator/stop.py`. Consequence: the spec repeats `LoopAction` as
   `LoopActionName` and `StopOption` as a surface enum. Promote both to contracts and
   the duplication disappears.
3. **One 422, two codes.** `contract_violation` (raised by services) and
   `validation_error` (raised by the FastAPI handler) are both 422 with different
   envelopes: the first is the standard `{code, message, context}`, the second adds
   `context.errors[]` with a *typed* shape. A client switching on `code` needs both;
   one code with one `context` schema would be simpler.
4. **`needs_user_input` is 409, not 422.** It is a state condition (`NeedsUserInput`),
   not a schema violation, and the spec keeps 409. Flagged because the API brief that
   asked for this spec expected 422.
5. **Agent bindings break the envelope.** `GET /api/agents/{role}/binding` raises a
   FastAPI `HTTPException`, so its 404 body is `{"detail": {"code", "message"}}` while
   every other error is `{"error": {...}}`. The spec documents both
   (`HttpExceptionEnvelope`), but the console would rather not special-case one route.
6. **`RunView` has no quality report.** The UI must show eight gate booleans and nine
   criteria scores; `RunView` carries only `q_series`/`best_q`. This spec adds a planned
   `GET /api/runs/{run_id}/quality`. The alternative - adding `latest_report:
   QualityReport | None` to `RunView` - is cheaper but makes every poll larger.
7. **There is no `Project` contract.** Project name/description cannot be stored
   anywhere today, so `ProjectView.name` is derived from the label. A `Project` record
   (id, name, description, owner, timestamps) would make the projects API real instead
   of a derivation over run records.
8. **`AccountView.budget` uses a surface round-trip.** `BudgetPatch` (both fields
   nullable, absent means unchanged) mirrors `MonthlyBudget` but is a different model.
   Reusing the contract plus `model_fields_set` on the wire would remove the pair.

## Provenance

Built as the API-design step of the ScholarLoop build (`docs/arena-log/step-10/` holds
the prompt and the raw output). Spec-only: no file outside `docs/openapi/` and
`docs/arena-log/` is changed, and CONTRACTS v1 is untouched.
