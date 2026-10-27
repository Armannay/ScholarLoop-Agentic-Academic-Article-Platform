# Step 11 — Projects API (backend)

Saved verbatim as the record of what was asked. Nothing below was edited; where the
prompt and `docs/openapi/openapi.yaml` disagree, the divergence is logged in
`spec-change-requests.md` and in the PR body rather than being papered over.

---

## Context

```
CONTEXT: Repo https://github.com/Armannay/ScholarLoop-Agentic-Academic-Article-Platform. Create your branch from the latest main.
FIRST read these files (do not modify them) and confirm in one line each that you read them: everything under backend/app/contracts/, backend/app/core/errors.py, backend/app/providers/{base,errors,mock}.py, config/policies.yaml, plus the EXTRA FILES below. Contracts are frozen (tag <contracts-v1.1>). If you need a contract change, do NOT edit it: list it under CONTRACT-CHANGE-REQUESTS.
Quality gate: make check must pass. Open a PR to main when done, filling Step / Depends on / Contract changes. Save this prompt and your raw output in docs/arena-log/step-<NN>/.
```

## Task

```
< first the Projects PR (backend), after the API PR is merged:

TASK: Implement the Projects API exactly as in docs/openapi/openapi.yaml (read it; do not change it): POST/GET/PATCH/DELETE /projects with cursor pagination. Add an Alembic migration for projects (id ULID, owner_id, name, brief JSON validated by contracts.Brief, policy_overrides JSON validated through core.policies, status, timestamps). Enforce ownership per user using the auth from Step 1. PATCH/DELETE return 409 when a run is active (use a RunsQuery Protocol with an in-memory fake until Step 9b is merged). Invalid Brief returns 422 with per-field errors. Tests: CRUD, authorization (user B cannot read user A's project), pagination, validation, 409. No secrets in logs.>
```

## Extra files

```
<backend/app/accounts/, backend/app/api/, docs/openapi/.>
```
