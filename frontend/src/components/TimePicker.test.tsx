// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { TimePicker } from './TimePicker'
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

async function confirm() {
  const popup = document.querySelector('[role="group"]')!
  await act(async () => [...popup.querySelectorAll('button')].find(button => button.textContent === '确定')!.click())
}

it('keeps existing minute mode and its HH:mm callback', async () => {
  const change = vi.fn()
  function Field() {
    const [value, setValue] = useState('13:05')
    return <TimePicker aria-label="同步时间" value={value} onChange={next => { setValue(next); change(next) }} />
  }
  await render(<Field />)
  await click('同步时间')
  expect(document.querySelector('[aria-label="00 秒"]')).toBeNull()
  await click('14 时')
  await click('30 分')
  expect(change).not.toHaveBeenCalled()
  await confirm()
  expect(change).toHaveBeenCalledExactlyOnceWith('14:30')
  expect(document.querySelector('[aria-label="同步时间"]')?.textContent).toBe('14:30')
})

it('selects hours minutes and seconds and saves only after confirmation', async () => {
  const change = vi.fn()
  function Field() {
    const [value, setValue] = useState('10:30:32')
    return <TimePicker showSeconds aria-label="止损时间" value={value}
      onChange={next => { setValue(next); change(next) }} />
  }
  await render(<Field />)
  expect(document.querySelector('[aria-label="止损时间"]')?.textContent).toBe('10:30:32')
  await click('止损时间')
  expect(document.querySelector('[aria-label="32 秒"]')?.getAttribute('aria-pressed')).toBe('true')
  await click('35 分')
  await click('45 秒')
  expect(change).not.toHaveBeenCalled()
  await confirm()
  expect(change).toHaveBeenCalledExactlyOnceWith('10:35:45')
  expect(document.querySelector('[aria-label="止损时间"]')?.textContent).toBe('10:35:45')
  expect(document.activeElement).toBe(document.querySelector('[aria-label="止损时间"]'))
})

it('pads a minute-only value with zero seconds in second mode', async () => {
  const change = vi.fn()
  await render(<TimePicker showSeconds aria-label="兜底时间" value="10:45" onChange={change} />)
  expect(document.querySelector('[aria-label="兜底时间"]')?.textContent).toBe('10:45:00')
  await click('兜底时间')
  await confirm()
  expect(change).toHaveBeenCalledExactlyOnceWith('10:45:00')
})

it('reopens the current saved seconds after its parent updates the value', async () => {
  const change = vi.fn()
  await render(<TimePicker showSeconds aria-label="兜底时间" value="10:30:00" onChange={change} />)
  await act(async () => root.render(<TimePicker showSeconds aria-label="兜底时间" value="10:50:15" onChange={change} />))
  await click('兜底时间')
  expect(document.querySelector('[aria-label="15 秒"]')?.getAttribute('aria-pressed')).toBe('true')
  expect(document.querySelector('[aria-label="50 分"]')?.getAttribute('aria-pressed')).toBe('true')
  await confirm()
  expect(change).toHaveBeenCalledExactlyOnceWith('10:50:15')
})

it('discards an unconfirmed second selection with Escape while keeping its modal open', async () => {
  const close = vi.fn()
  const change = vi.fn()
  await render(<Modal ariaLabel="时间设置" onClose={close}>
    <TimePicker showSeconds aria-label="止损时间" value="10:15:32" onChange={change} />
  </Modal>)
  await click('止损时间')
  await click('45 秒')
  const option = document.querySelector<HTMLButtonElement>('[aria-label="45 秒"]')!
  option.focus()
  await act(async () => option.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(close).not.toHaveBeenCalled()
  expect(change).not.toHaveBeenCalled()
  expect(document.querySelector('[aria-label="止损时间"]')?.textContent).toBe('10:15:32')
  expect(document.activeElement).toBe(document.querySelector('[aria-label="止损时间"]'))
})

it('opens selected seconds without scrolling its enclosing modal', async () => {
  const close = vi.fn()
  const change = vi.fn()
  const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
  // Browser scrollIntoView also moves scrollable ancestors on narrow panels.
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value() {
      document.querySelector('[data-modal-scroll]')?.dispatchEvent(new Event('scroll'))
    },
  })
  try {
    await render(<Modal ariaLabel="时间设置" onClose={close}>
      <div data-modal-scroll>
        <TimePicker showSeconds aria-label="启动时间" value="14:30:20" onChange={change} />
      </div>
    </Modal>)
    await click('启动时间')
    expect(document.querySelector('[aria-label="启动时间"]')?.getAttribute('aria-expanded')).toBe('true')
    const selected = document.querySelector<HTMLButtonElement>('[aria-label="20 秒"]')!
    expect(selected.getAttribute('aria-pressed')).toBe('true')
    selected.focus()
    await act(async () => selected.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    expect(close).not.toHaveBeenCalled()
    expect(change).not.toHaveBeenCalled()
    expect(document.querySelector('[aria-label="启动时间"]')?.textContent).toBe('14:30:20')
  } finally {
    if (original) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', original)
    else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView
  }
})

it('preserves clearable and disabled controls in second mode', async () => {
  const change = vi.fn()
  await render(<TimePicker showSeconds clearable aria-label="可选时间" value="10:15:32" onChange={change} />)
  await click('可选时间')
  await act(async () => [...document.querySelectorAll('button')].find(button => button.textContent === '清空时间')!.click())
  expect(change).toHaveBeenCalledExactlyOnceWith('')
  await act(async () => root.render(<TimePicker showSeconds disabled aria-label="不可选时间" value="10:15:32" onChange={change} />))
  await click('不可选时间')
  expect(document.querySelector('[aria-label="不可选时间"]')?.getAttribute('aria-expanded')).toBe('false')
})
