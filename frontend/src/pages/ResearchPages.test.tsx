// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'

const loaded = vi.hoisted(() => ({ views: [] as string[] }))
vi.mock('./backtest/FactorDiscovery', () => { loaded.views.push('inspect'); return {
  FactorDiscovery: ({ focusFactor }: any) => <div data-view="inspect">{focusFactor}</div>,
} })
vi.mock('./factors/FactorLibrary', () => { loaded.views.push('library'); return {
  FactorLibrary: ({ onEdit }: any) => <button onClick={() => onEdit('saved-factor')}>edit saved factor</button>,
} })
vi.mock('./factors/FactorEditor', () => { loaded.views.push('editor'); return {
  FactorEditor: ({ editId }: any) => <div data-view="editor">{editId}</div>,
} })
vi.mock('./factors/FactorComposite', () => { loaded.views.push('composite'); return { FactorComposite: () => <div data-view="composite" /> } })
vi.mock('./backtest/MiningWorkbench', () => { loaded.views.push('mining'); return { MiningWorkbench: () => <div data-view="mining" /> } })
vi.mock('./backtest/StrategyBacktest', () => { loaded.views.push('strategy'); return {
  StrategyBacktest: ({ loadCandidate, onLoadConsumed }: any) => <button data-view="strategy" onClick={onLoadConsumed}>{loadCandidate?.id ?? 'strategy ready'}</button>,
} })
vi.mock('./backtest/RobustnessValidation', () => { loaded.views.push('robustness'); return { RobustnessValidation: () => <div data-view="robustness" /> } })
vi.mock('./backtest/ResearchCandidatesDialog', () => { loaded.views.push('candidates'); return {
  ResearchCandidatesDialog: ({ onLoadStrategy }: any) => <button role="dialog" onClick={() => onLoadStrategy({ id: 'saved-candidate' })}>load candidate</button>,
} })
vi.mock('./backtest/ResearchHistoryDialog', () => { loaded.views.push('history'); return {
  ResearchHistoryDialog: () => <div role="dialog">saved history</div>,
} })
vi.mock('@/lib/accountSession', () => ({
  multitenancyEnabled: () => true,
  accountScope: () => 'research-page-test',
}))
vi.mock('@/lib/api', () => ({
  api: { businessInitializationDefaults: vi.fn().mockResolvedValue({ backtest: {} }) },
}))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root | undefined
let node: HTMLDivElement
function Location() { return <output>{useLocation().search}</output> }
async function mount(Page: () => React.ReactNode, url: string) {
  node = document.createElement('div'); document.body.append(node)
  root = createRoot(node)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root!.render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}><Page /><Location /></MemoryRouter>
    </QueryClientProvider>,
  ))
}
async function click(text: string) {
  const button = [...node.querySelectorAll('button')].find(b => b.textContent === text)!
  expect(button).toBeDefined()
  await act(async () => button.click())
}
afterEach(async () => {
  if (root) await act(async () => root!.unmount())
  node?.remove(); root = undefined; loaded.views.length = 0; vi.resetModules()
})

it('loads only the selected factor view and retains focus/edit parameters on navigation', async () => {
  const { Factors } = await import('./Factors')
  await mount(Factors, '/factors?focus=focused-factor')
  await vi.waitFor(() => expect(node.querySelector('[data-view="inspect"]')?.textContent).toBe('focused-factor'))
  expect(loaded.views).toEqual(['inspect'])
  await click('因子库')
  await vi.waitFor(() => expect(node.textContent).toContain('edit saved factor'))
  await click('edit saved factor')
  await vi.waitFor(() => expect(node.querySelector('[data-view="editor"]')?.textContent).toBe('saved-factor'))
  expect(node.querySelector('output')?.textContent).toContain('edit=saved-factor')
  expect(loaded.views).toEqual(['inspect', 'library', 'editor'])
  await click('候选方案')
  await vi.waitFor(() => expect(node.querySelector('[role="dialog"]')).not.toBeNull())
})

it('defers backtest validation/history/candidates and consumes a saved candidate after switching tabs', async () => {
  const { Backtest } = await import('./Backtest')
  await mount(Backtest, '/backtest')
  await vi.waitFor(() => expect(node.querySelector('[data-view="strategy"]')).not.toBeNull())
  expect(loaded.views).toEqual(['strategy'])
  await click('验证')
  await vi.waitFor(() => expect(node.querySelector('[data-view="robustness"]')).not.toBeNull())
  await click('候选方案')
  await vi.waitFor(() => expect(node.querySelector('[role="dialog"]')).not.toBeNull())
  await click('load candidate')
  expect(node.querySelector('[data-view="strategy"]')?.textContent).toBe('saved-candidate')
  expect(node.querySelector('output')?.textContent).toBe('?tab=strategy')
  await click('saved-candidate')
  expect(node.querySelector('[data-view="strategy"]')?.textContent).toBe('strategy ready')
  await click('任务历史')
  await vi.waitFor(() => expect(node.textContent).toContain('saved history'))
})
