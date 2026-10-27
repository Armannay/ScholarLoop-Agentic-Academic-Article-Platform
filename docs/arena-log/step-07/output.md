# Step 07 — raw output

Date: 2026-10-27. Session branch: `arena/198decc6-scholarloop-agentic-academic-a`, started
at `83101026e9d62589c097545bb1eafef71315cacd` (the session's `main` merge of PR #22).
The Arena session branch is fixed, so no other branch was created or used.

## Required-file confirmations

Read `backend/app/contracts/__init__.py`.
Read `backend/app/contracts/account.py`.
Read `backend/app/contracts/agent.py`.
Read `backend/app/contracts/binding.py`.
Read `backend/app/contracts/brief.py`.
Read `backend/app/contracts/common.py`.
Read `backend/app/contracts/enums.py`.
Read `backend/app/contracts/ledger.py`.
Read `backend/app/contracts/llm.py`.
Read `backend/app/contracts/loop.py`.
Read `backend/app/contracts/patch.py`.
Read `backend/app/contracts/plan.py`.
Read `backend/app/contracts/quality.py`.
Read `backend/app/contracts/registry.py`.
Read `backend/app/contracts/section.py`.
Read `backend/app/contracts/source.py`.
Read `backend/app/contracts/task.py`.
Read `backend/app/contracts/verdict.py`.
Read `backend/app/core/errors.py` — `ScholarLoopError` carries `code`, `http_status` and a
`**context` bag, which is why role errors subclass it instead of inventing codes.
Read `backend/app/providers/base.py` — `LLMProvider.generate(request, *, model, role)`
and `estimate_tokens = ceil(chars / 4)`, the convention every cap in this step uses.
Read `backend/app/providers/errors.py`.
Read `backend/app/providers/mock.py` — `MockProvider` is scripted by `(role, call_index)`,
which is what lets a repair test script the second answer separately.
Read `config/policies.yaml` — model tiers, `call_input_cap: 24000`, budgets.
Read `config/rubric.yaml` — the 9 weighted criteria and the per-article-type section list.
Read `backend/app/core/rubric.py` — `resolve_sections()`, `slugify`, `target_words` = range
midpoint; the architect and the writer both read it rather than restating section ids.
Read `backend/app/agents/base.py` (Step 5 runtime) and `backend/app/agents/context.py`.
Read `backend/app/skills/loader.py` and `skills/` (budgeted skill selection).
Read `backend/app/tools/base.py`, `tools/registry.py` and the five Step 6 tools.
Read `config/tool_policy.yaml` — root grants: researcher `scholar_search` +
`sanitize_text`; validator `doi_verify`, `citation_check`, `text_checks`,
`sanitize_text`. No other role may call a tool.
Read `backend/app/api/routers/runs.py` and `backend/app/core/policies.py`.

`git tag -l` is empty on this clone: the `contracts-v1.1` tag the brief names is not
advertised, so the frozen files were checked against commit `8310102` instead of a tag
SHA. No file under `backend/app/contracts/` was modified (`git diff --stat` shows none).

## What was built

### The shared body: `agents/roles/base.py`

`RoleAgent` extends the Step 5 `Agent` and changes exactly six things:

1. **Prompt asset.** `prompts/<role>.md` is appended as a cached system block
   (`RolePromptContextBuilder`), so the rules + skill index + prompt prefix stays
   byte-stable per role and remains cacheable.
2. **Two documents, one schema.** `role_call_schema(AgentResult, role_output)` composes
   the model-facing schema: `{"result": ..., "output": ...}`, both required,
   `additionalProperties: false`, and each role's `forbidden_result_fields` are removed
   from the envelope half. The same schema is what the provider is constrained with and
   what the validator parses against, so the two cannot drift.
3. **Deterministic preparation.** `prepare()` returns system blocks computed *before* the
   model call: the search result (researcher), the section text and word window (writer),
   the dirty sections (reviewer), the gate pack and the fresh reviewer scores (validator).
4. **Deterministic acceptance.** `output_problems()` runs inside the runtime's validation
   step, so a role violation earns exactly the same single repair a schema error gets.
   `_forbidden_field_problems()` enforces the forbidden envelope fields on the *parsed*
   answer too: a provider that ignores the schema cannot slip a reviewer patch or a
   model-made `QualityReport` through.
5. **Caps as ceilings.** `max_input_tokens` is checked locally before a call (no provider
   call is made for an oversized prompt) and `max_output_tokens` clamps the request even
   when the binding is more generous.
6. **Scratch per run.** `RoleRun` holds the prompt blocks, the computed data and the
   validated role document; `take_output(task)` hands the document to `postprocess`, and
   the scratch is dropped in a `finally`, so a failed run leaves nothing behind.

`role_call_schema` had a real bug, caught by the new `$ref`-resolution test: `_prefix_defs`
rewrote `$ref` strings at the top level but rebuilt `$defs` from the un-rewritten
definitions, so a nested reference (`SectionReview → ReviewIssue`) pointed at a definition
the prefix had renamed away. Fixed by re-keying the already-rewritten definitions.

### The five roles

| Role | Tier | Tools | Document (`output`) | Deterministic refusals |
| --- | --- | --- | --- | --- |
| `architect` | mid | – | `SectionSeed`s: id, title, goal, target_words, key_claims, required_sources | seeds must mirror the outline in order; ids must be rubric ids; required rubric sections present; targets within ±15 % of the brief; a re-plan may not drop or reorder drafted sections |
| `researcher` | mid | `scholar_search` | `Finding`s (one per card, ≤ 2 sentences) + `no_results` + `note` | ≤ 12 cards; every card must match a search result or a stored card (id, DOI, or title+year); every finding must name a returned card; `no_results` and findings are mutually exclusive |
| `writer` | mid | – | `section_id`, `status`, `cited_source_ids`, `missing_inputs` | exactly one section per task; `replace_section` only while empty/draft; cite ⊆ supplied ids and declared == cited; words within ±10 % of the target; no placeholders; `base_version` must match the section; `needs_user_input` must name what is missing and carry no patch |
| `reviewer` | strong | – | `reviews`: ≤ 8 issues (`severity`, `anchor` ≤ 25 words, `problem` ≤ 80 words, `suggested_fix` ≤ 30 words) + 0–100 per rubric criterion | dirty sections only (a `final` section is refused before the call); no patch field at all; `reviewed` requires zero high issues; never `final`; every rubric criterion scored; scores must be known criteria |
| `validator` | cheap | `text_checks`, `citation_check`, `doi_verify`, `sanitize_text` | `subjective_checks` (5 booleans) + `rationale` (≤ 8 × 30 words) | no report/score/patch/section from the model; brief, rubric, text provider and tools are required up front; gates come from tools, Q from the reviewer |

Support modules: `schemas.py` (the five role documents, all `extra="forbid"` and
word-capped), `inputs.py` (`SectionTextProvider`, `SectionTargetProvider`,
`ReviewScoreboard`, `ReportSource` + mapping/static implementations), `validation.py`
(citation markers, inserted text, estimated words, dirty sections, source matching),
`quality.py` (gate pack, figure scan, score merge, weighted Q, section settlement),
`errors.py` (`RoleTaskInvalid`), `golden.py` (fixture loading), `factory.py`
(`ROLE_AGENTS`, `build_role_agent`, `register_role_agents`).

### Prompt assets, rules and goldens

- `prompts/<role>.md`: 188–211 tokens each, `{"result", "output"}` shape first, then the
  role's field contract and the rules that are specific to it.
- `rules/agents/<role>.rules.md`: 270–316 tokens each, numbered, deferring to
  `rules/root.rules.md` (the existing test pins both properties).
- `prompts/<role>.golden.json`: one worked input → expected answer per role on a shared
  pilot run (11 iterations, 612,400 tokens, Q 68 → 81): an 8-section plan summing to 4500
  words, two real-looking cards with findings, a 130-word introduction citing both cards,
  a review scoring all 9 criteria with one `med` issue, and a validation answer whose
  deterministic pack fails the gates the article genuinely fails.

### Wiring

`register_role_agents()` registers all five roles behind one shared scoreboard.
`app/api/routers/runs.py` now uses it (with `build_tool_registry()` and the rubric) instead
of constructing the classes directly, so the API and the tests take the same path.

## Design decisions worth recording

1. **The role document travels beside the envelope, not inside it.** The frozen
   `AgentResult` has no field for a goal, a claim map, a finding, a per-section score or
   `missing_inputs`. Rather than bend the contracts (forbidden) or drop the information
   (dishonest), the answer is one JSON object with two validated documents, and the
   orchestrator discards `output` after `postprocess`. Recorded as CCR-7.1.
2. **Search before the model.** The researcher's prompt contains the tool's rendered,
   untrusted result; a card the search did not return cannot pass acceptance. That is a
   stronger anti-fabrication guard than asking a model to be careful.
3. **Text arrives through a provider, never through the task.** HARD RULE 3 keeps bodies
   out of the context; the three roles that must read text get it through an injected
   `SectionTextProvider`, so the runtime and the contracts stay text-blind.
4. **Scores travel through a hash-keyed scoreboard.** The validator computes Q from the
   reviewer's scores and carries one forward only while the section hash still matches the
   text that was scored; a stale score falls back to the previous report, then to 50.
5. **Gates are tool output, full stop.** The validator's model answer has no gate, score
   or report field; a model that says everything is fine still produces
   `no_placeholders: false` when the text contains a TODO.
6. **Caps are ceilings, in both directions.** A generous binding cannot raise a role's
   output cap, and an oversized prompt is refused locally with `BudgetExceeded` before any
   call is made.
7. **"I need input" is an answer.** The writer's `needs_user_input` path is a first-class
   result (no patch, `entry.status` set, a named missing input), not an exception.

## CONTRACT-CHANGE-REQUESTS

- **CCR-7.1** — `AgentResult.role_output: dict[str, Any] | None`: give a role's own
  document (architect plan, researcher findings, reviewer scores, writer
  `missing_inputs`) a home inside the envelope instead of beside it. Additive.
- **CCR-7.2** — per-section soft scores in `QualityReport`
  (`scores_by_section: dict[SectionId, dict[CriteriaName, int]]`), so a report can say
  *where* quality was lost rather than only the article-wide weighted mean.

Neither was applied: `backend/app/contracts/` is untouched and `make schemas-check`
reports 23 schemas up to date.

## Raw checks

The complete `make check` stdout/stderr is in `make-check.txt`:

```
ruff check backend: All checks passed!
ruff format --check backend: 193 files already formatted
mypy: Success: no issues found in 130 source files
skill lint: 7 skill(s) OK in skills
pytest: 1096 passed, 8 skipped, 1 warning in 20.10s
EXIT=0
```

`make schemas-check`: 23 contract schemas are up to date. The single warning is Starlette's
pre-existing httpx deprecation notice, not a result of this step.

`backend/tests/test_role_agents.py` collects 80 tests; the notable ones:

- `test_golden_expected_matches_the_role_schemas[role]` — every fixture answer is legal as
  both `AgentResult` and the role document.
- `test_call_schema_is_the_envelope_plus_the_role_document[role]` — two required parts, no
  forbidden envelope field offered, every `$ref` resolvable.
- `test_role_answers_its_golden_fixture[role]` — one end-to-end run per role against
  `MockProvider`, asserting the role-specific effect (seeds, cards, patch, status, report).
- `test_writer_cannot_cite_an_unknown_id` — refused, and `attempts == 2` proves the single
  repair was spent.
- `test_reviewer_cannot_return_a_rewritten_paragraph` / `..._suggested_fix_is_capped` —
  the caps are schema rules.
- `test_validator_gates_are_the_tool_results` — every assessed gate equals the raw tool
  payload; `verified_citation_ratio` equals `citation_check`'s value.
- `test_validator_gates_ignore_a_model_that_claims_all_is_well` — a TODO in the text still
  fails `no_placeholders`.
- `test_reviewer_to_validator_round_trip_scores_q` — the reviewer's scores become the
  article's Q through the shared scoreboard.
- `test_role_output_cap_is_a_ceiling_over_the_binding` /
  `test_role_refuses_a_prompt_over_its_input_cap` — both directions of the token caps.

## Pull request

```
gh pr create --base main --head arena/198decc6-scholarloop-agentic-academic-a \
  --title "Step 7: the five role agents on the Step 5 runtime" \
  --body-file docs/arena-log/step-07/pr-body.md
```

## Limitations

- Run-scoped section text (and the previous report) is an orchestrator concern: `runs.py`
  now builds the roles through the factory with the Step 6 tools and the rubric, but passes
  no text provider. The writer still drafts empty sections; the reviewer and validator
  refuse with a `ConfigError` that names the missing provider rather than reviewing text
  they cannot read. Wiring a run-scoped provider is Step 8/9 work.
- Token caps use the shared `chars / 4` estimate, so a dense prompt can be refused slightly
  early; this errs on the cheap side deliberately.
- No live provider or live scholarly index was contacted: every test runs on
  `MockProvider` and the deterministic tools.
