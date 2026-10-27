# Step 12 - Frontend shell and API client (raw output)

## What was done

Built the ScholarLoop console frontend shell and typed API client on branch
`arena/b04b6275-scholarloop-agentic-academic-a`, replacing the Step 0 status-console
feature pages with the shell described in the task.

### Typed API client (`npm run api:gen`, output committed)
- Added `api:gen` script to `frontend/package.json`:
  `openapi-typescript ../docs/openapi/openapi.yaml -o src/api/openapi.gen.d.ts`
- Generated `src/api/openapi.gen.d.ts` (typed schemas, paths, operations from the
  frozen OpenAPI 3.1 spec) and committed it.
- Wrote `src/api/client.ts`: a thin fetch layer on top of the generated types with
  typed wrappers for every operation in the spec (health, version, config, contracts,
  auth register/login/me, projects, runs incl. create (202), decision, stop, events
  URL builder, history, digest, ledger, quality, article, sections, snapshots, restore,
  usage, errors catalog, accounts CRUD + test probe, agent bindings get/put), an
  `ApiError` class switching on `error.code`, bearer-token auth held in module memory
  only, unauthorized listeners, and 404→null for missing bindings.
- Wrote `src/api/auth.tsx`: `AuthProvider`/`useAuth` with login/register/logout; the
  session token lives in memory only (no localStorage/cookies — closing the tab ends
  the session); `/me` resolved once on boot.
- Wrote `src/api/useEventSource.ts`: SSE over fetch (browsers' EventSource cannot send
  Authorization headers) with Authorization + Last-Event-ID headers, replay from the
  last id, exponential-backoff reconnect (capped), fatal handling for 401/404, and
  reconnect-on-new-token.

### Shell
- `src/router.tsx`: minimal hash router (`/`, `/login`), `useHash()`/`navigate()`.
- `src/layout.tsx`: masthead, nav, signed-in/signed-out status, sign out.
- `src/app.tsx`: `AuthProvider` → `ErrorBoundary` → `ToastProvider` (+ `ToastList`) → `Router` inside `Layout`; `/` renders Home when signed in, Login otherwise.
- `src/pages/login.tsx`, `src/pages/home.tsx` (polls `/api/health` every 5s through the dev proxy).

### Shared components (`src/components/`)
`Badge` (ok/warn/bad/idle/neutral), `Table<T>`, `Modal` (portal, Escape/backdrop close),
`ToastProvider`/`useToast`/`ToastList` (auto-dismiss), `ErrorBoundary` (class component,
reset, fallback prop).

### Dev setup using the Prism mock
- `vite.config.ts`: `/api` proxied to `VITE_PROXY_TARGET`, defaulting to the Prism mock
  on `http://127.0.0.1:4010` (completion scenario) for both dev and preview.
- `npm run mock`: starts `docs/openapi/mock/server.mjs completion --port 4010`
  (Prism static mode; requires `make openapi-install` once).
- Verified: `curl /api/health` → 200 via the proxy; app loads at `/` (200) against the
  mock; `npm run build` (lint + vite build) and `npm run preview` both green.

### Playwright smoke test
- `frontend/playwright.config.ts` + `frontend/playwright/smoke.test.ts`: spawns Prism
  (composed spec) + Vite dev server, waits for `/api/health`, navigates to `/`,
  asserts the ScholarLoop heading and sign-in page, and checks `/api/health` returns
  `status: ok` through the mock.
- Playwright 1.64.1 added to devDependencies; the browser binary could not be
  downloaded in this sandbox (network allowlist), so the test could not be executed
  locally — it runs in any environment with Chromium (e.g. CI with
  `playwright install chromium`).

### Removed (Step 0 status console, not needed for this step)
`src/api.ts`, `src/auth.tsx`, `src/types.ts`, `src/useApi.ts`, `src/mockServer.ts`,
`src/openapi.json`, `src/App.tsx`, `src/AccountsPage.tsx`, `src/AgentsPage.tsx`,
`src/AuthPage.tsx`, `src/SchemaViewer.tsx`, `src/StatusPill.tsx`, `src/StatusConsole.tsx`,
`src/__tests__/app.test.tsx` (replaced by a shell SSR render test).

### Verification
- `npm run lint` (tsc --noEmit, strict): pass
- `npm test` (SSR render of the shell): 2/2 pass
- `npm run build` (lint + vite build): pass
- App loads against the Prism mock on http://localhost:5173 (dev) and 4173 (preview):
  `/api/health` → 200 `{"status":"ok", ...}`, `/` → 200

### Backend gate
No backend files changed; `make check` unaffected (re-run below for confirmation).
