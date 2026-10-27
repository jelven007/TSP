// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { DatePicker } from './DatePicker'
import { Modal } from './Modal'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
afterEach(async () => {
  await act(async () => root?.unmount())
  host?.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function render(content: React.ReactNode) {
  vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockImplementation(function (this: HTMLElement) {
    return this.hidden ? null : document.body
  })
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    queueMicrotask(() => callback(0)); return 1
  })
  vi.stubGlobal('cancelAnimationFrame', () => {})
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  await act(async () => root.render(content))
}

async function click(label: string) {
  const button = document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!
  expect(button).not.toBeNull()
  await act(async () => button.click())
}

it('lets an optional date be cleared and selected again within its bounds', async () => {
  const change = vi.fn()
  function Field() {
    const [value, setValue] = useState('2026-10-22')
    return <DatePicker aria-label="筛选日期" value={value} min="2026-10-20" max="2026-10-23"
      clearable onChange={next => { setValue(next); change(next) }} placeholder="不限日期" />
  }
  await render(<Field />)
  await click('筛选日期')
  expect(document.querySelector<HTMLButtonElement>('[aria-label="2026-10-19"]')?.disabled).toBe(true)
  expect(document.querySelector<HTMLButtonElement>('[aria-label="2026-10-24"]')?.disabled).toBe(true)
  await act(async () => [...document.querySelectorAll('button')].find(button => button.textContent === '清空日期')!.click())
  expect(change).toHaveBeenLastCalledWith('')
  expect(document.querySelector('[aria-label="筛选日期"]')?.textContent).toContain('不限日期')
  await click('筛选日期')
  await click('2026-10-21')
  expect(change).toHaveBeenLastCalledWith('2026-10-21')
})

it('keeps a disabled date labelled and prevents opening its calendar', async () => {
  await render(<DatePicker id="end-date" aria-label="结束日期" aria-invalid aria-describedby="range-error"
    disabled value="2026-10-22" onChange={() => {}} />)
  const trigger = document.querySelector<HTMLButtonElement>('#end-date')!
  expect(trigger?.disabled).toBe(true)
  expect(trigger.getAttribute('aria-invalid')).toBe('true')
  expect(trigger.getAttribute('aria-describedby')).toBe('range-error')
  await act(async () => trigger.click())
  expect(trigger.getAttribute('aria-expanded')).toBe('false')
})

it('closes the calendar with Escape before dismissing its containing modal', async () => {
  const close = vi.fn()
  await render(<Modal ariaLabel="筛选" onClose={close}>
    <DatePicker aria-label="开始日期" value="2026-10-22" onChange={() => {}} />
  </Modal>)
  await click('开始日期')
  const day = document.querySelector<HTMLButtonElement>('[aria-label="2026-10-21"]')!
  day.focus()
  await act(async () => day.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(close).not.toHaveBeenCalled()
  expect(document.activeElement).toBe(document.querySelector('[aria-label="开始日期"]'))
  expect(document.activeElement?.getAttribute('aria-expanded')).toBe('false')
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(close).toHaveBeenCalledOnce()
})
