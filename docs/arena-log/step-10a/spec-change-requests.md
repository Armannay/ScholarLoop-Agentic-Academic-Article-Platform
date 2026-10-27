# SPEC-CHANGE-REQUESTS — none required for step 10a

Step 10a is UI-only and implements an already-designed OpenAPI surface. No file under
`backend/app/contracts/` was edited, `docs/schemas/` is untouched (`make schemas-check`
still passes), and `docs/openapi/openapi.yaml` was read, not modified.

The endpoint set this UI consumes — `POST /api/auth/register`, `POST /api/auth/login`,
`GET /api/auth/me`, `GET/POST /api/accounts`, `GET/PATCH/DELETE /api/accounts/{id}`,
`POST /api/accounts/{id}/test`, `GET/PUT /api/agents/{role}/binding` — is served by the
backend already wired behind those routes, and the frontend's typed client
(`frontend/src/api.ts`) targets exactly those paths. No endpoints were invented.

If a later OpenAPI revision changes any of these paths, the delta goes through
CONTRACT-CHANGE-REQUEST / spec-change-requests, not into the UI source. `AccountCreate`
and `AccountPatch` keep `api_key` write-only (`writeOnly: true`) and `AccountView`
never exposes key material or ciphertext, matching `AccountPublic` from the frozen
contracts.

CONTRACTS v1 is untouched and no CONTRACT-CHANGE-REQUEST is required — the UI never
crosses an agent, ledger or provider boundary; it only renders redacted views.
