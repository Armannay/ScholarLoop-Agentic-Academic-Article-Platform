# Step: runtime account store, bindings, live provider adapters

ROLE: Senior backend and security engineer.
Status: complete. Gate green (`make check`: 432 passed, 8 skipped; mypy --strict clean;
secret scan clean; contract schemas unchanged).

### SUMMARY

Any user can now add, rotate and delete provider accounts (API keys) at runtime for
the Claude Planner and for every agent, safely and with hot-swap. Keys are sealed with
AES-256-GCM envelope encryption (`APP_MASTER_KEY`), write-only through the API, and
resolved **at call time** from `AgentBinding` by `providers/pool.py` — so a key rotation
or account deletion mid-run is seen by the very next call while in-flight calls finish
on the provider they started with. Minimal auth (argon2 + JWT) and per-user ownership
(404 cross-user) protect the surface; every mutation lands in an audit log that can
never contain secrets. Live adapters (`AnthropicProvider`, `OpenAICompatibleProvider`)
sit behind the frozen `LLMProvider` protocol with jittered backoff, timeout handling
and full `ProviderError` mapping.

### DELIVERABLES

- `backend/app/accounts/` — `crypto.py` (envelope encryption, `rotate_master_key`,
  masked display, `KeyRedactionFilter`), `models.py` (users, accounts,
  agent_bindings, account_usage_daily, audit_log), `db.py`, `auth.py` (argon2 + JWT),
  `schemas.py` (surface models; `SecretStr` request keys), `service.py` (CRUD, delete
  protection, bindings incl. planner rule, usage sink, audit, resolver).
- `backend/app/api/` — routers `auth.py`, `accounts.py`, `agents.py`; deps (session,
  bearer auth, services, pool); main wiring (engine, pool, key-redaction filter).
- `backend/app/providers/` — `anthropic.py`, `openai_compatible.py`, `pool.py`
  (strategies, circuit breaker, hot-swap cache, usage records), `factory.py` extended
  with `build_provider(account, *, api_key)`.
- `backend/app/core/` — `errors.py` (+ `ConflictError` 409, `AuthError` 401),
  `pricing.py` (# VERIFY rate table for `est_cost`).
- `backend/app/migrations/` + `alembic.ini` — revision `0001_account_store`
  (matches `Base` exactly; `make migrate` / `make migration`).
- Tests: `test_accounts_crypto.py`, `test_accounts_auth.py`, `test_accounts_service.py`,
  `test_accounts_api.py`, `test_no_key_leak.py`, `test_providers_adapters.py` (respx),
  `test_provider_pool.py`, `test_migrations.py` (84 new tests).

### VERIFICATION

- `make check` green: ruff + format, `mypy --strict`, **432 passed, 8 skipped**.
- `secret_scan` clean; `make schemas-check` green (contracts untouched).
- Tests cover: encryption round-trip + rotation, CRUD allowed/denied (incl. 404
  cross-user), delete protection (409 / replacement / disable_roles), a full-lifecycle
  test asserting **no key in any response or log line**, respx-mocked adapter request
  shapes / usage mapping / error mapping / jittered backoff, hot-swap mid-run,
  failover, circuit-breaker open + half-open recovery, usage emission, migration
  upgrade/downgrade parity with the models.
- Live smoke against uvicorn: register → add account (Console-key shape) → Pro/Max
  token rejected → planner binding → runtime key rotation → delete 409 →
  disable_roles delete → audit trail correct, zero key material in the server log.

### ASSUMPTIONS

- Master key env is `APP_MASTER_KEY` (aliases `SCHOLARLOOP_MASTER_KEY` /
  `SCHOLARLOOP_SECRET_KEY`); without it the API boots with `account_store_ready=false`
  and account ops fail with a config error instead of the process dying.
- JWT secret comes from `APP_JWT_SECRET`, else is derived from the master key with
  domain separation (never the raw KEK bytes); tokens are HS256, 12h TTL.
- Contracts are frozen: API shapes live in `app/accounts/schemas.py`; requests for
  contract edits are listed under HANDOFF and were **not** applied.
- Delete semantics: `disable_roles=true` leaves a sole binding disabled with its
  account id still recorded (ids may dangle; call-time resolution raises
  `NoAccountAvailable`), matching "bindings hold ids, never rows". Removal from a
  multi-account binding that would break a 2-account strategy relaxes it to
  `dedicated` (audited).
- Cross-user access answers **404**, not 403, so the API never confirms another
  user's account ids exist.
- `TokenUsage.cached` counts cache *reads* only; cache *creation* is folded into
  `in_` at full rate (adapter-level normalisation). `est_cost` uses `core/pricing.py`
  rates marked `# VERIFY`; unknown models record tokens at cost 0.
- The Planner must use a Claude **Console** API key: `validate_api_key` accepts
  `sk-ant-api…` and rejects `sk-ant-oat…` (Pro/Max subscription tokens); planner
  bindings accept only `anthropic`/`mock` accounts.
- `alembic upgrade head` is the production DDL; `init_db` (create_all) remains as the
  dev/test bootstrap and is checked against the migration in tests.

### HANDOFF

Public interfaces other steps rely on:

- **Run-loop entry point**: `app.providers.pool.ProviderPool.generate(request, *,
  role, user_id, model=None) -> LLMResponse` (roles include `planner`), plus
  `evict(account_id)`, `aclose()`, `breaker` (`CircuitBreaker`, `BreakerState`).
  Fails with `NoAccountAvailable` (user condition, 409) or `ProviderError` subclasses.
- **Protocols** (implemented by `app.accounts.service`): `AccountResolver`
  (`load_for_call(account_id) -> ResolvedAccount{account, api_key}`,
  `get_binding(user_id, role)`, `set_account_status(...)`), `UsageSink.emit(UsageRecord)`.
  `UsageRecord` fields map 1:1 onto `account_usage_daily`.
- **Factory**: `build_provider(account, *, api_key=None) -> LLMProvider` — the only
  place a stored account becomes a token-spending object. `MockProvider` ignores the
  key; live adapters require it.
- **Adapters**: `AnthropicProvider(api_key, ...)`, `OpenAICompatibleProvider(api_key,
  base_url=...)`, both implementing the frozen `LLMProvider` protocol
  (`generate/count_tokens/aclose`). Vendor-uncertain fields are marked `# VERIFY`
  (anthropic-version pin, cache_control shape, forced-tool JSON, default model ids,
  `prompt_tokens_details.cached_tokens`).
- **Account store services** (`app.accounts.service`): `AccountService`
  (`create/get/get_row/list_for_owner/patch/delete/test/decrypt_key/resolve_for_call`),
  `BindingService` (`get/put/to_contract`), `UserService`, `AuditService`,
  `SqlAccountResolver`, `SqlUsageSink`. `app.accounts.crypto`:
  `EnvelopeEncryptor(encrypt/decrypt)`, `rotate_master_key(session, new_key=...)`,
  `mask_secret`, `register_secret/forget_secret`, `install_key_redaction`,
  `KeyRedactionFilter`. `app.accounts.auth`: `create_access_token/decode_access_token/
  hash_password/verify_password/AuthContext`.
- **REST**: `POST/GET /api/auth/register|login`, `GET /api/auth/me`;
  `POST/GET/PATCH/DELETE /api/accounts`, `POST /api/accounts/{id}/test`
  (delete query: `replacement_account_id`, `disable_roles`); `GET/PUT
  /api/agents/{role}/binding` (`role` = `BindingRole`, includes `planner`).
  Responses never carry key material (`key_fingerprint` + `key_mask` only).
- **Errors**: `core.errors` gains `ConflictError` (409, `code="conflict"`) and
  `AuthError` (401, `code="auth_error"`); the existing handler maps them unchanged.
- **DB** (alembic `0001_account_store`): `users(id,email,password_hash,created_at)`,
  `accounts(...,key_ciphertext,key_mask,budget_*,status,...)`,
  `agent_bindings(user_id,role,account_ids JSON,strategy,model,caps,enabled)`,
  `account_usage_daily(account_id,date,role,model,tokens_in,tokens_out,tokens_cached,
  est_cost)`, `audit_log(actor,action,target,ts,detail[redacted])`.
- **Env**: `APP_MASTER_KEY` (required for the store), `APP_JWT_SECRET`,
  `APP_JWT_TTL_SECONDS` (optional).

CONTRACT-CHANGE-REQUESTS (not applied — contracts untouched):

1. `AccountPublic`: add `key_mask: str` ("****" + last 4) so every consumer gets the
   masked display hint without the `AccountView` surface fork (which will keep it
   until then).
2. `AgentBinding`: add optional `updated_at` for optimistic-concurrency checks on
   `PUT /agents/{role}/binding`.
3. `TokenUsage`: decide whether `est_cost` belongs in the contract or stays
   external; it is currently computed in `app/core/pricing.py` (# VERIFY rates) and
   persisted only in `account_usage_daily`.
