import { chromium } from '/Users/bytedance/.npm/_npx/e41f203b7505f1fb/node_modules/playwright/index.mjs'
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'

const output = '/tmp/tspp-indices-browser'
await fs.mkdir(output, { recursive: true })
const catalog = [
  ['000001.SH', '上证指数'], ['399001.SZ', '深证成指'], ['399006.SZ', '创业板指'],
  ['000680.SH', '科创综指'], ['000300.SH', '沪深300'], ['000016.SH', '上证50'],
  ['399330.SZ', '深证100'],
].map(([symbol, name]) => ({ symbol, name, code: symbol.split('.')[0], asset_type: 'index' }))
const calls = [], errors = [], checks = []
let job = null, jobSequence = 0, jobStatus = 'running', repairError = false, empty = false, klineError = false
const browser = await chromium.launch({
  executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true,
})
async function createPage(theme = 'light', width = 1600, canManage = true) {
  const context = await browser.newContext({ viewport: { width, height: 1000 }, timezoneId: 'Asia/Shanghai' })
  await context.addInitScript(theme => localStorage.setItem('tf-theme', theme), theme)
  await context.route('**/api/**', async route => {
    const req = route.request(), url = new URL(req.url()), path = url.pathname
    if (!path.startsWith('/api/')) return route.continue()
    calls.push({ path, search: url.search, method: req.method(), body: req.postDataJSON() })
    let data = { rows: [], count: 0, items: [], total: 0, menus: [], groups: [], status: 'ok' }, status = 200
    if (path === '/api/auth/status') data = {
      auth_required: true, authenticated: true, multitenancy_enabled: true,
      user: { id: 'browser-fixture', auth_version: 1, username: 'fixture', permissions: [
        'business:read', 'business:write', ...(canManage ? ['data:manage', 'settings:manage'] : []),
      ] },
    }
    else if (path === '/api/settings' || path === '/api/business/settings') data = { onboarding_completed: true }
    else if (path.endsWith('/preferences')) data = { realtime_quotes_enabled: false, watchlist_groups_in_nav: false }
    else if (path.includes('capability-matrix') || path === '/api/business/capabilities') data = {
      capabilities: ['daily', 'minute'].map(id => ({ id, usable: true, effective: 'fixture', effective_display: '本地验证' })),
    }
    else if (path === '/api/capabilities') data = { capabilities: { 'kline.daily.batch': true, 'kline.minute.batch': true } }
    else if (path === '/api/version') data = { version: '0.4.34' }
    else if (path === '/api/index/catalog') data = { rows: catalog, count: 7 }
    else if (path === '/api/intraday/indices') data = {
      rows: catalog.map((row, i) => ({ ...row, last_price: 3000 + i * 200, change_pct: i % 2 ? -0.53 : 1.25 })),
      count: 7,
    }
    else if (path === '/api/index/kline') {
      const symbol = url.searchParams.get('symbol'), period = url.searchParams.get('period')
      const finish = new Date(url.searchParams.get('end_date') + 'T00:00:00Z')
      const rows = []
      for (let i = 129; i >= 0; i--) {
        const d = new Date(finish)
        if (period === '1d') d.setUTCDate(d.getUTCDate() - i)
        else if (period === '1w') d.setUTCDate(d.getUTCDate() - i * 7)
        else d.setUTCMonth(d.getUTCMonth() - i)
        const trade_date = d.toISOString().slice(0, 10)
        if (period === '1w') d.setUTCDate(d.getUTCDate() - (d.getUTCDay() + 6) % 7)
        if (period === '1mo') d.setUTCDate(1)
        const n = 129 - i, close = 3200 + n * 2 + Math.sin(n / 5) * 35
        rows.push({
          date: d.toISOString().slice(0, 10), trade_date, open: close - Math.cos(n) * 8,
          high: close + 15, low: close - 17, close, volume: 1e8 + n * 1e6, amount: 1e11,
          ma5: close - 4, ma10: close - 8, ma20: close - 15, ma60: close - 25,
          macd_dif: Math.sin(n / 5) * 3, macd_dea: Math.sin(n / 5 - .3) * 2.5,
          macd_hist: Math.cos(n / 5), rsi_6: 55 + Math.sin(n / 5) * 15,
          rsi_14: 50, rsi_24: 52, kdj_k: 55, kdj_d: 50, kdj_j: 65,
          boll_upper: close + 45, boll_lower: close - 45,
        })
      }
      data = klineError ? { detail: '指数K线验证错误' } : { symbol, period, rows: empty ? [] : rows, source: 'local' }
      if (klineError) status = 502
    }
    else if (path === '/api/index/minute') {
      const day = url.searchParams.get('date')
      data = { symbol: url.searchParams.get('symbol'), date: day, prev_close: 3450,
        rows: Array.from({ length: 120 }, (_, i) => ({
          datetime: `${day}T${String(9 + Math.floor((30 + i) / 60)).padStart(2, '0')}:${String((30 + i) % 60).padStart(2, '0')}:00`,
          open: 3450 + i / 3, high: 3455 + i / 3, low: 3445 + i / 3, close: 3450 + i / 3 + Math.sin(i / 8) * 5,
          volume: 1e6, amount: 1e9,
        })), source: 'fixture' }
    }
    else if (path === '/api/index/repair') {
      if (repairError) { status = 403; data = { detail: '当前K线数据源不具备日K能力' } }
      else {
        jobStatus = 'running'
        job = { id: `fixture-index-job-${++jobSequence}`, kind: 'index_repair', status: jobStatus, stage: 'index_fetch',
          progress: 42, stage_pct: 42, duration_s: 2, log: [{ ts: new Date().toISOString(), stage: 'index_fetch', msg: '获取七只指数' }],
          error: null, request: req.postDataJSON(), result: null }
        data = { job_id: job.id, status: 'started' }
      }
    }
    else if (path === '/api/pipeline/jobs') data = {
      jobs: job ? [{ ...job, status: jobStatus }] : [], active_id: job && jobStatus === 'running' ? job.id : null,
    }
    else if (path.endsWith('/cancel')) { jobStatus = 'cancelled'; data = { cancelled: job.id } }
    else if (path.startsWith('/api/pipeline/jobs/')) data = {
      ...job, status: jobStatus,
      ...(jobStatus === 'succeeded' ? { progress: 100, stage: 'done', result: {
        period: job.request.period, mode: job.request.mode, rows: 7, universe_size: 7, remaining: 0, missing_before: 7,
        start: job.request.start_date, end: job.request.end_date,
      } } : {}),
    }
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })
  })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
  await page.goto('http://127.0.0.1:5173/indices')
  await page.getByRole('heading', { name: '上证指数', exact: true }).waitFor()
  await page.locator('canvas').first().waitFor()
  return { page, context }
}
try {
  const { page, context } = await createPage()
  const indexButtons = page.locator('aside button').filter({ hasText: /(?:SH|SZ)/ })
  assert.equal(await indexButtons.count(), 7)
  for (const row of catalog) {
    await indexButtons.filter({ hasText: row.name }).click()
    await page.getByRole('heading', { name: row.name, exact: true }).waitFor()
    for (const period of ['1d', '1w', '1mo']) {
      const response = page.waitForResponse(r => r.url().includes('/api/index/kline?') && r.url().includes(`symbol=${row.symbol}`) && r.url().includes(`period=${period}`))
      await page.getByLabel('K线周期', { exact: true }).selectOption(period)
      // Same value can be cached; verify the rendered active period and request history.
      await Promise.race([response, page.locator('canvas').first().waitFor()])
      assert.equal(await page.getByLabel('K线周期', { exact: true }).inputValue(), period)
    }
  }
  checks.push('七只指数切换日/周/月K，真实ECharts组件渲染')
  await page.getByLabel('K线周期', { exact: true }).selectOption('1d')
  await page.getByText('60 / 130 根', { exact: true }).waitFor()
  await page.getByRole('button', { name: '放大', exact: true }).click()
  await page.getByText('90 / 130 根', { exact: true }).waitFor()
  await page.getByRole('button', { name: '缩小', exact: true }).click()
  await page.getByText('45 / 130 根', { exact: true }).waitFor()
  await page.getByRole('button', { name: '默认', exact: true }).click()
  checks.push('默认60根、放大90根、缩小45根、恢复默认')
  await page.screenshot({ path: `${output}/desktop-light.png`, fullPage: true, animations: 'disabled' })
  await page.getByRole('button', { name: '切换到暗色模式' }).click()
  await page.screenshot({ path: `${output}/desktop-dark.png`, fullPage: true, animations: 'disabled' })
  await page.getByRole('button', { name: '数据修正', exact: true }).click()
  const dialog = page.getByRole('dialog')
  assert.equal(await dialog.getByLabel('K线周期', { exact: true }).inputValue(), '1d')
  await dialog.getByText('每次修正包含七只指数', { exact: false }).waitFor()
  await page.screenshot({ path: `${output}/repair-desktop-dark.png`, fullPage: true, animations: 'disabled' })
  await dialog.getByLabel('起始日期', { exact: true }).click()
  await page.getByRole('group', { name: '起始日期日历' }).getByRole('button', { name: '清空日期', exact: true }).click()
  assert.equal(await dialog.getByRole('button', { name: '获取遗漏数据', exact: true }).isDisabled(), true)
  await dialog.getByLabel('起始日期', { exact: true }).click()
  await page.getByRole('group', { name: '起始日期日历' }).getByRole('button', { name: '2026-10-20', exact: true }).click()
  await dialog.getByLabel('K线周期', { exact: true }).selectOption('1w')
  await dialog.getByRole('button', { name: '获取遗漏数据', exact: true }).click()
  await page.getByRole('progressbar', { name: '同步总进度' }).waitFor()
  assert.equal(await page.getByRole('progressbar', { name: '同步总进度' }).getAttribute('aria-valuenow'), '42')
  await page.getByRole('button', { name: '停止修正', exact: true }).click()
  await page.getByText('已取消', { exact: true }).waitFor()
  checks.push('修正日期校验、周K遗漏请求、42%进度与取消')
  await page.getByRole('button', { name: '数据修正', exact: true }).click()
  await dialog.getByLabel('K线周期', { exact: true }).selectOption('1mo')
  await dialog.getByRole('button', { name: '获取全量数据', exact: true }).click()
  await page.getByRole('button', { name: '停止修正', exact: true }).waitFor()
  jobStatus = 'succeeded'
  await page.getByText('完成', { exact: true }).waitFor()
  checks.push('月K全量请求与完成后查询刷新')
  await page.getByRole('button', { name: '数据修正', exact: true }).click()
  repairError = true
  await dialog.getByRole('button', { name: '获取全量数据', exact: true }).click()
  await dialog.getByRole('alert').getByText('当前K线数据源不具备日K能力', { exact: true }).waitFor()
  checks.push('服务端权限/能力错误保留在弹窗')
  await page.getByRole('button', { name: '关闭数据修正', exact: true }).click()
  empty = true
  await indexButtons.filter({ hasText: '上证50' }).click()
  await page.getByText('所选周期和日期范围暂无K线数据', { exact: true }).waitFor()
  empty = false; klineError = true
  await indexButtons.filter({ hasText: '沪深300' }).click()
  await page.getByText('K线加载失败，请检查数据源后重试', { exact: true }).waitFor({ timeout: 20000 })
  checks.push('K线空数据与加载失败提示')
  klineError = false; repairError = false
  await context.close()
  job = null
  const mobile = await createPage('light', 390)
  assert.equal(await mobile.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
  await mobile.page.screenshot({ path: `${output}/mobile-light.png`, fullPage: true, animations: 'disabled' })
  await mobile.page.locator('main.rounded-card div.border-t').last().scrollIntoViewIfNeeded()
  assert.equal(await mobile.page.locator('main.rounded-card canvas').count(), 2)
  await mobile.page.screenshot({ path: `${output}/mobile-light-intraday.png`, fullPage: true, animations: 'disabled' })
  await mobile.page.getByRole('button', { name: '数据修正', exact: true }).click()
  assert.equal(await mobile.page.getByRole('dialog').isVisible(), true)
  await mobile.page.screenshot({ path: `${output}/repair-mobile-light.png`, fullPage: true, animations: 'disabled' })
  await mobile.context.close()
  const reader = await createPage('dark', 390, false)
  assert.equal(await reader.page.getByRole('button', { name: '数据修正', exact: true }).count(), 0)
  await reader.page.screenshot({ path: `${output}/mobile-dark-reader.png`, fullPage: true, animations: 'disabled' })
  await reader.context.close()
  checks.push('390px浅/深色、无横向溢出、窄屏弹窗、只读账户隐藏修正')
  assert.equal(calls.some(c => c.path.startsWith('/api/kline/daily')), false)
  assert(calls.some(c => c.path === '/api/intraday/indices' && new URLSearchParams(c.search).get('symbols')?.split(',').length === 7))
  for (const row of catalog) for (const period of ['1d', '1w', '1mo']) {
    assert(calls.some(c => c.path === '/api/index/kline' && new URLSearchParams(c.search).get('symbol') === row.symbol && new URLSearchParams(c.search).get('period') === period))
  }
  checks.push('七只行情单次批量请求，未请求股票日K')
  const unexpected = errors.filter(e => !e.includes('403 (Forbidden)') && !e.includes('502 (Bad Gateway)'))
  assert.deepEqual(unexpected, [])
  await fs.writeFile(`${output}/results.json`, JSON.stringify({ checks, calls, expectedErrors: errors }, null, 2))
  console.log(JSON.stringify({ status: 'passed', checks, requests: calls.length }))
} catch (error) {
  await fs.writeFile(`${output}/failure.json`, JSON.stringify({ error: String(error), errors, calls }, null, 2))
  console.error(error)
  process.exitCode = 1
} finally {
  await browser.close()
}
