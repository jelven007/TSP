// @vitest-environment jsdom
// 新建模板与存量策略作用域独立，批量同步仅更新勾选策略的数值过滤。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { DefaultStrategyParamsDialog, BUILTIN_DEFAULT_BASIC_FILTER, loadDefaultBasicFilter } from './DefaultStrategyParamsDialog'
import { buildCustomTemplate, renderBasicFilterPy } from './StrategyBuilderDialog'
import { storage } from '@/lib/storage'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { confirmAccount } from '@/lib/accountSession'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const mockPatchConfig = vi.fn(async (_id: string, _overrides: Record<string, unknown>) => ({ ok: true }))
vi.mock('@/lib/api', () => ({
  api: {
    screenerStrategies: vi.fn(async () => ({
      presets: [
        { id: 'strategy_a', timeframes: ['1d'] },
        { id: 'strategy_b', timeframes: ['1m'] },
      ],
    })),
    strategyPatchConfig: (id: string, ov: Record<string, unknown>) => mockPatchConfig(id, ov),
    watchlistGroups: vi.fn(async () => ({ groups: [{ id: 'core', name: '核心' }, { id: 'other', name: '观察' }] })),
  },
}))

let root: Root | null = null
let container: HTMLDivElement
const mount = document.createElement('div')
document.body.appendChild(mount)

// 渲染前预取策略列表进缓存: query 同步命中, 避免 jsdom 下异步解析时序抖动
async function render(props: { show: boolean; onClose?: () => void }) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  await queryClient.fetchQuery({
    queryKey: [...QK.screenerStrategies('all'), 'all'],
    queryFn: () => api.screenerStrategies(undefined, 'all'),
  })
  root = createRoot(mount)
  await act(async () => {
    root!.render(
      <QueryClientProvider client={queryClient}>
        <DefaultStrategyParamsDialog show={props.show} onClose={props.onClose ?? (() => {})} />
      </QueryClientProvider>,
    )
  })
  container = document.querySelector<HTMLDivElement>('[role="dialog"][aria-labelledby="default-strategy-params-title"]') ?? mount
}

beforeEach(() => {
  confirmAccount({
    auth_required: false, configured: false, has_users: false, legacy_migration_required: false,
    registration_enabled: false, email_verification_required: false, authenticated: true, user: null,
  })
  localStorage.clear()
  mockPatchConfig.mockClear()
})
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  mount.innerHTML = ''
})

it('renders nothing when show=false', async () => {
  await render({ show: false })

  expect(container.textContent).toBe('')
  expect(document.querySelector('[role="dialog"]')).toBeNull()
})

it('shows the basic filter fields when open', async () => {
  await render({ show: true })

  expect(container.textContent).toContain('新建策略模板')
  expect(container.textContent).toContain('总市值')
  expect(container.textContent).toContain('上市天数')
  expect(container.textContent).toContain('价格')
  expect(container.textContent).toContain('流通市值')
  expect(container.textContent).toContain('自定义')
  expect(container.textContent).toContain('剔除')
})

it('save persists to localStorage and closes', async () => {
  const onClose = vi.fn()
  await render({ show: true, onClose })

  // 改价格下限: 第一对 min/max 输入 (RangeField 价格)
  // React 受控 input 需经原生 value setter + input 事件触发 onChange
  const input = container.querySelector('input[aria-label="价格范围最小值"]') as HTMLInputElement
  const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  act(() => {
    nativeSetter.call(input, '3')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  const saveBtn = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!
  act(() => { saveBtn.click() })

  const saved = storage.defaultStrategyBasicFilter.get(null)
  expect(saved?.price_min).toBe(3)
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(loadDefaultBasicFilter().price_min).toBe(3)
})

it('reset restores builtin defaults', async () => {
  storage.defaultStrategyBasicFilter.set({ ...BUILTIN_DEFAULT_BASIC_FILTER, price_min: 100 })
  await render({ show: true })

  const resetBtn = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('恢复模板初始值'))!
  act(() => { resetBtn.click() })

  expect((container.querySelector('input[aria-label="价格范围最小值"]') as HTMLInputElement).value).toBe('5')
})

// ===== 模板注入 (纯函数) =====

it('renderBasicFilterPy emits Python literals for the template META', () => {
  const py = renderBasicFilterPy({
    ...BUILTIN_DEFAULT_BASIC_FILTER,
    price_min: 3, exclude_st: false, float_cap_max: null,
  })

  expect(py).toContain('"price_min": 3, "price_max": 200,')
  expect(py).toContain('"exclude_st": False,')
  expect(py).toContain('"float_cap_max": None,')
  expect(py).toContain('"boards": ["沪主板", "深主板", "创业板", "科创板"],')
})

it('buildCustomTemplate embeds custom defaults into the code template', () => {
  const code = buildCustomTemplate({ ...BUILTIN_DEFAULT_BASIC_FILTER, price_min: 3, turnover_min: 2 })

  expect(code).toContain('"basic_filter": {')
  expect(code).toContain('"price_min": 3,')
  expect(code).toContain('"turnover_min": 2,')
  expect(code).toContain('def filter(df: pl.DataFrame, params: dict) -> pl.Expr:')
})

it('saves custom-only scope with multiple groups and preserves it in generated code', async () => {
  await render({ show: true })
  await act(async () => container.querySelector<HTMLInputElement>('input[aria-label="自定义"]')!.click())
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 25)) })
  await act(async () => {
    for (const board of ['沪主板', '深主板', '创业板', '科创板']) {
      container.querySelector<HTMLInputElement>(`input[aria-label="${board}"]`)!.click()
    }
    container.querySelector<HTMLInputElement>('input[aria-label="核心"]')!.click()
    container.querySelector<HTMLInputElement>('input[aria-label="观察"]')!.click()
  })
  act(() => [...container.querySelectorAll('button')].find(b => b.textContent === '保存模板')!.click())
  const saved = storage.defaultStrategyBasicFilter.get(null)!
  expect(saved).toMatchObject({ boards: [], include_custom: true, watchlist_group_ids: ['core', 'other'],
    data_reference: 'previous_trade_day', target_mode: 'selected' })
  const code = renderBasicFilterPy(saved)
  expect(code).toContain('"boards": []')
  expect(code).toContain('"include_custom": True, "watchlist_group_ids": ["core","other"]')
  expect(code).toContain('"data_reference": "previous_trade_day"')
})

// ===== 一键应用到全部策略 =====


it('batch application starts without preselected strategies', async () => {
  await render({ show: true })
  const btn = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('应用到所选策略'))!
  expect(btn.disabled).toBe(true)
  expect(mockPatchConfig).not.toHaveBeenCalled()
})

it('batch application only patches selected numeric filters and does not save the template', async () => {
  await render({ show: true })
  const input = container.querySelector('input[aria-label="价格范围最小值"]') as HTMLInputElement
  const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  act(() => {
    nativeSetter.call(input, '3')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await act(async () => { container.querySelector<HTMLInputElement>('input[aria-label="strategy_a"]')!.click() })
  const applyBtn = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('应用到所选策略'))!
  await act(async () => { applyBtn.click() })
  expect(mockPatchConfig).not.toHaveBeenCalled()
  expect(input.disabled).toBe(true)
  const confirmBtn = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('确认应用'))!
  await act(async () => { confirmBtn.click() })
  await act(async () => {})

  expect(mockPatchConfig).toHaveBeenCalledTimes(1)
  const ids = mockPatchConfig.mock.calls.map(c => c[0])
  expect(ids).toEqual(['strategy_a'])
  const applied = mockPatchConfig.mock.calls[0][1].basic_filter as Record<string, unknown>
  expect(applied.price_min).toBe(3)
  expect(applied.boards).toBeUndefined()
  expect(applied.exclude_st).toBeUndefined()
  expect(applied.target_mode).toBeUndefined()
  expect(storage.defaultStrategyBasicFilter.get(null)).toBeNull()
})

it('blocks invalid ranges and carries all numeric template fields into generated code', async () => {
  const onClose = vi.fn()
  await render({ show: true, onClose })
  const input = container.querySelector('input[aria-label="价格范围最小值"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '300')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  act(() => [...container.querySelectorAll('button')].find(b => b.textContent === '保存模板')!.click())
  expect(storage.defaultStrategyBasicFilter.get(null)).toBeNull()
  expect(onClose).not.toHaveBeenCalled()
  expect(container.textContent).toContain('最小值不能大于最大值')
  const code = renderBasicFilterPy({ ...BUILTIN_DEFAULT_BASIC_FILTER,
    market_cap_min: 20e8, market_cap_max: 100e8, exclude_new_days: 60, enabled: false })
  expect(code).toContain('"market_cap_min": 2000000000, "market_cap_max": 10000000000')
  expect(code).toContain('"exclude_new_days": 60')
  expect(code).toContain('"enabled": False')
  expect(code).toContain('"target_mode": "selected"')
})

it('shows failed strategy names and freezes controls throughout a batch request', async () => {
  let reject!: (error: Error) => void
  mockPatchConfig.mockImplementationOnce(() => new Promise((_resolve, rejectFn) => { reject = rejectFn }))
  await render({ show: true })
  act(() => container.querySelector<HTMLInputElement>('input[aria-label="strategy_a"]')!.click())
  act(() => [...container.querySelectorAll('button')].find(b => b.textContent?.includes('应用到所选策略'))!.click())
  await act(async () => { [...container.querySelectorAll('button')].find(b => b.textContent === '确认应用')!.click() })
  expect(container.querySelector<HTMLInputElement>('input[type="number"]')!.disabled).toBe(true)
  expect(container.querySelector<HTMLButtonElement>('button[aria-label="关闭新建策略模板"]')!.disabled).toBe(true)
  await act(async () => reject(new Error('测试保存失败')))
  expect(container.textContent).toContain('strategy_a：测试保存失败')
  expect(container.textContent).toContain('已应用 0/1')
  expect(storage.defaultStrategyBasicFilter.get(null)).toBeNull()
})
