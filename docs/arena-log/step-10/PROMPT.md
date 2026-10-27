# Step 10 - OpenAPI 3.1 spec for the UI (Projects, Runs, Article, Usage, Errors)

Saved verbatim, with two annotations, as the record of what was asked.

---

## Role

API designer.

## Task

Produce the complete **OpenAPI 3.1** spec for the ScholarLoop UI, covering **Projects**,
**Runs**, **Article**, **Usage** and **Errors**. **Spec only - no backend code.** The
CONTRACTS v1 models must not be modified.

### Required output

- `docs/openapi/openapi.yaml` - schemas referencing the CONTRACTS v1 names (`Brief`,
  `Task`, `Plan`, `SourceCard`, `Patch`, `PatchOp`, `ChangeEntry`, `TokenUsage`,
  `AgentResult`, `SectionState`, `CitationRef`, `QualityReport`, `QualityIssue`,
  `Verdict`, `LoopDecision`, `Account`, `AccountPublic`, `MonthlyBudget`,
  `AgentBinding`, `LLMRequest`/`LLMResponse`, `SystemBlock`, `LLMMessage`); endpoints
  exactly as listed in the endpoint list.
- `docs/openapi/examples/` - a request/response example for **every** endpoint,
  including `422 needs_input`, `409 active-run`, and SSE event payloads.
- A mock-server config (Prism) with three scenarios: `completion`, `erosion stop`,
  `needs_input`.
- A README explaining how to run the mock and regenerate the typed TS client.

### Done when

The spec validates with an OpenAPI linter; every endpoint has examples; the mock starts
with one command; the generated TS client compiles.

## Process constraints

- First read (do not modify) everything under `backend/app/contracts/`,
  `backend/app/core/errors.py`, `backend/app/providers/{base,errors,mock}.py`,
  `config/policies.yaml`, plus the extra files (`backend/app/api/`,
  `backend/app/article/store.py`, `backend/app/orchestrator/`), and confirm in one line
  each that they were read.
- Contracts are frozen at tag `contracts-v1.1`; a needed contract change is listed
  under `CONTRACT-CHANGE-REQUESTS`, never applied.
- `make check` must pass.
- Open a PR to `main`, filling Step / Depends on / Contract changes.
- Save the prompt and the raw output under `docs/arena-log/step-<NN>/`.

---

## Annotation 1 - the endpoint list was empty

The task pointed at "the endpoint list above", but no endpoint list reached the agent:
the placeholder arrived as "(missing)". Rather than invent a surface, the list was
derived from the code that exists - `backend/app/api/routers/*.py`, the orchestrator's
read models (`article_data`, `usage_summary`, `digest_snapshot`) and the article store
(`history`, `diff`, `snapshots`, `restore`) - and every operation the routers do not
serve is marked `x-scholarloop-status: planned` instead of being presented as shipped.
The full table is in `docs/openapi/README.md`.

## Annotation 2 - two status codes that do not match the code

The brief asked for examples of `422 needs_input` and `409 active-run`. The frozen
error classes say otherwise:

- `NeedsUserInput.http_status == 409` (`backend/app/core/errors.py`), and
  `IntakeNeedsInput` keeps it (`backend/app/orchestrator/errors.py`);
- there is no "active-run" error class at all; the closest is `RunConflict` (409,
  code `run_conflict`).

The spec therefore documents `409 needs_user_input` and
`409 run_conflict` with `context.reason=active_run`, and *additionally* ships a
`422` example for the case where a `needs_input` answer arrives without the
information that was asked for (`examples/errors/needs_input.422.json`). The mismatch is
listed in `CONTRACT-CHANGE-REQUESTS` and in the README rather than papered over.
