// @vitest-environment jsdom
// 分钟策略卡片数字未出时的占位引导: awaitRun → 「待计算」; computing → 脉冲 ···。
// 未运行的策略显示占位，卡片点击只选择；首次执行由监控按钮触发。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategyCard, loadCardSize, CARD_STYLES } from './StrategyCard'
import { storage } from '@/lib/storage'

vi.mock('@/lib/accountSession', () => ({ accountStorage: window.localStorage }))

const noop = () => {}
const base = {
  name: '分钟突破', description: '', source: 'custom' as const,
  active: false, loading: false, cardSize: 'normal' as const,
  onRun: noop, disabled: false, onSettings: noop,
  timeframeBadge: '分钟',
}

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
})

function render(props: Record<string, unknown>) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  root = createRoot(container)
  act(() => { root!.render(<StrategyCard {...base} {...props} />) })
}

it('shows 待计算 hint when awaitRun and count is missing', () => {
  render({ awaitRun: true })

  expect(container.textContent).toContain('待计算')
  expect(container.textContent).not.toContain('···')
})

it.each(['normal', 'large'])('directs the waiting hint to monitoring in %s cards', cardSize => {
  render({ awaitRun: true, cardSize })
  expect(container.querySelector('[title="点击开启策略监控后计算"]')).not.toBeNull()
  expect(container.querySelector('[title="点击卡片运行策略"]')).toBeNull()
})

it('shows pulsing placeholder (not hint) when computing', () => {
  render({ awaitRun: true, computing: true })

  expect(container.textContent).toContain('···')
  expect(container.textContent).not.toContain('待计算')
})

it('shows nothing extra for daily strategies without awaitRun', () => {
  render({})

  expect(container.textContent).not.toContain('待计算')
  expect(container.textContent).not.toContain('···')
})

it('hint yields to the real count once available', () => {
  render({ awaitRun: true, count: 7 })

  expect(container.textContent).not.toContain('待计算')
  expect(container.textContent).toContain('7')
})

it('only standard and detailed sizes remain and old sizes fall back to standard', () => {
  expect(Object.keys(CARD_STYLES).sort()).toEqual(['large', 'normal'])
  for (const size of ['mini', 'hidden']) {
    storage.screenerCardSize.set(size)
    expect(loadCardSize()).toBe('normal')
  }
  storage.screenerCardSize.set('large')
  expect(loadCardSize()).toBe('large')
  storage.screenerCardSize.set('normal')
})

it('onRun fires when the card is clicked', () => {
  const onRun = vi.fn()
  render({ awaitRun: true, onRun })

  act(() => { container.querySelector('button')!.click() })

  expect(onRun).toHaveBeenCalledTimes(1)
})
