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
