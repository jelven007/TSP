// @vitest-environment jsdom
import { act, StrictMode, useEffect, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { confirmAccount } from '@/lib/accountSession'
import { storage } from '@/lib/storage'
import { SettingsModal } from './SettingsModal'
import { getCardOrder, getCardVisibility, PageSettingsModal } from './PageSettingsModal'

const expectedOrder = [
  'instruments', 'minute', '5m', '10m', '15m', 'quotes', 'daily', 'weekly', 'adj_factor', 'enriched',
  'financials', 'index', 'regime', 'etf',
]
const expectedLabels = [
  '个股维表', '1分K', '5分K', '10分K', '15分K', '实时行情', '日 K', '周K', '除权因子', 'Enriched',
  '财务数据', '指数', '环境', 'ETF',
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
  vi.unstubAllGlobals()
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
  const saved = ['financials', 'index', 'removed-card', 'daily', 'weekly']
  storage.dataCardOrder.set(saved)
  storage.dataCardVisible.set({ minute: false, etf: true })

  expect(getCardOrder()).toEqual([
    'financials', 'index', 'daily', 'weekly', 'instruments', 'minute', '5m', '10m', '15m',
    'quotes', 'adj_factor', 'enriched', 'regime', 'etf',
  ])
  expect(storage.dataCardOrder.get([])).toEqual(saved)
  expect(getCardVisibility(caps)).toMatchObject({ minute: false, etf: true })
})

it('restores the system order and displays all requested cards', async () => {
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
    expect(getCardOrder().filter(key => getCardVisibility(caps)[key])).toEqual(expectedOrder.slice(0, -1))
    expect(host.querySelectorAll('[role="checkbox"][aria-checked="true"]')).toHaveLength(13)
    expect(changed).toHaveBeenCalledOnce()
  } finally {
    window.removeEventListener('data-card-visible-change', changed)
  }
})

it('places new periods into the requested order for the previous card layout', () => {
  storage.dataCardOrder.set(['instruments', 'minute', 'daily', 'adj_factor', 'enriched', 'regime', 'index', 'financials', 'etf'])
  expect(getCardOrder()).toEqual(expectedOrder)
})

it('inserts quotes after 15m in saved six-period layouts without moving existing cards', () => {
  const saved = ['weekly', '15m', 'financials', 'minute', 'daily', 'instruments', '5m', '10m', 'adj_factor', 'enriched', 'index', 'regime', 'etf']
  storage.dataCardOrder.set(saved)
  expect(getCardOrder()).toEqual(['weekly', '15m', 'quotes', ...saved.slice(2)])
  expect(storage.dataCardOrder.get([])).toEqual(saved)
})

it('preserves a user-selected quote position and visibility after reopening', async () => {
  const saved = ['quotes', ...expectedOrder.filter(key => key !== 'quotes')]
  storage.dataCardOrder.set(saved)
  storage.dataCardVisible.set({ quotes: false })
  await act(async () => root.render(<PageSettingsModal caps={caps} />))
  expect(getCardOrder()).toEqual(saved)
  expect(renderedLabels()[0]).toBe('实时行情')
  expect(host.querySelector('[aria-label="显示实时行情"]')?.getAttribute('aria-checked')).toBe('false')
})

it.each([null, 'weekly', { length: 1 }, 1])('opens page settings with invalid saved order %j', async (saved) => {
  storage.dataCardOrder.set(saved as unknown as string[])
  await act(async () => root.render(<PageSettingsModal caps={caps} />))
  expect(renderedLabels()).toEqual(expectedLabels)
})

it('ignores duplicate cards and invalid visibility values when opening', async () => {
  storage.dataCardOrder.set(['weekly', 'daily', 'daily', 'unknown'])
  storage.dataCardVisible.set(null as unknown as Record<string, boolean>)
  await act(async () => root.render(<PageSettingsModal caps={caps} />))
  expect(renderedLabels().filter(label => label === '日 K')).toHaveLength(1)
  expect(getCardVisibility(caps).daily).toBe(true)
})

it('opens, toggles and reopens through the shared modal during StrictMode parent updates', async () => {
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    queueMicrotask(() => callback(0)); return 1
  })
  vi.stubGlobal('cancelAnimationFrame', () => {})
  const errors = vi.spyOn(console, 'error')
  function Page() {
    const [open, setOpen] = useState(false)
    const [visibility, setVisibility] = useState(() => getCardVisibility(caps))
    useEffect(() => {
      const update = () => setVisibility(getCardVisibility(caps))
      window.addEventListener('data-card-visible-change', update)
      return () => window.removeEventListener('data-card-visible-change', update)
    }, [])
    return (
      <>
        <button onClick={() => setOpen(true)}>页面设置</button>
        <output>{visibility.daily ? '显示日K' : '隐藏日K'}</output>
        {open && (
          <SettingsModal title="页面设置" onClose={() => setOpen(false)}>
            <PageSettingsModal caps={{ ...caps }} />
          </SettingsModal>
        )}
      </>
    )
  }
  await act(async () => root.render(<StrictMode><Page /></StrictMode>))
  await act(async () => host.querySelector('button')!.click())
  let dialog = document.querySelector('[role="dialog"]')!
  expect(dialog).not.toBeNull()
  expect(host.contains(dialog)).toBe(false)
  const toggle = dialog.querySelector<HTMLButtonElement>('[role="checkbox"][aria-label="显示日 K"]')!
  await act(async () => toggle.click())
  expect(host.querySelector('output')?.textContent).toBe('隐藏日K')
  expect(toggle.getAttribute('aria-checked')).toBe('false')
  await act(async () => dialog.querySelector<HTMLButtonElement>('[aria-label="关闭页面设置"]')!.click())
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  await act(async () => host.querySelector('button')!.click())
  dialog = document.querySelector('[role="dialog"]')!
  expect(dialog.querySelector('[aria-label="显示日 K"]')?.getAttribute('aria-checked')).toBe('false')
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(errors).not.toHaveBeenCalled()
})
