## Step

Step 07 — the five role agents (architect, researcher, writer, reviewer, validator) on
the Step 5 runtime.

## Depends on

- Step 5: `Agent.run(task, run_state)`, `ContextBuilder`/skills, `AgentRegistry`,
  governor precheck, the single repair path, `MockProvider`.
- Step 6: the scoped `ToolRegistry` and the deterministic tools — `scholar_search`,
  `doi_verify`, `citation_check`, `text_checks`, `sanitize_text`.
- Frozen CONTRACTS v1 (`AgentResult`, `ChangeEntry`, `Patch`, `SectionState`,
  `SourceCard`, `QualityReport`) and `config/{rubric,policies,tool_policy}.yaml`.

## Contract changes

None. Nothing under `backend/app/contracts/` was edited; `make schemas-check` still
reports the 23 schemas up to date.

**CONTRACT-CHANGE-REQUESTS:**

- **CCR-7.1 — a home for a role's own document.** Add an optional
  `role_output: dict[str, Any] | None` (or named per-role members) to `AgentResult`.
  Today a role's answer is `{"result": AgentResult, "output": <role document>}`: one
  schema, validated together, but the role document lives beside the envelope instead of
  inside it. The payloads that need the field are the architect's per-section goal /
  claims map / required sources, the researcher's two-sentence findings, the reviewer's
  per-section scores and the writer's `missing_inputs`. Everything is additive: consumers
  that ignore `role_output` keep working.
- **CCR-7.2 — per-section soft scores.** `QualityReport.scores` is one global
  criterion → score map, so a per-section score can only survive as the reviewer's role
  document plus the shared scoreboard. A `scores_by_section: dict[SectionId,
  dict[CriteriaName, int]]` member (or making `scores` a mapping keyed by section) would
  let a report explain *where* it lost points instead of only *how many*.

Neither request is required for this step to work; both are recorded here rather than
applied, per working agreement 4.

## Summary

- **`backend/app/agents/roles/`** (13 modules): `RoleAgent` on the Step 5 runtime plus one
  class per role. Five prompt assets (`prompts/<role>.md`, 188–211 tokens), five rules
  files (`rules/agents/<role>.rules.md`, 270–316 tokens) and five golden
  `input → expected answer` fixtures (`prompts/<role>.golden.json`) replayed by the
  suite.
- **Strict JSON, two documents.** The model is constrained to
  `{"result": AgentResult, "output": <role document>}`; both are validated, and each role
  removes the envelope fields it must not fill (`patches`/`source_cards`/`report`/
  `sections`) from the model-facing schema *and* refuses them at runtime.
- **Architect** plans the rubric's section ids in order with a goal, a word target, a
  claims map and required sources per section, returns empty `SectionState` seeds, and
  rejects a plan whose targets miss the brief by more than 15 %. A re-plan may add
  sections but never drops or reorders drafted work.
- **Researcher** runs `scholar_search` *before* the model call, passes the result as an
  untrusted data block, returns ≤ 12 cards with a two-sentence finding each, and refuses
  any card the search (or the source store) does not hold; `no_results: true` is the
  honest empty answer and the only accepted way to return no cards.
- **Writer** owns exactly one section per task, patches it (`replace_section` only while
  it is empty/draft; targeted ops once reviewed), cites only supplied cards, reports the
  ids it cited, stays inside target words ±10 %, and answers `needs_user_input` with a
  named missing input instead of drafting around it.
- **Reviewer** reads dirty sections only, returns ≤ 8 issues per section with
  `suggested_fix` ≤ 30 words, cannot emit prose (every field is word-capped and the
  schema has no patch), never marks a section `final`, and records its scores in a shared
  hash-keyed scoreboard.
- **Validator** sets the eight hard gates from `doi_verify` → `text_checks` →
  `citation_check` → a local figure/table scan, asks a cheap model only the five
  subjective yes/no checks, computes `Q` as the weighted mean of the reviewer's fresh
  scores (carried forward only while a section's hash is unchanged, else the previous
  report, else 50), and publishes the `QualityReport` including
  `verified_citation_ratio`.
- **Wiring.** `register_role_agents()` is the single factory path; the runs API now uses
  it with the Step 6 tool registry and the rubric. `prompts/`, `rules/agents/`, README and
  `docs/ARCHITECTURE.md` describe the roles.
- **Bug found by the new tests:** the role schema composer prefixed `$defs` names but
  rebuilt them from the un-rewritten definitions, so nested `$ref`s (e.g.
  `SectionReview → ReviewIssue`) dangled. Fixed in `role_call_schema`/`_prefix_defs`.

## Verification

- `make check`: **1096 passed, 8 skipped**, one pre-existing Starlette/httpx
  deprecation warning. Full output in `docs/arena-log/step-07/make-check.txt`.
- `make schemas-check`: 23 contract schemas up to date.
- `backend/tests/test_role_agents.py`: 80 tests — golden-fixture schema compliance for
  all five roles, one end-to-end run per role, cross-role task refusal, output/input token
  ceilings, unknown `[[CITE:...]]` ids, invented sources, reviewer rewrite caps, dirty-only
  scope, rubric-coverage scoring, tool-vs-model gates (a model claiming all is well still
  yields failing gates), Q from the reviewer, stale-hash carry-forward, the baseline
  fallback, and the reviewer → validator round trip.
- Everything runs on `MockProvider` and the deterministic Step 6 tools: no network, no
  keys, no clock.

## Limitations

- **Run-scoped text is not wired yet.** Section text and the previous report live in a
  run's article store, which does not exist at API wiring time, so `runs.py` builds the
  roles without a `SectionTextProvider`/`ReportSource`. The writer can still draft an
  empty section (rubric-midpoint targets), but the reviewer and the validator refuse with
  a `ConfigError` naming the missing provider instead of reviewing or scoring text they
  cannot read. Step 8/9 should inject a run-scoped provider (and the last report).
- Token estimates use the shared `chars / 4` convention; the role caps are enforced on
  the estimate, so a dense prompt can still be rejected slightly early.
- `doi_verify` is granted to the validator, but the validator's own tests use
  `text_checks` + `citation_check` only; verification status therefore stays `unknown`
  in fixtures until the orchestrator wires the full tool set.
- The reviewer's scores are per section in the role document and the scoreboard; the
  frozen `QualityReport` can only report a global per-criterion score (CCR-7.2).
