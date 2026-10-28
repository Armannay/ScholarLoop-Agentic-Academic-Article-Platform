# Step 10b follow-up: cost availability and Vite security update

**Step:** 10b follow-up — correct unsupported monetary reporting and retain the Vite 7.3.7 security update.

**Depends on:** Step 10b UI from merged PR #29 (`main` at `e8b0762` when rebased). The existing console implementation is intentionally not duplicated in this PR.

**Contract changes:** None. CONTRACTS v1.1, `docs/openapi/openapi.yaml`, and policy files remain unchanged. The prospective `usd_spent` / `usd_cap` fields remain a documented follow-up in `spec-change-requests.md`.

## Changes unique to this PR

- Remove the uncontracted USD rate table, token-cap-equivalent denominator, and role-level dollar estimates. Run usage and stop reports now state **Cost unavailable** because the API reports neither billed USD nor a USD limit; token and iteration meters remain API-backed.
- Upgrade Vite from 7.2.2 to 7.3.7 and refresh its lockfile dependencies.
- Add a concise correction note and update the related tests and API-surface follow-up.

## Verification

- `make check` — 1,162 passed, 8 skipped.
- `make schemas-check` — 23 contract schemas up to date.
- Frontend lint, Vitest, and production build — 48 tests passed; build completed with Vite 7.3.7.
- `npm audit --audit-level=low` — 0 vulnerabilities.
- Secret scan — clean.
- Playwright smoke tests are discovered (3 tests) but were not executed because Chromium is unavailable in this sandbox; the earlier browser download failed with `ECONNRESET`.
