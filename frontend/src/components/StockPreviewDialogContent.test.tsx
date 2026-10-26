// @vitest-environment jsdom
import { act, useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockPreviewDialogContent } from './StockPreviewDialogContent'

const panel = vi.hoisted(() => ({ props: {} as any, cached: false }))
vi.mock('@/components/StockPanel', () => ({
  getDefaultRange: () => ({ start: '2026-04-26', end: '2026-10-26' }),
  StockPanel: (props: any) => {
    panel.props = props
    const { symbol, klinePeriod, onAvailableBarsChange, onVisibleBarsChange } = props
    useEffect(() => {
      if (panel.cached) { onAvailableBarsChange?.(65); onVisibleBarsChange?.(60) }
    }, [symbol, klinePeriod, onAvailableBarsChange, onVisibleBarsChange])
    return <div data-testid="panel" />
  },
}))
vi.mock('@/components/StockMultiDayIntradayChart', () => ({ StockMultiDayIntradayChart: () => <div>分时图</div> }))
vi.mock('@/components/WatchlistAddMenu', () => ({ WatchlistAddMenu: () => null }))
vi.mock('@/extensions/ExtensionSlot', () => ({ ExtensionSlot: () => null }))
vi.mock('@/lib/useCustomSignalNames', () => ({ useCustomSignalNames: () => ({}) }))
vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: {} }) }))
vi.mock('@/lib/useQuoteStream', () => ({ setFocusSymbol: vi.fn(), clearFocusSymbol: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  watchlistList: async () => ({ symbols: [] }),
  monitorRulesList: async () => ({ rules: [] }),
  abnormalOverview: async () => ({ rows: [] }),
} }))

let root: ReturnType<typeof createRoot>
let host: HTMLDivElement
let qc: QueryClient

beforeEach(async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  panel.cached = false
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await render('600000.SH')
})
afterEach(async () => {
  await act(async () => root.unmount())
  qc.clear()
  host.remove()
})
async function render(symbol: string | null) {
  await act(async () => root.render(
    <QueryClientProvider client={qc}>
      <StockPreviewDialogContent symbol={symbol} onClose={() => {}} />
    </QueryClientProvider>,
  ))
}
function button(label: string) {
  return Array.from(document.querySelectorAll<HTMLButtonElement>('button')).find(el => el.textContent === label)!
}
async function click(label: string) { await act(async () => button(label).click()) }

it('分时在前，K线下拉提供六周期，首次打开默认日K与60根', async () => {
  const intraday = button('分时')
  const select = document.querySelector<HTMLSelectElement>('[aria-label="K线周期"]')
  expect(select).not.toBeNull()
  expect(intraday.compareDocumentPosition(select!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(Array.from(select!.options).map(el => el.textContent)).toEqual(['1分K', '5分K', '10分K', '15分K', '日K', '周K'])
  expect(select!.value).toBe('1d')
  expect(panel.props.visibleBars).toBe(60)
  expect(button('半年')).toBeUndefined()
  expect(button('1年')).toBeUndefined()
})

it('放大和缩小按当前实际可见根数计算，默认反复点击也重置到最新60根', async () => {
  expect(button('放大')).toBeDefined()
  await act(async () => { panel.props.onAvailableBarsChange(300); panel.props.onVisibleBarsChange(60) })
  await click('放大')
  expect(panel.props.visibleBars).toBe(90)
  await act(async () => panel.props.onVisibleBarsChange(90))
  await click('放大')
  expect(panel.props.visibleBars).toBe(135)
  await act(async () => panel.props.onVisibleBarsChange(135))
  await click('缩小')
  expect(panel.props.visibleBars).toBe(68)
  // 鼠标缩放之后以图表报告的实际根数计算
  await act(async () => panel.props.onVisibleBarsChange(20))
  await click('放大')
  expect(panel.props.visibleBars).toBe(30)
  await click('默认')
  expect(panel.props.visibleBars).toBe(60)
  const reset = panel.props.zoomResetKey
  await click('默认')
  expect(panel.props.zoomResetKey).not.toBe(reset)
})

it('数量最少1根、最多可用数据，切周期及重新打开重置，分时下隐藏数量控件', async () => {
  expect(button('缩小')).toBeDefined()
  await act(async () => { panel.props.onAvailableBarsChange(65); panel.props.onVisibleBarsChange(60) })
  await click('放大')
  expect(panel.props.visibleBars).toBe(65)
  await act(async () => panel.props.onVisibleBarsChange(1))
  await click('缩小')
  expect(panel.props.visibleBars).toBe(1)
  const select = document.querySelector<HTMLSelectElement>('[aria-label="K线周期"]')!
  await act(async () => { select.value = '1w'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(panel.props.klinePeriod).toBe('1w')
  expect(panel.props.visibleBars).toBe(60)
  await click('分时')
  expect(panel.props.infoBarOnly).toBe(true)
  expect(button('默认')).toBeUndefined()
  await render(null)
  await render('600000.SH')
  expect(panel.props.klinePeriod).toBe('1d')
  expect(panel.props.visibleBars).toBe(60)
})

it('切股直接命中缓存时仍可操作数量控件', async () => {
  panel.cached = true
  await render('000001.SZ')
  expect(button('放大').disabled).toBe(false)
})
