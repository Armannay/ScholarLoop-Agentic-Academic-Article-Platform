# Step 10 - raw output

Captured 2026-10-27T16:07:30Z on Linux 6.1.158+ x86_64, node v22.22.3, python 3.11.2.

Every command below was run from a clean checkout of this branch, in this order.

## 1. `cd docs/openapi && npm install`

```
added 241 packages, and audited 242 packages in 9s   # (EBADENGINE warnings only: Prism asks for node >=24, runs on 22)
```

## 2. `npm run lint` (Redocly, OpenAPI 3.1)

```
validating openapi.yaml using lint rules for api 'scholarloop-ui@v1'...
openapi.yaml: validated in 182ms

Woohoo! Your API description is valid. 🎉

```

## 3. `npm run verify` (ajv 2020-12 over every example, contract schemas included)

```

✔ OpenAPI spec verified

  spec: 36 operations, 134 examples validated, 6 SSE frames, 69 example files referenced
  ArticleSection = SectionState + text (field-for-field)
  scenario:completion: 36 operations, 136 examples validated, 6 SSE frames
  scenario:erosion-stop: 36 operations, 137 examples validated, 6 SSE frames
  scenario:needs-input: 36 operations, 139 examples validated, 6 SSE frames

```

## 4. `npm run client:check` (openapi-typescript + tsc --noEmit)

```

✔ Typed client regenerated and typechecked

  schemas:    108 components
  operations: 36 operation types
  operations with no request body: 27
  output:     generated/api-schema.d.ts (git-ignored)

```

## 5. `make check` (ruff, mypy --strict, skill lint, pytest - the repository gate)

```
.venv/bin/ruff check backend
All checks passed!
.venv/bin/ruff format --check backend
176 files already formatted
.venv/bin/mypy
Success: no issues found in 119 source files
.venv/bin/python -m app.skills.lint --skills-dir skills
ok   abstract-writing          283/400 tokens (ceiling 400)
...
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
974 passed, 8 skipped, 1 warning in 22.35s
```

## 6. Mock smoke test - `node mock/server.mjs completion --port 4020`

Started with one command; responses are the spec's own examples. Prism enforces the bearer scheme, so a token (any token) is required on authenticated operations.

```
$ curl -s localhost:4020/api/health
{"status":"ok","version":"0.9.0","contract_version":1,"env":"local","policies_loaded":true,"rubric_loaded":true,"providers":{"mock":true,"live_adapters_implemented":[],"accounts_configured":2,"live_providers_enabled":false,"account_store_ready":true},"config_errors":{}}

$ curl -s -H 'Authorization: Bearer any-token' localhost:4020/api/runs/$RID
{
  "state": "CHECKPOINT",
  "checkpoint_kind": "completion",
  "q_series": [
    54,
    63,
    71,
    78
  ],
  "best_q": 78
}
stop_report.options = ['finish', 'continue', 'rollback']

$ curl -s -H 'Authorization: Bearer any-token' localhost:4020/api/runs/$RID/quality
Q = 78  gates passed = 8 / 8  series = [54, 63, 71, 78]

$ curl -s -H 'Authorization: Bearer any-token' -X POST .../decision -d '{"option":"finish"}'
state = DONE

$ curl -sN .../events?once=true
id: 0
event: run_created
data: {"seq":0,"ts":"2026-10-26T20:14:03Z","type":"run_created","state":"INTAKE","iter":0,"detail":{"kind":"brief","project_id":"peer-review-2026","on_complete":"ask"}}```

The other two scenarios were exercised the same way (each on its own port):

```
erosion-stop: state=STOPPED_EROSION   q_series=[72, 74, 73, 72]
              stop_report.action=STOP_EROSION  options=[rollback, edit_goals, add_budget, continue_anyway]
              best_snapshot_id points at label "iter-1-q74"

needs-input:  state=CHECKPOINT  checkpoint_kind=needs_input  action=ASK_USER  options=[continue, rollback]
              Prefer: code=422 -> {"error":{"code":"validation_error","message":"body.note: Field required",...}}
              POST decision {option: continue, note: ...} -> state=REPLANNING running=true
```

## 7. Follow-up: the `runs/` examples were silently ignored, then rebuilt

The first commit of this step did not contain `docs/openapi/examples/runs/`: the
repository's `.gitignore` excludes local runtime state with a bare `runs/` rule, which
also matched that directory, so `git add -A` skipped all 21 files while the spec kept
`$ref`-ing them. `npm run verify` caught it the moment it ran against a fresh checkout
(`example file is missing: ./examples/runs/...`).

Fix, in two parts:

* `.gitignore` now re-includes the one directory that is a deliverable:
  `!docs/openapi/examples/runs/`;
* the 21 files were regenerated from the single story the rest of the examples tell
  (run `01M5Z0X96Y000000000000005H`, Q 54 → 63 → 71 → 78, 862 body words, 59 910
  tokens, best snapshot `…61` = `iter-3-q78`), with the brief and the plan reused
  verbatim from `examples/scenarios/needs-input/run.json` so the two cannot drift.

Re-run of the three gates after the fix:

```
$ cd docs/openapi && npm run check

validating openapi.yaml using lint rules for api 'scholarloop-ui@v1'...
openapi.yaml: validated in 205ms

Woohoo! Your API description is valid. 🎉

✔ OpenAPI spec verified

  spec: 36 operations, 134 examples validated, 8 SSE frames, 69 example files referenced
  ArticleSection = SectionState + text (field-for-field)
  scenario:completion: 36 operations, 136 examples validated, 8 SSE frames
  scenario:erosion-stop: 36 operations, 137 examples validated, 8 SSE frames
  scenario:needs-input: 36 operations, 139 examples validated, 8 SSE frames

✔ Typed client regenerated and typechecked

  schemas:    108 components
  operations: 36 operation types
  operations with no request body: 27
  output:     generated/api-schema.d.ts (git-ignored)
```

(The frame count moved from 6 to 8 because two of the six SSE examples now show a
two-frame transition instead of one frame; the file count - 69 referenced, 0 missing -
is the number that mattered.)

Mock smoke test against the regenerated fixtures, `node mock/server.mjs completion
--port 4030`, `Authorization: Bearer <any>`:

```
GET  /api/runs/01M5Z0X96Y000000000000005H          -> 200 CHECKPOINT completion, q_series [54,63,71,78]
GET  /api/runs/01M5Z0X96Y000000000000005H/quality  -> 200 Q=78, 8/8 gates
GET  /api/runs/01M5Z0X96Y000000000000005H/ledger   -> 200 9 entries, orchestrator entry last
GET  /api/runs/01M5Z0X96Y000000000000005H/events?once=true
                                                   -> id: 0 / event: run_created / data {...}
POST /api/runs/01M5Z0X96Y000000000000005H/decision -> 200 DONE
```

And the repository gate, unchanged by this step's scope (no backend file is touched):

```
$ make check
ruff check backend        All checks passed!
ruff format --check       176 files already formatted
mypy                      Success: no issues found in 119 source files
skills lint               7 skill(s) OK in skills
pytest                    974 passed, 8 skipped
```
