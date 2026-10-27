# Step 10a — Login/register + Accounts + Agents UI (frontend)

Saved verbatim as the record of what was asked. Nothing below was edited; where the
prompt and `docs/openapi/openapi.yaml` disagree, the divergence is logged in
`spec-change-requests.md` and in the PR body rather than being papered over.

---

## Context

```
CONTEXT: Repo https://github.com/Armannay/ScholarLoop-Agentic-Academic-Article-Platform. Create your branch from the latest main.
FIRST read these files (do not modify them) and confirm in one line each that you read them: everything under backend/app/contracts/, backend/app/core/errors.py, backend/app/providers/{base,errors,mock}.py, config/policies.yaml, plus the EXTRA FILES below. Contracts are frozen (tag <contracts-v1.1>). If you need a contract change, do NOT edit it: list it under CONTRACT-CHANGE-REQUESTS.
Quality gate: make check must pass. Open a PR to main when done, filling Step / Depends on / Contract changes. Save this prompt and your raw output in docs/arena-log/step-<NN>/.                                                                                                                 Use the generated client and shell from main. Do not invent endpoints; if one is missing, list it under CONTRACT-CHANGE-REQUESTS for the API spec.
```

## Task

```
10a: login/register; Accounts (list with masked keys, add, edit and rotate key, delete with the 409 replacement flow, Test connection, budget fields, status badge); Agents (planner plus the 5 roles: choose account(s), strategy dedicated/round_robin/failover, model, max tokens, enable/disable).
```

## Extra files

```
<frontend/src/, docs/openapi/openapi.yaml (read-only)>
```
