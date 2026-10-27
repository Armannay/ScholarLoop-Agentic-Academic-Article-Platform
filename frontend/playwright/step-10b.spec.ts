import { expect, test, type Page, type TestInfo } from '@playwright/test'

/**
 * Playwright smoke test for the Step 10b flows.
 *
 * Deliberately end-to-end and short: register against the in-memory mock, create a
 * project from a Brief (including the inline-validation round trip), follow the run to
 * its checkpoint, answer it, then read and export the article. Every locator is a role
 * or a label, so the test fails if a screen stops being reachable by keyboard or a
 * screen reader.
 */

const PASSWORD = 'placeholder-pass'

/**
 * Register a fresh operator. The email is unique per test (and per retry) so a retried
 * test never trips the mock's duplicate-email 409.
 */
async function register(page: Page, testInfo: TestInfo): Promise<void> {
  const email = `smoke-${testInfo.workerIndex}-${testInfo.retry}-${testInfo.title.length}@example.org`
  await page.goto('/#/login')
  await expect(page.getByRole('heading', { name: /sign in to scholarloop/i })).toBeVisible()
  await page.getByRole('button', { name: /need an account\? register/i }).click()
  await expect(page.getByRole('heading', { name: /create an account/i })).toBeVisible()
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible({ timeout: 15_000 })
}

test.describe('ScholarLoop Step 10b smoke', () => {
  test('project -> brief -> run -> checkpoint -> article -> export', async ({ page }, testInfo) => {
    await register(page, testInfo)

    // Project list (seeded fixtures live in the mock).
    await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Projects' }).click()
    await expect(page.getByRole('heading', { name: 'Projects' })).toBeVisible()
    await expect(page.getByText('Peer review checklists 2026')).toBeVisible()

    // New project: inline validation first, then a valid brief.
    await page.getByRole('button', { name: /new project/i }).first().click()
    await expect(page.getByRole('heading', { name: 'New project' })).toBeVisible()

    await page.getByRole('button', { name: /create project & run/i }).click()
    await expect(page.getByRole('alert').filter({ hasText: /need attention/i })).toBeVisible()
    await expect(page.getByText('Project name is required.')).toBeVisible()
    await expect(page.getByLabel(/project name/i)).toBeFocused()

    await page.getByLabel(/project name/i).fill('peer-review-smoke')
    await page.getByLabel(/^topic/i).fill('Tightening peer review with structured checklists')
    await page.getByLabel(/audience/i).fill('CS faculty and graduate reviewers')
    await page.getByLabel(/language/i).fill('en')
    await page.getByLabel(/target words/i).fill('850')
    await page.getByLabel(/must include/i).fill('a comparison of checklist and free-form reviews')

    await page.getByRole('button', { name: /create project & run/i }).click()

    // The run view: state badge, plan board, gates, meters, live feed.
    await expect(page.getByText('Waiting for you').first()).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Plan' })).toBeVisible()
    await expect(
      page.getByText('Draft the section outline and the contribution statement'),
    ).toBeVisible()
    await expect(page.getByRole('progressbar', { name: 'Tokens' })).toBeVisible()
    await expect(page.getByRole('progressbar', { name: 'Est. cost' })).toBeVisible()
    await expect(page.getByText('run_created').first()).toBeVisible()
    await expect(page.locator('.gate-chip')).toHaveCount(8)

    // Checkpoint modal -> Finish.
    await page.getByRole('button', { name: /review checkpoint/i }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    await expect(dialog.getByRole('radio')).toHaveCount(3)
    await dialog.getByRole('radio', { name: /finish/i }).check()
    await dialog.getByRole('button', { name: 'Finish', exact: true }).click()
    await expect(page.getByText('Done').first()).toBeVisible()

    // Article viewer: sections, history, export.
    await page.getByRole('button', { name: /open article/i }).click()
    await expect(page.getByRole('heading', { name: 'Article' })).toBeVisible()
    await expect(page.getByText('Methodology')).toBeVisible()
    await page.getByRole('button', { name: /version history/i }).first().click()
    await expect(page.getByText(/Section abstract/)).toBeVisible()

    const download = page.waitForEvent('download')
    await page.getByRole('button', { name: /export \.md/i }).click()
    const file = await download
    expect(file.suggestedFilename()).toMatch(/^scholarloop-.*\.md$/)
    await expect(page.getByText(/exported scholarloop-/i)).toBeVisible()
  })

  test('a needs_input checkpoint offers only legal options', async ({ page }, testInfo) => {
    await register(page, testInfo)
    await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Projects' }).click()
    await page.getByRole('button', { name: 'Peer review checklists 2026' }).click()

    await page.getByLabel(/mock scenario/i).selectOption('needs-input')
    await page.getByRole('button', { name: /start a run/i }).click()

    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    await expect(dialog.getByText(/needs your input/i)).toBeVisible()
    // finish is not legal while the loop is blocked on the user.
    await expect(dialog.getByRole('radio')).toHaveCount(2)
    await dialog.getByLabel(/note to the planner/i).fill('The DOI is 10.1000/xyz123.')
    await dialog.getByRole('button', { name: /continue/i }).click()
    await expect(dialog).toBeHidden()
  })

  test('a Persian project renders the article right-to-left', async ({ page }, testInfo) => {
    await register(page, testInfo)
    await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Projects' }).click()
    await page.getByRole('button', { name: /اخلاق/ }).click()
    await page.getByRole('button', { name: 'Open' }).first().click()
    await page.getByRole('button', { name: /open article/i }).click()

    const body = page.locator('.article-body')
    await expect(body).toHaveAttribute('dir', 'rtl')
    await expect(body).toHaveAttribute('lang', 'fa')
    await expect(page.getByText('مقدمه')).toBeVisible()
  })
})
