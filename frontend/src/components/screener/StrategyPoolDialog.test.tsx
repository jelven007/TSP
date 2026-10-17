// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategyPoolDialog } from './StrategyPoolDialog'

const { list } = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { strategyList: list } }))

const strategies = [
  { id: 'ma_golden_cross', name: 'MA金叉', source: 'builtin', family: { id: 'trend', name: '趋势跟随' } },
  { id: 'oversold_bounce', name: '超跌反弹', source: 'builtin', family: { id: 'reversal', name: '超跌修复' } },
  { id: 'oversold_reversal', name: '超跌反转', source: 'builtin', family: { id: 'reversal', name: '超跌修复' } },
  { id: 'custom_legacy', name: '旧策略', source: 'custom' },
]
let root: Root
let container: HTMLDivElement
const confirm = vi.fn()
const close = vi.fn()

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  confirm.mockClear()
  close.mockClear()
  list.mockReset().mockResolvedValue({ strategies })
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
})

async function render(pool: string[] = []) {
  await act(async () => {
    root.render(<StrategyPoolDialog pool={pool} onConfirm={confirm} onClose={close} />)
  })
}

function button(text: string) {
  return [...container.querySelectorAll('button')].find(b => b.textContent?.includes(text))!
}

function choose(value: string) {
  const select = container.querySelector('select')!
  act(() => {
    select.value = value
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
}

it('adds only the selected family and preserves original IDs until confirmation', async () => {
  await render(['ma_golden_cross'])
  choose('reversal')
  expect(button('超跌反弹')).toBeDefined()
  expect(button('旧策略')).toBeUndefined()
  act(() => button('本组全加').click())
  expect(confirm).not.toHaveBeenCalled()
  expect(container.textContent).toContain('此分组无待选策略')
  expect(button('本组全加').disabled).toBe(true)
  act(() => button('确定').click())
  expect(confirm).toHaveBeenCalledWith(['ma_golden_cross', 'oversold_bounce', 'oversold_reversal'])
  expect(list).toHaveBeenCalledTimes(1)
})

it('keeps old responses without family usable and resets the category on source switch', async () => {
  await render()
  choose('reversal')
  act(() => button('自定义').click())
  expect(container.querySelector('select')!.value).toBe('all')
  choose('uncategorized')
  act(() => button('旧策略').click())
  act(() => button('取消').click())
  expect(confirm).not.toHaveBeenCalled()
  expect(close).toHaveBeenCalledTimes(1)
})

it('blocks confirmation while loading and after failure, then allows retry', async () => {
  let reject!: (reason: Error) => void
  list.mockImplementationOnce(() => new Promise((_, rejectPromise) => { reject = rejectPromise }))
  await render(['ma_golden_cross'])
  expect(button('确定').disabled).toBe(true)
  await act(async () => reject(new Error('offline')))
  expect(container.querySelector('[role="alert"]')!.textContent).toContain('加载失败')
  expect(button('确定').disabled).toBe(true)
  await act(async () => button('重试').click())
  expect(button('确定').disabled).toBe(false)
  act(() => button('确定').click())
  expect(confirm).toHaveBeenCalledWith(['ma_golden_cross'])
})

it('shows an empty catalog without calling it a fully selected pool', async () => {
  list.mockResolvedValueOnce({ strategies: [] })
  await render()
  expect(container.textContent).toContain('暂无可用策略')
  expect(button('本组全加').disabled).toBe(true)
})

it('excludes research templates from the pool total and AI draft section', async () => {
  list.mockResolvedValueOnce({
    strategies: [
      ...strategies,
      {
        id: 'factor_rank_research',
        name: '因子排名研究',
        source: 'builtin',
        research_only: true,
      },
      {
        id: 'ai_draft',
        name: 'AI 草稿',
        source: 'ai',
        research_only: true,
      },
    ],
  })

  await render()

  expect(container.textContent).toContain('策略池 0 / 4')
  act(() => button('AI').click())
  expect(container.textContent).toContain('1 个待发布')
  expect(container.textContent).toContain('AI 草稿')
  expect(container.textContent).not.toContain('因子排名研究')
})
