# Step 10a output record

This is the raw output of the work performed for step 10a on branch
`arena/bbd5048d-scholarloop-agentic-academic-a`, branched from main at
`63ada01` (PR #24 merged). The work confirmed step 10a is fully implemented and
green on this branch; no source changes were required.

## 1. Required-file read confirmation (one line each)

All of the following were read (not modified):
- backend/app/contracts/__init__.py, account.py, agent.py, binding.py, brief.py,
  common.py, enums.py, ledger.py, llm.py, loop.py, patch.py, plan.py, quality.py,
  registry.py, section.py, source.py, task.py, verdict.py
- backend/app/core/errors.py
- backend/app/providers/base.py, errors.py, mock.py
- config/policies.yaml

CONTRACTS v1 is frozen (tag <contracts-v1.1>): no contract file was edited.

## 2. Repo state found on this branch

Step 10a is already complete on `arena/bbd5048d-scholarloop-agentic-academic-a`
(merged via PR #24 at the branch point):

Frontend (`frontend/src/`):
- `AuthPage.tsx` — login/register tabs, email canonicalized to lowercase, password
  >= 8 chars on register, demo sign-in in mock mode, Argon2id + JWT bearer auth,
  `AuthProvider` restores session via `/api/auth/me`, 401 signs the user out.
- `AccountsPage.tsx` — list accounts with 4-char masked key + 12-char fingerprint,
  status badge (ok/warn/bad), monthly budget fields (tokens 1,000–1,000,000,000,
  USD ≤ 1,000,000), add account (provider/label/key/base_url/budget), edit &
  rotate key (optional new key rotates fingerprint), delete with 409 replacement
  flow (`replacement_account_id` or `disable_roles=true`), Test connection probe,
  per-row probe results.
- `AgentsPage.tsx` — planner + 5 executing roles (architect, researcher, writer,
  reviewer, validator) + optional compactor; choose account(s), strategy
  dedicated/round_robin/failover, model, max input/output tokens, enable/disable;
  status pill, per-role quick-bind defaults, account ordering for multi-account
  strategies.
- Supporting: `api.ts`, `auth.tsx`, `types.ts`, `mockServer.ts`, `useApi.ts`,
  `App.tsx`, `StatusConsole`, `StatusPill`, `openapi.json`.
- Tests: `frontend/src/__tests__/app.test.tsx` — 6 tests covering OpenAPI spec
  completeness (write-only keys, all required paths), auth flows (register/login/me,
  duplicate 409, 401 unauthorized listener), account CRUD + masking + rotation +
  budgets + key validation + test probe, planner provider rules, multi-account
  strategies, 409 delete replacement/disable flows, SSE run events, and page
  rendering without secrets.

Backend (`backend/app/`): auth router, accounts router, agents bindings router,
encrypted account store (AES-256-GCM), argon2 + JWT, service layer, providers pool
with dedicated/round_robin/failover strategies, MockProvider, API error envelope
`{code, message, context}` with 409 conflict for the delete-protection flow.

## 3. Verification run

Backend gate (`make check`):
- ruff check backend -> All checks passed!
- ruff format --check backend -> 204 files already formatted
- mypy --strict -> Success: no issues found in 138 source files
- skill-lint -> 7 skill(s) OK
- pytest backend/tests -> 1145 passed, 8 skipped

Frontend gate:
- npm run lint (tsc --noEmit) -> clean
- npm test -> 6 tests, 1 suite, 6 pass, 0 fail

End-to-end smoke test against a live backend (:8000) with the frontend proxying
/api (:5173):
- register -> login -> /me all 200; email canonicalized
- create Anthropic account with budget: key_mask `****1234`, 12-char fingerprint,
  key never echoed in any response
- rotate key on PATCH: fingerprint changes, old key gone
- Test connection probe: ok when active; 409 `no_account_available` when disabled
- bind planner + writer to the account (dedicated, claude-sonnet-4)
- DELETE account -> 409 `conflict` with `context.roles = ["planner","writer"]`
- DELETE with `?replacement_account_id=<other>` -> roles swapped to replacement
- DELETE with `?disable_roles=true` -> roles disabled, account deleted

## 4. Contract changes

None. No file under `backend/app/contracts/` was edited; `docs/schemas/` untouched
(`make schemas-check` passes); `docs/openapi/openapi.yaml` read, not modified.
See `spec-change-requests.md`.

## 5. Cleanup note

A temporary `.env` (APP_MASTER_KEY) and `scholarloop.sqlite3` were created solely
for the live smoke test and removed afterward so the secret-scan test
(`test_no_env_file_is_committed`) and the tree stay clean. Both paths are
git-ignored anyway.

## 6. Follow-ups / known deltas

- The frontend consumes the already-designed `docs/openapi/openapi.yaml` paths; no
  new endpoints were invented. Any future OpenAPI change to these paths goes through
  CONTRACT-CHANGE-REQUEST / spec-change-requests.
- The live Anthropic probe returns `provider_unavailable` (no real key/network in
  this environment) — expected; the MockProvider path and the mock-server tests cover
  the happy path.
