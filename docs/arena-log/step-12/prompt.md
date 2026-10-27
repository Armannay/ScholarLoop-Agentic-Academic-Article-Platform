# Arena step prompt - ScholarLoop frontend shell and API client

## Context
Repo: https://github.com/Armannay/ScholarLoop-Agentic-Academic-Article-Platform
Branch: arena/b04b6275-scholarloop-agentic-academic-a (from commit 63ada01 of main)
Read-only reference files (frozen contracts v1.1): backend/app/contracts/*.py, backend/app/core/errors.py, backend/app/providers/{base,errors,mock}.py, config/policies.yaml

## Task
Build the frontend shell and API client:
1. Read docs/openapi/openapi.yaml. Generate the typed TS client from it (`npm run api:gen`, commit the output).
2. Provide: router, layout, auth session (token kept in memory only), error boundary, shared components (Table, Modal, Toast, Badge), a useEventSource hook for SSE with reconnect, and a dev setup using the Prism mock from the spec.
3. Do NOT build feature pages.
4. Reuse only what is needed from the Step 0 status console under frontend/ and remove the rest.
5. Add a Playwright smoke test that the app loads against the mock.

## Quality gate
`make check` must pass. Open a PR to main when done, filling Step / Depends on / Contract changes. Save this prompt and raw output in docs/arena-log/step-<NN>/.
