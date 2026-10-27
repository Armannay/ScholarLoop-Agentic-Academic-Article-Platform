# Step 10a — Login/register + Accounts + Agents UI (frontend)

**Step:** 10a — login/register; Accounts (list with masked keys, add, edit and rotate
key, delete with the 409 replacement flow, Test connection, budget fields, status
badge); Agents (planner plus the 5 roles: choose account(s), strategy
dedicated/round_robin/failover, model, max tokens, enable/disable).

**Depends on:** Step 10 (OpenAPI 3.1 spec for Projects/Runs/Article/Usage/Errors,
`docs/openapi/openapi.yaml`, read-only here) and Step 1 (auth: argon2 + HS256 JWT,
`AuthContext`, `CurrentUserDep`, `users` table). The backend accounts/bindings API
(`/api/auth/*`, `/api/accounts/*`, `/api/agents/{role}/binding`) is consumed exactly as
specified — no endpoints invented. Branch point is main at `63ada01` (PR #24 merged).

**Contract changes:** **none.** No file under `backend/app/contracts/` was edited,
`docs/schemas/` is untouched (`make schemas-check` still passes), and
`docs/openapi/openapi.yaml` was read, not modified. The UI never crosses an agent,
ledger or provider boundary — it only renders redacted account views
(`AccountPublic`: 4-char mask + 12-char fingerprint, never key material). No
CONTRACT-CHANGE-REQUEST is required (see `spec-change-requests.md`).

## What this adds (already present and green on this branch)

**Auth** — `AuthPage.tsx`, `auth.tsx` (`AuthProvider`): login/register tabs, email
canonicalized to lowercase, password >= 8 chars on register, demo sign-in in mock
mode, session restore via `GET /api/auth/me`, 401 signs the user out, unauthorized
listener clears the token.

**Accounts** — `AccountsPage.tsx`:
- List with masked key (`****<last4>`) + 12-char ciphertext fingerprint, status badge
  (ok/warn/bad per active/degraded/disabled), monthly budget fields (tokens
  1,000–1,000,000,000 / USD ≤ 1,000,000, blank = unlimited).
- Add account: provider (anthropic / openai_compatible / mock), label, write-only API
  key, base_url (required only for openai_compatible), budget.
- Edit / rotate key: optional new key re-encrypts with a fresh DEK and rotates the
  fingerprint; relabel, status, budget changes supported.
- Delete: protected 409 when the account is the only candidate of an enabled role
  binding; resolved via `?replacement_account_id=` (swap roles to the replacement,
  planner requires anthropic or mock) or `?disable_roles=true` (disable affected
  roles, then delete).
- Test connection: one probe call; reports ok/model/latency or the error code without
  echoing the key.

**Agents** — `AgentsPage.tsx` + `RoleBindingCard`:
- Planner (planning & evaluation only) plus architect, researcher, writer, reviewer,
  validator, and optional compactor.
- Per role: choose account(s) (planner restricted to anthropic/mock), strategy
  dedicated / round_robin / failover (multi-account strategies require ≥2 accounts),
  model id, max input tokens (1,000–2,000,000), max output tokens (64–64,000, ≤ input),
  enable/disable toggle; status pill; per-role quick-bind defaults from policy tiers;
  account ordering for multi-account strategies.

**Supporting** — `api.ts` (typed client + SSE run-event reader), `types.ts`,
`mockServer.ts` (OpenAPI-conformant stateful mock used by UI tests and preview), `useApi.ts`,
`App.tsx` (hash-routed accounts/agents/status shell), `StatusConsole`, `StatusPill`.

**Tests** — `frontend/src/__tests__/app.test.tsx` (6 tests): OpenAPI spec completeness
(write-only `api_key`, all required paths), register/login/me, duplicate 409, 401
unauthorized listener, account CRUD + masking + rotation + budgets + key validation +
test probe, planner provider rules, multi-account strategies, 409 delete replacement
and disable flows, SSE streaming, page rendering without secrets.

## Tests

`make check` is green:

```text
ruff check + ruff format --check   -> clean
mypy --strict (backend/app)        -> Success: no issues found in 138 source files
skill-lint                         -> 7 skill(s) OK
pytest                             -> 1145 passed, 8 skipped
```

Frontend:
```text
npm run lint (tsc --noEmit)        -> clean
npm test                           -> 6 pass, 0 fail
```

An end-to-end smoke test against a real backend (:8000) with the frontend proxying
`/api` (:5173) exercised register → login → create account with budget → rotate key
→ test probe → bind planner+writer → delete 409 → replacement swap → disable roles;
all flows returned the expected codes and redacted views.

## Known deltas / follow-ups

- The live Anthropic probe returns `provider_unavailable` (no real key/network in this
  environment) — expected; MockProvider and the mock-server tests cover the happy path.
- A temporary `.env` + `scholarloop.sqlite3` used for the live smoke test were removed
  afterward; both are git-ignored.
