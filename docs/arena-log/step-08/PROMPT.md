# Step 8 - Planner: planning and evaluation under strict budgets

Saved verbatim as the record of what was asked, with annotations at the end.

---

## Context

Repo `Armannay/ScholarLoop-Agentic-Academic-Article-Platform`. Create your branch from
the latest `main` (the prompt named a sibling Arena branch as the source; the branch was
cut from `main` at `2bf4bd8`, which already contains that sibling's merge).

FIRST read these files (do not modify them) and confirm in one line each that you read
them: everything under `backend/app/contracts/`, `backend/app/core/errors.py`,
`backend/app/providers/{base,errors,mock}.py`, `config/policies.yaml`, plus the EXTRA
FILES below. Contracts are frozen (tag `<contracts-v1.1>`). If you need a contract change,
do NOT edit it: list it under CONTRACT-CHANGE-REQUESTS.

Quality gate: `make check` must pass. Open a PR to `main` when done, filling Step /
Depends on / Contract changes. Save this prompt and your raw output in
`docs/arena-log/step-<NN>/`.

## Task

**ROLE:** AI engineer designing LLM planning and evaluation under strict budgets.

**GOAL:** Claude acts ONLY as planner and evaluator. It never writes article text and
never edits files.

**USE:** contracts, `policies.yaml`, `rubric.yaml`, the `claude.md` format (Step 3),
ContextBuilder and the provider binding named `planner` (Steps 1-2) (pasted).

### Deliverables (`backend/app/planner/`, `prompts/planner/`)

1. `service.py`: `plan(brief, digest, budget) -> Plan`;
   `replan(digest, report, last_decision, strategy_change) -> Plan delta`;
   `evaluate(digest, report, probes) -> Verdict`.
2. `plan_validator.py` (mandatory distribution): every task has exactly one valid role;
   dependencies form a DAG; each task has acceptance criteria and a `token_budget`; the
   sum of `token_budget <=` remaining run budget; at most 8 tasks per iteration; section
   ids exist or are created by an earlier architect task; the Planner never assigns work
   to itself. On failure retry at most 2 times, sending only the validator's error list.
3. `probes.py`: a deterministic compact article view for evaluation, never the full text:
   headings, per-section word and citation counts, first and last 2 sentences per section,
   plus at most 3 probe excerpts (the lowest-scored sections) of <= 300 tokens each.
   `deep_check=True` (user opt-in) sends the full text once.
4. Evaluation rules: hard gates computed by the validator cannot be overridden; any
   failing gate means `meets_minimum=false`. The `Verdict` lists what is missing
   concretely.
5. Call policy (event-driven, not every iteration): plan at the start; replan only when
   the task queue is empty, a hard gate newly fails, a milestone completes, or the loop
   controller flags a first plateau (then `strategy_change=true` once).
6. Prompts: system prompt <= 700 tokens plus short templates (plan, replan, evaluate),
   JSON-schema outputs, `max_tokens` per call; model and account resolved through the
   `planner` binding.

### Tests

- golden plans for 3 sample briefs (`MockProvider`);
- invalid-plan rejection matrix;
- planner input never exceeds its token cap (assert via `ContextBuilder`);
- `evaluate` cannot pass with a failing gate;
- call-policy tests.

### Extra files

`backend/app/memory/` (digest format), `backend/app/article/` (for probes),
`config/rubric.yaml`, `backend/app/core/rubric.py`, `rules/root.rules.md`.

ContextBuilder (Step 2) is not merged yet: code against a Protocol.

---

## Annotation 1 - the prerequisite work was already on `main`

This brief describes Step 8, and Step 8 had already landed: `backend/app/planner/`
(service, validator, probes, evaluation, events, context, budget, drafts, prompts,
errors) and five test modules were present at `2bf4bd8`, and `docs/ARCHITECTURE.md`
refers to it as "Step 8". Re-implementing it would have been a regression, so the step
was executed as an **audit against the brief followed by the work that was actually
missing**:

* `prompts/planner/` did not exist. The brief names it as a deliverable directory
  alongside `backend/app/planner/`, and the repository's convention for every other role
  is `prompts/<role>.md` plus a golden answer. The planner prompt lived only as a string
  constant in `app/planner/prompts.py`, with no golden answers at all.
* The `ContextBuilder` seam was typed against the governor's concrete class rather than a
  Protocol, which is what the brief explicitly asked for ("code against a Protocol").

Everything the brief lists as deliverables 1-6 and as tests was verified line by line and
is recorded in `OUTPUT.md` with the file and test that proves it.

## Annotation 2 - the pasted prerequisite section arrived empty

The prompt says "ContextBuilder and the provider binding named `planner` (Steps 1-2)
(pasted)", but nothing was pasted. The seam was therefore read from the repository:
`backend/app/governor/context_builder.py` (the `ContextBuilder`, its `ContextPlan`, the
trim order and the `GovernorRunState` Protocol) and `backend/app/contracts/binding.py` +
`backend/app/agents/binding.py` (the `planner` binding, resolved at call time). No
contract change was needed for any of it; see CONTRACT-CHANGE-REQUESTS in `OUTPUT.md`.
