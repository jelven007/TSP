// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ScheduleEditor } from './ScheduleEditor'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let host: HTMLDivElement
let root: Root
const onSave = vi.fn()

beforeEach(() => {
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  onSave.mockReset()
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

async function render(value = { hour: 16, minute: 0 }, loading = false) {
  await act(async () => root.render(<ScheduleEditor value={value} loading={loading} onSave={onSave} />))
}

async function fill(index: number, value: string) {
  const input = host.querySelectorAll<HTMLInputElement>('input')[index]
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

it('saves the exact edited time and restores the saved value when reopened', async () => {
  await render()
  await fill(0, '17')
  await fill(1, '42')
  await act(async () => host.querySelector('button')!.click())
  expect(onSave).toHaveBeenCalledWith(17, 42)
  await render({ hour: 17, minute: 42 })
  expect(Array.from(host.querySelectorAll<HTMLInputElement>('input')).map(input => input.value))
    .toEqual(['17', '42'])
  expect(host.querySelector('button')?.disabled).toBe(true)
  expect(host.textContent).toContain('交易日')
})

it('explains the supported lower limit and prevents a silently changed save', async () => {
  await render()
  await fill(0, '15')
  await fill(1, '10')
  expect(host.querySelector('button')?.disabled).toBe(true)
  expect(host.textContent).toContain('15:35')
  await act(async () => host.querySelector('button')!.click())
  expect(onSave).not.toHaveBeenCalled()
  await fill(1, '35')
  expect(host.querySelector('button')?.disabled).toBe(false)
})

it('keeps the draft and disables duplicate submission while saving', async () => {
  await render()
  await fill(0, '18')
  await render({ hour: 16, minute: 0 }, true)
  expect(host.querySelector<HTMLInputElement>('input')?.value).toBe('18')
  expect(host.querySelector('button')?.disabled).toBe(true)
})
