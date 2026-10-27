# Step 9 - Deterministic orchestrator (part b): intake, state machine, scheduler, runs API

Saved verbatim as the record of what was asked, with annotations at the end. Step 9 ran
in two sessions: 9a already delivered items 4 (loop controller), 5 (stop handling) and 7
(simulator); this log covers 9b - items 1, 2, 3 and 6.

---

## Context

Repo `Armannay/ScholarLoop-Agentic-Academic-Article-Platform`. Create your branch from
the latest `main`.

FIRST read these files (do not modify them) and confirm in one line each that you read
them: everything under `backend/app/contracts/`, `backend/app/core/errors.py`,
`backend/app/providers/{base,errors,mock}.py`, `config/policies.yaml`, plus the EXTRA
FILES below. Contracts are frozen (tag `contracts-v1.1`). If you need a contract change,
do NOT edit it: list it under CONTRACT-CHANGE-REQUESTS.

Quality gate: `make check` must pass. Open a PR to `main` when done, filling Step /
Depends on / Contract changes. Save this prompt and your raw output in
`docs/arena-log/step-<NN>/`.

## Task

**ROLE:** staff engineer (workflow engines and state machines).

**GOAL:** implement the deterministic orchestrator for ScholarLoop.

Items 4, 5 and 7 (loop controller, stop handling, simulator) were delivered in session 9a:
import them, do NOT rewrite them.

### Deliverables

1. `backend/app/orchestrator/intake.py`: deterministic Brief validation -> OK or
   NEEDS_INPUT(missing_fields[]), no LLM; `quick_edit` requests skip the Planner -> one
   agent with rules, still logged and validated.
2. `backend/app/orchestrator/state_machine.py`: states INTAKE, PLANNING, EXECUTING,
   VALIDATING, DECIDING, CHECKPOINT, REPLANNING, DONE, STOPPED_EROSION, STOPPED_BUDGET,
   STOPPED_ERRORS, STOPPED_USER; every transition persisted (crash-safe resume); tasks
   idempotent by id.
3. `backend/app/orchestrator/scheduler.py`: dependency-aware; at most `max_parallel`
   concurrent; per-section locks; retry(2) with backoff; per-task timeout; resolves
   role->binding->account at call time.
4. (9a) loop controller.
5. (9a) stop handling.
6. `backend/app/api/routers/runs.py`: `POST /projects/{id}/runs`,
   `POST /runs/{id}/decision`, `POST /runs/{id}/stop`, `GET /runs/{id}`,
   `GET /runs/{id}/events` (SSE), `GET /runs/{id}/digest`, `/ledger`, `/article`,
   `/usage` - EXACTLY as `docs/openapi/openapi.yaml`; the spec is unchanged.
7. (9a) simulator `simulate_loop.py`.

Use the real planner, agents registry, article store, and memory from `main`; where Step 7
is not merged, use MockProvider scripts.

### Tests

- a hypothesis property test proving the loop stops within `max_iterations` for ANY report
  sequence;
- each detector unit-tested at threshold +/- 1;
- crash-resume;
- parallel locks;
- checkpoint flows.

---

## Annotations (added afterwards, not part of the prompt)

- Run 9a had previously delivered `loop_controller.py`, `stop.py` and
  `script/simulate_loop.py`; session 9b delivered `intake.py`, `state_machine.py`,
  `scheduler.py`, `persistence.py`, `runner.py` (the Orchestrator), the SSE event store,
  and the full `runs.py` router.
- During this session the spec audit found that `docs/openapi/openapi.yaml` had grown
  since the brief was written: beyond the eight endpoints named above, it also lists
  `GET /runs/{id}/events/history`, `/quality`, `/article/sections/{id}/history`,
  `/article/sections/{id}/diff`, `/article/snapshots`, `POST /runs/{id}/article/restore`,
  and it makes the one-active-run guard mandatory (`active_run.409.json` exists as an
  example for the 409 on `POST /projects/{id}/runs`). Those spec-mandated items were
  implemented as part of item 6 (`api/runs.py` exactly as the spec) so the code could
  match the frozen spec word for word.
- The spec was NOT modified (frozen). The six endpoints whose `x-scholarloop-status` is
  still `planned` but which are now implemented are listed in the PR's
  CONTRACT-CHANGE-REQUESTS section as spec-metadata follow-ups for the spec authors.
