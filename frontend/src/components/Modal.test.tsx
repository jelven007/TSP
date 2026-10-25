// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { Modal } from './Modal'
import { DatePicker } from './DatePicker'

let cleanup: (() => Promise<void>) | undefined
afterEach(async () => { await cleanup?.(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

async function mount(children: React.ReactNode) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockImplementation(function (this: HTMLElement) {
    return this.hidden ? null : document.body
  })
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    queueMicrotask(() => callback(0)); return 1
  })
  vi.stubGlobal('cancelAnimationFrame', () => {})
  const host = document.createElement('div')
  host.style.transform = 'translateX(20px)'
  host.style.overflow = 'hidden'
  document.body.append(host)
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()); host.remove() }
  await act(async () => root.render(children))
  return host
}

it('skips hidden upload controls when moving initial focus into the dialog', async () => {
  await mount(<Modal onClose={() => {}} ariaLabel="策略池"><input type="file" hidden /><button>关闭</button></Modal>)
  expect(document.activeElement).toBe(document.querySelector('[role="dialog"] button'))
})

it('keeps calendar controls inside the focus trap while preserving date selection', async () => {
  const change = vi.fn()
  await mount(<Modal onClose={() => {}} ariaLabel="日期"><DatePicker value="2026-10-25" onChange={change} /></Modal>)
  const dialog = document.querySelector('[role="dialog"]')!
  await act(async () => dialog.querySelector<HTMLButtonElement>('button')!.click())
  const day = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button')).find(el => el.textContent === '20')!
  expect(day).toBeDefined()
  day.focus()
  expect(document.activeElement).toBe(day)
  await act(async () => day.click())
  expect(change).toHaveBeenCalledWith('2026-10-20')
})

it('renders outside clipping ancestors and locks background scrolling until the last dialog closes', async () => {
  const previousOverflow = document.body.style.overflow
  const host = await mount(<Modal onClose={() => {}} ariaLabel="编辑"><input /></Modal>)
  const dialog = document.querySelector('[role="dialog"]')!
  expect(host.contains(dialog)).toBe(false)
  expect(dialog.parentElement?.parentElement).toBe(document.body)
  expect(document.body.style.overflow).toBe('hidden')
  await cleanup?.()
  cleanup = undefined
  expect(document.body.style.overflow).toBe(previousOverflow)
})

it('Escape closes only the top dialog and restores editing focus in its parent', async () => {
  const parentClose = vi.fn()
  function Nested() {
    const [child, setChild] = useState(false)
    return (
      <Modal onClose={parentClose} ariaLabel="父弹窗">
        <input aria-label="父输入" />
        <button onClick={() => setChild(true)}>打开确认</button>
        {child && <Modal onClose={() => setChild(false)} ariaLabel="确认"><button>取消</button></Modal>}
      </Modal>
    )
  }
  await mount(<Nested />)
  const parent = document.querySelector('[role="dialog"]')!
  const trigger = parent.querySelector('button')!
  trigger.focus()
  await act(async () => trigger.click())
  expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(2)
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1)
  expect(parentClose).not.toHaveBeenCalled()
  expect(document.activeElement).toBe(trigger)
  expect(document.body.style.overflow).toBe('hidden')
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(parentClose).toHaveBeenCalledOnce()
})

it('prevents a text-selection drag from dismissing the dialog and respects pending submissions', async () => {
  const onClose = vi.fn()
  await mount(<Modal onClose={onClose} ariaLabel="确认"><p>选择文本</p></Modal>)
  const dialog = document.querySelector('[role="dialog"]')!
  const overlay = dialog.parentElement!
  dialog.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  overlay.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  expect(onClose).not.toHaveBeenCalled()
  overlay.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  overlay.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  expect(onClose).toHaveBeenCalledOnce()
  await cleanup?.()
  await mount(<Modal onClose={onClose} closeDisabled ariaLabel="处理中"><button disabled>保存</button></Modal>)
  const pendingOverlay = document.querySelector('[role="dialog"]')!.parentElement!
  pendingOverlay.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  pendingOverlay.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  expect(onClose).toHaveBeenCalledOnce()
})
