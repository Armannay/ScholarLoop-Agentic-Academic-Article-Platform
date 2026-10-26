import assert from 'node:assert/strict'
import { beforeEach, describe, it } from 'node:test'
import { renderToStaticMarkup } from 'react-dom/server'
import openapi from '../openapi.json'
import { AccountsPage } from '../AccountsPage'
import { AgentsPage } from '../AgentsPage'
import { AppShell } from '../App'
import {
  ApiError,
  createAccount,
  deleteAccount,
  fetchAllBindings,
  fetchBinding,
  fetchConfig,
  fetchCurrentUser,
  fetchHealth,
  listAccounts,
  loginUser,
  patchAccount,
  putBinding,
  registerUser,
  setAccessToken,
  setCustomTransport,
  streamRunEvents,
  subscribeUnauthorized,
  testAccount,
} from '../api'
import { AuthProvider } from '../auth'
import { MockApiServer } from '../mockServer'
import { EXECUTING_AGENT_ROLES, PRIMARY_BINDING_ROLES, type UserView } from '../types'

const ANTHROPIC_KEY_1 = 'sk-ant-api03-placeholder-1234'
const ANTHROPIC_KEY_2 = 'sk-ant-api03-placeholder-9876'
const OAUTH_SUBSCRIPTION_TOKEN = 'sk-ant-oat01-placeholder-5555'
const ROUTER_KEY = 'or-placeholder-key-7788'
const MOCK_KEY = 'mock-placeholder-key-0001'
const USER_CRED = 'placeholder-pass'

describe('ScholarLoop 10a Frontend (OpenAPI + MockServer + Accounts + Agents)', () => {
  let server: MockApiServer

  beforeEach(() => {
    server = new MockApiServer()
    setCustomTransport((url, init) => server.fetch(url, init))
    setAccessToken(null)
  })

  it('ships a complete OpenAPI 3.1 spec with write-only keys and all required paths', () => {
    assert.equal(openapi.openapi, '3.1.0')
    assert.ok(openapi.components.securitySchemes.bearerAuth)
    const requiredPaths = [
      '/api/health',
      '/api/config',
      '/api/contracts',
      '/api/contracts/{name}/schema',
      '/api/auth/register',
      '/api/auth/login',
      '/api/auth/me',
      '/api/accounts',
      '/api/accounts/{account_id}',
      '/api/accounts/{account_id}/test',
      '/api/agents/{role}/binding',
      '/api/projects/{project_id}/runs',
      '/api/runs/{run_id}',
      '/api/runs/{run_id}/decision',
      '/api/runs/{run_id}/stop',
      '/api/runs/{run_id}/events',
    ]
    for (const p of requiredPaths) {
      assert.ok(p in openapi.paths, `missing OpenAPI path ${p}`)
    }
    assert.equal(openapi.components.schemas.AccountCreate.properties.api_key.writeOnly, true)
    assert.ok(!('api_key' in openapi.components.schemas.AccountView.properties))
    assert.ok(!('key_ciphertext' in openapi.components.schemas.AccountView.properties))
  })

  it('handles register, login, /me, duplicate 409, and 401 unauthorized listener', async () => {
    const reg = await registerUser({ email: 'Alice@Example.org', password: USER_CRED })
    assert.equal(reg.user.email, 'alice@example.org')
    setAccessToken(reg.access_token)

    const me = await fetchCurrentUser()
    assert.equal(me.id, reg.user.id)

    await assert.rejects(
      () => registerUser({ email: 'alice@example.org', password: USER_CRED }),
      (err: unknown) => err instanceof ApiError && err.status === 409 && err.code === 'conflict',
    )

    const loggedIn = await loginUser({ email: 'alice@example.org', password: USER_CRED })
    assert.ok(loggedIn.access_token.length > 0)

    await assert.rejects(
      () => loginUser({ email: 'alice@example.org', password: 'wrong-placeholder' }),
      (err: unknown) => err instanceof ApiError && err.status === 401,
    )

    let unauthorizedCount = 0
    const unsub = subscribeUnauthorized(() => {
      unauthorizedCount += 1
    })
    setAccessToken('expired-invalid-token')
    await assert.rejects(() => listAccounts(), (err: unknown) => err instanceof ApiError && err.status === 401)
    assert.equal(unauthorizedCount, 1)
    unsub()
  })

  it('supports account CRUD, masking, key rotation, budgets, key validation, and test probe', async () => {
    const { access_token } = await registerUser({ email: 'ops@example.org', password: USER_CRED })
    setAccessToken(access_token)

    // Rejects Pro/Max subscription OAuth tokens without echoing the token
    await assert.rejects(
      () =>
        createAccount({
          provider: 'anthropic',
          label: 'bad-oauth',
          api_key: OAUTH_SUBSCRIPTION_TOKEN,
        }),
      (err: unknown) =>
        err instanceof ApiError &&
        err.status === 422 &&
        err.message.includes('Pro/Max') &&
        !err.message.includes(OAUTH_SUBSCRIPTION_TOKEN),
    )

    // Create Anthropic account with monthly budget
    const created = await createAccount({
      provider: 'anthropic',
      label: 'claude-main',
      api_key: ANTHROPIC_KEY_1,
      budget: { tokens: 500_000, usd: 25 },
    })
    assert.equal(created.key_mask, '****1234')
    assert.equal(created.key_fingerprint.length, 12)
    assert.equal(created.budget.tokens, 500_000)
    assert.equal(created.budget.usd, 25)
    assert.ok(!JSON.stringify(created).includes(ANTHROPIC_KEY_1))

    // Rotate key + clear budget + relabel
    const rotated = await patchAccount(created.id, {
      label: 'claude-rotated',
      api_key: ANTHROPIC_KEY_2,
      budget: { tokens: null, usd: null },
    })
    assert.equal(rotated.label, 'claude-rotated')
    assert.equal(rotated.key_mask, '****9876')
    assert.notEqual(rotated.key_fingerprint, created.key_fingerprint)
    assert.equal(rotated.budget.tokens, null)
    assert.equal(rotated.budget.usd, null)

    // Test connection active vs disabled
    const okProbe = await testAccount(created.id)
    assert.equal(okProbe.ok, true)
    assert.equal(okProbe.model, 'claude-sonnet-4')

    await patchAccount(created.id, { status: 'disabled' })
    const disabledProbe = await testAccount(created.id)
    assert.equal(disabledProbe.ok, false)
    assert.equal(disabledProbe.error_code, 'account_disabled')
  })

  it('enforces planner provider rules, multi-account strategies, and 409 delete replacement/disable flows', async () => {
    const { access_token } = await registerUser({ email: 'lead@example.org', password: USER_CRED })
    setAccessToken(access_token)

    const claudeA = await createAccount({
      provider: 'anthropic',
      label: 'claude-a',
      api_key: ANTHROPIC_KEY_1,
    })
    const claudeB = await createAccount({
      provider: 'mock',
      label: 'mock-fallback',
      api_key: MOCK_KEY,
    })
    const router = await createAccount({
      provider: 'openai_compatible',
      label: 'openrouter',
      api_key: ROUTER_KEY,
      base_url: 'https://openrouter.example/api/v1',
    })

    // Unconfigured binding returns null
    assert.equal(await fetchBinding('planner'), null)

    // Planner rejects openai_compatible
    await assert.rejects(
      () =>
        putBinding('planner', {
          account_ids: [router.id],
          strategy: 'dedicated',
          model: 'claude-opus-4',
          max_input_tokens: 24_000,
          max_output_tokens: 4_096,
          enabled: true,
        }),
      (err: unknown) => err instanceof ApiError && err.status === 422 && err.message.includes('planner'),
    )

    // Failover with 1 account is rejected
    await assert.rejects(
      () =>
        putBinding('writer', {
          account_ids: [claudeA.id],
          strategy: 'failover',
          model: 'claude-sonnet-4',
          max_input_tokens: 24_000,
          max_output_tokens: 4_096,
          enabled: true,
        }),
      (err: unknown) => err instanceof ApiError && err.status === 422,
    )

    // Bind planner to claudeA (dedicated) and writer to router (dedicated)
    await putBinding('planner', {
      account_ids: [claudeA.id],
      strategy: 'dedicated',
      model: 'claude-opus-4',
      max_input_tokens: 24_000,
      max_output_tokens: 4_096,
      enabled: true,
    })
    await putBinding('writer', {
      account_ids: [router.id],
      strategy: 'dedicated',
      model: 'gpt-4.1-mini',
      max_input_tokens: 24_000,
      max_output_tokens: 4_096,
      enabled: true,
    })

    // Deleting claudeA without flags returns 409 with roles: ['planner']
    let conflictRoles: unknown = null
    await assert.rejects(
      () => deleteAccount(claudeA.id),
      (err: unknown) => {
        if (err instanceof ApiError && err.status === 409) {
          conflictRoles = err.context?.roles
          return true
        }
        return false
      },
    )
    assert.deepEqual(conflictRoles, ['planner'])

    // Resolve 409 via replacement_account_id -> swaps planner to claudeB
    const replaced = await deleteAccount(claudeA.id, { replacementAccountId: claudeB.id })
    assert.deepEqual(replaced.replaced_in, ['planner'])
    const updatedPlanner = await fetchBinding('planner')
    assert.deepEqual(updatedPlanner?.account_ids, [claudeB.id])

    // Resolve 409 on router via disable_roles=true -> disables writer
    const disabledRes = await deleteAccount(router.id, { disableRoles: true })
    assert.deepEqual(disabledRes.disabled_roles, ['writer'])
    const updatedWriter = await fetchBinding('writer')
    assert.equal(updatedWriter?.enabled, false)
  })

  it('streams authenticated SSE RunEvent frames for Step 10b compatibility', async () => {
    const { access_token } = await registerUser({ email: 'sse@example.org', password: USER_CRED })
    setAccessToken(access_token)

    const createRes = await server.fetch('http://scholarloop.local/api/projects/demo/runs', {
      method: 'POST',
      headers: {
        authorization: `Bearer ${access_token}`,
        'content-type': 'application/json',
      },
      body: JSON.stringify({ quick_edit: { section_id: 'intro', instructions: 'tighten' } }),
    })
    assert.equal(createRes.status, 202)
    const run = (await createRes.json()) as { id: string }

    const events: string[] = []
    await streamRunEvents(run.id, (ev) => events.push(ev.type), { once: true })
    assert.deepEqual(events, ['run_created', 'checkpoint_reached'])
  })

  it('renders AuthPage, AccountsPage, and AgentsPage (planner + 5 roles) cleanly without secrets', async () => {
    const health = await fetchHealth()
    assert.equal(health.status, 'ok')
    const cfg = await fetchConfig()

    // Unauthenticated shell renders AuthPage
    const unauthHtml = renderToStaticMarkup(
      <AuthProvider>
        <AppShell />
      </AuthProvider>,
    )
    assert.ok(unauthHtml.includes('Sign in to ScholarLoop'))

    // Authenticated data setup
    const { access_token, user } = await registerUser({ email: 'ui@example.org', password: USER_CRED })
    setAccessToken(access_token)
    const acc = await createAccount({
      provider: 'anthropic',
      label: 'lab-claude',
      api_key: ANTHROPIC_KEY_1,
      budget: { tokens: 100_000, usd: 15 },
    })
    for (const role of PRIMARY_BINDING_ROLES) {
      await putBinding(role, {
        account_ids: [acc.id],
        strategy: 'dedicated',
        model: 'claude-sonnet-4',
        max_input_tokens: 24_000,
        max_output_tokens: 4_096,
        enabled: true,
      })
    }
    const accounts = await listAccounts()
    const bindings = await fetchAllBindings()

    const accountsHtml = renderToStaticMarkup(
      <AccountsPage
        accounts={accounts}
        bindings={bindings}
        loading={false}
        error={null}
        onRefresh={() => undefined}
      />,
    )
    assert.ok(accountsHtml.includes('lab-claude'))
    assert.ok(accountsHtml.includes('****1234'))
    assert.ok(accountsHtml.includes(`fp:${acc.key_fingerprint}`))
    assert.ok(accountsHtml.includes('$15.00/mo'))
    assert.ok(!accountsHtml.includes(ANTHROPIC_KEY_1))

    const agentsHtml = renderToStaticMarkup(
      <AgentsPage
        accounts={accounts}
        bindings={bindings}
        policies={cfg.policies}
        loading={false}
        onRefresh={() => undefined}
        onOpenAccounts={() => undefined}
      />,
    )
    assert.ok(agentsHtml.includes('Planner (planning &amp; evaluation only)'))
    for (const role of EXECUTING_AGENT_ROLES) {
      assert.ok(agentsHtml.includes(`<h2>${role}</h2>`), `missing role heading ${role}`)
    }

    const shellUser: UserView = user
    const authedShellHtml = renderToStaticMarkup(
      <AuthProvider initialUser={shellUser}>
        <AppShell initialPage="agents" />
      </AuthProvider>,
    )
    assert.ok(authedShellHtml.includes('ui@example.org'))
    assert.ok(authedShellHtml.includes('Sign out'))
  })
})
