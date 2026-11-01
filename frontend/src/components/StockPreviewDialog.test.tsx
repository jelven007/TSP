// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { StockPreviewDialog } from './StockPreviewDialog'

vi.mock('./StockPreviewDialogContent', () => ({
  StockPreviewDialogContent: () => { throw new Promise(() => {}) },
}))
it('shows the requested stock and a closable modal while the detail chunk loads', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div'); document.body.append(host)
  const root = createRoot(host); const close = vi.fn()
  await act(async () => root.render(<StockPreviewDialog symbol="600519.SH" name="贵州茅台" onClose={close} />))
  expect(document.querySelector('[role="dialog"][aria-label="个股详情"]')).not.toBeNull()
  expect(document.body.textContent).toContain('600519.SH')
  expect(document.body.textContent).toContain('加载个股详情')
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="关闭个股详情"]')!.click())
  expect(close).toHaveBeenCalled()
  await act(async () => root.unmount()); host.remove()
})
