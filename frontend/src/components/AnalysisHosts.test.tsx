// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({
  task: null as any, mode: null as 'active' | 'history' | null, minimized: false,
  financialLoaded: vi.fn(), stockLoaded: vi.fn(),
}))
vi.mock('@/lib/aiReportStore', () => ({
  useDialogTask: () => ({ task: state.task, mode: state.mode }),
  useDialogState: () => ({ minimized: state.minimized }),
}))
vi.mock('@/lib/stockAnalysisStore', () => ({
  useDialogTask: () => ({ task: state.task, mode: state.mode }),
  useDialogState: () => ({ minimized: state.minimized }),
}))
vi.mock('./financials/AiAnalysisDialog', () => {
  state.financialLoaded()
  return { AiAnalysisDialog: ({ task, mode, minimized }: any) =>
    minimized ? null : <div data-financial>{`${task?.content}:${mode}`}</div> }
})
vi.mock('./stock-analysis/StockAnalysisDialog', () => {
  state.stockLoaded()
  return { StockAnalysisDialog: ({ task, mode, minimized }: any) =>
    minimized ? null : <div data-stock>{`${task?.content}:${mode}`}</div> }
})
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

afterEach(() => { vi.resetModules(); vi.clearAllMocks(); state.task = null; state.mode = null; state.minimized = false })

it('keeps both report view modules unloaded until a task is opened and preserves minimized task updates', async () => {
  const { AiAnalysisHost } = await import('./financials/AiAnalysisHost')
  const { StockAnalysisHost } = await import('./stock-analysis/StockAnalysisHost')
  const node = document.createElement('div')
  const root = createRoot(node)
  const render = async () => { await act(async () => root.render(<><AiAnalysisHost /><StockAnalysisHost /></>)) }
  try {
    await render()
    expect(state.financialLoaded).not.toHaveBeenCalled()
    expect(state.stockLoaded).not.toHaveBeenCalled()
    state.task = { content: 'streaming report' }
    state.mode = 'active'
    state.minimized = true
    await render()
    expect(state.financialLoaded).not.toHaveBeenCalled()
    expect(state.stockLoaded).not.toHaveBeenCalled()
    state.minimized = false
    await render()
    await vi.waitFor(() => expect(node.querySelector('[data-stock]')?.textContent).toBe('streaming report:active'))
    expect(node.querySelector('[data-financial]')?.textContent).toBe('streaming report:active')
    state.minimized = true
    await render()
    expect(node.textContent).toBe('')
    state.task = { content: 'finished history' }
    state.mode = 'history'
    state.minimized = false
    await render()
    expect(node.querySelector('[data-financial]')?.textContent).toBe('finished history:history')
    expect(state.stockLoaded).toHaveBeenCalledOnce()
    expect(state.financialLoaded).toHaveBeenCalledOnce()
  } finally { await act(async () => root.unmount()) }
})
