# Step 02 — Token governor and context builder (raw output)

Branch: `arena/7c6aa227-scholarloop-agentic-academic-a` (from `main` @ `b300a6e`).
Date: 2026-10-27.

## 0. Provenance / state observed

- `git rev-parse HEAD` = `b300a6e33fd48f3c18bd069f006d1dd862d573c8` (== `origin/main`, clean tree).
- The Step 02 change is already merged into `main`:
  - GitHub PR **#12** "Governor: nested budgets, token estimator, cache-friendly context,
    telemetry, tiers" (head `arena/53d3f269`, merged `2026-10-23T19:10:48Z`, merge commit
    `4ee6d0bd`), which added exactly `backend/app/governor/{budget,estimate,context_builder,telemetry,tiers}.py`,
    `backend/tests/test_governor_*.py`, and the `governor` pytest marker.
- Therefore the working tree already contains the full Step 02 implementation. This log
  records the verification pass rather than a from-scratch rewrite: the implementation was
  audited line-by-line against every spec bullet, `make check` was re-run, and nothing that
  already satisfies the spec was changed.

## 1. Required files read (confirmed, one line each)

Contracts (`backend/app/contracts/`):
- `__init__.py` — CONTRACTS v1 package exports; bottom of the dependency graph.
- `account.py` — `Account` / `AccountPublic` / `MonthlyBudget` / `key_fingerprint`.
- `agent.py` — `AgentResult` (report-only validator; patch/target consistency).
- `binding.py` — `AgentBinding` (account_ids, strategy, model, caps, enabled).
- `brief.py` — `Brief` (article_type, citation_style, must_include/avoid).
- `common.py` — `Contract` / `VersionedContract`, `word_count`, annotated id/word types.
- `enums.py` — closed vocabularies incl. `BindingRole`, `ModelTier`, `AgentRole`.
- `ledger.py` — `ChangeEntry`, `TokenUsage` (non-overlapping in/out/cached), `EntryRole`.
- `llm.py` — `LLMRequest` / `LLMResponse` / `SystemBlock` (the `cache` hint) / `LLMMessage`.
- `loop.py` — `LoopDecision` (stable snake_case reasons).
- `patch.py` — `Patch` / `PatchOp` (payload rules; whitespace is payload).
- `plan.py` — `Plan` (acyclic graph, unique ids, required roles).
- `quality.py` — `QualityReport` (all 8 gates required), `QualityIssue`, `GATE_NAMES`.
- `registry.py` — `CONTRACTS` name->model map, `json_schema_for`.
- `section.py` — `SectionState`, `CitationRef`.
- `source.py` — `SourceCard` (<=60-word snippet, DOI normalise, locator required).
- `task.py` — `Task` (single role, `token_budget`, `source_ids`).
- `verdict.py` — `Verdict` (meets_minimum / recommendation coherence).

Core / providers:
- `backend/app/core/errors.py` — `ScholarLoopError` family incl. `BudgetExceeded` (code
  `budget_exceeded`, HTTP 429).
- `backend/app/providers/base.py` — `LLMProvider` Protocol (`generate`/`count_tokens`/`aclose`),
  `estimate_tokens` = `ceil(chars/4)`, `prompt_tokens`, `cached_prompt_tokens`.
- `backend/app/providers/errors.py` — provider error taxonomy + `retryable` class facts.
- `backend/app/providers/mock.py` — `MockProvider` (scripted replies, injected failures, exact
  `count_tokens`, `total_tokens()`).
- `config/policies.yaml` — `run_token_cap`, `iteration_token_cap`, `call_input_cap`,
  `model_tiers` (planner strong, validator/compactor cheap), plateau/regression/etc.

Extra files (Step 0/1/5/6 surface the governor codes against):
- `backend/app/accounts/` — `auth.py`, `crypto.py`, `db.py`, `models.py`, `schemas.py`,
  `service.py` (encrypted keys, `Account.monthly_budget`, `SqlAccountResolver`, usage sink).
- `backend/app/providers/pool.py` — `ProviderPool` (per-call account resolution, strategies,
  circuit breaker, `UsageRecord`); `factory.py`, `anthropic.py`, `openai_compatible.py`.
- `backend/app/memory/` — `ledger.py`, `sources.py`, `compactor.py`, `digest.py`, `errors.py`.
- `backend/app/agents/` — `base.py` (`Governor` Protocol + `precheck` seam), `context.py`
  (`RunState`, `DefaultContextBuilder`), `binding.py`, `registry.py`, `governor.py`
  (Step-5 `CallBudgetGovernor`), `pool.py`, `roles/*`, `errors.py`.
- `backend/app/skills/loader.py` — real Step 5 `SkillLoader` (`index`/`render_index`/`select`)
  used by the governor instead of a Protocol.
- `backend/app/tools/registry.py` — real Step 6 `build_tool_registry`.

## 2. Deliverable audit (spec bullet -> implementation -> status)

| # | Spec | Where | Status |
|---|------|-------|--------|
| 1 | nested budgets account-month -> run -> iteration -> call | `budget.py` `BudgetLevel`, `LEVEL_ORDER`, `BudgetScope` | PASS |
| 1 | `precheck(estimate)` raises `BudgetExceeded(level, remaining)` | `BudgetGovernor.precheck`, `_exceeded` | PASS |
| 1 | `record(usage)` atomic + persisted | `record` (store lock, refund + settle), `BudgetStore`/`InMemory`/`File` | PASS |
| 1 | soft warn 80 %, hard stop 100 % | `DEFAULT_SOFT_WARN`, `LevelUsage.is_soft_warning`/`over_limit`, `_emit_warnings` | PASS |
| 1 | limits from policies.yaml (+ project overrides) + per-account | `BudgetLimits.from_policies`, `Account.monthly_budget`, `for_run` | PASS |
| 2 | provider.count_tokens when available | `TokenEstimator.estimate`, `_count_tokens` | PASS |
| 2 | else chars/4 + 15 % margin | `margined = raw + ceil(raw * 0.15)`, `DEFAULT_SAFETY_MARGIN` | PASS |
| 2 | reconcile estimate vs actual, expose drift | `reconcile`/`record`, `DriftReport` (bias, MAE, suggested margin) | PASS |
| 3 | fixed STABLE -> SEMI -> VOLATILE layout, `cache=true` prefix | `ContextBuilder.plan`, `_stable_blocks`, `_skill_blocks`, `_card_blocks`, `_volatile_text` | PASS |
| 3 | project rules add, never remove mandatory | `MandatoryRuleGuard`, `mandatory_rules` | PASS |
| 3 | prefix byte-identical per role in a run | `prefix_hash`, stable blocks exclude volatile data | PASS |
| 3 | trim order digest -> skills -> card notes -> `ContextTooLarge` | `TRIM_ORDER`, trim loop | PASS |
| 3 | never truncate task / section text | raise instead; tests `..._never_cuts_the_task`, `..._refused_not_truncated` | PASS |
| 4 | per-call record incl. in/out/cached/latency/est cost | `CallRecord` / `from_response` | PASS |
| 4 | tokens by role & iteration; cache-hit ratio; ROI | `tokens_by_role`, `tokens_by_iteration`, `cache_hit_ratio`, `TokenROI.per_10k` | PASS |
| 5 | resolve model by tier from policies.yaml | `TierResolver.tier_for`, `DEFAULT_MODELS` | PASS |
| 5 | per-agent override via `AgentBinding` | `resolve(binding=...)` -> `BINDING_OVERRIDE` | PASS |

### TESTS (all present and passing)
`pytest -m governor` -> 107 passed:
- budget hierarchy: `test_account_month_budget_is_the_outer_scope`,
  `test_limits_reject_inconsistent_nesting`, `test_limits_from_policies_and_account_budget`.
- atomicity under concurrency: `test_atomicity_under_concurrency` (20 threads).
- trim order: `test_trim_order_is_monotone_and_never_cuts_the_task`,
  `test_digest_is_trimmed_first_and_keeps_the_newest_detail`,
  `test_skill_bodies_go_but_the_index_stays`, `test_source_card_notes_are_dropped_...`.
- prefix byte-stability: `test_stable_prefix_is_byte_identical_across_tasks`,
  `test_prefix_hash_matches_the_stable_text`, `test_stable_prefix_changes_with_the_role`.
- estimator margin: `test_heuristic_applies_the_fifteen_percent_margin`,
  `test_provider_tokeniser_wins_and_needs_no_margin`, `test_drift_report_bias_and_suggested_margin`.
- ROI calculation: `test_token_roi_is_q_gained_per_ten_thousand_tokens`,
  `test_token_roi_can_be_restricted_to_an_iteration_window`.
- mandatory-rule protection: `test_project_rules_may_not_remove_a_mandatory_rule`,
  `test_guard_rejects_a_composition_that_lost_a_rule`, `test_root_rule_items_are_all_present_in_the_prefix`.

## 3. Raw output

### `make check` (see `make-check.txt`)
```
ruff check backend            -> All checks passed!
ruff format --check backend   -> 176 files already formatted
mypy                          -> Success: no issues found in 119 source files
skills lint                   -> 7 skill(s) OK
pytest                        -> 974 passed, 8 skipped, 1 warning in 21.80s
EXIT=0
```
(The 8 skips are contract round-trip tests for non-document-level contracts, unrelated to Step 2.)

### `pytest -m governor` + schema sync (see `governor-tests.txt`)
```
107 passed, 875 deselected in 1.29s
export_schemas --check -> 23 contract schemas are up to date in docs/schemas
```

## 4. CONTRACT-CHANGE-REQUESTS

None needed for this step. `backend/app/contracts/` is untouched and `docs/schemas/` is in
sync. The requests the Step 02 HANDOFF already raised (recorded, deliberately not applied
here, see `docs/CONTRACT_CHANGELOG.md` and PR #12):
1. `budget_counters(counter_key PK, run_id, iteration, account_id, period, tokens, cost_usd)`
   table + migration so `BudgetStore` can be SQL-backed (the shipped JSON store is
   single-process).
2. `Policies.output_token_cap` / per-tier default so the builder has a completion ceiling
   when no `AgentBinding` exists (today `DEFAULT_MAX_OUTPUT_TOKENS = 4096`).
3. `tier_models: {provider: {tier: model_id}}` in `config/policies.yaml` (vendor ids currently
   live in `tiers.DEFAULT_MODELS` behind `# VERIFY`).
4. Document that `LLMRequest.system_blocks` is ordered and `cache=true` blocks must form a
   prefix (prompt caching depends on it).
5. A `RunState` contract (`iter`, `brief`, `sections`, `sources`, `digest`, section-text view).
