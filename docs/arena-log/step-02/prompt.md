# Step 02 — Token governor and context builder (raw prompt)

Branch: `arena/7c6aa227-scholarloop-agentic-academic-a` (from `main` @ `b300a6e`).

> CONTEXT: Repo https://github.com/Armannay/ScholarLoop-Agentic-Academic-Article-Platform.
> Create your branch from the latest main: `arena/29d74702-scholarloop-agentic-academic-a` branch.
> FIRST read these files (do not modify them) and confirm in one line each that you read
> them: everything under `backend/app/contracts/`, `backend/app/core/errors.py`,
> `backend/app/providers/{base,errors,mock}.py`, `config/policies.yaml`, plus the EXTRA FILES
> below. Contracts are frozen (tag `<contracts-v1.1>`). If you need a contract change, do NOT
> edit it: list it under CONTRACT-CHANGE-REQUESTS.
> Quality gate: `make check` must pass. Open a PR to main when done, filling Step / Depends on /
> Contract changes. Save this prompt and your raw output in `docs/arena-log/step-<NN>/`.

## TASK

<ROLE: Performance-minded backend engineer.
GOAL: Make token use predictable and minimal: hierarchical budgets, estimates,
cache-friendly prompt assembly, telemetry.
USE: contracts, LLMProvider, policies.yaml (Step 0) and the provider pool and usage
tables (Step 1). Code against Protocols for skills and source cards (Steps 5 and 6 are
not built yet).
DELIVERABLES (backend/app/governor/)
1. budget.py: nested budgets account-month -> run -> iteration -> call.
   precheck(estimate) raises BudgetExceeded(level, remaining); record(usage) is atomic
   and persisted; soft warning at 80 percent, hard stop at 100 percent; limits come from
   policies.yaml (project overrides) and per-account budgets from Step 1.
2. estimate.py: TokenEstimator uses provider.count_tokens when available, otherwise
   chars/4 with a 15 percent safety margin; reconcile estimate vs actual usage and expose
   the drift.
3. context_builder.py: build(task, run_state) -> LLMRequest with this fixed layout:
   [STABLE PREFIX, cache=true: root rules, then project rules, then role rules, rubric
   excerpt, skill index (name plus one line), brief] -> [SEMI-STABLE: selected skill bodies,
   source cards the task references] -> [VOLATILE: digest slice, task, text of the section
   being edited]. Project rules may add to root rules but never remove rules marked
   mandatory. The prefix must be byte-identical across calls of the same role within a run
   (test it). Enforce max_input_tokens with a documented trim order: older digest detail,
   then optional skills, then source-card notes, else raise ContextTooLarge. Never silently
   truncate the task or the section being edited.
4. telemetry.py: per-call record (run, iter, task, role, model, tokens in/out/cached,
   latency, est. cost) and queries: tokens by role and iteration, cache-hit ratio,
   Token ROI = Q gained per 10k tokens.
5. tiers.py: resolve the model by tier from policies.yaml (planner strong, validator and
   compactor cheap, and so on) with per-agent override through AgentBinding.
TESTS: budget hierarchy and atomicity under concurrency; trim order; prefix byte-stability;
estimator margin; ROI calculation; mandatory-rule protection.
EXTRA FILES: backend/app/accounts/, backend/app/providers/pool.py and the other new Step 1
files, backend/app/memory/, backend/app/agents/ (read only). Add this line: "Skills and tool
schemas from Step 5 are real files now; use them instead of Protocols where they exist."
