// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategyDailyResultToolbar } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
afterEach(async () => { await act(async () => root?.unmount()); host?.remove() })
const base: ScreenerResult = {
  strategy: 'custom_mvxxcs01', as_of: '2026-11-23', rows: [], total: 0, elapsed_ms: 0,
  section_mode: 'signals', sections: { sell: [], select: [], buy: [] },
}

it.each([
  [undefined, '—'],
  [{ pool_up_pct: 0 }, '0.00%'],
  [{ pool_up_pct: 65.125 }, '65.13%'],
  [{ pool_up_pct: null }, '—'],
  [{ pool_up_pct: NaN }, '—'],
  [{ pool_up_pct: 80, progress: { as_of: '2026-11-20', steps: { breadth: { up_pct: 90 } } } }, '—'],
  [{ pool_up_pct: null, progress: { as_of: '2026-11-23', steps: { breadth: { up_pct: 72.5 } } } }, '72.50%'],
])('keeps same-date breadth after tabs across changes: %j', async (quality, expected) => {
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  for (const activeTab of ['sell', 'select', 'buy'] as const) {
    await act(async () => root.render(<StrategyDailyResultToolbar result={{
      ...base, entry_data_quality: quality ? { csgi_late_session_strength_s01: quality } as any : undefined,
    }} activeTab={activeTab} onTabChange={vi.fn()} currentDate="2026-11-24" />))
    const tabs = host.querySelector('[role="tablist"]')!
    expect(tabs.nextElementSibling?.getAttribute('aria-label')).toBe('上涨占比')
    expect(tabs.nextElementSibling?.textContent).toBe(`上涨占比 · ${expected}`)
    expect(host.querySelectorAll('[aria-label="上涨占比"]')).toHaveLength(1)
  }
})
