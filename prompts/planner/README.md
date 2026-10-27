# Planner prompts

The Planner is a *prompt*, not an agent: it plans and evaluates and never writes article
text. These files are the prompt assets and the golden answers for the three calls the
planner makes. `make check` enforces every budget below.

| File | Purpose | Budget |
| --- | --- | --- |
| `system.md` | planning system prompt (`PLANNER_SYSTEM_PROMPT`), used for `plan` and `replan` | ≤ 700 tokens |
| `evaluator.md` | evaluation system prompt (`EVALUATOR_SYSTEM_PROMPT`) | ≤ 700 tokens |
| `plan.md` | short template for the plan user message | ≤ 200 tokens |
| `replan.md` | short template for the replan user message (returns a delta) | ≤ 200 tokens |
| `evaluate.md` | short template for the evaluate user message (compact view) | ≤ 200 tokens |
| `plan.golden.json` | a plan draft that passes the plan validator | — |
| `replan.golden.json` | a delta that applies to `plan.golden.json`'s plan | — |
| `evaluate.golden.json` | an evaluation draft that passes the evaluation checks | — |

## Single source of truth

`system.md` and `evaluator.md` are copied **verbatim** from
`backend/app/planner/prompts.py`; `tests/test_planner_prompts.py` fails if the two ever
disagree, so the code stays authoritative and these files stay honest. The plan/replan/
evaluate instructions quoted in the templates are the `*_INSTRUCTION` constants from
`backend/app/planner/context.py`, and are checked the same way.

## How one call is assembled

1. **Stable prefix (cached)** — the governor `ContextBuilder` layout: root rules, the
   planner system prompt, skill index, the rubric excerpt for the brief's article type
   and the brief itself. Project rules may add to this; the mandatory-rule guard refuses
   any attempt to remove a root rule.
2. **Volatile part** — the digest slice (`claude.md`), the synthetic task and, for
   `evaluate`, the deterministic gate results, the compact article view (headings, per
   section word and citation counts, first and last two sentences, ≤ 3 excerpts of
   ≤ 300 tokens) and the validator's report. The full text is sent once, and only on a
   user-opted `deep_check`.
3. **Output cap** — `PLANNER_MAX_OUTPUT_TOKENS` (4,096) for plan/replan,
   `EVALUATOR_MAX_OUTPUT_TOKENS` (2,048) for evaluate, further capped by the planner
   `AgentBinding`.
4. **Schema** — the request carries `plan_draft_schema()`, `plan_delta_schema()` or
   `evaluation_schema()` generated from the pydantic draft models, so a field cannot
   drift out of the prompt without failing a test.
5. **Retry** — at most twice, and the retry message is the validator's error list and
   nothing else: no previous plan, no article text, no conversation.

## Golden answers

The planner's wire shapes are *drafts*: tasks carry short local keys (`T1`) and the
validator mints the ULIDs. The goldens are the reference examples a prompt change is
diffed against — they are validated in the test-suite against the real `config/rubric.yaml`
and a real `Brief`, so a golden that stops being legal fails the build.
