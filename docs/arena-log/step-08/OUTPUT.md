# Step 8 - raw output

Captured 2026-10-27 on Linux, python 3.11, `MockProvider` only (no network, no keys).
Every command below was run from the repository root on this branch.

## 1. Read confirmations (one line each)

| Path | Confirmation |
| --- | --- |
| `backend/app/contracts/` (18 modules) | read: `common` (ULID/slug/word-bound types, `extra="forbid"`, `schema_version=1`), `enums` (closed vocabularies, `ORCHESTRATOR_ROLE` deliberately outside `AgentRole`), `plan` (DAG/duplicate/dangling checks, `REQUIRED_ROLES`), `task` (one required role, `acceptance` non-empty, `token_budget`), `verdict` (the two recommendation/precedence validators), `quality` (the 8 gate names, `Q` 0..100), `brief`, `section` (`EMPTY_SECTION_HASH`, status lifecycle), `source`, `patch`, `ledger`, `llm`, `loop`, `agent`, `account`, `binding`, `registry`. |
| `backend/app/core/errors.py` | read: `ScholarLoopError` + `code`/`http_status`, `ConfigError`/`PolicyError`/`RubricError`, `ContractViolation`, `BudgetExceeded`, `NeedsUserInput`, `format_validation_errors`. |
| `backend/app/providers/base.py` | read: `LLMProvider` Protocol (`generate`/`count_tokens`/`aclose`), `estimate_tokens` = `ceil(chars/4)`, `prompt_tokens`, `cached_prompt_tokens`, `ProviderRouting`. |
| `backend/app/providers/errors.py` | read: `ProviderError` family with class-level `retryable`, `NoAccountAvailable` -> 409. |
| `backend/app/providers/mock.py` | read: scripted `(role, call index)` replies, `Script`/`fail_next`, failure modes `timeout`/`rate_limit`/`malformed_json`, deterministic usage, no transport and no credential field. |
| `config/policies.yaml` | read: `call_input_cap 24000`, `digest_max_tokens 2000`, `q_min 70`, plateau/regression/oscillation/diminishing knobs, `model_tiers.planner: strong`. |
| `backend/app/memory/` (digest format) | read: `DigestSnapshot` (`markdown`, `tokens`, `max_tokens`, `compact_at_tokens`), `DigestBuilder.build/abuild`, ledger window of 3 verbatim entries, compaction trigger at 80 %. |
| `backend/app/article/` | read: `store.py` (versioned sections, snapshots, `best_snapshot`), `patch.py` (four ops, `StalePatch`, bounded repair hints), `recurrence.py` (erosion signals), `assemble.py`. |
| `config/rubric.yaml` | read: 4 article types with per-section word ranges, abstract range, the 8 blocking gates, 9 soft criteria whose weights sum to 100, placeholder patterns. |
| `backend/app/core/rubric.py` | read: `load_rubric` invariant checks, `SectionSpec.target_words`, `resolve_sections(extra_titles=...)`, `slugify`. |
| `rules/root.rules.md` | read: the six "Never" rules and the "Always" list (patch op over rewrite, `AgentResult` only, ids over content, `needs_user_input`). |
| `backend/app/governor/context_builder.py` (Step 2 seam) | read: `ContextBuilder.plan(...) -> ContextPlan`, `GovernorRunState` Protocol, `RuleBundle`, `TrimStep` order, `ContextTooLarge`, `TASK_INSTRUCTION`. |
| `backend/app/agents/{base,binding}.py` (Step 2/5 seams) | read: `Governor`/`BindingResolver` Protocols, `parse_agent_json`, `MappingBindingResolver`; `planner` is a `BindingRole`, resolved per call. |

## 2. Audit of the brief against the tree as it arrived

`backend/app/planner/` already existed at `2bf4bd8` with all six deliverables. Each item
was checked against the file that implements it and the test that proves it.

| Brief item | Implementation | Test |
| --- | --- | --- |
| `plan`/`replan`/`evaluate` | `service.py` (`PlannerService.plan/replan/evaluate`) | `test_planner_service.py` (24) |
| exactly one valid role; no self-assignment | `plan_validator._task_problems` (`VALID_PLAN_ROLES`, explicit "the planner may not assign a task to itself") | `test_planner_may_not_assign_a_task_to_itself` |
| dependencies form a DAG | `plan_validator._topological_order` + `Plan._assert_acyclic` | `test_planner_plan_validator.py` cycle/self/dangling cases |
| acceptance criteria + `token_budget` per task | `_task_problems` | rejection matrix |
| sum `token_budget` <= remaining run budget | `_problems` (`budget.remaining_run_tokens`) | `test_planner_service.py::test_budget_overflow_is_rejected` |
| at most 8 tasks per iteration | `MAX_TASKS_PER_ITERATION = 8`, `PlanBudget.max_tasks` | `test_too_many_tasks_is_rejected`, `test_eight_tasks_are_allowed` |
| section ids exist or are created by an earlier architect task | `_section_problems` (topological order) | `test_section_ids_must_exist_or_be_created_by_an_earlier_architect`, `test_delta_cannot_orphan_a_dependency` |
| retry at most twice, error list only | `MAX_PLANNER_RETRIES = 2`, `retry_instruction(problems)` | `test_retry_gets_the_error_list_only`, `test_retry_budget_is_two`, `test_no_retry_when_retries_are_disabled` |
| compact view, never full text; <= 3 excerpts of <= 300 tokens; first/last 2 sentences | `probes.build_compact_view` (`MAX_EXCERPTS=3`, `MAX_EXCERPT_TOKENS=300`, `SENTENCES_PER_EDGE=2`) | `test_compact_view_reports_headings_counts_and_edge_sentences`, `test_compact_view_uses_at_most_three_excerpts_of_300_tokens`, `test_edge_sentences_are_capped` |
| `deep_check` sends the full text once | `probes.build_probe_report(deep_check=...)` + hash memory in `service._evaluation_payload` | `test_deep_check_is_opt_in_and_bounded`, `test_deep_check_is_sent_once_then_replaced_by_a_note` |
| gates cannot be overridden; a failing gate means `meets_minimum=false` | `evaluation.merge_verdict` (stricter-only merge) | `test_a_failing_gate_overrules_a_confident_finish`, `test_merge_only_ever_gets_stricter` |
| concrete `missing` list | `merge_verdict` names the gate and the probe detail | `test_model_missing_items_survive_the_merge`, `test_evaluation_problems_are_specific` |
| event-driven call policy, first plateau buys one `strategy_change` | `events.PlannerCallPolicy` | `test_first_plateau_asks_for_a_strategy_change_once`, `test_one_call_per_iteration`, `test_gate_newly_failed_requires_something_new`, `test_milestone_complete_is_delivered_once`, `test_empty_queue_is_a_planning_event`, `test_stop_actions_never_call_the_planner` |
| system prompt <= 700 tokens, JSON schema, `max_tokens` per call | `prompts.py` (`MAX_SYSTEM_PROMPT_TOKENS=700`, 4096/2048 output caps), `drafts.*_schema()` | `test_planner_service.py`, `test_planner_prompts.py` |
| model + account via the `planner` binding | `service._resolve_binding` per call | `test_binding_is_resolved_at_call_time`, `test_missing_binding_is_a_user_condition` |
| golden plans for 3+ sample briefs | `prompts/planner/plan.golden.json` | `test_golden_plan_for_each_brief` (all 4 article types), `test_golden_plan_passes_the_plan_validator` |

Gaps found, and what this step did about them:

1. **`prompts/planner/` did not exist.** Added the directory: `system.md` and
   `evaluator.md` (byte-for-byte copies of the code constants), short `plan.md`/
   `replan.md`/`evaluate.md` templates, the three golden answers and a `README.md`.
2. **The `ContextBuilder` dependency was the concrete governor class**, not a Protocol as
   the brief requires. Added `PlannerContextBuilder` (a `runtime_checkable` Protocol) in
   `app/planner/context.py`, and typed `PlannerContexts` and `PlannerPromptBuilder`
   against it. The governor's `ContextBuilder` satisfies it structurally, so no call site
   changed.
3. No test pinned the prompt assets to the code. Added
   `backend/tests/test_planner_prompts.py`.

## 3. Files added or changed

```
added      prompts/planner/README.md
added      prompts/planner/system.md            (== PLANNER_SYSTEM_PROMPT)
added      prompts/planner/evaluator.md         (== EVALUATOR_SYSTEM_PROMPT)
added      prompts/planner/plan.md              (template)
added      prompts/planner/replan.md            (template)
added      prompts/planner/evaluate.md          (template)
added      prompts/planner/plan.golden.json     (valid PlanDraft, 6 tasks)
added      prompts/planner/replan.golden.json   (valid PlanDeltaDraft, applies to the plan)
added      prompts/planner/evaluate.golden.json (valid EvaluationDraft, all 9 criteria)
added      backend/tests/test_planner_prompts.py (20 tests)
changed    backend/app/planner/context.py       (PlannerContextBuilder Protocol seam)
changed    backend/app/planner/__init__.py      (exports the Protocol)
changed    README.md                            (prompts/ in the layout, Step 8 status)
changed    docs/DECISIONS.md                     (ADR-0006 prompt assets pinned to code,
                                                  ADR-0007 context-builder Protocol)
added      docs/arena-log/step-08/PROMPT.md
added      docs/arena-log/step-08/OUTPUT.md
```

Asset sizes (`estimate_tokens`, `chars / 4`):

```
system.md                339 tokens   (ceiling 700)
evaluator.md             323 tokens   (ceiling 700)
plan.md                   99 tokens   (ceiling 200)
replan.md                128 tokens   (ceiling 200)
evaluate.md              145 tokens   (ceiling 200)
plan.golden.json         920 tokens
replan.golden.json       181 tokens
evaluate.golden.json     221 tokens
```

## 4. `make check` (the repository gate)

```
$ .venv/bin/ruff check backend
All checks passed!
$ .venv/bin/ruff format --check backend
177 files already formatted
$ .venv/bin/mypy
Success: no issues found in 119 source files
$ .venv/bin/python -m app.skills.lint --skills-dir skills
ok   abstract-writing          283/400 tokens (ceiling 400)
ok   citation-apa7             287/400 tokens (ceiling 400)
ok   citation-ieee             265/400 tokens (ceiling 400)
ok   imrad-structure           358/400 tokens (ceiling 400)
ok   literature-matrix         261/400 tokens (ceiling 400)
ok   patch-format              308/400 tokens (ceiling 400)
ok   peer-review-checklist     285/400 tokens (ceiling 400)
7 skill(s) OK in skills
$ .venv/bin/python -m pytest
994 passed, 8 skipped, 1 warning in 17.89s
```

Baseline before this step was 974 passed, 8 skipped; the delta is the 20 new prompt-asset
tests.

## 5. Planner suite on its own

```
$ .venv/bin/python -m pytest backend/tests/test_planner_service.py \
      backend/tests/test_planner_plan_validator.py backend/tests/test_planner_probes.py \
      backend/tests/test_planner_evaluation.py backend/tests/test_planner_events.py \
      backend/tests/test_planner_prompts.py -p no:warnings
123 tests, all passing
  test_planner_evaluation.py   16
  test_planner_events.py       14
  test_planner_plan_validator.py 25
  test_planner_probes.py       24
  test_planner_prompts.py      20
  test_planner_service.py      24
```

## 6. Spot checks recorded for the record

* A rendered plan call (printed from the `SpyProvider` in a scratch script) starts with
  the planner instruction, then `## Digest (claude.md)`, then `## Task` JSON - and the
  system prefix contains `# Root rules` and `# Planner`, never `# Evaluator` and never
  `AgentResult`. This is asserted by
  `test_request_is_bounded_planner_shaped_and_schema_constrained`.
* The golden plan (6 tasks: architect -> researcher -> writer -> writer -> reviewer ->
  validator) passes `PlanValidator.problems` for the research brief at a 100,000-token
  budget, and the golden delta merges into it without breaking the DAG or the required
  roles.
* The golden evaluation scores exactly the 9 rubric criteria, reports three concrete
  `missing` entries and `meets_minimum: false` with `recommendation: "continue"`.

## 7. CONTRACT-CHANGE-REQUESTS

None. Nothing under `backend/app/contracts/` was read-only in name only: no file there was
modified, and no change was needed. The planner's own wire shapes (`PlanDraft`,
`PlanDeltaDraft`, `EvaluationDraft`) live in `app/planner/drafts.py` precisely so that the
model-facing shapes can move without touching the frozen contracts, and task ids are minted
by the validator rather than by the model because a ULID is not something a model mints
reliably.
