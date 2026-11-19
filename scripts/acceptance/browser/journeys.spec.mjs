import { test, expect } from '@playwright/test'
import { performance } from 'node:perf_hooks'

test('login, persistent watchlist form and logout protection', async ({ page, context, baseURL }, info) => {
  const errors = []
  const warnings = []
  const requests = []
  const external = []
  const substitutions = []
  const timings = []
  const symbol = info.project.name === 'desktop' ? '600000.SH' : '000001.SZ'
  const group = `浏览器验收-${info.project.name}`
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text())
    if (message.type() === 'warning') warnings.push(message.text())
  })
  page.on('response', response => {
    const url = new URL(response.url())
    if (url.pathname.startsWith('/api/')) {
      requests.push({ path: url.pathname, status: response.status() })
    }
  })
  // The browser may only reach this fresh fixture, never an external provider.
  await context.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.href === 'https://api.github.com/repos/jelven007/TSP/releases/latest') {
      substitutions.push('application release check: local fixture response')
      return route.fulfill({
        json: { tag_name: 'v0.4.67', html_url: 'https://github.com/jelven007/TSP/releases/tag/v0.4.67', body: '' },
      })
    }
    if (url.origin !== baseURL) {
      external.push(url.origin + url.pathname)
      return route.abort('blockedbyclient')
    }
    return route.continue()
  })
  await page.addInitScript(() => {
    window.__acceptanceLongTasks = []
    window.__acceptanceScrolls = []
    window.addEventListener('scroll', event => {
      const input = document.querySelector('input[placeholder="搜索…"]')
      if (input === document.activeElement) {
        window.__acceptanceScrolls.push({
          target: event.target instanceof Element ? event.target.tagName : 'document',
          popup: !!document.querySelector('[data-watchlist-search-results]'),
          time_ms: performance.now(),
        })
      }
    }, { capture: true, passive: true })
    new PerformanceObserver(list => {
      for (const entry of list.getEntries()) {
        window.__acceptanceLongTasks.push({ start: entry.startTime, duration: entry.duration })
      }
    }).observe({ type: 'longtask', buffered: true })
  })

  async function step(name, action) {
    await test.step(name, async () => {
      const started = performance.now()
      await action()
      timings.push({ name, elapsed_ms: performance.now() - started })
    })
  }
  async function screenshot(name) {
    const file = info.outputPath(`${name}.png`)
    await page.screenshot({ path: file, fullPage: false })
    await info.attach(name, { path: file, contentType: 'image/png' })
  }

  try {
    await step('protected entry requires login', async () => {
      await page.goto('/watchlist')
      await expect(page).toHaveURL(/\/login/)
      await expect(page.getByRole('heading', { name: '登录', exact: true })).toBeVisible()
      await expect(page).toHaveTitle(/Tick Stock/)
      await screenshot('login')
    })
    await step('login renders market', async () => {
      await page.getByRole('textbox', { name: '用户名 / 邮箱' }).fill('admin')
      await page.getByPlaceholder('输入密码', { exact: true }).fill('isolated-web-acceptance')
      await page.getByRole('button', { name: '登录', exact: true }).click()
      await expect(page).toHaveURL(baseURL + '/')
      await expect(page.getByRole('heading', { name: '行情', exact: true })).toBeVisible()
      await screenshot('market')
    })
    for (let index = 0; index < 3; index++) {
      await step(index === 0 ? 'watchlist browser-cold entry' : `watchlist browser-warm entry ${index}`, async () => {
        await page.goto('/watchlist')
        await expect(page.getByRole('button', { name: '管理自选分组' })).toBeVisible()
      })
    }
    await step('watchlist empty state and create group', async () => {
      if (info.project.name === 'narrow') {
        await page.getByRole('button', { name: '打开菜单', exact: true }).click()
      }
      await page.getByRole('link', { name: '自选', exact: true }).click()
      await expect(page).toHaveURL(/\/watchlist/)
      await page.getByRole('button', { name: '管理自选分组' }).click()
      const dialog = page.getByRole('dialog')
      await dialog.getByPlaceholder('新分组名称').fill(group)
      await dialog.getByRole('button', { name: '新建', exact: true }).click()
      await expect(dialog.getByText(group, { exact: true })).toBeVisible()
      await dialog.getByRole('button', { name: '关闭', exact: true }).click()
      await page.getByRole('tab', { name: group }).click()
    })
    await step('search and add fixture instrument', async () => {
      await page.getByPlaceholder('搜索…', { exact: true }).fill(symbol.slice(0, 6))
      await page.getByRole('button', { name: `快速加入自选 ${symbol}`, exact: true }).click()
      await page.getByPlaceholder('搜索…', { exact: true }).fill('')
      await expect(page.locator('[data-watchlist-search-results]')).toHaveCount(0)
      await expect(page.locator('table tbody tr').filter({ hasText: symbol })).toBeVisible()
      const response = await context.request.get(baseURL + '/api/watchlist')
      expect(response.ok()).toBeTruthy()
      expect((await response.json()).symbols.some(row => row.symbol === symbol)).toBeTruthy()
      await screenshot('watchlist-saved')
    })
    await step('reload preserves group and member', async () => {
      await page.reload()
      await page.getByRole('tab', { name: group }).click()
      await expect(page.locator('table tbody tr').filter({ hasText: symbol })).toBeVisible()
      await expect(page.locator('vite-error-overlay')).toHaveCount(0)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy()
    })
    await step('logout removes authenticated access', async () => {
      await page.goto('/settings?tab=account')
      await page.getByRole('button', { name: '退出登录', exact: true }).click()
      await expect(page).toHaveURL(/\/login/)
      expect((await context.request.get(baseURL + '/api/watchlist')).status()).toBe(401)
      await page.goto('/watchlist')
      await expect(page.getByRole('heading', { name: '登录', exact: true })).toBeVisible()
    })
    expect(external, 'unexpected external browser requests').toEqual([])
    expect(requests.filter(row => row.status >= 500), 'server errors').toEqual([])
    expect(errors, 'browser runtime and console errors').toEqual([])
  } finally {
    const browserPerformance = await page.evaluate(() => ({
      navigation: performance.getEntriesByType('navigation').map(entry => entry.toJSON()),
      long_tasks: window.__acceptanceLongTasks,
      search_scrolls: window.__acceptanceScrolls,
      resources: performance.getEntriesByType('resource').map(entry => ({
        path: new URL(entry.name).pathname,
        duration_ms: entry.duration,
        bytes: entry.transferSize,
      })),
    })).catch(() => null)
    await info.attach('journey-evidence', {
      body: Buffer.from(JSON.stringify({
        scope: 'standalone fixture; three browser-cache entry samples per viewport, not production P95',
        timings, errors, warnings, requests, external, substitutions, browserPerformance,
      }, null, 2)),
      contentType: 'application/json',
    })
  }
})
