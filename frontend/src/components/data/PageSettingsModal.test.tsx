// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { confirmAccount } from '@/lib/accountSession'
import { storage } from '@/lib/storage'
import { getCardOrder, getCardVisibility, PageSettingsModal } from './PageSettingsModal'

const expectedOrder = [
  'instruments', 'minute', 'daily', 'adj_factor', 'enriched',
  'regime', 'index', 'financials', 'etf',
]
const expectedLabels = [
  '个股维表', '分钟 K', '日 K', '除权因子', 'Enriched',
  '环境', '指数', '财务数据', 'ETF',
]
const caps = { adj_factor: true, 'kline.minute.batch': true, financial: true }
let host: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  confirmAccount({
    auth_required: false, configured: false, has_users: false,
    legacy_migration_required: false, registration_enabled: false,
    email_verification_required: false, authenticated: true, user: null,
  })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  localStorage.clear()
  vi.restoreAllMocks()
})

function renderedLabels() {
  return Array.from(host.querySelectorAll('button[title="拖动排序"]'), button =>
    button.parentElement?.querySelector('.font-medium')?.textContent,
  )
}

it('initializes the settings and portrait order without writing a user preference', async () => {
  await act(async () => root.render(<PageSettingsModal caps={caps} />))
  expect(renderedLabels()).toEqual(expectedLabels)
  expect(getCardOrder()).toEqual(expectedOrder)
  expect(storage.dataCardOrder.get([])).toEqual([])
  expect(getCardVisibility(caps).etf).toBe(false)
})

it('preserves saved ordering and visibility while appending missing cards', async () => {
  const saved = ['financials', 'index', 'removed-card', 'daily']
  storage.dataCardOrder.set(saved)
  storage.dataCardVisible.set({ minute: false, etf: true })

  expect(getCardOrder()).toEqual([
    'financials', 'index', 'daily', 'instruments', 'minute',
    'adj_factor', 'enriched', 'regime', 'etf',
  ])
  expect(storage.dataCardOrder.get([])).toEqual(saved)
  expect(getCardVisibility(caps)).toMatchObject({ minute: false, etf: true })
})

it('restores the system order and displays its first five cards', async () => {
  storage.dataCardOrder.set([...expectedOrder].reverse())
  storage.dataCardVisible.set({ etf: true, instruments: false })
  const changed = vi.fn()
  window.addEventListener('data-card-visible-change', changed)
  try {
    await act(async () => root.render(<PageSettingsModal caps={caps} />))
    const reset = Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent === '恢复默认')
    expect(reset).toBeDefined()
    await act(async () => reset!.click())

    expect(renderedLabels()).toEqual(expectedLabels)
    expect(storage.dataCardOrder.get([])).toEqual(expectedOrder)
    expect(getCardOrder().filter(key => getCardVisibility(caps)[key])).toEqual([
      'instruments', 'minute', 'daily', 'adj_factor', 'enriched',
    ])
    expect(host.querySelectorAll('[role="checkbox"][aria-checked="true"]')).toHaveLength(5)
    expect(changed).toHaveBeenCalledOnce()
  } finally {
    window.removeEventListener('data-card-visible-change', changed)
  }
})
