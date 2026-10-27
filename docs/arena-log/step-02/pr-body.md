## Step

Step 02 — token governor and context builder (`backend/app/governor/`).

## Status / what this PR contains

The Step 02 implementation is already merged into `main`: PR **#12** "Governor: nested
budgets, token estimator, cache-friendly context, telemetry, tiers" (merge commit
`4ee6d0bd`, head `arena/53d3f269`). This branch is cut from `main` @ `b300a6e`, so the five
deliverables and their tests are present and green.

This PR adds the required `docs/arena-log/step-02/` artifact (raw prompt + raw output):

- files-read confirmations for every `backend/app/contracts/` module, `core/errors.py`,
  `providers/{base,errors,mock}.py`, `config/policies.yaml`, and the extra Step 0/1/5/6
  files (`accounts/`, `providers/pool.py`, `memory/`, `agents/`, `skills/loader.py`,
  `tools/registry.py`);
- a spec-by-spec audit of the five deliverables (all PASS);
- the test evidence.

No source change was needed or made: the merged implementation already satisfies the
spec line for line, and rewriting it would risk the green gate. `make check` was re-run on
this branch:

| Gate | Result |
| --- | --- |
| `ruff check backend` | all checks passed (176 files formatted) |
| `mypy --strict` | no issues in 119 source files |
| `skills lint` | 7 skill(s) OK |
| `pytest` | 974 passed, 8 skipped (skips are unrelated non-document contract round-trip cases) |
| `pytest -m governor` | 107 passed |
| `export_schemas --check` | 23 contract schemas are up to date |

## Deliverables (verified present, all PASS)

1. **budget.py** — nested `account-month -> run -> iteration -> call`; `precheck()` reserves
   and raises `BudgetExceeded(level, remaining, unit, limit, used, projected)`; `record()`
   settles atomically behind a `BudgetStore` (`InMemory`, atomic `File`); soft warning at 80 %,
   hard stop at 100 %; limits from `Policies` (project overrides) + `Account.monthly_budget`.
2. **estimate.py** — `TokenEstimator` uses `provider.count_tokens` when available, else
   `chars/4 + 15 %` margin; `reconcile()` pairs estimate with actual prompt usage and
   `report()` exposes bias / MAE / suggested margin.
3. **context_builder.py** — fixed `STABLE (cache=true) -> SEMI -> VOLATILE` layout; stable
   prefix is byte-identical per role in a run (`prefix_hash`); project rules add but never
   remove mandatory root rules (`MandatoryRuleGuard`); trim order digest -> skill bodies ->
   card notes -> `ContextTooLarge`; task and section text are never truncated.
4. **telemetry.py** — `CallRecord` (run, iter, task, role, model, in/out/cached, latency,
   est. cost); `tokens_by_role`, `tokens_by_iteration`, `cache_hit_ratio`, `TokenROI`
   (Q gained per 10k tokens).
5. **tiers.py** — `TierResolver` maps `Policies.model_tiers` -> concrete model via
   `DEFAULT_MODELS`, with per-agent override through `AgentBinding`.

## Tests (present and passing)

`test_governor_budget.py` (hierarchy, 20-thread atomicity, soft warning, release /
duplicate-settle, file store), `test_governor_context_builder.py` (layout + cache flags,
prefix byte-stability, mandatory-rule protection, trim order, schema tokens),
`test_governor_estimate.py` (15 % margin, provider fallback, drift),
`test_governor_telemetry.py` (role/iteration totals, cache-hit ratio, ROI arithmetic),
`test_governor_tiers.py` (tiers, overrides, errors), `test_governor_integration.py`
(context -> token -> ROI, no network, no key material).

## Depends on

- Step 0: contracts (`backend/app/contracts/`), `config/policies.yaml`, `LLMProvider`.
- Step 1: accounts (`Account.monthly_budget`), `providers/pool.py` usage records.
- Step 5/6 files are real: the builder codes against the real `SkillLoader` (not a
  Protocol); tool schemas exist via `tools/registry.py`.

## Contract changes

None applied. `backend/app/contracts/` is untouched and `docs/schemas/` is in sync. The
Step 02 HANDOFF requests are recorded under CONTRACT-CHANGE-REQUESTS in
`docs/arena-log/step-02/output.md` §4 and deliberately not applied here:

1. `budget_counters(counter_key PK, run_id, iteration, account_id, period, tokens, cost_usd)`
   table + migration (SQL-backed `BudgetStore` for multi-worker runs).
2. `Policies.output_token_cap` / per-tier default (the builder uses `DEFAULT_MAX_OUTPUT_TOKENS = 4096` today).
3. `tier_models: {provider: {tier: model_id}}` in `config/policies.yaml`.
4. Document that `LLMRequest.system_blocks` is ordered and `cache=true` blocks must form a prefix.
5. A `RunState` contract (`iter`, `brief`, `sections`, `sources`, `digest`, section-text view).
