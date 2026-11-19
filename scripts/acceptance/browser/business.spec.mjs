import { test, expect } from '@playwright/test'
import { performance } from 'node:perf_hooks'

// Browser plugin not available. This uses the repository's isolated Playwright server.
test.beforeEach(async ({ context, baseURL }) => {
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.href === 'https://api.github.com/repos/jelven007/TSP/releases/latest') {
      return route.fulfill({ json: { tag_name: 'v0.4.67', body: '' } })
    }
    return url.origin === baseURL ? route.continue() : route.abort('blockedbyclient')
  })
  const response = await context.request.post(baseURL + '/api/auth/login', {
    data: { email: 'admin', password: 'isolated-web-acceptance' },
  })
  expect(response.ok()).toBeTruthy()
})

async function observe(page, info, route, ready, action) {
  const errors = []
  const warnings = []
  const serverErrors = []
  const samples = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text())
    if (message.type() === 'warning') warnings.push(message.text())
  })
  page.on('response', response => {
    if (response.status() >= 500) serverErrors.push({ url: new URL(response.url()).pathname, status: response.status() })
  })
  try {
    // Three browser-cache samples, same temporary backend. Not a server cold-cache claim.
    for (let index = 0; index < 3; index++) {
      const start = performance.now()
      await page.goto(route)
      await ready()
      samples.push({ cache: index === 0 ? 'browser-cold' : 'browser-warm', elapsed_ms: performance.now() - start })
    }
    await action()
    await expect(page).toHaveTitle(/Tick Stock/)
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors, 'browser exceptions').toEqual([])
    expect(serverErrors, 'API server errors').toEqual([])
  } finally {
    const screenshot = info.outputPath('business.png')
    await page.screenshot({ path: screenshot, fullPage: false })
    await info.attach('business-screen', { path: screenshot, contentType: 'image/png' })
    await info.attach('business-evidence', {
      body: Buffer.from(JSON.stringify({
        route, viewport: info.project.name, samples, errors, warnings, serverErrors,
        scope: 'isolated standalone fixtures; authenticated setup uses real login API; not production P95',
      }, null, 2)), contentType: 'application/json',
    })
  }
}

async function selectDate(page, picker, target) {
  await picker.click()
  const calendar = page.getByRole('group', { name: /日历$/ })
  const [year, month] = target.split('-').map(Number)
  for (let attempt = 0; attempt < 18; attempt++) {
    const title = await calendar.getByRole('button', { name: /^\d{4} 年 \d+ 月$/ }).textContent()
    const parts = title.match(/(\d{4}) 年 (\d+) 月/)
    const offset = (Number(parts[1]) - year) * 12 + Number(parts[2]) - month
    if (!offset) break
    await calendar.getByRole('button', { name: offset > 0 ? '上个月' : '下个月', exact: true }).click()
  }
  await calendar.getByRole('button', { name: target, exact: true }).click()
  await expect(calendar).toHaveCount(0)
}

test('data profile, fixture sync and persisted completion', async ({ page, context, baseURL }, info) => {
  await observe(page, info, '/data',
    () => expect(page.getByRole('heading', { name: '数据总览', exact: true })).toBeVisible(),
    async () => {
      const started = page.waitForResponse(response => new URL(response.url()).pathname === '/api/pipeline/run' && response.request().method() === 'POST')
      await page.getByRole('button', { name: '立即同步', exact: true }).click()
      const confirmation = page.getByRole('button', { name: '仍然同步', exact: true })
      if (await confirmation.isVisible()) await confirmation.click()
      const response = await started
      expect(response.ok()).toBeTruthy()
      const { job_id: id } = await response.json()
      await expect.poll(async () => {
        return (await (await context.request.get(`${baseURL}/api/pipeline/jobs/${id}`)).json()).status
      }).toBe('succeeded')
      await page.reload()
      await expect(page.getByText(id, { exact: false }).first()).toBeVisible()
      const quality = await (await context.request.get(baseURL + '/api/data/status')).json()
      expect(quality.quality.latest_daily.coverage_ratio).toBe(1)
    })
})

test('strategy archive date navigation and empty result protection', async ({ page }, info) => {
  await observe(page, info, '/screener',
    () => expect(page.getByRole('button', { name: '策略日期', exact: true })).toBeVisible(),
    async () => {
      await selectDate(page, page.getByRole('button', { name: '策略日期', exact: true }), '2026-10-23')
      await expect(page.getByRole('button', { name: '策略日期', exact: true })).toContainText('2026-10-23')
      await page.getByRole('button', { name: '日线', exact: true }).click()
      await page.getByRole('button', { name: '分钟', exact: true }).click()
      await page.getByRole('button', { name: '日线', exact: true }).click()
      await expect(page.getByRole('button', { name: '历史结果', exact: true })).toBeDisabled()
      await page.reload()
      await expect(page.getByRole('button', { name: '策略日期', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: '历史结果', exact: true })).toBeDisabled()
    })
})

test('research form runs real fixture strategy and renders completion', async ({ page }, info) => {
  test.setTimeout(150_000)
  await observe(page, info, '/backtest?tab=strategy',
    () => expect(page.getByRole('button', { name: '运行回测', exact: true })).toBeVisible(),
    async () => {
      await expect(page.getByRole('button', { name: '运行回测', exact: true })).toBeDisabled()
      await page.getByRole('button', { name: /^验收价格策略/ }).click()
      await selectDate(page, page.getByRole('button', { name: /^\d{4}-\d{2}-\d{2}$/ }).nth(0), '2026-07-01')
      await selectDate(page, page.getByRole('button', { name: /^\d{4}-\d{2}-\d{2}$/ }).nth(1), '2026-10-23')
      await page.getByRole('button', { name: '运行回测', exact: true }).click()
      await expect(page.getByRole('button', { name: /交易明细/ })).toBeVisible({ timeout: 90_000 })
      await page.getByRole('button', { name: /交易明细/ }).click()
      await expect(page.getByText('600000.SH', { exact: true }).first()).toBeVisible()
      await expect(page.getByRole('button', { name: '运行回测', exact: true })).toBeEnabled()
    })
})

test('paper account invalid capital, create, freeze and unfreeze persist', async ({ page, context, baseURL }, info) => {
  const name = `浏览器资金-${info.project.name}`
  await observe(page, info, '/paper',
    () => expect(page.getByRole('button', { name: '单账户', exact: true })).toBeVisible(),
    async () => {
      await page.getByRole('button', { name: '单账户', exact: true }).click()
      await page.getByPlaceholder(/^账户 /).fill(name)
      await page.getByPlaceholder('1000000', { exact: true }).fill('0')
      await expect(page.getByRole('button', { name: '创建账户', exact: true })).toBeDisabled()
      await page.getByPlaceholder('1000000', { exact: true }).fill('100000')
      await page.getByRole('button', { name: '创建账户', exact: true }).click()
      await page.getByRole('table').getByText(name, { exact: true }).click()
      await page.getByRole('button', { name: '冻结账户', exact: true }).click()
      await expect(page.getByRole('button', { name: '解冻账户', exact: true })).toBeVisible()
      await page.reload()
      await page.getByRole('table').getByText(name, { exact: true }).click()
      await page.getByRole('button', { name: '解冻账户', exact: true }).click()
      await expect(page.getByRole('button', { name: '冻结账户', exact: true })).toBeVisible()
      const accounts = await (await context.request.get(baseURL + '/api/paper/accounts')).json()
      const account = accounts.accounts.find(row => row.name === name)
      expect(account).toBeTruthy()
      const overview = await (await context.request.get(`${baseURL}/api/paper/overview?account=${account.id}`)).json()
      expect(overview.cash).toBe(100000)
      expect(overview.status).toBe('active')
    })
})
